# sau-mcp: Model Context Protocol server

`sau-mcp` exposes every social-auto-upload CRUD + publish/schedule operation
as [Model Context Protocol](https://modelcontextprotocol.io) tools so AI
agents (Cursor, Claude Desktop, custom OpenAI Agents, etc.) can manage
accounts, schedule posts, and read publish-job state without direct
database or filesystem access.

The server is a thin wrapper around the same Python modules the Flask
backend uses — `myUtils.profiles`, `myUtils.jobs`,
`myUtils.publish_orchestrator`, `myUtils.publish_templates`, and
`myUtils.account_events` — so behaviour matches the web UI byte-for-byte.

## Install

The `sau-mcp` console script is registered alongside `sau` (the CLI):

```bash
uv sync --extra web          # installs fastmcp + Flask + the MCP server
# or
pip install -e .             # installs both entry points
```

The `fastmcp==3.4.7` dependency is pinned in **both** `pyproject.toml`
and `requirements.txt` per the project's dependency-manifest policy.

## First-time setup

`sau-mcp` is a *control surface* on top of the existing social-auto-upload
workspace. The first account has to be connected through the web UI; once it
is in the SQLite file, the agent sees it on the next `accounts_list`.

1. **Connect at least one account in the web UI.** Cookie-based platforms
   (Douyin, Xiaohongshu, Kuaishou, Bilibili, WeChat Channel) need a real
   browser to harvest the session — go to `/#/accounts`, click a platform,
   and complete the login. OAuth platforms (TikTok, YouTube, Meta, Reddit,
   Threads, X, Patreon) start their callback from the UI as well.
2. **Register the MCP server with your AI client.** `sau skill install`
   patches Claude Desktop / Cursor / Claude Code in one shot (idempotent
   — safe to re-run). Use `sau skill install --dry-run` to preview, or
   `sau skill remove` to undo.
3. **Restart your AI client** so it discovers the new MCP server.
4. **Sanity-check from the agent:** ask it to call `whoami` (workspace
   info + DB path) and `accounts_health` (per-platform `{ready, total}`
   summary). If a platform shows `ready: 0`, finish its login in the web
   UI and re-run `accounts_check` from the agent.

### What the agent can do end-to-end without touching the UI

Once an account row exists in the DB:

- Full CRUD on profiles, accounts (including `group` reassignment),
  publish templates, and jobs
- `publish_submit` / `publish_preview` / `publish_regenerate` against any
  account already registered
- Read all job state (`jobs_list` / `jobs_get`); cancel / drain (`jobs_cancel`
  / `jobs_run` for dev)
- Register new media files under `videoFile/` via `upload_register`
- Self-introspection via `whoami` and `supported_platforms`

The MCP server reads from the same SQLite file the web UI writes to.
Anything you create, edit, regroup, or schedule in `/#/accounts` or
`/#/profiles` is visible to the agent on its next list call — no
restart, no resync.

#### Live sync (no resync step required)

Because every MCP tool calls `myUtils.profiles` / `myUtils.publish_templates`
/ `myUtils.account_events` against the project SQLite directly, the
agent sees UI-driven changes the next time it issues a list/get call —
no MCP restart, no cache flush, no manual signal needed.

| UI action                                           | Agent sees it on               |
| --------------------------------------------------- | ------------------------------ |
| Add / edit / delete an account in `/#/accounts`     | `accounts_list` / `accounts_get` |
| Reassign an account to a different group            | `accounts_list` / `accounts_groups` |
| Create / rename / delete a profile in `/#/profiles` | `profiles_list` / `profiles_get` |
| Save / edit a Publish Center template               | `publish_templates_list`       |
| Schedule a video from `/#/publish-center`           | `jobs_list` (after worker picks it up) |
| Connect a brand-new platform account               | `accounts_list` + `accounts_health` |

The only exception is a hard MCP-server restart: in-process state
held by the running agent (e.g. the cursor returned from one tool call
referenced by the next) resets, but the *workspace state* on disk does
not.

### What still needs the UI

- The first cookie / OAuth login for each platform (cookie files require a
  browser session; OAuth callbacks hit `/oauth/<platform>/callback` on the
  Flask backend)
- The very first schema bootstrap (`python db/createTable.py` or
  `alembic upgrade head`)
- Manual edits to media assets (watermark configs, video processing)
  unless you wire them through the REST API

## Transports

| Transport          | Use case                                | Config                                       |
| ------------------ | --------------------------------------- | -------------------------------------------- |
| `stdio` (default)  | Local agents (Claude Desktop, `mcp` CLI) | `SAU_MCP_TRANSPORT=stdio` (or unset)        |
| `streamable-http`  | Remote agents (network-exposed)          | `SAU_MCP_TRANSPORT=http` + `SAU_MCP_HOST`/`SAU_MCP_PORT` |

```bash
# stdio (default) — the agent's runtime spawns the process directly.
sau-mcp

# HTTP — bind on 0.0.0.0:8765 for a remote agent.
SAU_MCP_TRANSPORT=http SAU_MCP_HOST=0.0.0.0 SAU_MCP_PORT=8765 sau-mcp
```

The HTTP transport binds the FastMCP Streamable-HTTP server. Reverse-proxy
auth (e.g. an OAuth bearer-token middleware) is the operator's
responsibility — the MCP layer is intentionally transport-agnostic today.

### Database path resolution

Every tool accepts an optional `db_path` argument. Resolution order:

1. The `db_path` argument (if provided)
2. The `SAU_MCP_DB_PATH` environment variable
3. The project default (`myUtils.profiles.DB_PATH`)

Production callers should leave `db_path` unset so the operator's
existing SQLite file is used; tests pass an explicit path.

### Workspace scoping

Multi-tenant operators can set:

- `SAU_MCP_WORKSPACE_ID` — surfaced by `whoami` for client introspection.
- `SAU_MCP_TENANCY_MODE` — surfaced the same way; defaults to `single`.

The MCP layer is workspace-agnostic today (same posture as the open-mode
Flask backend). Wire `workspace_id` into the tool calls when the tenant
expansion lands.

## Tool reference

### Discovery

| Tool                  | Purpose                                                           |
| --------------------- | ----------------------------------------------------------------- |
| `whoami`              | Server version, transport, resolved DB path, workspace hints.     |
| `supported_platforms` | Slug + `requiresCookie` / `defaultsToOauth` / `supportsDirectPublish` / `supportsSheetExport` per platform. |

### Profiles

| Tool                | Purpose                                              |
| ------------------- | ---------------------------------------------------- |
| `profiles_list`     | List every profile.                                  |
| `profiles_get`      | Fetch one profile.                                   |
| `profiles_create`   | Create a profile (name, description, settings).      |
| `profiles_update`   | Update name / description / settings / extended fields. |
| `profiles_delete`   | Delete a profile.                                    |

### Accounts

| Tool                | Purpose                                                              |
| ------------------- | -------------------------------------------------------------------- |
| `accounts_list`     | List with optional `profile_id`, `platform`, `enabled`, `group`.    |
| `accounts_get`      | Fetch one account.                                                   |
| `accounts_groups`   | Distinct, non-empty operator-defined groups.                         |
| `accounts_create`   | Create an account. Auto-resolves cookie_path when omitted.           |
| `accounts_update`   | Update nickname, group, account_name, profile_id, auth_type, config, enabled, status. |
| `accounts_delete`   | Delete an account.                                                   |
| `accounts_check`    | Probe the live cookie/OAuth connection.                              |
| `accounts_health`   | Per-platform `{platform, total, ready, pct}` summary.                |

### Publish templates

| Tool                       | Purpose                                          |
| -------------------------- | ------------------------------------------------ |
| `publish_templates_list`   | List every template.                             |
| `publish_templates_get`    | Fetch one template.                              |
| `publish_templates_create` | Create a template.                               |
| `publish_templates_update` | Update name / description / config / settings.   |
| `publish_templates_delete` | Delete a template.                               |

### Files

| Tool              | Purpose                                                                            |
| ----------------- | ---------------------------------------------------------------------------------- |
| `upload_register` | Register an existing file under `videoFile/` (path sandboxed, idempotent).         |

### Publish / schedule

| Tool                  | Purpose                                                              |
| --------------------- | -------------------------------------------------------------------- |
| `publish_submit`      | Fan out + enqueue jobs. `schedule` accepts `{publishNow:true}` or `{startAt:"…"}`. |
| `publish_preview`     | Dry-run: build per-account drafts without enqueuing jobs.            |
| `publish_regenerate`  | Re-run the LLM draft for one (profile, account) pair.                 |

### Jobs

| Tool          | Purpose                                                  |
| ------------- | -------------------------------------------------------- |
| `jobs_list`   | List recent jobs (status / platform / limit filters).    |
| `jobs_get`    | Fetch one job + its targets.                             |
| `jobs_cancel` | Cancel a queued or running job.                          |
| `jobs_run`    | Drain the queue synchronously in this process (dev only). |

## Error envelopes

Every tool returns either the success payload or a structured error
envelope so the agent can branch on `error` rather than parsing free-form
strings:

```json
{"error": "not_found",      "message": "Profile not found: id=999"}
{"error": "invalid_input",  "message": "Unsupported platform: 'nonsense'"}
{"error": "forbidden",      "message": "..."}
{"error": "unsupported",    "message": "..."}
{"error": "internal",       "message": "...", "type": "IntegrityError"}
```

## Smoke test

```bash
# In-memory tool listing (via fastmcp's CLI):
.venv/bin/python -c "
import asyncio
from mcp_server.server import build_server

async def main():
    server = build_server()
    tools = await server.list_tools()
    print(f'{len(tools)} tools registered')

asyncio.run(main())
"
```

## Claude Desktop example

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(Mac) / `%APPDATA%/Claude/claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "sau": {
      "command": "/home/will/social-auto-upload/.venv/bin/sau-mcp",
      "env": {
        "SAU_MCP_DB_PATH": "/home/will/social-auto-upload/db/database.db"
      }
    }
  }
}
```

Claude will discover 28 tools and can drive CRUD + publish + scheduling
through the chat interface.

## Testing

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py -x -q
```

22 tests cover discovery, profile/account/template CRUD, file register,
job list/get/cancel, publish preview/regenerate, and error-envelope
shapes.
