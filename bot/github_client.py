"""Async GitHub Contents API client for the leaderboard ``players.json`` file.

This module is responsible for *data integrity and transport only*:

* fetching the current ``players.json`` (decoding Base64 + JSON, capturing SHA),
* validating the resulting array before any write,
* committing an updated array (Base64 + pretty-printed JSON) with the SHA,
* transparently retrying once on a concurrent-edit (SHA) conflict.

All HTTP is performed with :mod:`aiohttp`. Domain mutations (add/remove/move/...)
live in ``bot.py`` and are passed in as callables to :meth:`GitHubClient.commit_change`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Awaitable, Callable

import aiohttp

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"

# A mutation takes the current player list and returns ``(new_list, info)``.
# ``info`` must contain at least a ``"commit_message"`` key and may carry any
# extra data the caller wants echoed back to the user (e.g. resulting rank).
Mutation = Callable[[list[dict[str, Any]]], tuple[list[dict[str, Any]], dict[str, Any]]]


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class GitHubError(RuntimeError):
    """Base class for GitHub transport / API failures."""


class GitHubAuthError(GitHubError):
    """401/403 caused by a bad or under-scoped token (not rate limiting)."""


class GitHubRateLimitError(GitHubError):
    """Rate limit or secondary rate limit hit."""


class GitHubConflictError(GitHubError):
    """The file SHA was stale — another commit landed first (HTTP 409)."""


class GitHubNotFoundError(GitHubError):
    """The configured file or repository could not be found (HTTP 404)."""


class ValidationError(ValueError):
    """The resulting players array failed structural validation."""


# --------------------------------------------------------------------------- #
# Data validation (rollback protection — never commit corrupted data)
# --------------------------------------------------------------------------- #
def validate_players(players: Any) -> None:
    """Validate the full players array prior to committing.

    Rules (per the data contract):
        * root must be a list,
        * every entry is an object with ``name``, ``region``, ``builds``, ``modes``,
        * ``name`` and ``region`` are non-empty strings,
        * ``builds`` and ``modes`` are lists of non-empty strings.

    Raises:
        ValidationError: on the first violation found.
    """
    if not isinstance(players, list):
        raise ValidationError("Root structure must be a JSON array.")

    for index, player in enumerate(players):
        where = f"player at index {index}"
        if not isinstance(player, dict):
            raise ValidationError(f"{where} is not an object.")

        for key in ("name", "region", "builds", "modes"):
            if key not in player:
                raise ValidationError(f"{where} is missing required field '{key}'.")

        name = player["name"]
        region = player["region"]
        builds = player["builds"]
        modes = player["modes"]

        if not isinstance(name, str) or not name.strip():
            raise ValidationError(f"{where}: 'name' must be a non-empty string.")
        if not isinstance(region, str) or not region.strip():
            raise ValidationError(f"{where}: 'region' must be a non-empty string.")
        if not isinstance(builds, list):
            raise ValidationError(f"{where}: 'builds' must be an array.")
        if not isinstance(modes, list):
            raise ValidationError(f"{where}: 'modes' must be an array.")
        for b in builds:
            if not isinstance(b, str) or not b.strip():
                raise ValidationError(f"{where}: every build must be a non-empty string.")
        for m in modes:
            if not isinstance(m, str) or not m.strip():
                raise ValidationError(f"{where}: every mode must be a non-empty string.")


def serialize_players(players: list[dict[str, Any]]) -> str:
    """Pretty-print players as JSON: 2-space indent, Unicode preserved, trailing newline."""
    return json.dumps(players, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class GitHubClient:
    """Async client around the GitHub Contents API for a single JSON file."""

    def __init__(
        self,
        *,
        token: str,
        owner: str,
        repo: str,
        file_path: str,
        branch: str | None = None,
        request_timeout: float = 20.0,
    ) -> None:
        self._token = token
        self._owner = owner
        self._repo = repo
        self._file_path = file_path.lstrip("/")
        self._branch = branch
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)
        self._session: aiohttp.ClientSession | None = None

    # ----- lifecycle ------------------------------------------------------- #
    async def __aenter__(self) -> "GitHubClient":
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        """Create the underlying aiohttp session (idempotent)."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION,
                    "Authorization": f"Bearer {self._token}",
                    "User-Agent": "bloxfruits-leaderboard-bot",
                },
            )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ----- low-level helpers ---------------------------------------------- #
    @property
    def _contents_url(self) -> str:
        return (
            f"{GITHUB_API_BASE}/repos/{self._owner}/{self._repo}"
            f"/contents/{self._file_path}"
        )

    def _require_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            raise GitHubError("GitHubClient session is not started. Call start() first.")
        return self._session

    @staticmethod
    def _raise_for_status(status: int, payload: dict[str, Any] | str) -> None:
        """Translate a non-2xx GitHub response into a typed exception."""
        message = payload.get("message", "") if isinstance(payload, dict) else str(payload)
        if status in (401, 403):
            lowered = message.lower()
            if "rate limit" in lowered or "secondary rate" in lowered:
                raise GitHubRateLimitError(f"GitHub rate limit hit: {message}")
            raise GitHubAuthError(
                f"GitHub authorization failed ({status}): {message or 'check token scopes'}"
            )
        if status == 404:
            raise GitHubNotFoundError(
                f"File or repository not found (404): {message or 'check owner/repo/path'}"
            )
        if status == 409:
            raise GitHubConflictError(f"SHA conflict (409): {message}")
        if status == 422:
            raise GitHubConflictError(
                f"Unprocessable (422) — usually a stale SHA: {message}"
            )
        if status == 429:
            raise GitHubRateLimitError(f"Too many requests (429): {message}")
        raise GitHubError(f"Unexpected GitHub response ({status}): {message}")

    # ----- file operations ------------------------------------------------- #
    async def get_players(self) -> tuple[list[dict[str, Any]], str]:
        """Fetch and decode the current ``players.json``.

        Returns:
            ``(players, sha)`` — the decoded list and the file's blob SHA.

        Raises:
            GitHubError subclasses on transport/API failure,
            ValidationError if the stored content is not a JSON array.
        """
        session = self._require_session()
        params = {"ref": self._branch} if self._branch else None
        try:
            async with session.get(self._contents_url, params=params) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    self._raise_for_status(resp.status, body)
        except aiohttp.ClientError as exc:
            raise GitHubError(f"Network error fetching players.json: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise GitHubError("Timed out fetching players.json from GitHub.") from exc

        sha = body.get("sha")
        encoded = body.get("content", "")
        encoding = body.get("encoding", "base64")
        if not sha:
            raise GitHubError("GitHub response missing file SHA.")
        if encoding != "base64":
            raise GitHubError(f"Unexpected content encoding from GitHub: {encoding!r}")

        try:
            raw = base64.b64decode(encoded).decode("utf-8")
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValidationError(f"Stored players.json is not valid JSON: {exc}") from exc

        if not isinstance(data, list):
            raise ValidationError("Stored players.json root is not an array.")
        return data, sha

    async def put_players(
        self, players: list[dict[str, Any]], message: str, sha: str
    ) -> dict[str, Any]:
        """Commit a new ``players.json`` using the supplied SHA.

        Returns:
            A dict with ``commit_sha`` and ``status`` (HTTP status code).

        Raises:
            GitHubConflictError if the SHA is stale,
            other GitHubError subclasses on failure.
        """
        session = self._require_session()
        content_b64 = base64.b64encode(serialize_players(players).encode("utf-8")).decode("ascii")
        payload: dict[str, Any] = {
            "message": message,
            "content": content_b64,
            "sha": sha,
        }
        if self._branch:
            payload["branch"] = self._branch

        try:
            async with session.put(self._contents_url, json=payload) as resp:
                body = await resp.json(content_type=None)
                if resp.status not in (200, 201):
                    self._raise_for_status(resp.status, body)
        except aiohttp.ClientError as exc:
            raise GitHubError(f"Network error committing players.json: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise GitHubError("Timed out committing players.json to GitHub.") from exc

        commit_sha = (body.get("commit") or {}).get("sha", "")
        return {"commit_sha": commit_sha, "status": resp.status}

    # ----- high-level orchestration --------------------------------------- #
    async def commit_change(
        self,
        mutate: Mutation,
        *,
        max_retries: int = 1,
        retry_delay: float = 0.6,
    ) -> dict[str, Any]:
        """Fetch → mutate → validate → commit, retrying once on a SHA conflict.

        The ``mutate`` callable receives a *fresh* copy of the current players
        list every attempt and must return ``(new_list, info)``. ``info`` must
        include a ``"commit_message"`` string; any domain errors it raises
        (e.g. player-not-found) propagate unchanged and are NOT retried.

        Concurrency safety: because the players list is re-fetched (with a fresh
        SHA) on every attempt, a retry re-applies the mutation against the newest
        state — newer updates are never silently overwritten.

        Returns:
            The ``info`` dict augmented with ``commit_sha`` and ``status``.
        """
        attempt = 0
        while True:
            players, sha = await self.get_players()
            # mutate works on a defensive copy so a partial mutation can never
            # leak into a retry.
            working_copy = json.loads(json.dumps(players))
            new_players, info = mutate(working_copy)

            if "commit_message" not in info:
                raise GitHubError("Mutation did not provide a commit message.")

            validate_players(new_players)

            try:
                result = await self.put_players(new_players, info["commit_message"], sha)
            except GitHubConflictError:
                if attempt >= max_retries:
                    raise
                attempt += 1
                logger.warning(
                    "SHA conflict committing %s — retry %d/%d after refetch",
                    self._file_path,
                    attempt,
                    max_retries,
                )
                await asyncio.sleep(retry_delay)
                continue

            info.update(result)
            return info
