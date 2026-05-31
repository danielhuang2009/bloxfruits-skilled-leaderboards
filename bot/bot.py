"""Discord bot that manages the Blox Fruits skill-build leaderboards.

The bot edits ``players.json`` in the website's GitHub repository via the
GitHub Contents API. Every successful command produces a commit, which triggers
an automatic Cloudflare Pages redeploy — no manual file editing is ever needed.

Data model (v2)
---------------
``players.json`` holds a set of INDEPENDENT rankings keyed by a normalized
``REGION|Build|mode`` combo. A player may appear in any number of combos at
independent ranks; array order within a combo is the ranking (first = #1)::

    { "version": 2, "leaderboards": { "NA|Sword|1v1s": ["truck", "kriz"] } }

Architecture
------------
* ``config.py``        — environment loading / validation.
* ``github_client.py`` — async transport, document validation, concurrency retry.
* ``bot.py`` (here)    — Discord slash commands, permissions, input parsing,
                         domain mutations, logging, and user-facing responses.

All commands are slash commands. Everyone may use ``/list``; all mutating
commands require the configured admin role (or the guild's Administrator perm).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import discord
from discord import app_commands
from discord.ext import commands

from config import Config, ConfigError, load_config
from github_client import (
    GitHubAuthError,
    GitHubClient,
    GitHubConflictError,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    ValidationError,
)

logger = logging.getLogger("leaderboard.bot")

# A mutation builder returns a callable suitable for GitHubClient.commit_change.
MutationBuilder = Callable[[], Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]]]


# --------------------------------------------------------------------------- #
# Domain errors (user-facing, friendly)
# --------------------------------------------------------------------------- #
class CommandError(Exception):
    """A friendly, user-facing error raised by input parsing or a mutation."""


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #
def normalize_combo(region: str, build: str, mode: str) -> tuple[str, str, str, str]:
    """Normalize the three combo parts and build the ``REGION|Build|mode`` key.

    * region -> UPPERCASE   (``na`` -> ``NA``)
    * build  -> Title Case  (``sword`` -> ``Sword``)
    * mode   -> lowercase   (``2V2S`` -> ``2v2s``)

    Returns ``(region, build, mode, key)``. Raises CommandError on an empty part.
    """
    region_n = str(region).strip().upper()
    build_n = str(build).strip().title()
    mode_n = str(mode).strip().lower()
    if not region_n:
        raise CommandError("`region` cannot be empty.")
    if not build_n:
        raise CommandError("`build` cannot be empty.")
    if not mode_n:
        raise CommandError("`mode` cannot be empty.")
    return region_n, build_n, mode_n, f"{region_n}|{build_n}|{mode_n}"


def format_combo(region: str, build: str, mode: str) -> str:
    """Human-friendly combo label, e.g. ``NA · Sword · 1v1s``."""
    return f"{region} · {build} · {mode}"


def find_name_index(names: list[Any], player: str) -> int:
    """Return the index of ``player`` (case-insensitive) in ``names`` or -1."""
    target = player.strip().lower()
    for i, n in enumerate(names):
        if str(n).strip().lower() == target:
            return i
    return -1


def board_text(names: list[str], *, limit: int = 25) -> str:
    """Render a board's order as ``#1 name`` lines, truncating very long boards."""
    if not names:
        return "_(empty)_"
    lines = [f"#{i + 1} {n}" for i, n in enumerate(names[:limit])]
    if len(names) > limit:
        lines.append(f"… (+{len(names) - limit} more)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Mutation builders — each returns a pure function (document -> (new, info)).
# They raise CommandError for domain problems; commit_change re-fetches and
# re-applies them on conflict, so they must be deterministic given the input.
# --------------------------------------------------------------------------- #
def build_setrank(player: str, region: str, build: str, mode: str, position: int) -> Callable:
    player = player.strip()
    if not player:
        raise CommandError("`player` cannot be empty.")
    if position < 1:
        raise CommandError("Position must be 1 or greater.")
    region_n, build_n, mode_n, key = normalize_combo(region, build, mode)
    label = format_combo(region_n, build_n, mode_n)

    def mutate(doc: dict[str, Any]):
        boards: dict[str, list[str]] = doc["leaderboards"]
        board = boards.setdefault(key, [])
        idx = find_name_index(board, player)
        if idx != -1:
            # Already in this combo — preserve their stored casing, then move.
            actual = board.pop(idx)
        else:
            actual = player
        # Positions beyond the list size clamp to the end.
        target = min(position - 1, len(board))
        board.insert(target, actual)
        boards[key] = board
        rank = target + 1
        return doc, {
            "commit_message": f"setrank: {actual} -> #{rank} in {key}",
            "response": (
                f"✅ Set **{actual}** to **#{rank}** in **{label}**\n\n"
                f"**{label}**\n{board_text(board)}"
            ),
        }

    return mutate


def build_removefrom(player: str, region: str, build: str, mode: str) -> Callable:
    player = player.strip()
    if not player:
        raise CommandError("`player` cannot be empty.")
    region_n, build_n, mode_n, key = normalize_combo(region, build, mode)
    label = format_combo(region_n, build_n, mode_n)

    def mutate(doc: dict[str, Any]):
        boards: dict[str, list[str]] = doc["leaderboards"]
        board = boards.get(key)
        if not board:
            raise CommandError(f"There is no ranking for **{label}** yet.")
        idx = find_name_index(board, player)
        if idx == -1:
            raise CommandError(f"**{player}** is not ranked in **{label}**.")
        actual = board.pop(idx)
        # Clean up a board that just became empty so it stops cluttering filters.
        if not board:
            del boards[key]
            tail = "\n\n_(that ranking is now empty and was removed)_"
        else:
            tail = f"\n\n**{label}**\n{board_text(board)}"
        return doc, {
            "commit_message": f"removefrom: {actual} out of {key}",
            "response": f"🗑️ Removed **{actual}** from **{label}**{tail}",
        }

    return mutate


def build_remove(player: str) -> Callable:
    player = player.strip()
    if not player:
        raise CommandError("`player` cannot be empty.")

    def mutate(doc: dict[str, Any]):
        boards: dict[str, list[str]] = doc["leaderboards"]
        removed_from: list[str] = []
        actual = player
        for key in list(boards.keys()):
            board = boards[key]
            idx = find_name_index(board, player)
            if idx == -1:
                continue
            actual = board.pop(idx)
            removed_from.append(key)
            if not board:
                del boards[key]
        if not removed_from:
            raise CommandError(f"**{player}** is not ranked in any leaderboard.")
        count = len(removed_from)
        return doc, {
            "commit_message": f"remove: {actual} from all ({count}) leaderboards",
            "response": f"🗑️ Removed **{actual}** from **{count}** leaderboard{'s' if count != 1 else ''}.",
        }

    return mutate


def build_rename(player: str, new_name: str) -> Callable:
    player = player.strip()
    new_name = new_name.strip()
    if not player:
        raise CommandError("`player` cannot be empty.")
    if not new_name:
        raise CommandError("`new_name` cannot be empty.")
    new_key = new_name.lower()

    def mutate(doc: dict[str, Any]):
        boards: dict[str, list[str]] = doc["leaderboards"]
        affected = 0
        old_actual = player
        for key in list(boards.keys()):
            board = boards[key]
            idx = find_name_index(board, player)
            if idx == -1:
                continue
            old_actual = board[idx]
            # Rename in place, then drop any *other* occurrence of new_name in
            # this same board so a rename can never create a duplicate.
            rebuilt: list[str] = []
            for i, n in enumerate(board):
                if i == idx:
                    rebuilt.append(new_name)
                elif str(n).strip().lower() == new_key:
                    continue
                else:
                    rebuilt.append(n)
            boards[key] = rebuilt
            affected += 1
        if affected == 0:
            raise CommandError(f"**{player}** is not ranked in any leaderboard.")
        return doc, {
            "commit_message": f"rename: {old_actual} -> {new_name} across {affected} leaderboards",
            "response": (
                f"✏️ Renamed **{old_actual}** to **{new_name}** "
                f"across **{affected}** leaderboard{'s' if affected != 1 else ''}."
            ),
        }

    return mutate


# --------------------------------------------------------------------------- #
# Bot definition
# --------------------------------------------------------------------------- #
class LeaderboardBot(commands.Bot):
    """commands.Bot subclass holding the config + GitHub client and syncing commands."""

    cfg: Config | None = None
    github: GitHubClient | None = None

    async def setup_hook(self) -> None:
        assert self.github is not None and self.cfg is not None
        await self.github.start()
        if self.cfg.guild_id:
            guild = discord.Object(id=self.cfg.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info("Synced %d command(s) to guild %s", len(synced), self.cfg.guild_id)
        else:
            synced = await self.tree.sync()
            logger.info("Synced %d global command(s) (may take up to ~1h to appear)", len(synced))

    async def close(self) -> None:
        if self.github is not None:
            await self.github.close()
        await super().close()


intents = discord.Intents.default()
bot = LeaderboardBot(command_prefix="!", intents=intents, help_command=None)


# --------------------------------------------------------------------------- #
# Permission check
# --------------------------------------------------------------------------- #
def is_admin():
    """app_commands check: allow the configured admin role or guild Administrators."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None or bot.cfg is None:
            return False
        member = interaction.user
        roles = getattr(member, "roles", [])
        role_ok = any(getattr(r, "id", None) == bot.cfg.admin_role_id for r in roles)
        perms = getattr(member, "guild_permissions", None)
        admin_perm = bool(getattr(perms, "administrator", False))
        return role_ok or admin_perm

    return app_commands.check(predicate)


# --------------------------------------------------------------------------- #
# Response + error plumbing
# --------------------------------------------------------------------------- #
async def _safe_send(interaction: discord.Interaction, content: str, *, ephemeral: bool) -> None:
    """Send a message whether or not the interaction was already responded to/deferred."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)
    except discord.HTTPException as exc:  # Discord-side delivery failure — never crash.
        logger.error("Failed to deliver Discord response: %s", exc)


def _error_message(exc: Exception) -> str:
    """Map an exception to a friendly, user-facing string."""
    if isinstance(exc, CommandError):
        return f"⚠️ {exc}"
    if isinstance(exc, GitHubConflictError):
        return (
            "⚠️ Another admin updated the leaderboard at the same time and the "
            "automatic retry also conflicted. Please run the command again."
        )
    if isinstance(exc, GitHubRateLimitError):
        return "⚠️ GitHub's rate limit was reached. Please wait a moment and try again."
    if isinstance(exc, GitHubAuthError):
        return "⚠️ GitHub authentication failed. The bot's token may be invalid or missing repo write access."
    if isinstance(exc, GitHubNotFoundError):
        return "⚠️ Could not find `players.json` or the repository. Check the bot's GitHub configuration."
    if isinstance(exc, ValidationError):
        return f"⚠️ Update aborted — the change would create invalid data: {exc}"
    if isinstance(exc, GitHubError):
        return f"⚠️ A GitHub error occurred: {exc}"
    return "⚠️ An unexpected error occurred. Please try again later."


async def _execute(interaction: discord.Interaction, label: str, builder: MutationBuilder) -> None:
    """Run a mutating command end-to-end with logging and error handling.

    Success is reported publicly; every error is reported ephemerally.
    """
    user = interaction.user
    logger.info("cmd=%s user=%s id=%s status=invoked", label, user, user.id)

    # Ephemeral 'thinking' so only the invoker sees the spinner; the eventual
    # success confirmation is sent publicly.
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except discord.HTTPException as exc:
        logger.error("cmd=%s could not defer: %s", label, exc)
        return

    assert bot.github is not None
    try:
        mutation = builder()  # input parsing/validation (may raise CommandError)
        info = await bot.github.commit_change(mutation)
    except Exception as exc:  # noqa: BLE001 — we deliberately surface everything safely
        level = logging.WARNING if isinstance(exc, (CommandError, GitHubError, ValidationError)) else logging.ERROR
        logger.log(
            level,
            "cmd=%s user=%s id=%s status=failure error=%s",
            label, user, user.id, exc,
            exc_info=level == logging.ERROR,
        )
        await _safe_send(interaction, _error_message(exc), ephemeral=True)
        return

    logger.info(
        "cmd=%s user=%s id=%s status=success commit=%s http=%s",
        label, user, user.id, info.get("commit_sha", "?"), info.get("status", "?"),
    )
    await _safe_send(interaction, info["response"], ephemeral=False)


# --------------------------------------------------------------------------- #
# Slash commands — mutating (admin only)
# --------------------------------------------------------------------------- #
@bot.tree.command(name="setrank", description="Set a player's rank in a region+build+mode ranking.")
@app_commands.describe(
    player="Player name",
    region="Region (e.g. NA, EU, ASIA, SA, OCE)",
    build="Build (e.g. Gun, Sword)",
    mode="Mode (e.g. 1v1s, 2v2s)",
    position="Target rank in that ranking (1 = top; beyond the size → end).",
)
@is_admin()
async def setrank(
    interaction: discord.Interaction,
    player: str,
    region: str,
    build: str,
    mode: str,
    position: int,
) -> None:
    await _execute(interaction, "setrank", lambda: build_setrank(player, region, build, mode, position))


@bot.tree.command(name="removefrom", description="Remove a player from ONE region+build+mode ranking.")
@app_commands.describe(
    player="Player name (case-insensitive)",
    region="Region (e.g. NA, EU)",
    build="Build (e.g. Gun, Sword)",
    mode="Mode (e.g. 1v1s, 2v2s)",
)
@is_admin()
async def removefrom(
    interaction: discord.Interaction,
    player: str,
    region: str,
    build: str,
    mode: str,
) -> None:
    await _execute(interaction, "removefrom", lambda: build_removefrom(player, region, build, mode))


@bot.tree.command(name="remove", description="Remove a player from ALL rankings entirely.")
@app_commands.describe(player="Player name (case-insensitive)")
@is_admin()
async def remove(interaction: discord.Interaction, player: str) -> None:
    await _execute(interaction, "remove", lambda: build_remove(player))


@bot.tree.command(name="rename", description="Rename a player across every ranking they appear in.")
@app_commands.describe(player="Current player name (case-insensitive)", new_name="New player name")
@is_admin()
async def rename(interaction: discord.Interaction, player: str, new_name: str) -> None:
    await _execute(interaction, "rename", lambda: build_rename(player, new_name))


# --------------------------------------------------------------------------- #
# Slash command — public
# --------------------------------------------------------------------------- #
@bot.tree.command(name="list", description="Show one region+build+mode ranking (live from GitHub).")
@app_commands.describe(
    region="Region (e.g. NA, EU)",
    build="Build (e.g. Gun, Sword)",
    mode="Mode (e.g. 1v1s, 2v2s)",
)
async def list_players(
    interaction: discord.Interaction,
    region: str,
    build: str,
    mode: str,
) -> None:
    user = interaction.user
    logger.info("cmd=list user=%s id=%s status=invoked", user, user.id)
    try:
        await interaction.response.defer(thinking=True)
    except discord.HTTPException as exc:
        logger.error("cmd=list could not defer: %s", exc)
        return

    # Parse/normalize the combo first so a bad input is a friendly ephemeral error.
    try:
        region_n, build_n, mode_n, key = normalize_combo(region, build, mode)
    except CommandError as exc:
        await _safe_send(interaction, _error_message(exc), ephemeral=True)
        return
    label = format_combo(region_n, build_n, mode_n)

    assert bot.github is not None
    try:
        doc, _sha = await bot.github.get_document()
    except Exception as exc:  # noqa: BLE001
        level = logging.WARNING if isinstance(exc, (GitHubError, ValidationError)) else logging.ERROR
        logger.log(level, "cmd=list user=%s id=%s status=failure error=%s", user, user.id, exc,
                   exc_info=level == logging.ERROR)
        await _safe_send(interaction, _error_message(exc), ephemeral=True)
        return

    board = doc.get("leaderboards", {}).get(key) or []
    if not board:
        await _safe_send(interaction, f"No ranking exists for **{label}** yet. 🏴‍☠️", ephemeral=False)
        logger.info("cmd=list user=%s id=%s status=success combo=%s count=0", user, user.id, key)
        return

    lines = [f"#{i + 1} {name}" for i, name in enumerate(board)]
    description = "\n".join(lines)
    if len(description) > 4000:  # embed description hard limit is 4096
        description = description[:3990] + "\n… (truncated)"

    embed = discord.Embed(
        title=f"🏆 {label}",
        description=description,
        color=0xECC97F,
    )
    embed.set_footer(text=f"{len(board)} player(s) · live from players.json")
    try:
        await interaction.followup.send(embed=embed)
    except discord.HTTPException as exc:
        logger.error("cmd=list failed to send embed: %s", exc)
        return
    logger.info("cmd=list user=%s id=%s status=success combo=%s count=%d", user, user.id, key, len(board))


# --------------------------------------------------------------------------- #
# Global slash-command error handler (permissions etc.)
# --------------------------------------------------------------------------- #
@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    cmd_name = interaction.command.name if interaction.command else "?"
    user = interaction.user
    if isinstance(error, app_commands.CheckFailure):
        logger.warning("cmd=%s user=%s id=%s status=denied", cmd_name, user, user.id)
        await _safe_send(interaction, "You do not have permission to use this command.", ephemeral=True)
        return
    logger.error("cmd=%s user=%s id=%s status=error error=%s", cmd_name, user, user.id, error, exc_info=error)
    await _safe_send(interaction, _error_message(error), ephemeral=True)


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (id=%s). Managing %s", bot.user, getattr(bot.user, "id", "?"),
                bot.cfg.repo_slug if bot.cfg else "?")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        raise SystemExit(1) from exc

    bot.cfg = config
    bot.github = GitHubClient(
        token=config.github_token,
        owner=config.github_owner,
        repo=config.github_repo,
        file_path=config.players_file_path,
        branch=config.github_branch,
    )
    logger.info("Starting leaderboard bot for repo %s (file: %s)", config.repo_slug, config.players_file_path)
    # log_handler=None: reuse our root logging config instead of discord.py's.
    bot.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
