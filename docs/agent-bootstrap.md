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

## Agent handoff：部署完成（2026-10-01）

- **已 commit 並 push 到 `main`：** `fd53811`（32 files, +3037/−453）。CI（`ci.yml`）success、App image（`image.yml`）success，新 image digest `b5192f147b5d`。
- **部署方式與一個重要陷阱：** 主機常駐一個 1Panel 的 `Watchtower` 容器（`--interval 3600 --cleanup --label-enable`，憑證在 `/opt/1panel/apps/watchtower/watchtower/data/config.json`，以 `DOCKER_CONFIG=/config` 掛進 `/config/config.json`）。**這個版本重啟時不會立刻檢查**，只會把下一次排到一小時後（log 會說 "the first check will be performed in 59 minutes"）——所以 `docker restart Watchtower` 不能用來催更新。要立即套用，用同一顆憑證跑 one-shot：
  `sudo docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v /opt/1panel/apps/watchtower/watchtower/data/config.json:/config/config.json:ro -e DOCKER_CONFIG=/config containrrr/watchtower --run-once --cleanup social-auto-upload`
  （不掛那顆 config 會 `unauthorized`，因為 GHCR 套件需要認證。）
- **實測已上線：** 容器 `status=running health=healthy`、label `org.opencontainers.image.revision=fd53811`；`/healthz` 200；線上 `/publish-entities?limit=2` 回 200 且**已無 `destinations`**、media item 帶 `availableLocally` 與 `archive`（含 `drive.google.com/drive/search` 連結）；容器內 `sau_frontend/dist/assets` 已含 `mediaState-*.js`（`已封存至`／`availableLocally`）與 `CalendarView-*.js`（`清除篩選`），證實前後端都是新版。
- **未提交（刻意）：** `rclone-cache.conf`（內含 Drive token）與四個 `db/database.db.before-*.bak` 備份仍留在工作區未追蹤；本 commit 也順帶把 `.dockerignore` 擴大排除 `.env*`、`secrets`、媒體、`logs`、`*.db*`、`*.bak`、`.claude`、`.playwright-mcp`。

## Agent handoff：/getFile 子目錄修復（2026-10-01 第三輪）

- **`/getFile` 只服務 videoFile 根目錄的檔案（嚴重、且是既有的）。** `sau_backend.py` 的 `get_file()` 先把路徑正確 resolve 並驗證在 `videoFile/` 之內，最後卻呼叫 `send_from_directory(str(base_dir), target.name)` —— **只傳 basename**。於是任何放在子目錄的媒體（`_library/`、`_photos/`、`_batch*/`、`_inbox_cache/`）一律 404。實際影響：`file_records` 483 筆中 **482 筆在子目錄**（只有 1 筆在根目錄），其中 **313 筆當下就在磁碟上**卻完全無法預覽。這才是「佇列看不到素材」的主因之一——先前修的 offload 只是「檔案不在本機 → 顯示去了 Drive」那一半。此行為來自舊 commit `51c0d95`，不是本次工作造成。
- **修法：** 改傳 `str(target.relative_to(base_dir))`（上方的 containment 檢查已保證安全）。新增 `tests/test_sau_backend.py::GetFileServingTests` 5 個測試（巢狀／深層／根目錄／traversal 仍拒絕／缺檔仍 404）；已用「暫時還原舊行」驗證測試真的會抓到（2 個紅、還原後 5 個綠）。commit `5004c11`。
- **教訓（為什麼之前所有檢查都沒看到）：** entity payload 正確、mock API 的 UI 測試全過、單元測試全過 —— 只有**用瀏覽器打開「已部署的正式站 + 真實資料」**才會打到真正的檔案服務路徑。之後驗證這類「畫面壞掉」問題，請愛用 `live-verify.cjs` 這種對正式站跑真實資料的方式（本次就是這樣才發現）。正式站修復後：同樣那 7 張圖從全 404 變成 **0 個 4xx**，live 檢查 10/10 通過。
- **部署紀錄：** commit `fd53811` → image `b5192f147b5d`；commit `5004c11` → image `122ceaa21682`。兩次都以 one-shot Watchtower（需掛憑證 config，見上一節）套用，套用前都先確認 `publish_job_targets` 沒有 running/retrying，容器 `health=healthy` 且 revision label 正確。

## Agent handoff：sau-inbox 檔案、每日 TG digest（2026-10-01 第四輪）

- **`sau-inbox/` 的檔案各是什麼（有人一度以為是殘留垃圾，先查再刪）：**
  - `.sau-inbox-scan-*`：`watch.py` 每次執行的 `tempfile.mkdtemp`（watch.py:287），watch.py 也會清掉遺留的（:284）→ **短暫、自我清理**，不要手動刪（可能正在掃描中）。
  - `state.lock` / `.watch-run.lock`：**flock 檔**（0 bytes 是設計）（watch.py:144 / `_state_lock`）→ **絕對不要刪**。刪掉正在被持有的鎖檔會讓下一個 run 在**新的 inode** 上取得「同一把」鎖 → 兩個 watcher 同時跑。
  - `watch.log`：由 **`/etc/cron.d/sau-inbox-watch`（每分鐘一次）** `watch.py --once` 追加 → 約 1.4 MB/天，原本無輪替。已加 logrotate stanza（`size 2M`、rotate 3、copytruncate）到 `/home/will/.config/logrotate/logrotate.conf`，並把現有檔案由 2.6 MB 裁到 28 KB。
  - `sync.log`：來自 **`/etc/cron.d/sau-inbox-sync.disabled-20260926`**（cron 會忽略檔名含 `.` 的檔案 → 早已停用）→ 已刪。
  - `~/sau_debug.sh`：368 bytes 的 **NUL 垃圾**、無人引用 → 已刪。
  - 其餘（`both/ msl/ nw/ sw/ teaching/ _pub/ tests/`、`watch.py`、`thumb.py`、`gen_title.py`、`manual_platforms.py`、`state.json`）都是工具本體，別動。
- **每日 TG digest 以前根本不會送達（三個獨立原因，都已修）：**
  1. **沒有任何排程。** 模組刻意不自帶 scheduler。已加入 crontab（`CRON_TZ=Asia/Taipei`）：`0 9 * * * sudo docker exec -w /app social-auto-upload python3 -m myUtils.publish_digest`（修改前已備份 crontab，42→43 行）。
  2. **UI 連結是關掉的。** digest 與 worker 警報只讀 `SAU_PUBLIC_APP_URL`（未設），而本機的來源存在 `SAU_PUBLIC_BASE_URL`。新增 `ops_alerts.public_app_origin()`（`SAU_PUBLIC_APP_URL`／CLI `--app-url` 優先，回退 `SAU_PUBLIC_BASE_URL`，永不猜網域）。修好後連結長這樣：`link: https://socialupload.iamwillywang.com/#/publish/queue?entity=mg-1863`。
  3. **超過 Telegram 4096 字上限 → 被靜默丟棄。** digest 平常 5,586 字（每個排程目標一行）；`sendMessage` 回 400，而 alerting 是 best-effort，只留一行 log，digest 回報 `sender returned false`。`ops_alerts._split_message()` / `_telegram_payloads()` 現在按行切塊（單行過長才硬切）、在 HTML escape **之前**切（避免切斷 entity）、並加上 `[i/n]` 編號。**這三個都要靠「真的送一則」才會發現，`--dry-run` 完全看不出來。**
- **教訓：** alerting「best-effort、失敗只記 log」的設計，讓這條通道可以無聲地死掉好幾個月。日後動 alert 相關程式，請用真實通道各送一次短訊息與長訊息驗證。

## Agent handoff：佇列 UX、審核卡帶圖、文案×媒體檢查、以及一次自行造成的停機（2026-10-01 第五輪）

- **`cf2ed43` 修掉三個使用者回報的佇列問題：** ①「一直閃、難讀」→ 30 秒輪詢原本呼叫 `loadEntities(true)`，會先清空 `entities` 再重抓，整個網格每半分鐘消失重繪；改成 `refreshEntitiesQuietly()` 原地更新（共用 `entityQuery()`、不低於現有卡片數、失敗靜默、分頁暫停）。②「有些圖出不來」→ 能顯示的 7 個全是 image，10 個 video 都已 offload 到 Drive、只顯示裸標籤而看起來像壞掉；改成明確的 **已封存** badge。③「有人 2 個開啟媒體、有人 0 個」→ 卡片每個 artifact 各一個同字串連結（那兩個 URL 其實指向同一檔案）；新增 `artifactLinks()` 依 URL 去重，多筆時以 role 標示（video 1 / thumbnail 2）。瀏覽器實測 9/9。
- **`deec6f2` 審核卡帶圖 + 文案×媒體檢查：** ①`_send_media()` 在本機檔案不存在時直接回 None，而 offloader 會刪掉本機檔（現在是大多數），所以卡片幾乎都是純文字；新增 `_media_by_url()`，local 檔案不在時改用 payload 的 `public_url` 讓 Telegram 自己去抓（sendVideo/sendPhoto，僅 https）。②新增 `myUtils/media_copy_check.py`：用設定的 vision 模型只問一個窄問題「文案是否**明顯**與媒體矛盾」（不同主體/場景、媒體明確否定的具體主張、描述成另一種媒體），**不是**矛盾的一律放行（語氣、hashtag、emoji、模糊、單純沒提到都不算）；接在 `api_campaign_generate` 與 `api_campaign_validate`，把結果 append 進既有 `errors`，因此自動沿用既有的 `status="needs_review"` 機制而不是新狀態。**advisory by construction**：任何失敗（無媒體/無文案/模型不可達/輸出不可解析/被關掉）都回 `checked=False`，發布照舊。每次發文花一次呼叫，故有 `SAU_COPY_MEDIA_CHECK=0` 可關。
- **⚠️ 我造成的生產停機（16:53–17:04 Taipei，約 8 分鐘，0 筆發布遺失）：** `deec6f2` 內含 `f"...{base64.b64encode(...).decode("ascii")}"` —— f-string 內用同款引號在 **3.12 合法（PEP 701）、在 3.10 是 SyntaxError**，而映像跑的是 3.10、本機 venv 是 3.12，所以本機測試與映像建置都過，一部署就 crash loop（18 次重啟、exit 3）。處置：從容器自身 traceback 定位 → `docker stop` + `docker cp` 修好的檔案進容器 + `docker start` 立即恢復 → 再推真正的修復。**`3ab7c10` 修掉該行並在 Dockerfile 的 `COPY . .` 後加上 `RUN python3 -m compileall`（用映像自己的直譯器），任何語法錯誤從此在 build 就失敗**（已驗證該行在 3.10 會被擋、現有樹可通過）。**教訓：改 Python 後要用「容器／映像的直譯器」驗證，不能只驗本機 venv。** build 成功不再等於能啟動。
- **觀察到的營運問題（未處理，屬使用者決策）：** ①**LLM pool 6 個端點只有 1 個活著**（`ooioo.work`／gpt-5.6-terra；`wzw.pp.ua`、`muyuan.do` 與三個 `hf.space` 回 503/500）。②**`~/.cache` 有東西在清**——Playwright 瀏覽器（chromium-1228）在 session 中途整個消失，得重新下載。③需要重新授權的帳號：Twitter `光光`(id 103，2026-09-11 到期後未再自動更新)、YouTube `MSL`(id 108，2026-07-16 到期，系統 2026-09-20 已告警)；Threads 三個帳號 2026-11-23 到期（60 天長效，尚無需更新，但建議確認 maintenance 迴圈有涵蓋 threads）。其餘平台（FB/IG/Reddit/TikTok/Twitter 大部分/YT 其一）今日都有 `lastAutoRefreshAt`，自動更新正常；bluesky/telegram/兩個 blog 無 token 機制。
- **水印的正確心智模型（容易搞錯，我第一輪就搞錯）：** 發布路徑讀的是 **`profile.settings.watermark`**（四個 profile 都有：NW `Nakedwill.com | @nakedwill`(moving)、SW `Sexualwill.com | @Sexualwill`、Teaching `威威教育 @weiwei_wang0`、MSL `Money Systems Lab`），由 `_derive_watermark_spec()` 解析、對 image 與 video 都套用。**`watermark_configs` 這張表與其 CRUD API 完全沒有被發布路徑使用**——那 7 筆是殘留（2 筆指向已不存在的 profile 8/9，5 筆是空的 test 列），已刪除；同時刪掉停用的 reddit 殼帳號 id 126（0 筆 job/campaign/analytics 引用，4 筆 account_events 以 ON DELETE SET NULL 保留歷史）。刪前已用 sqlite online backup 備份到 `db/database.db.before-cleanup-*.bak`。另注意：**只發 TikTok 時不套水印**（`if watermark_spec and not tiktok_only`）。

## Agent handoff：Token 為何一週就過期、LLM pool 清理、log 輪替（2026-10-01 第六輪）

- **「重新授權後一週又過期」是兩個不同原因，都不是本專案的 bug（程式碼是對的）：**
  - **Twitter `光光`(id 103)：它是 cookie 帳號。** `worker._is_account_stale()` 明確跳過 cookie 型 Twitter（`# cookie-based Twitter is not refreshable via API`），所以它永遠不會自動更新，cookie 一過期就得重連。**解法：改用 OAuth 路徑重連該帳號**（同機的 `sexualwill`(id 77) 是 `twitterAuthType: api`，今天 17:50 還在自動更新，證明 OAuth 路徑正常）。
  - **YouTube `MSL`(id 108)：Google 直接回 `invalid_grant`**（refresh token 本身失效）。程式碼沒問題——`youtube_auth.build_authorize_url()` 同時帶了 `access_type=offline` 與 `prompt=consent`，所以重授權確實拿得到 refresh token。證據指向 **Google OAuth consent screen 處於 “Testing” 發佈狀態：Google 會讓 refresh token 7 天後失效**——108 於 2026-07-09 連接、最後一次成功更新 **2026-07-16（正好 7 天）**，而另一個 YouTube 帳號(id 110) 在 2026-09-24 重授權、今天仍在運作（也剛好 7 天，可能接著出事）。**解法在 Google Cloud Console（我們無法代做）：APIs & Services → OAuth consent screen → 若 Publishing status 是 Testing，按 PUBLISH APP**；上線後 refresh token 可存活到被撤銷或閒置 6 個月。YouTube 上傳是 sensitive scope，發佈可能需要驗證，但未驗證而已發佈的 app 仍可用（會顯示警告）且不再 7 天過期。系統本身有正確告警（108 的 `_reconnectAlertedAt=2026-09-20`）。
- **Threads 不需要處理（先前懷疑是多餘的）：** threads 同時在 `worker._REFRESHABLE_PLATFORMS` 與 `_LONG_LIVED_TOKEN_PLATFORMS`（用較寬裕的 buffer），2026-11-23 到期前會被自動更新。
- **LLM pool 已清理：6 → 2。** 逐個直接打 `/chat/completions` 分類（**沒有半個是 429/限流**）：三個 `2c2ch1u11-share-api-0.hf.space` 回 **404**「The requested endpoint '/chat/completions' does not exist」——那台根本不是 OpenAI 相容 API；`muyuan.do` 回 **403** Cloudflare「Just a moment…」挑戰頁。四個都屬於「掛掉/設定錯」故移除；保留 `ooioo.work`(gpt-5.6-terra) 與 `wzw.pp.ua`(deepseek-v4-flash)，兩者實測 200。（`.env` 改前已備份；容器 recreate 後已確認 pool=2。）注意 **vision 能力只有 `ooioo.work` 驗證過**。
- **Log 輪替（只做缺口，未搬動路徑）：** 兩份 logrotate 設定都加了 **`maxsize 5M`**（原本只有 time-based，才會發生 watch.log 兩天 2.6 MB）；新增 `/home/will/iamwillywang-mail/cron.log` 的 stanza（每分鐘寫、原本完全沒輪替）；`social-auto-upload/logs/jobs/`（788 個 write-once 的 per-job log、3.7 MB）改用 **cron 保留刪除**（`13 4 * * * find … -mtime +14 -delete`）而非輪替；compose 加 `logging: json-file max-size=10m max-file=3`（commit `5466649`，實測已生效）。這些主機設定**不在 repo 內**：`/home/will/.config/logrotate/{logrotate.conf,mailserver-sau.conf}`、crontab、`.env`。
- **仍待使用者處理：** ①Google consent screen 發佈（YouTube）。②Twitter 光光 改用 OAuth 重連。③`~/.cache` 有東西在清（Playwright 瀏覽器曾整個消失）。

## Agent handoff：為什麼 `~/.cache` 會被清空（2026-10-01 第七輪）

- **root crontab 有一個每週日 03:00 的清理工作**（`0 3 * * 0`），其中一段就是 **`rm -rf /home/will/.cache/*`**，所以 `~/.cache/ms-playwright`（用 `npx playwright install chromium` 下載、約 110–150 MB）**每個星期日都會被刪掉**，下一個 session 得重新下載。同一行還會 `rm -f ~/.bash_history`、`rm -rf ~/.cc-switch/logs/*`、`find /var/lib/docker/containers -name "*-json.log" -truncate -s 0`（這也是 `docker logs` 在本機不可用的原因之一，另一個是沒有 size cap，已於 `5466649` 補上）、`docker builder prune -af`、`docker image prune -f`、`apt-get autoremove --purge -y`。
  - **因應：** 若不想每週重抓瀏覽器，把 `PLAYWRIGHT_BROWSERS_PATH` 指到 `~/.cache` 之外（例如 `~/.local/share/ms-playwright`）再執行 `npx playwright install chromium`。
  - **注意：** 今天（週四）瀏覽器是在 session 中途消失的，**不是**這個週日排程造成的；同機可能還有其他 Claude session／手動清理跑過同一段指令。總之這個排程保證每週會清掉一次。
- **`logrotate -f /home/will/logrotate-will.conf` 是在跑一個不存在的檔案**（同一行工作內，錯誤被 `2>/dev/null` 吞掉）——即「強制輪替」其實從來沒發生，也沒有和 `5466649` 的輪替設定衝突。順帶說明：本機共有**三份** logrotate 相關設定在運作：`~/.config/logrotate/logrotate.conf`（每日，`/home/will/logs/*.log` + watch.log + mail cron.log）、`~/.config/logrotate/mailserver-sau.conf`（每週，monitor.log + `social-auto-upload/logs/*.log`），以及這個不存在的檔案。

## Agent handoff：`~/.cache` 清理、失效的 logrotate 參照、Twitter 一週過期的真正機制（2026-10-01 第八輪）

- **已修：root crontab 的週日 03:00 清理不再刪掉 Playwright 瀏覽器。** 原本是 `rm -rf /home/will/.cache/*`，改成 `find /home/will/.cache -mindepth 1 -maxdepth 1 ! -name ms-playwright -exec rm -rf {} +`（已用 dry-run 確認只會刪 `claude`/`claude-cli-nodejs`/`fontconfig`，不會刪 `ms-playwright`）。同一次編輯也移除了同一行裡 **`logrotate -f /home/will/logrotate-will.conf`**（該檔不存在，錯誤被 `2>/dev/null` 吞掉，從來沒有真的輪替過；真正的輪替在 will 的 crontab `7 3`/`13 3` 兩條）。root crontab 只改了那一行（42→42 行，diff 確認其餘 byte 相同），改前備份於 `/tmp/rootcron.bak.*`。瀏覽器已重新安裝。
- **Twitter「重授權後一週又過期」的真正機制（程式碼已驗證）：** 全 repo **只有一處**會寫 `twitterAuthType`——`sau_backend.py:4881` 的 **Twitter OAuth callback**，它寫入 `'api'` 並把 `auth_type='oauth'`。而自動更新路徑**要求必須是 `'api'`**（`prepared_publishers.py:2393`、`worker.py:455`），`worker._is_account_stale()` 也明確跳過 cookie 型。`/accounts/<id>/refresh-token` 對 cookie 帳號直接回 400「Twitter cookie accounts do not support token refresh; use OAuth 2.0 API mode instead」。→ **所以只要某個 Twitter 帳號仍是 `twitterAuthType: 'cookie'`，代表它從來沒走完那條 OAuth callback**（本次的 id 103 就是），無論重連幾次都不會自動更新、必定每週過期。**解法：用 Twitter OAuth 流程重新連接該帳號**（不要走 cookie 匯入），連完就會變 `'api'` 並開始自動更新（同機 id 77 就是這樣，今日仍在更新）。
- **未代做（刻意的判斷）：** 沒有手動把 id 103 的 `twitterAuthType` 改成 `'api'`。那會把**發文機制**從 cookie 改成 API，屬於行為變更；若它存的 refresh token 已失效，反而會讓一個目前可能還能用 cookie 發文的帳號完全壞掉。重新 OAuth 連接既安全又永久。
