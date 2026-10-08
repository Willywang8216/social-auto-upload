## Project Overview

This project, `social-auto-upload`, is a powerful automation tool designed to help content creators and operators efficiently publish video content to multiple domestic and international mainstream social media platforms in one click. The project implements video upload, scheduled release and other functions for platforms such as `Douyin`, `Bilibili`, `Xiaohongshu`, `Kuaishou`, `WeChat Channel`, `Baijiahao` and `TikTok`.

The project consists of a Python backend and a Vue.js frontend.

**Backend:**

*   Framework: Flask
*   Core Functionality:
    *   Handles file uploads and management.
    *   Interacts with a SQLite database to store information about files and user accounts.
    *   Uses `playwright` for browser automation to interact with social media platforms.
    *   Provides a RESTful API for the frontend to consume.
    *   Uses Server-Sent Events (SSE) for real-time communication with the frontend during the login process.

**Frontend:**

*   Framework: Vue.js
*   Build Tool: Vite
*   UI Library: Element Plus
*   State Management: Pinia
*   Routing: Vue Router
*   Core Functionality:
    *   Provides a web interface for managing social media accounts, video files, and publishing videos.
    *   Communicates with the backend via a RESTful API.

**Command-line Interface:**

The project also provides a command-line interface (CLI) for users who prefer to work from the terminal. For new Douyin CLI work, prefer the `sau douyin ...` entrypoint over legacy example scripts.

*   `login`: To log in to the Douyin uploader account.
*   `check`: To verify whether the saved Douyin cookie is still valid.
*   `upload`: To upload one video file with explicit metadata flags.

## Building and Running

### Backend

1.  **Install dependencies:**
    ```bash
    uv sync --extra web
    # or: pip install -e '.[web]'
    ```
    `pyproject.toml` is the primary Python dependency metadata for backend/development installs. Use the `web` extra when you need the Flask server entrypoints.

    For Docker / legacy compatibility installs, keep using:
    ```bash
    pip install -r requirements.txt
    ```
    `requirements.txt` still needs to mirror backend runtime dependencies because the current Dockerfile installs from it.

2.  **Install Playwright browser drivers:**
    ```bash
    playwright install chromium
    ```

3.  **Initialize the database:**
    ```bash
    python db/createTable.py
    ```

4.  **Run the backend server:**
    ```bash
    python sau_backend.py
    ```
    The backend server will start on `http://localhost:5409`.

5.  **(Production) Run a standalone worker:**
    ```bash
    python -m myUtils.worker --max-concurrent 3
    ```
    The worker drains the `publish_jobs` queue, runs uploads with bounded
    concurrency, retries failed targets with exponential backoff, and writes
    a per-job log to `logs/jobs/job-<id>.log` with structured fields
    (`job_id`, `target_id`, `platform`, `account_ref`, `attempt`). Pass
    `--once` to drain and exit; otherwise the worker runs until it receives
    `SIGINT`/`SIGTERM`, then drains in-flight targets before exiting. Set
    `SAU_JSON_LOGS=1` for newline-delimited JSON instead of human text.

6.  **(Production) Encrypt cookie files at rest:**
    ```bash
    # Generate a 32-byte AES key once (store it in your secret manager).
    python -c "import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"

    # Then export it before running the backend / worker.
    export SAU_COOKIE_ENCRYPTION_KEY=<base64-key>

    # Migrate every existing plaintext cookie in place (idempotent).
    sau cookies encrypt
    ```
    The encryption is opt-in: when `SAU_COOKIE_ENCRYPTION_KEY` is unset
    cookies stay as plaintext, exactly like before. When the key is set
    every read/write goes through `myUtils.cookie_storage`, which uses
    AES-GCM with the basename as AAD, atomic temp-file + rename, and
    `0o600` permissions. The worker decrypts to a `0o600` tempfile right
    before calling each uploader and re-encrypts on exit.

    Independently, `SAU_CONFIG_ENCRYPTION_KEY` (same base64 key format)
    encrypts the OAuth secret values inside `accounts.config_json`
    (access/refresh tokens, app secrets) at rest via `myUtils.config_crypto`,
    which reuses the same AES-GCM envelope (AAD = the secret's field name).
    Encryption is per-value, so non-secret fields stay readable; it is opt-in
    (unset key = plaintext, unchanged) and applied at the single `myUtils.profiles`
    choke point (encrypt in `add_account`/`update_account`, decrypt in
    `_row_to_account`), so every consumer still sees plaintext.

7.  **Schema migrations (Alembic):**
    The schema is now Alembic-managed under `migrations/versions/`.
    `python db/createTable.py` runs `alembic upgrade head` against
    `db/database.db`. To target a different file, set `SAU_DB_PATH` or
    invoke `alembic` directly: `alembic -x url=sqlite:///<path> upgrade head`.
    For new schema work, add a new revision under `migrations/versions/`
    rather than editing the legacy `CREATE TABLE` block in
    `db/createTable.py`.

### Frontend

1.  **Navigate to the frontend directory:**
    ```bash
    cd sau_frontend
    ```

2.  **Install dependencies:**
    ```bash
    npm install
    ```

3.  **Run the development server:**
    ```bash
    npm run dev
    ```
    The frontend development server will start on `http://localhost:5173`.

### Command-line Interface

The CLI is exposed as the `sau` console script (configured in `pyproject.toml` as `sau = "sau_cli:main"`). Install the project (`uv sync` or `pip install -e .`) and then call `sau <platform> <action> ...`.

**Login:**

```bash
sau douyin login --account <account_name>
```

**Check:**

```bash
sau douyin check --account <account_name>
```

**Upload:**

```bash
sau douyin upload --account <account_name> --file <video_file> --title <title> [--tags tag1,tag2] [--schedule YYYY-MM-DD HH:MM]
```

**Install bundled skill:**

```bash
sau skill install
```

## Development Conventions

*   The backend code is located in the root directory and the `myUtils` and `uploader` directories.
*   The frontend code is located in the `sau_frontend` directory.
*   The project uses a SQLite database for data storage. The database file is located at `db/database.db`.
*   The `conf.example.py` file should be copied to `conf.py` and configured with the appropriate settings.
*   `pyproject.toml` is the primary Python dependency metadata for `uv sync` / `pip install -e .`.
*   `requirements.txt` remains the Docker / legacy compatibility install path and must stay aligned for backend runtime dependencies.
*   The `package.json` file in the `sau_frontend` directory lists the frontend dependencies.
*   Platform posting limits are owned in code by `myUtils/platform_limits.py` (the enforced single source of truth, locked by `tests/test_platform_limits.py`); draft generation, media prep and the publishers all read from it. The human-readable table (with app-vs-API differences and notable restrictions) is in `docs/platform-posting-limits.md`, and the per-number source citations are in `logs/platform-limits-research.md`. Update the module first, then keep both docs in sync.

## Agent sessions (default hygiene)

Subagents and parallel sessions must not accumulate. **Ending finished sessions is
a default action, not something to be asked for** — do it as part of finishing the
work.

### Before ending a session, confirm it is safe

1. **No uncommitted work.** `git status --short` must show no modified source
   files. If a session's work is uncommitted, commit or report it first — killing
   the session loses it.
2. **Nothing is being written.** Check for files modified in the last ~15 minutes:
   ```bash
   find . -maxdepth 2 -newermt "-15 minutes" -type f \
     \( -name "*.py" -o -name "*.md" \) \
     -not -path "./.venv/*" -not -path "./.claude/*"
   ```
3. **Its task is settled.** `agent-manager task list` should show it `done`. A
   task still held by a finished session is a stale lock — ask that session to
   release it, or claim it yourself if it is genuinely complete.
4. **Its leases are released.** `agent-manager reservations` must not name it. If
   it does, message the session to release, or `release-files` once it is gone.

### Ending them

```bash
agent-manager sessions --json          # list; note ids and which are `self`
agent-manager archive <id>             # files it out of the active list
agent-manager kill <id>                # stops the process
```

Archive **then** kill — archiving alone leaves the process running. Never kill
your own `self: true` session.

### Reporting

When a cleanup pass runs, state how many sessions were ended and confirm each was
safe (work committed, tasks done, leases released). Do not silently leave orphans:
a session still running with no task and no recent activity is exactly what this
rule exists to prevent.
