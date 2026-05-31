"""Discord bot that manages the Blox Fruits skill-build leaderboard.

The bot edits ``players.json`` in the website's GitHub repository via the
GitHub Contents API. Every successful command produces a commit, which triggers
an automatic Cloudflare Pages redeploy — no manual file editing is ever needed.

Architecture
------------
* ``config.py``        — environment loading / validation.
* ``github_client.py`` — async transport, data validation, concurrency retry.
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
MutationBuilder = Callable[[], Callable[[list[dict[str, Any]]], tuple[list[dict[str, Any]], dict[str, Any]]]]


# --------------------------------------------------------------------------- #
# Domain errors (user-facing, friendly)
# --------------------------------------------------------------------------- #
class CommandError(Exception):
    """A friendly, user-facing error raised by input parsing or a mutation."""


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #
def parse_csv(raw: str) -> list[str]:
    """Split a comma-separated string into a trimmed, de-duplicated list.

    Empty fragments are dropped. De-duplication is case-insensitive but keeps
    the first-seen casing and original order.
    """
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def find_player_index(players: list[dict[str, Any]], name: str) -> int:
    """Return the index of ``name`` (case-insensitive) or -1 if not present."""
    target = name.strip().lower()
    for i, p in enumerate(players):
        if str(p.get("name", "")).strip().lower() == target:
            return i
    return -1


# --------------------------------------------------------------------------- #
# Mutation builders — each returns a pure function (players -> (new, info)).
# They raise CommandError for domain problems; commit_change re-fetches and
# re-applies them on conflict, so they must be deterministic given the input.
# --------------------------------------------------------------------------- #
def build_add(name: str, region: str, builds_raw: str, modes_raw: str) -> Callable:
    name = name.strip()
    region = region.strip()
    if not name:
        raise CommandError("`name` cannot be empty.")
    if not region:
        raise CommandError("`region` cannot be empty.")
    builds = parse_csv(builds_raw)
    modes = parse_csv(modes_raw)
    if not builds:
        raise CommandError("Provide at least one build (comma-separated).")
    if not modes:
        raise CommandError("Provide at least one mode (comma-separated).")

    def mutate(players: list[dict[str, Any]]):
        if find_player_index(players, name) != -1:
            raise CommandError(f"A player named **{name}** already exists.")
        players.append({"name": name, "region": region, "builds": builds, "modes": modes})
        rank = len(players)
        return players, {
            "commit_message": f"Add player: {name}",
            "response": f"✅ Added **{name}** at rank #{rank}",
        }

    return mutate


def build_remove(name: str) -> Callable:
    name = name.strip()
    if not name:
        raise CommandError("`name` cannot be empty.")

    def mutate(players: list[dict[str, Any]]):
        idx = find_player_index(players, name)
        if idx == -1:
            raise CommandError(f"No player named **{name}** was found.")
        actual = players[idx]["name"]
        players.pop(idx)
        return players, {
            "commit_message": f"Remove player: {actual}",
            "response": f"🗑️ Removed **{actual}**",
        }

    return mutate


def build_move(name: str, position: int) -> Callable:
    name = name.strip()
    if not name:
        raise CommandError("`name` cannot be empty.")
    if position < 1:
        raise CommandError("Position must be 1 or greater.")

    def mutate(players: list[dict[str, Any]]):
        idx = find_player_index(players, name)
        if idx == -1:
            raise CommandError(f"No player named **{name}** was found.")
        player = players.pop(idx)
        # Positions beyond the list size clamp to the end.
        target = min(position - 1, len(players))
        players.insert(target, player)
        rank = target + 1
        return players, {
            "commit_message": f"Move player: {player['name']} to rank {rank}",
            "response": f"↕️ Moved **{player['name']}** to rank #{rank}",
        }

    return mutate


def build_addbuild(name: str, build: str) -> Callable:
    name = name.strip()
    build = build.strip()
    if not name:
        raise CommandError("`name` cannot be empty.")
    if not build:
        raise CommandError("`build` cannot be empty.")

    def mutate(players: list[dict[str, Any]]):
        idx = find_player_index(players, name)
        if idx == -1:
            raise CommandError(f"No player named **{name}** was found.")
        actual = players[idx]["name"]
        builds: list[str] = players[idx]["builds"]
        if any(str(b).strip().lower() == build.lower() for b in builds):
            raise CommandError(f"**{actual}** already has the build **{build}**.")
        builds.append(build)
        return players, {
            "commit_message": f"Add build: {actual}",
            "response": f"✅ Added build **{build}** to **{actual}**",
        }

    return mutate


def build_removebuild(name: str, build: str) -> Callable:
    name = name.strip()
    build = build.strip()
    if not name:
        raise CommandError("`name` cannot be empty.")
    if not build:
        raise CommandError("`build` cannot be empty.")

    def mutate(players: list[dict[str, Any]]):
        idx = find_player_index(players, name)
        if idx == -1:
            raise CommandError(f"No player named **{name}** was found.")
        actual = players[idx]["name"]
        builds: list[str] = players[idx]["builds"]
        kept = [b for b in builds if str(b).strip().lower() != build.lower()]
        if len(kept) == len(builds):
            raise CommandError(f"**{actual}** does not have the build **{build}**.")
        players[idx]["builds"] = kept
        return players, {
            "commit_message": f"Remove build: {actual}",
            "response": f"🗑️ Removed build **{build}** from **{actual}**",
        }

    return mutate


def build_updateregion(name: str, region: str) -> Callable:
    name = name.strip()
    region = region.strip()
    if not name:
        raise CommandError("`name` cannot be empty.")
    if not region:
        raise CommandError("`region` cannot be empty.")

    def mutate(players: list[dict[str, Any]]):
        idx = find_player_index(players, name)
        if idx == -1:
            raise CommandError(f"No player named **{name}** was found.")
        actual = players[idx]["name"]
        players[idx]["region"] = region
        return players, {
            "commit_message": f"Update region: {actual}",
            "response": f"🌍 Updated region for **{actual}** to **{region}**",
        }

    return mutate


def build_rename(name: str, new_name: str) -> Callable:
    name = name.strip()
    new_name = new_name.strip()
    if not name:
        raise CommandError("`name` cannot be empty.")
    if not new_name:
        raise CommandError("`new_name` cannot be empty.")

    def mutate(players: list[dict[str, Any]]):
        idx = find_player_index(players, name)
        if idx == -1:
            raise CommandError(f"No player named **{name}** was found.")
        # Prevent duplicates: a *different* player must not already use new_name
        # (case-insensitive). Renaming a player to a new casing of their own name
        # is allowed because the match is at the same index.
        target = new_name.strip().lower()
        for i, p in enumerate(players):
            if i != idx and str(p.get("name", "")).strip().lower() == target:
                raise CommandError(f"A player named **{new_name}** already exists.")
        old = players[idx]["name"]
        # Change only the name; rank (array position), region, builds and modes
        # are left untouched.
        players[idx]["name"] = new_name
        return players, {
            "commit_message": f"Rename player: {old} to {new_name}",
            "response": f"✏️ Renamed **{old}** to **{new_name}**",
        }

    return mutate


def build_editplayer(
    name: str,
    region: str | None,
    builds_raw: str | None,
    modes_raw: str | None,
) -> Callable:
    name = name.strip()
    if not name:
        raise CommandError("`name` cannot be empty.")

    new_region = region.strip() if region is not None else None
    if region is not None and not new_region:
        raise CommandError("`region` cannot be empty when provided.")

    new_builds = parse_csv(builds_raw) if builds_raw is not None else None
    if builds_raw is not None and not new_builds:
        raise CommandError("Provide at least one build when editing builds.")

    new_modes = parse_csv(modes_raw) if modes_raw is not None else None
    if modes_raw is not None and not new_modes:
        raise CommandError("Provide at least one mode when editing modes.")

    if new_region is None and new_builds is None and new_modes is None:
        raise CommandError("Provide at least one field to edit (region, builds, or modes).")

    def mutate(players: list[dict[str, Any]]):
        idx = find_player_index(players, name)
        if idx == -1:
            raise CommandError(f"No player named **{name}** was found.")
        actual = players[idx]["name"]
        changed: list[str] = []
        if new_region is not None:
            players[idx]["region"] = new_region
            changed.append("region")
        if new_builds is not None:
            players[idx]["builds"] = new_builds
            changed.append("builds")
        if new_modes is not None:
            players[idx]["modes"] = new_modes
            changed.append("modes")
        return players, {
            "commit_message": f"Edit player: {actual}",
            "response": f"✏️ Updated {', '.join(changed)} for **{actual}**",
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
@bot.tree.command(name="add", description="Add a player to the leaderboard (appended at the bottom).")
@app_commands.describe(
    name="Player name",
    region="Region (e.g. NA, EU, ASIA, SA, OCE)",
    builds="Comma-separated builds (e.g. Gun, Sword)",
    modes="Comma-separated modes (e.g. 1v1s, 2v2s)",
)
@is_admin()
async def add(interaction: discord.Interaction, name: str, region: str, builds: str, modes: str) -> None:
    await _execute(interaction, "add", lambda: build_add(name, region, builds, modes))


@bot.tree.command(name="remove", description="Remove a player from the leaderboard.")
@app_commands.describe(name="Player name (case-insensitive)")
@is_admin()
async def remove(interaction: discord.Interaction, name: str) -> None:
    await _execute(interaction, "remove", lambda: build_remove(name))


@bot.tree.command(name="move", description="Move a player to a specific rank (1 = top).")
@app_commands.describe(name="Player name (case-insensitive)", position="Target rank (1 or greater)")
@is_admin()
async def move(interaction: discord.Interaction, name: str, position: int) -> None:
    await _execute(interaction, "move", lambda: build_move(name, position))


@bot.tree.command(name="rename", description="Rename a player, keeping their rank, region, builds and modes.")
@app_commands.describe(name="Current player name (case-insensitive)", new_name="New player name")
@is_admin()
async def rename(interaction: discord.Interaction, name: str, new_name: str) -> None:
    await _execute(interaction, "rename", lambda: build_rename(name, new_name))


@bot.tree.command(name="addbuild", description="Add a build to an existing player.")
@app_commands.describe(name="Player name (case-insensitive)", build="Build to add")
@is_admin()
async def addbuild(interaction: discord.Interaction, name: str, build: str) -> None:
    await _execute(interaction, "addbuild", lambda: build_addbuild(name, build))


@bot.tree.command(name="removebuild", description="Remove a build from an existing player.")
@app_commands.describe(name="Player name (case-insensitive)", build="Build to remove")
@is_admin()
async def removebuild(interaction: discord.Interaction, name: str, build: str) -> None:
    await _execute(interaction, "removebuild", lambda: build_removebuild(name, build))


@bot.tree.command(name="updateregion", description="Update a player's region.")
@app_commands.describe(name="Player name (case-insensitive)", region="New region")
@is_admin()
async def updateregion(interaction: discord.Interaction, name: str, region: str) -> None:
    await _execute(interaction, "updateregion", lambda: build_updateregion(name, region))


@bot.tree.command(name="editplayer", description="Edit a player's region, builds, and/or modes.")
@app_commands.describe(
    name="Player name (case-insensitive)",
    region="New region (optional)",
    builds="New comma-separated builds (optional — replaces existing)",
    modes="New comma-separated modes (optional — replaces existing)",
)
@is_admin()
async def editplayer(
    interaction: discord.Interaction,
    name: str,
    region: str | None = None,
    builds: str | None = None,
    modes: str | None = None,
) -> None:
    await _execute(interaction, "editplayer", lambda: build_editplayer(name, region, builds, modes))


# --------------------------------------------------------------------------- #
# Slash command — public
# --------------------------------------------------------------------------- #
@bot.tree.command(name="list", description="Show the current leaderboard (live from GitHub).")
async def list_players(interaction: discord.Interaction) -> None:
    user = interaction.user
    logger.info("cmd=list user=%s id=%s status=invoked", user, user.id)
    try:
        await interaction.response.defer(thinking=True)
    except discord.HTTPException as exc:
        logger.error("cmd=list could not defer: %s", exc)
        return

    assert bot.github is not None
    try:
        players, _sha = await bot.github.get_players()
    except Exception as exc:  # noqa: BLE001
        level = logging.WARNING if isinstance(exc, (GitHubError, ValidationError)) else logging.ERROR
        logger.log(level, "cmd=list user=%s id=%s status=failure error=%s", user, user.id, exc,
                   exc_info=level == logging.ERROR)
        await _safe_send(interaction, _error_message(exc), ephemeral=True)
        return

    if not players:
        await _safe_send(interaction, "The leaderboard is currently empty. 🏴‍☠️", ephemeral=False)
        logger.info("cmd=list user=%s id=%s status=success count=0", user, user.id)
        return

    lines = [f"#{i + 1} {p['name']}" for i, p in enumerate(players)]
    description = "\n".join(lines)
    if len(description) > 4000:  # embed description hard limit is 4096
        description = description[:3990] + "\n… (truncated)"

    embed = discord.Embed(
        title="🏆 Skill Build — Current Leaderboard",
        description=description,
        color=0xECC97F,
    )
    embed.set_footer(text=f"{len(players)} player(s) · live from players.json")
    try:
        await interaction.followup.send(embed=embed)
    except discord.HTTPException as exc:
        logger.error("cmd=list failed to send embed: %s", exc)
        return
    logger.info("cmd=list user=%s id=%s status=success count=%d", user, user.id, len(players))


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
