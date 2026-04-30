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
    pip install -r requirements.txt
    ```

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
*   The `requirements.txt` file lists the Python dependencies.
*   The `package.json` file in the `sau_frontend` directory lists the frontend dependencies.
