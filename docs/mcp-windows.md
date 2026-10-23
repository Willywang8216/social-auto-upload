# Running `sau-mcp` on Windows

`docs/mcp.md` is the reference for the MCP server itself. This page covers the
Windows-specific gaps: what did not work out of the box, what changed, and how
to install, run and verify the server on a Windows host.

The branch you are on (`windows`) is `main` plus the three fixes below. It
carries **no** functional change for the Linux/Docker deployment — every
changed path keeps its original POSIX behaviour as the last fallback.

---

## 1. What was broken on Windows

### 1.1 `pip install -e ".[web]"` failed outright

The `web` extra pinned `gunicorn>=21.2` unconditionally. gunicorn imports
`fcntl` at startup and has no Windows support, so the install aborted. Because
setuptools only writes the console scripts **after** a successful install, the
failure also removed the `sau-mcp` entry point — the MCP server could not be
installed at all, and the error message pointed at gunicorn rather than at the
MCP.

**Fix** — `pyproject.toml` gates it:

```toml
"gunicorn>=21.2; sys_platform != 'win32'",
```

Windows installs `.[web]` without gunicorn and serves the app with the dev
server (`python sau_backend.py`) or with `waitress` if you want a production
WSGI server. gunicorn remains mandatory on POSIX.

### 1.2 A Chinese payload killed the stdio transport

The stdio transport writes JSON-RPC frames to `sys.stdout`. On Windows the
default text encoding is the ANSI code page — **cp950** on a Traditional
Chinese install, cp1252 elsewhere — not UTF-8. Measured on a Windows 11
zh-TW host with no `PYTHONUTF8`/`PYTHONIOENCODING` set:

```
sys.stdout.encoding           -> cp950
locale.getpreferredencoding() -> cp950
```

cp950 *does* cover common Traditional Chinese, so a profile name like
`威威教學` passes. It does **not** cover emoji, Simplified Chinese or Hangul,
and the first frame carrying one raises `UnicodeEncodeError` *inside the
transport*:

```
UnicodeEncodeError: 'cp950' codec can't encode character '\U0001f31e' in position 2: illegal multibyte sequence
```

That is not a hypothetical here: the account names in this deployment include
`威威🌞` (YouTube), `Willy Dev 😁` (X) and `色色的光光🥵`, all of which contain
code points cp950 rejects. Measured rejections: emoji `U+1F31E` / `U+1F601` /
`U+1F975`, Simplified `教学`, Hangul `한글`; accepted: `威威教學`, kana.

The client sees a dead server, not an error, so the symptom is "the tools
disappeared" rather than "this tool failed". That is the worst possible
failure mode for an agent.

**Fix** — `mcp_server/server.py` reconfigures the three standard streams to
UTF-8 as the first statement of `main()`:

```python
_force_utf8_stdio()
```

It is a no-op where the streams are already UTF-8 or cannot be reconfigured
(pytest capture, a non-file stream), so the Linux behaviour is unchanged.

### 1.3 `jobs_system_health` was silently blind

`jobs_system_health` probed hardcoded POSIX locations:

```python
for p in ("/home/will/sau-inbox", "/app/sau-inbox"):          # inbox state
for logp in ("/home/will/social-auto-upload/logs/offload.log",
             "/app/logs/offload.log"):                        # offload log
```

Neither exists on Windows, so the tool reported `ready: 0` and
`offload: {healthy: null}` — indistinguishable from a genuinely empty queue.
A silent wrong answer is worse than an error.

**Fix** — `mcp_server/tools/jobs.py` resolves candidates in this order:

1. an explicit override — `SAU_WATCH_STATE` (the same variable
   `myUtils/inbox_archive.py` already honours) and `SAU_OFFLOAD_LOG`;
2. `BASE_DIR`-relative — `<checkout>/sau-inbox/state.json` and
   `<checkout>/logs/offload.log`, which resolves correctly on either OS;
3. the original POSIX literals, last, so Linux and Docker are unaffected.

---

## 2. Install on Windows

Python 3.10–3.12 (the range `pyproject.toml` declares). From PowerShell:

```powershell
git clone https://github.com/Willywang8216/social-auto-upload.git
cd social-auto-upload

py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1

# PyPI mirror first if you are in Taiwan — the default index is slow here.
python -m pip install -U pip
python -m pip install -e ".[web]"
```

`pip install -e ".[web]"` is what registers both console scripts. Confirm:

```powershell
Get-Command sau, sau-mcp
```

Expect `.\.venv\Scripts\sau.exe` and `.\.venv\Scripts\sau-mcp.exe`.

Browser-driven platforms (Douyin, Xiaohongshu, Kuaishou, Bilibili, WeChat
Channel) additionally need Chromium for `patchright`; see `docs/CLI.md`.

---

## 3. Run it

```powershell
# stdio — the client spawns this process itself (the normal case)
sau-mcp

# HTTP — bind locally for a remote/agent client
$env:SAU_MCP_TRANSPORT = "http"
$env:SAU_MCP_HOST = "127.0.0.1"
$env:SAU_MCP_PORT = "8765"
sau-mcp
```

Do not bind HTTP to `0.0.0.0` on Windows without a firewall rule and an
access-control layer in front: this server exposes full CRUD plus publishing.

### Pointing at a database

Resolution order is `db_path` argument → `SAU_MCP_DB_PATH` →
`SAU_DB_PATH` → `BASE_DIR/db/database.db`:

```powershell
$env:SAU_MCP_DB_PATH = "D:\sau-data\database.db"
```

`SAU_DB_PATH` is the app-wide override and must be set **before** the process
starts, because modules bind `DB_PATH` at import time (see the note in
`utils/conf_defaults.py`).

---

## 4. Client configuration

Claude Desktop — `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sau": {
      "command": "C:\\path\\to\\social-auto-upload\\.venv\\Scripts\\sau-mcp.exe",
      "args": [],
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "SAU_MCP_DB_PATH": "C:\\path\\to\\social-auto-upload\\db\\database.db"
      }
    }
  }
}
```

Use the **absolute path to `sau-mcp.exe`**. A bare `sau-mcp` only resolves if
the venv's `Scripts` directory happens to be on `PATH`, which it is not for a
GUI-launched client.

`PYTHONIOENCODING=utf-8` is belt-and-braces on top of the in-process fix in
§1.2 — it also covers any subprocess the server spawns.

---

## 5. Verify

```powershell
# initialize + tools/list over stdio. Needs no DB and no connected account.
@'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
'@ | sau-mcp
```

Expect two JSON-RPC responses: the first carries `serverInfo` naming `sau-mcp`
with its version, the second a `tools` array of 36 entries. This is the
cheapest end-to-end proof that the install, the entry point and the stdio
transport are all working — it needs the `fastmcp` dependency but nothing else.

Expect a `serverInfo` line naming `sau-mcp`. Then, from the agent: `whoami`
(workspace + DB path), `accounts_health` (per-platform `{ready, total}`), and
`jobs_system_health` (this is the one that used to lie — it should now name a
real `inbox.statePath` when the watcher has written one).

---

## 6. Known remaining POSIX assumptions

These live outside `mcp_server/` and are **not** fixed on this branch. They
affect the SAU-Inbox / Drive-offload features, not the MCP control surface:

| File | Assumption |
|---|---|
| `myUtils/inbox_archive.py` | defaults to `/home/will/sau-inbox/state.json` and `/home/will/.config/rclone/rclone.conf` |
| `myUtils/inbox_autopublish.py` | same inbox default |
| `myUtils/inbox_ops.py`, `myUtils/inbox_drive.py` | parse `/sau-inbox/` and `/app/sau-inbox/` markers out of paths |
| `myUtils/campaigns.py`, `myUtils/campaign_media_prep.py` | match `/app/`-prefixed media paths |
| `demo/generate_narrations.py` | `/home/will/social-auto-upload/111.m4a` sample |

Each already accepts an environment override for the common case, so a
Windows host that does not use the inbox/Drive pipeline is unaffected.
