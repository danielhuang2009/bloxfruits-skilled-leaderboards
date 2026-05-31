"""Async GitHub Contents API client for the leaderboard ``players.json`` file.

This module is responsible for *data integrity and transport only*:

* fetching the current ``players.json`` (decoding Base64 + JSON, capturing SHA),
* validating the resulting document before any write,
* committing an updated document (Base64 + pretty-printed JSON) with the SHA,
* transparently retrying once on a concurrent-edit (SHA) conflict.

Data model (v2)
---------------
``players.json`` is a JSON **object** holding a set of independent rankings,
each keyed by a normalized ``REGION|Build|mode`` combo::

    {
      "version": 2,
      "leaderboards": {
        "NA|Sword|1v1s": ["truck", "kriz"],   # array order = rank, #1 first
        "NA|Sword|2v2s": ["kriz", "truck"]
      }
    }

A player can appear in any number of leaderboards at independent positions.
Legacy v1 files (a flat array of player objects) are migrated on read by
:func:`migrate_legacy`, so an older file is upgraded transparently.

All HTTP is performed with :mod:`aiohttp`. Domain mutations (setrank/remove/...)
live in ``bot.py`` and are passed in as callables to :meth:`GitHubClient.commit_change`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Callable

import aiohttp

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"

# The schema version written by this bot.
CURRENT_VERSION = 2

# A mutation takes the current document and returns ``(new_document, info)``.
# ``info`` must contain at least a ``"commit_message"`` key and may carry any
# extra data the caller wants echoed back to the user (e.g. the new order).
Mutation = Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]]


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
    """The resulting document failed structural validation."""


# --------------------------------------------------------------------------- #
# Data validation + migration (rollback protection — never commit corrupt data)
# --------------------------------------------------------------------------- #
def validate_document(doc: Any) -> None:
    """Validate the full leaderboard document prior to committing.

    Rules (per the v2 data contract):
        * root must be an object with an integer ``version`` and an object
          ``leaderboards``,
        * every leaderboard key is a non-empty ``REGION|Build|mode`` string
          (exactly three non-empty parts),
        * every value is a list of non-empty, case-insensitively-unique names.

    Raises:
        ValidationError: on the first violation found.
    """
    if not isinstance(doc, dict):
        raise ValidationError("Root structure must be a JSON object.")

    version = doc.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValidationError("'version' must be an integer.")

    leaderboards = doc.get("leaderboards")
    if not isinstance(leaderboards, dict):
        raise ValidationError("'leaderboards' must be an object.")

    for key, names in leaderboards.items():
        if not isinstance(key, str) or not key.strip():
            raise ValidationError("Every leaderboard key must be a non-empty string.")
        parts = key.split("|")
        if len(parts) != 3 or not all(p.strip() for p in parts):
            raise ValidationError(
                f"Leaderboard key '{key}' must be 'REGION|Build|mode' (three non-empty parts)."
            )
        if not isinstance(names, list):
            raise ValidationError(f"Leaderboard '{key}' must be an array of player names.")

        seen: set[str] = set()
        for n in names:
            if not isinstance(n, str) or not n.strip():
                raise ValidationError(f"Leaderboard '{key}': every player must be a non-empty string.")
            lowered = n.strip().lower()
            if lowered in seen:
                raise ValidationError(f"Leaderboard '{key}': duplicate player '{n}'.")
            seen.add(lowered)


def migrate_legacy(players: list[Any]) -> dict[str, Any]:
    """Convert a legacy v1 flat player array into a v2 document.

    Each player is placed into every ``region × build × mode`` combo their tags
    cover, preserving the array's relative order (the first player to land in a
    combo becomes that board's #1). Nobody is dropped except entries missing a
    name or region. Casing is normalized: region UPPERCASE, build Title Case,
    mode lowercase — matching the bot's combo keys.
    """
    leaderboards: dict[str, list[str]] = {}
    for player in players:
        if not isinstance(player, dict):
            continue
        region = str(player.get("region", "")).strip().upper()
        name = str(player.get("name", "")).strip()
        if not region or not name:
            continue
        builds = [str(b).strip() for b in player.get("builds", []) if str(b).strip()]
        modes = [str(m).strip() for m in player.get("modes", []) if str(m).strip()]
        for build in builds:
            for mode in modes:
                key = f"{region}|{build.title()}|{mode.lower()}"
                board = leaderboards.setdefault(key, [])
                if not any(existing.strip().lower() == name.lower() for existing in board):
                    board.append(name)
    return {"version": CURRENT_VERSION, "leaderboards": leaderboards}


def _coerce_document(data: Any) -> dict[str, Any]:
    """Normalize stored JSON into a v2 document, migrating a legacy array."""
    if isinstance(data, list):
        return migrate_legacy(data)
    if isinstance(data, dict):
        doc = dict(data)
        doc.setdefault("version", CURRENT_VERSION)
        if not isinstance(doc.get("leaderboards"), dict):
            raise ValidationError("Stored players.json is missing a 'leaderboards' object.")
        return doc
    raise ValidationError("Stored players.json root is neither an array nor an object.")


def serialize_document(doc: dict[str, Any]) -> str:
    """Pretty-print the document as JSON: 2-space indent, Unicode preserved, trailing newline."""
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


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
    async def get_document(self) -> tuple[dict[str, Any], str]:
        """Fetch and decode the current ``players.json`` as a v2 document.

        Returns:
            ``(document, sha)`` — the decoded document and the file's blob SHA.
            A legacy v1 array is migrated to a v2 document transparently.

        Raises:
            GitHubError subclasses on transport/API failure,
            ValidationError if the stored content cannot be coerced.
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

        return _coerce_document(data), sha

    async def put_document(
        self, doc: dict[str, Any], message: str, sha: str
    ) -> dict[str, Any]:
        """Commit a new ``players.json`` using the supplied SHA.

        Returns:
            A dict with ``commit_sha`` and ``status`` (HTTP status code).

        Raises:
            GitHubConflictError if the SHA is stale,
            other GitHubError subclasses on failure.
        """
        session = self._require_session()
        content_b64 = base64.b64encode(serialize_document(doc).encode("utf-8")).decode("ascii")
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

        The ``mutate`` callable receives a *fresh* copy of the current document
        every attempt and must return ``(new_document, info)``. ``info`` must
        include a ``"commit_message"`` string; any domain errors it raises
        (e.g. player-not-found) propagate unchanged and are NOT retried.

        Concurrency safety: because the document is re-fetched (with a fresh SHA)
        on every attempt, a retry re-applies the mutation against the newest
        state — newer updates are never silently overwritten.

        Returns:
            The ``info`` dict augmented with ``commit_sha`` and ``status``.
        """
        attempt = 0
        while True:
            doc, sha = await self.get_document()
            # mutate works on a defensive deep copy so a partial mutation can
            # never leak into a retry.
            working_copy = json.loads(json.dumps(doc))
            new_doc, info = mutate(working_copy)

            if "commit_message" not in info:
                raise GitHubError("Mutation did not provide a commit message.")

            validate_document(new_doc)

            try:
                result = await self.put_document(new_doc, info["commit_message"], sha)
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
