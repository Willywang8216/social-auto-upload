# Agent Bootstrap Prompt

这份文档是写给 `OpenClaw`、`Codex`、`Claude Code / cc` 这类 agent 客户端用户的。

目标不是让 agent 先通读整个仓库，而是先把 `social-auto-upload` 安装到可运行、可验证、可继续执行任务的状态。

当前主线已接入的平台：

- `bilibili`
- `douyin`
- `kuaishou`
- `xiaohongshu`

## 这份文档解决什么问题

现在仓库里已经有：

- 安装说明
- CLI 文档
- 平台 skill

但这些内容更偏向“agent 进入仓库之后怎么执行”。

这份文档补的是“用户第一次把仓库交给 agent 客户端时，应该怎么说”。

## 推荐使用方式

1. 把整个仓库给你的 agent 客户端。
2. 把下面这段启动提示词完整发给它。
3. 等 agent 完成安装和 CLI 验证后，再继续给它下达登录、上传、定时发布等任务。

## 通用启动提示词

复制下面整段，发给你的 agent：

```text
你现在在一个名为 `social-auto-upload` 的仓库中工作。

这是一个多平台社交媒体自动发布项目。当前主线已经接入：

- bilibili
- douyin
- kuaishou
- xiaohongshu

你的第一目标不是通读全部源码，也不是优先运行历史 examples，而是先把项目安装到“可运行、可验证、可继续执行任务”的状态。

请遵守以下规则：

1. 默认把仓库根目录视为当前工作目录。
2. 优先使用 `uv` 管理 Python 环境，不要默认回退到旧的 `requirements.txt`。
3. 优先使用当前主线 CLI：`sau`。
4. 优先参考这些文档：
   - `docs/install.md`
   - `docs/CLI.md`
   - `docs/update.md`
5. 如果需要平台级操作，优先参考这些 skill：
   - `skills/douyin-upload/`
   - `skills/kuaishou-upload/`
   - `skills/xiaohongshu-upload/`
   - `skills/bilibili-upload/`
6. 不要默认走历史 `examples/` 和旧 Web 路径，除非当前 CLI 主线不可用。
7. 如果登录流程生成二维码图片，不要只返回图片路径；请直接展示图片，或者明确告诉我该打开哪个本地图片文件扫码。
8. 如果是 Bilibili 登录，不要在非交互环境里强行代跑；应改为指导我在本地真实终端执行。
9. 安装完成后，请优先验证以下命令：
   - `sau --help`
   - `sau douyin --help`
   - `sau kuaishou --help`
   - `sau xiaohongshu --help`
   - `sau bilibili --help`
10. 完成后，请明确输出：
   - 你实际执行了哪些命令
   - 哪些验证通过了
   - 当前项目是否已经进入“可继续登录/上传”的状态
   - 推荐我下一步执行什么

如果过程中遇到错误，不要跳过，请先说明错误，再给出你准备采取的下一步动作。
```

## 安装完成后，你可以继续怎么说

下面这些是你可以继续发给 agent 的任务示例。

### 做一次平台登录

```text
请继续帮我登录小红书账号，使用有头模式，账号名用 `creator`。
```

```text
请继续帮我登录抖音账号，使用无头模式，账号名用 `creator`。
```

### 做一次 CLI 可用性检查

```text
请检查 bilibili、douyin、kuaishou、xiaohongshu 四个平台的 CLI 入口是否都可用，并告诉我缺什么依赖。
```

### 做一次真实上传

```text
请使用 xiaohongshu CLI，帮我上传一个图文草稿，使用定时发布，不要立即发布。
```

```text
请使用 douyin CLI，帮我上传一个视频，优先走当前主线，不要走历史 example。
```

## OpenClaw / Codex / Claude Code 使用建议

### OpenClaw

- 适合直接粘贴上面的完整启动提示词
- 如果支持把仓库作为工作目录挂载进去，优先先挂载仓库，再发提示词
- 如果支持本地文件展示，登录二维码应让 agent 直接展示图片

### Codex

- 建议先让它完成 bootstrap，再继续发平台任务
- 让它优先使用 `docs/install.md`、`docs/CLI.md` 和 `skills/`
- 不要让它一开始自由探索整个仓库，否则容易走到历史路径

### Claude Code / cc

- 建议先让仓库成为当前 workspace
- 再发完整启动提示词
- 后续按“安装 -> 验证 -> 登录 -> 上传”顺序继续给任务

## 为什么不按平台拆四套提示词

因为这个项目现在已经有统一的 CLI 主线。

用户第一次把仓库交给 agent 时，更需要的是：

- agent 知道主入口是什么
- agent 知道应该优先走哪条路径
- agent 知道哪些是历史路径
- agent 安装完成后先给出明确验收结果

等进入执行阶段，再让 agent 根据你的实际目标去选择：

- `bilibili`
- `douyin`
- `kuaishou`
- `xiaohongshu`

这样比给用户准备四套平台 prompt 更稳，也更容易维护。

## Agent handoff：排程、Telegram 與 Drive 發佈（2026-09-30）

- **Project shape:** Flask/Python backend + Vue/Vite frontend; `/publish/queue` wraps `JobsView.vue`, and `/publish/calendar` is a separate `CalendarView.vue` route. Keep the user's existing `JobsView.vue` worktree edit intact.
- **Schedule UI:** calendar events are per scheduled target (not one earliest timestamp per content entity); `showAll` filters statuses correctly; day overflow opens all that day's destinations. Details separate profiles, media, platform copy, target times/status/errors.
- **Drive offload:** root cause was user crontab (`will`) trying to unlink Docker-created `root:root` media after Drive transfer. The crontab runs the script as `will` (no sudo wrapper); the script transfers with `rclone copy` and deletes locally only through `sudo -n rm` after `rclone check --size-only --one-way` confirms the remote copy. Fake-rclone tests cover verified delete and mismatch-keeps-source. See the 2026-10-01 section for the `check`-form bug that had disabled that purge.
- **Telegram delivery:** Bot API fan-out persists completed operations per Telegram chat and per worker target (`telegramCompletedByDelivery` + `_telegramDeliveryKey`), so retry doesn't resend successful chats or skip later targets. Ambiguous transport failures are marked non-retryable to avoid duplicate posts; HTTP 429 remains retryable. Worker persists its recovery state on exceptions. Shutdown now tracks/settles the maintenance task and drains already-claimed targets. Follow-up: add dedicated Telethon fake-client partial-delivery tests; inspect live logs after deployment before deciding on historic retry.
- **Media recovery:** required restores validate a non-empty file and use atomic sibling-temp replacement; generated remote-upload artifacts can restore only from validated public HTTPS (redirect hosts checked at each step); localhost-only generated artifacts deliberately fail safely. Older generated local artifacts with no preparation manifest cannot be reconstructed by substituting the original source. Direct `uploads/` and `videoFile/` refs normalize correctly.
- **Chinese copy:** active DB had 2,882 scheduled targets; no verifiable Simplified copy needed conversion. Five OpenCC variant-only changes were restored from audited originals. Backups and restricted rclone config are local data; keep them out of commits and Docker images.
- **Architecture decision:** no evidence justifies Go/Rust rewrite. Likely lag sources are SQLite write contention, API/worker process coupling, platform/browser I/O, and media prep/restore. Runtime still has ~87 raw sqlite3 connect sites; `sau_app/db` SQLAlchemy repositories are partial. PostgreSQL is a measured follow-up: benchmark queue/API/lock timings, converge Alembic/schema bootstrap, migrate repositories incrementally, dual-verify a copied database, then stage/canary with rollback; do not switch production by connection string alone.
- **Validation:** backend reliability suite 162 tests pass; frontend Vitest 16 tests pass (exclude Playwright demo from Vitest); frontend production build, Python/shell syntax, `git diff --check` pass. Local ARM64 image built and Python modules compiled. Multiarch QEMU amd64 build crashed inside esbuild with `lfstack.push invalid packing`; native GitHub Actions build succeeded.
- **Image publish:** workflow `https://github.com/Willywang8216/social-auto-upload/actions/runs/36669965558` completed successfully for current commit `65329a8728009381c41304693038def7ef7547a3`. `ghcr.io/willywang8216/social-auto-upload:latest` OCI index digest: `sha256:54a9876263c42190c9ffc0fe6ae588e02af203e56cd714477b13f45e86cf8930`; manifests include linux/amd64 and linux/arm64. `.dockerignore` excludes `.env`, secrets, databases/backups, rclone config, cookies, media, logs, local backups and agent/playwright state.
- **Current schedule loading milestone:** `/publish-entities` now selects bounded job candidates with SQL workspace/profile/month/date/platform/account predicates before entity hydration; entity-level status/keyword semantics remain as a correctness-preserving post-filter. The route logs sanitized candidate/result/page/filter duration metrics. This is a query-pushdown mitigation, not a PostgreSQL cutover; measure timing and SQLite lock/busy rates before migrating.
- **Current queue/calendar UX milestone:** queue cards now show up to three lazy safe media previews plus overflow count; calendar has debounced profile/platform/account/keyword filters, target-level events, compact summaries/read-more day list, and no forced 900px grid width. Axios distinguishes timeout/cancel/network failures, and calendar aborts superseded requests.
- **Final date-filter wiring check:** entity-list SQL candidate selection now receives month, date, date range, platform and account filters; exact rollup status/keyword filtering remains after hydration. Publish-entity API tests pass after this correction.

## Agent handoff：排程效能、媒體可見度、行事曆（2026-10-01）

- **Offload rc=1 已修好（實作缺陷，非權限本身）。** `purge_verified_sources` 用 `rclone check <local_file> <remote_file>`；rclone 不接受**檔案**當目的地，直接回 `is a file not a directory`，所以「驗證後再 sudo rm」那條路徑從上線起就是死碼，每個 30 分鐘週期都只剩 rclone 自己的刪除失敗 → rc=1。改成目錄對目錄 `check "$source_root" "$remote_root" --size-only --one-way --include "/$relative"`（已對 5 個滯留檔逐一驗證 rc=0）。同時把搬移從 `rclone move` 換成 `rclone copy`：cron 以 `will` 身分跑，但 SAU 容器以 root 掛載 `videoFile/`，move 必然在刪除階段失敗；copy 不刪來源，刪除只走驗證過的 purge。purge 另改為**單一檔案失敗不再中止整個清理**（先前一個卡住的檔案會擋掉其他所有檔案）。首輪執行實測：5 個根擁有檔案清掉、`rc=0`、`local videoFile` 324→319，且 5 筆在 `file_records` 都已有 `storage_key`+backend 416（可被 worker 抓回）。
- **排程很慢的真正原因（結論：不需要 PostgreSQL）。** 資料量很小（3,817 jobs、2,366 campaigns、payload 平均 1.1 KB、DB 33 MB），引擎不是瓶頸，存取模式才是。`/publish-entities` 的每個 builder 都「一行一次 sqlite 連線」，實測 500 候選的分解：N+1 jobs 535 ms、N+1 campaigns 544 ms、N+1 targets 199 ms、**2,102 次帳號查詢（僅 21 個不同帳號）2,199 ms**，再加每 campaign 的 posts/artifacts 查詢。修法是在 `_EntityLoadCache`（`sau_backend.py`）先批次預載再讓 builder 讀記憶體：新增 `jobs.get_jobs_by_ids`/`list_targets_for_jobs`、`campaigns.get_campaigns_by_ids`/`list_posts_for_campaigns`/`list_artifacts_for_campaigns`、`profiles.get_accounts_by_ids`/`get_profiles_by_ids`、`media_groups.get_media_groups_by_ids`，並批次化 media-group 檔案查詢。**實測 500 個 entity 由約 4.5 秒降到 230–470 ms**（另經真實 HTTP：baseline 357 ms）。
- **`_entity_account_ref_payload` 必須複製字典。** 快取對每個帳號共用一份 payload，而該函式會加 `ref` 鍵；不複製就會把 `ref` 汙染進其他貼文的 accounts 陣列（已用實際資料斷言 1,602 筆貼文帳號 0 外洩）。
- **SQLite 併發：只加 `busy_timeout=15000`，刻意不開 WAL。** UI 長讀取與 worker 寫入會互卡；原本預設 5 秒逾時會直接失敗。**不要開 `journal_mode=WAL`**：`/home/will/backup-apps.sh` 與 repo 內備份腳本是用一般檔案複製 `db/database.db`，WAL 內容在 `-wal` 側檔，會讓備份靜默缺資料（`db/*.bak` 檔名裡的 "copy-correction" 就是前一次這類事故）。已在 `myUtils/jobs.py`、`campaigns.py`、`profiles.py`、`media_groups.py` 的 `_connect` 註明。
- **Migration 0021 已套用到實測 DB**（先前僅到 0020，新複合索引根本沒生效）：`alembic upgrade head`，套用前用 sqlite online backup API 備份為 `db/database.db.before-0021-indexes-*.bak`。
- **「網路連線失敗」的判讀：** `sau_frontend/src/utils/request.js` 只有在「完全沒有 response」時才顯示此訊息（逾時是「請求逾時」）。所以那是「伺服器沒回」，不等於慢查詢；慢查詢已修，若再出現請往容器重啟（Watchtower 自動更新）或 proxy 方向查。
- **佇列媒體可見度（使用者抱怨的核心）。** 實測 500 筆 mediaItems 中 **495 筆 `availableLocally=false`**（檔案已被 offload 到 Drive），但後端仍照舊發出 `previewUrl` → 前端對不存在的檔案發 `/getFile`，縮圖全破。`_entity_media_item` 現在額外回傳 `availableLocally`、`publicUrl`（file_records.storage_cdn_url）、`archive`（`{archived, provider, label:"Google Drive", openUrl}`，Drive 以檔名搜尋連結呈現；**遠端路徑仍不外洩**，維持原有不洩漏儲存佈局的慣例）。前端改用 `src/utils/mediaState.js`（`mediaStateLabel`/`mediaPreviewSource`，11 個 Vitest 覆蓋）決定是否真的渲染預覽，否則顯示「已封存至 Google Drive」與開啟連結。
- **行事曆寬度與 read-more。** `.cal-ev .cl` 先前漏了 `white-space: nowrap`，摘要會折行把日格撐高；現在單行 ellipsis，摘要長度 46→34（`EVENT_SUMMARY_LENGTH`），超過長度才出現「更多」按鈕（`@click.stop`）開既有詳情對話框。順手修掉一行壞掉的正則：`replace(/\\s+/g, ' ')`（兩個反斜線，實際比對的是字面 `\s`，從未壓縮空白）→ `/\s+/g`。
- **行事曆篩選（第 4 項需求）本來就已完成**，本次只做驗證：`profileIds`/`platforms`/`accountIds`/`q` 由 route 解析後再於 Python 端過濾，實測會正確縮小結果（baseline 500 → profile 246、account 22、platform 278、keyword 139），分頁 `hasMore`/`offset` 正常。未新增篩選欄位。
- **驗證足跡：** 後端 venv 全量與子集測試（274 passed；2 個 Meta token-refresh 失敗已用還原成 HEAD 的模組證明為既有環境問題）；前端 `vitest run src/` 27 passed、`vite build` 乾淨。**未做瀏覽器實測**：本機沒有任何 Playwright 瀏覽器（需下載約 150 MB），因此新 UI 只經過建置、單元測試與舊 payload 的向後相容推理，尚未眼見為憑。
- **補充：** entity 讀取路徑的 5 個直連 `sqlite3.connect` 也統一走 `_entity_read_conn`（`timeout=15` + `busy_timeout`），與 store 模組一致；行事曆篩選列新增「清除篩選（n）」按鈕（僅在有篩選時出現）。`tests/test_offload_script.py` 補了兩個回歸斷言：`check` 的目的地必須是傳輸根目錄（不是檔案，這正是原本失效的原因）、以及**單一檔案驗證失敗不得阻擋其他檔案的清理**，共 3 個測試。
- **待辦（本次未做，需使用者決定）：** ①`/publish-entities` 回傳偏大（limit=50 → 1.1 MB、limit=200 → 3.4 MB，約 22 KB/entity，行事曆每次換月或改篩選都會下載 200 筆）。主因是 `destinations[].copy` 重複了 `posts[].draft`。移除屬 API 契約變更（手機端可能在用，repo 內沒有測試釘住它），故未動；若要瘦身，建議改為「只在 `posts` 回一份 draft」或替行事曆加一個精簡回傳模式。②前端 `sau_frontend/dist` 沒有 bind mount，後端與前端都要等映像重建（push main → Actions → Watchtower）才會生效。③本機沒有 Playwright 瀏覽器（需下載約 150 MB），UI 尚未做瀏覽器實測。
- **回應瘦身（依量測）：** entity payload 裡的 `destinations` 佔整包 **40%** 且是 `posts[].draft` 與 `jobs[].targets[]` 的第二份複本 —— repo 內唯一呼叫 `/publish-entities` 的是 `sau_frontend/src/api/jobs.js`（詳細頁另抓 `/publish-entities/<id>`），前端與 `myUtils/publish_digest.py` 都沒有讀它（digest 只在 docstring 提到這個字），也沒有測試釘住，故直接移除並在原處留註解。實測 limit=50 1,114,885 → **662,679 bytes**、limit=200 3,361,977 → **1,939,094 bytes**；limit=200 的伺服器時間 1,114 → **516 ms**（順帶移除那段 O(posts × targets) 的推導）。**若日後要恢復，請用旗標開啟，不要讓每次列表請求都付這筆錢。**
- **行事曆篩選狀態改放 URL（依研究：filter 狀態屬於 query string，可分享／重新整理不丟／上一頁可還原）。** 新增 `sau_frontend/src/utils/calendarFilters.js`（`parseCalendarFilters`／`applyCalendarFiltersToQuery`／`sameCalendarFilterQuery`，11 個 Vitest）。關鍵規則：寫入必須 **merge** 進現有 query，否則會把 `?entity=` 與其他篩選一起刪掉（該檔註解已寫明）；篩選用 `router.replace`（refinement 不產生歷史），並以 no-op 比對切斷 state→URL→state 的迴圈。未做：月份沒有進 URL（留給後續，會與 `?entity=` 深連結流程有互動，需要另外設計）。
- **UI 已在瀏覽器實測（先前「未做瀏覽器實測」的保留已解除）。** 用 `npx playwright install chromium`（裝到 `~/.cache/ms-playwright`）＋ `/tmp/ui-verify.cjs`：對**正式建置**（`vite build --outDir /tmp/sau-prod` + `serve -s` 於 4173）注入 mock API 跑 21 項檢查，**21/21 通過**並留下截圖。涵蓋：佇列卡片（封存媒體顯示「影片 + 檔名 + Google Drive」連結、`openUrl` 確為 `drive.google.com/drive/search`、本機檔案渲染真縮圖、已封存者不再渲染壞掉的 `<video>`）、抽屜顯示「已封存至 Google Drive」、行事曆事件、只有長標題才有「更多」、`.cl` 為 `nowrap`+`ellipsis`、日格無水平溢出、「更多」開啟對話框並顯示文案與媒體狀態、URL 篩選還原（請求帶 `profileIds=1&q=launch`）、清除鈕、載入篩選 URL 後 3 秒內**無請求迴圈**、無 console error。**未觸及正式後端或發佈佇列**（API 全部由瀏覽器攔截 mock）。
- **注意（dev-only 陷阱）：** vite **dev server**（5173，未打包）下，造訪 `/publish/queue` 後再導覽到其他 `/publish/*` 路由，`<router-view>` 會渲染成空註解、內容區一片空白（且無 console error）。**正式建置完全正常**（`compose→queue→compose`、`queue→calendar` 皆正常）。所以這是 dev server 現象、不是使用者會遇到的 bug；日後在 dev 下驗證這幾頁請用整頁重載，或直接測正式建置（本檔的 UI 驗證就是這樣做的）。
- **「rc=1 又發生了」其實是舊告警延遲送達（已查證，非新故障）。** 2026-10-01 07:38 又收到同一個 `SAU->Drive offload FAILED (rc=1) on Main-WP-Oracle424-U`，但主機端 `logs/offload.log` 自修好後 **每一輪都是 rc=0**（連 9 輪），`system_health.collect()` 也回 `exitCode=0, healthy=true`。逐項排除後確認不可能有新故障：①全機只有 `offload_to_drive.sh` 會輸出這串字；②`tools/system_status.py` 是手動 CLI、沒有任何 cron/systemd timer 在跑；③容器內雖然有 `/app/offload_to_drive.sh`（映像內的舊版，Sep 29），但容器 hostname 是 `e0b537aa980e`（不是 `Main-WP-Oracle424-U`），且容器看不到 `/home/will/mailserver/monitor/.telegram_env`，不可能送出這封告警。→ 結論：那是 **21:04Z 之前那次真實故障的延遲／重複投遞**。
- **告警改成可自我辨識（根治上述混淆）。** 原本的失敗訊息不含時間、且永遠不會說「已恢復」，所以一則延遲送達的舊 FAILED 看起來跟現行故障一模一樣。現在 `notify()`（`offload_to_drive.sh`）：失敗訊息帶 **UTC 時間戳**；並在「上一輪非 0 → 本輪 0」時送**一次** RECOVERED（由 `offload_previous_rc()` 從 log 倒數第二行推得，不需新增狀態檔；健康→健康保持靜默，不會變成 heartbeat）。`tests/test_offload_script.py` 現有 8 個測試，涵蓋失敗必帶時間戳、恢復只送一次、無前一輪資料時靜默、無 Telegram env 時 no-op、以及 `previous_rc` 讀的是倒數第二行。**若日後又收到本機 FAILED 告警，先看訊息裡的時間戳再決定要不要追。**

## Agent handoff：月份進 URL、憑證外洩修補、既有紅燈測試（2026-10-01 第二輪）

- **行事曆月份也進 URL。** `sau_frontend/src/utils/calendarFilters.js` 的 `parseCalendarFilters`/`applyCalendarFiltersToQuery` 現在一併處理 `month`（格式 `YYYY-MM`，不合法就忽略）；`applyCalendarFiltersToQuery` 只在月份**不等於當月**時才寫入（預設值不進 URL，URL 保持乾淨），並用 `currentMonthKey()` 判斷。`CalendarView.vue`：`?month=` 深連結直接開該月、`monthKey` computed 驅動「換月 → 同步 URL → 重新載入」、`route.query` watcher 也套用月份（缺月份＝當月）。瀏覽器實測 8/8：深連結開對月份、請求帶 `month=2026-09`、按上一月 URL 變 `2026-08` 並重抓、按「今天」把月份從 URL 移除、3 秒內 0 次多餘請求（無迴圈）、無 console error。
- **修掉一個憑證外洩（順著紅燈測試找到的）。** `/accounts/<id>/refresh-token` 及其他 56 處都以 `str(exc)` 當 `msg` 回傳，而 HTTP client 的例外會帶上**完整請求 URL** —— Meta token URL 內含 `client_secret`，所以一次失敗的 refresh 等於把自家 app secret 直接回給呼叫方（實測原始回應：`...&client_secret=48b4…&fb_exchange_token=…`）。修法是在 `sau_backend.py` 加一個 **`@app.after_request` 集中遮蔽**：只處理 `status >= 400`、只改 `msg` 欄位、用專案既有的 `_redact_entity_error`（它的正則本來就抓 `token=|secret=|password=|api_key=|authorization=` 與 `Bearer …`），成功的大 payload 完全不會被碰或重掃。同一條真實路徑複測已變成 `client_secret=[redacted]&fb_exchange_token=[redacted]`，且無害的 `grant_type=fb_exchange_token` 仍可讀。回歸測試 `test_refresh_error_body_does_not_leak_the_app_secret` 就放在 `tests/test_campaigns_http.py`。
- **那兩個紅燈測試的真正原因是「測試不隔離」，不是產品壞掉。** `test_refresh_facebook/instagram_token_resyncs_*` 只 mock 了 `fetch_managed_pages`，但路徑上 `meta_auth.debug_token_info()` 是**真的打 Graph API**：在有網路的機器上 Facebook 回報假 token 無效 → 程式決定輪替 → 真的發出 exchange 呼叫 → 400。它們只是在「無網路」的環境下才會過。修法是把 `debug_token_info`（`{'is_valid': True, 'expires_at': 0}`）與 `fetch_managed_pages` 一起 pin 住，與同檔 1080/1107 行既有慣例一致。
- **`test_llm_client` 的紅燈同理：本機設了 pool。** client 依設計**優先** `SAU_LLM_POOL` 而 `patch.dict` 只設了舊的 `SAU_LLM_API_BASE_URL`，所以請求跑到 pool 的端點、URL 斷言當然失敗。新增 `_llm_env()` helper 明確把 `SAU_LLM_POOL` 設空，兩個測試都改用它（`transcribe_audio` 那個雖然目前會過，但同樣脆弱）。
- **這一輪的驗證：** 後端全量測試、前端 `vitest run src/` **43 passed**（calendarFilters 16、mediaState 11）、`vite build` 乾淨、正式建置瀏覽器檢查 **21/21 + 月份 8/8**（另有 13 路由回歸掃描全 OK）。
