"""Configuration loading and validation for the leaderboard Discord bot.

All runtime configuration comes from environment variables (optionally loaded
from a local ``.env`` file via python-dotenv). Importing this module and calling
:func:`load_config` gives you a fully validated, immutable :class:`Config`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True)
class Config:
    """Immutable, validated bot configuration."""

    discord_token: str
    github_token: str
    github_owner: str
    github_repo: str
    admin_role_id: int
    players_file_path: str
    # Optional: if set, slash commands are synced to this single guild and
    # become available instantly (global sync can take up to an hour).
    guild_id: int | None
    # Optional: branch to commit to. Defaults to the repository default branch
    # when left empty (GitHub picks it for us).
    github_branch: str | None

    @property
    def repo_slug(self) -> str:
        """``owner/repo`` convenience string for logging."""
        return f"{self.github_owner}/{self.github_repo}"


def _require(name: str) -> str:
    """Return a required environment variable or raise :class:`ConfigError`."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"Required environment variable '{name}' is missing or empty. "
            f"See bot/.env.example for the full list."
        )
    return value


def _require_int(name: str) -> int:
    """Return a required environment variable parsed as an int."""
    raw = _require(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"Environment variable '{name}' must be an integer, got: {raw!r}"
        ) from exc


def _optional_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"Environment variable '{name}' must be an integer when set, got: {raw!r}"
        ) from exc


def load_config() -> Config:
    """Load and validate configuration from the environment.

    Reads a ``.env`` file if present (without overriding already-set process
    environment variables), then validates required values.

    Raises:
        ConfigError: if any required variable is missing or malformed.
    """
    # ``override=False`` means real environment variables (e.g. set by a hosting
    # platform such as Railway/Render) win over the .env file.
    load_dotenv(override=False)

    players_path = os.environ.get("PLAYERS_FILE_PATH", "").strip() or "players.json"
    branch = os.environ.get("GITHUB_BRANCH", "").strip() or None

    return Config(
        discord_token=_require("DISCORD_TOKEN"),
        github_token=_require("GITHUB_TOKEN"),
        github_owner=_require("GITHUB_OWNER"),
        github_repo=_require("GITHUB_REPO"),
        admin_role_id=_require_int("ADMIN_ROLE_ID"),
        players_file_path=players_path,
        guild_id=_optional_int("DISCORD_GUILD_ID"),
        github_branch=branch,
    )
