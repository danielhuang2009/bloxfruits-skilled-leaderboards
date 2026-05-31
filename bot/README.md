# Skill Build Leaderboard — Discord Bot

A production-ready Discord bot that manages the **Blox Fruits skill-build
leaderboard** by editing `players.json` in this repository through the GitHub
Contents API. Every change is a real commit, which automatically triggers a
**Cloudflare Pages** redeploy of the website — no manual file editing required.

* **Website data source:** `players.json` (array order = ranking, top = #1)
* **Bot:** Python 3.12, `discord.py` 2.x, `aiohttp`, `python-dotenv`
* **Commands:** slash commands only

---

## Table of contents

1. [How it works](#how-it-works)
2. [Installation](#1-installation)
3. [Discord Developer Portal setup](#2-discord-developer-portal-setup)
4. [Create the application](#3-create-the-application)
5. [Create the bot](#4-create-the-bot)
6. [Invite the bot](#5-invite-the-bot)
7. [Discord role configuration](#6-discord-role-configuration)
8. [GitHub token creation](#7-github-token-creation)
9. [Repository permissions](#8-repository-permissions)
10. [Environment variables](#9-environment-variables)
11. [Local development](#10-local-development)
12. [Cloudflare Pages workflow](#11-cloudflare-pages-workflow)
13. [Running on a VPS](#12-running-on-a-vps)
14. [Running on Railway](#13-running-on-railway)
15. [Running on Render](#14-running-on-render)
16. [Troubleshooting](#15-troubleshooting)
17. [Common GitHub API errors](#16-common-github-api-errors)
18. [Common Discord permission errors](#17-common-discord-permission-errors)
19. [Command reference](#command-reference)

---

## How it works

```
Admin runs /add … in Discord
        │
        ▼
   bot.py validates input
        │
        ▼
github_client.commit_change:
   1. GET  players.json   → decode Base64 + JSON, capture SHA
   2. apply the mutation (add/remove/move/…)
   3. validate the resulting array  (rollback protection)
   4. PUT  players.json   → Base64 + pretty JSON + SHA
   5. on SHA conflict → re-fetch & retry once
        │
        ▼
GitHub commit  →  Cloudflare Pages auto-redeploy  →  live website updates
```

Concurrency is safe: the file is always re-fetched with a fresh SHA before each
write, and a conflicting write is retried once against the newest state, so a
newer admin's change is never silently overwritten.

---

## 1. Installation

**Prerequisites:** Python **3.12+** and `git`.

```bash
# From the repository root:
cd bot
python -m venv .venv

# Activate the virtual environment:
#   macOS / Linux:
source .venv/bin/activate
#   Windows (PowerShell):
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

Then create your config file:

```bash
cp .env.example .env      # Windows PowerShell: Copy-Item .env.example .env
```

Fill in `.env` using the sections below.

---

## 2. Discord Developer Portal setup

1. Go to <https://discord.com/developers/applications>.
2. Log in with the Discord account that should own the bot.

---

## 3. Create the application

1. Click **New Application**, give it a name (e.g. `SkillBuild Leaderboard`),
   accept the terms, and click **Create**.
2. (Optional) Set an icon and description under **General Information**.

---

## 4. Create the bot

1. In your application, open the **Bot** tab.
2. Click **Reset Token** → **Yes, do it!**, then **Copy** the token.
   Put it in `.env` as `DISCORD_TOKEN`. *Treat this like a password.*
3. **Privileged Gateway Intents:** none are required. This bot uses slash
   commands and reads the invoking member's roles from the interaction payload,
   so you can leave Presence / Server Members / Message Content **off**.

---

## 5. Invite the bot

1. Open **OAuth2 → URL Generator**.
2. Under **Scopes**, check:
   * `bot`
   * `applications.commands`
3. Under **Bot Permissions**, the bot only needs to post messages:
   * `Send Messages`
   * `Embed Links`
   (Use Slash Commands is granted automatically by the `applications.commands` scope.)
4. Copy the generated URL, open it, choose your server, and **Authorize**.

---

## 6. Discord role configuration

Mutating commands require a specific role (Guild Administrators are also always
allowed).

1. In your server: **Server Settings → Roles → Create Role** (e.g. `Leaderboard Admin`).
2. Assign that role to the admins who may edit the leaderboard.
3. Get the role ID: **User Settings → Advanced → Developer Mode = ON**, then
   **Server Settings → Roles → right-click the role → Copy Role ID**.
4. Put it in `.env` as `ADMIN_ROLE_ID`.

> To get instant slash-command availability while testing, also copy your
> **Server ID** (right-click the server icon → *Copy Server ID*) into
> `DISCORD_GUILD_ID`. Without it, global commands can take up to ~1 hour to appear.

---

## 7. GitHub token creation

A **fine-grained personal access token** (recommended) scoped to this repo:

1. <https://github.com/settings/tokens?type=beta> → **Generate new token**.
2. **Resource owner:** the account/org that owns the repo.
3. **Repository access:** *Only select repositories* → choose
   `bloxfruits-skilled-leaderboards`.
4. **Permissions → Repository permissions → Contents:** set to **Read and write**.
5. Generate and copy the token into `.env` as `GITHUB_TOKEN`.

*(A classic token with the `repo` scope also works but grants far more access.)*

---

## 8. Repository permissions

* The token must have **Contents: Read and write** on the target repo.
* Set `GITHUB_OWNER` and `GITHUB_REPO` to match the repository
  (`danielhuang2009` / `bloxfruits-skilled-leaderboards`).
* `PLAYERS_FILE_PATH` defaults to `players.json` at the repo root.
* If you commit to a non-default branch, set `GITHUB_BRANCH`; otherwise leave it
  blank to use the default branch.

---

## 9. Environment variables

| Variable            | Required | Description                                                        |
| ------------------- | -------- | ------------------------------------------------------------------ |
| `DISCORD_TOKEN`     | ✅       | Bot token from the Developer Portal.                               |
| `GITHUB_TOKEN`      | ✅       | Token with **Contents: Read and write** on the repo.               |
| `GITHUB_OWNER`      | ✅       | Repo owner (user or org).                                          |
| `GITHUB_REPO`       | ✅       | Repository name.                                                   |
| `ADMIN_ROLE_ID`     | ✅       | Discord role ID allowed to run mutating commands.                  |
| `PLAYERS_FILE_PATH` | ⬜       | Path to the JSON file. Default: `players.json`.                    |
| `DISCORD_GUILD_ID`  | ⬜       | Guild ID for instant command sync. Default: global sync.           |
| `GITHUB_BRANCH`     | ⬜       | Branch to commit to. Default: repository default branch.           |

See [`.env.example`](./.env.example) for a ready-to-copy template.

---

## 10. Local development

```bash
cd bot
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
python bot.py
```

On startup the bot logs in, syncs its slash commands, and prints the managed
repo. If you set `DISCORD_GUILD_ID`, commands appear in that server within
seconds; otherwise allow up to an hour for global propagation.

Logs are written to stdout with timestamps, e.g.:

```
2026-05-30 16:40:11 | INFO    | leaderboard.bot | cmd=add user=danewl#0 id=123 status=success commit=ab12cd3 http=200
```

---

## 11. Cloudflare Pages workflow

This repo is deployed by Cloudflare Pages (see `wrangler.jsonc`). The pipeline:

1. An admin runs a slash command.
2. The bot commits an updated `players.json` to GitHub.
3. Cloudflare Pages detects the push and **automatically redeploys** the site.
4. `index.html` fetches `players.json` on load, so the new ranking appears once
   the deploy finishes (typically seconds to a minute).

No Cloudflare configuration is needed for the bot itself — it only talks to
GitHub. Just ensure Cloudflare Pages is connected to this repository with
auto-deploy on push enabled (the default).

---

## 12. Running on a VPS

Run it under `systemd` so it restarts on crashes/reboots.

```ini
# /etc/systemd/system/leaderboard-bot.service
[Unit]
Description=Skill Build Leaderboard Discord Bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/bloxfruits-skilled-leaderboards/bot
ExecStart=/opt/bloxfruits-skilled-leaderboards/bot/.venv/bin/python bot.py
EnvironmentFile=/opt/bloxfruits-skilled-leaderboards/bot/.env
Restart=always
RestartSec=5
User=botuser

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now leaderboard-bot
sudo journalctl -u leaderboard-bot -f      # follow logs
```

---

## 13. Running on Railway

1. Create a new project → **Deploy from GitHub repo** → select this repository.
2. **Settings → Root Directory:** `bot`.
3. **Build / Start:**
   * Install command: `pip install -r requirements.txt`
   * Start command: `python bot.py`
4. **Variables:** add every variable from [section 9](#9-environment-variables)
   (do **not** upload `.env`).
5. Deploy. Railway keeps the worker running; check the **Deploy Logs** for the
   "Synced N command(s)" line.

> The bot is a long-running worker, not a web service — it needs no public port.

---

## 14. Running on Render

1. **New → Background Worker** (not a Web Service) → connect this repo.
2. **Root Directory:** `bot`.
3. **Build Command:** `pip install -r requirements.txt`
4. **Start Command:** `python bot.py`
5. **Environment:** add all variables from [section 9](#9-environment-variables).
6. Create the worker and watch the logs for a successful login + command sync.

---

## 15. Troubleshooting

| Symptom                                   | Likely cause / fix                                                                 |
| ----------------------------------------- | ---------------------------------------------------------------------------------- |
| `Configuration error: … is missing`       | A required env var is unset. Check `.env` / platform variables.                    |
| Bot starts but commands don't appear      | Global sync is slow — set `DISCORD_GUILD_ID` for instant guild sync.               |
| Commands appear but "did not respond"     | The bot process isn't running, or it can't reach GitHub. Check logs.               |
| `/list` works, edits fail                 | The GitHub token lacks **Contents: write**, or owner/repo/path is wrong.           |
| "You do not have permission…"             | The user lacks `ADMIN_ROLE_ID` and isn't a Guild Administrator.                    |
| Website didn't update after a commit      | Cloudflare deploy still in progress, or browser cached `players.json` (hard-refresh). |
| `ImproperlyConfigured` / login fails      | `DISCORD_TOKEN` is wrong or was reset — regenerate and update `.env`.              |

---

## 16. Common GitHub API errors

The bot maps these to friendly Discord replies; here's what they mean:

| HTTP | Meaning                          | Resolution                                                          |
| ---- | -------------------------------- | ------------------------------------------------------------------- |
| 401  | Bad credentials                  | `GITHUB_TOKEN` invalid/expired — regenerate it.                     |
| 403  | Forbidden / rate limited         | Token lacks write access, or you hit the rate limit (wait & retry). |
| 404  | Not Found                        | Wrong `GITHUB_OWNER`/`GITHUB_REPO`/`PLAYERS_FILE_PATH`, or token can't see the repo. |
| 409  | Conflict (stale SHA)             | Two edits raced. The bot retries once automatically; if it still fails, re-run the command. |
| 422  | Unprocessable (usually stale SHA)| Same as 409 — handled as a conflict and retried.                    |
| 429  | Too Many Requests                | Secondary rate limit — wait a moment and try again.                 |

`players.json` is always written **pretty-printed (2-space indent), with Unicode
preserved and no minification**, and is validated before every commit — a change
that would produce an invalid array is **aborted** and never pushed.

---

## 17. Common Discord permission errors

| Message / behavior                          | Cause / fix                                                                  |
| ------------------------------------------- | ---------------------------------------------------------------------------- |
| "You do not have permission…" (ephemeral)   | Expected for non-admins. Give them `ADMIN_ROLE_ID` or Administrator.         |
| Slash commands missing for a user           | They need the role only for *mutating* commands; `/list` is open to everyone.|
| Bot can't post in a channel                 | Grant it `Send Messages` + `Embed Links` in that channel.                    |
| "Missing Access" on invite                  | Re-invite with both `bot` and `applications.commands` scopes (section 5).    |
| Commands don't show at all                  | The `applications.commands` scope wasn't authorized — re-invite the bot.     |

---

## Command reference

Everyone:

| Command | Description |
| ------- | ----------- |
| `/list` | Show the current leaderboard, live from GitHub. |

Admin only (require `ADMIN_ROLE_ID` or Guild Administrator):

| Command        | Parameters                                              | Effect |
| -------------- | ------------------------------------------------------- | ------ |
| `/add`         | `name`, `region`, `builds`, `modes`                     | Append a player at the bottom. `builds`/`modes` are comma-separated. |
| `/remove`      | `name`                                                  | Remove a player (case-insensitive). |
| `/move`        | `name`, `position`                                      | Move a player to a rank (1 = top; beyond the list size → end). |
| `/addbuild`    | `name`, `build`                                         | Add a build to a player (no case-insensitive duplicates). |
| `/removebuild` | `name`, `build`                                         | Remove a build from a player. |
| `/updateregion`| `name`, `region`                                        | Change a player's region. |
| `/editplayer`  | `name`, `region?`, `builds?`, `modes?`                  | Edit any of region/builds/modes (provide at least one). |

All inputs are trimmed; comma-separated values become arrays with case-insensitive
de-duplication. Player matching is case-insensitive throughout.
