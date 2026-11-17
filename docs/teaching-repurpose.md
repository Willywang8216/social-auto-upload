# Teaching 文章重製：圖卡、摘要影片與交付核對

這是 **disabled-by-default** 的 Teaching-only 功能。Notion Teaching DB 是唯一文章主檔；SAU 不另建文章編輯器、不設定本人核准、不發布網站。實作與離線檢查不代表正式啟用或一次真實交付。

## 範圍與既有流程

- 固定 profile **4**、Instagram **43**、Facebook **58**、Threads **61**、Bluesky **125**；繁體中文與既有 SFW 路由。
- 原準備 owner 提供最後文章快照及 Notion page/version。產生衍生 copy、來源參照、圖卡、影片、平台文案及格式計畫後，放進同一份 Notion review child。
- 只有本人改動既有核准欄位。網站仍由原 Manager runner 發布；台北 **20:00 / 72 elapsed hours / no catch-up** 不變。
- 原 owner 驗證確切網站 commit、canonical、Markdown/body、catalog、sitemap 後，才交接已審閱的社群素材。`useLlm:false`、不重寫、不重編碼、不從舊核准自動補發。
- 重啟、重複事件及未知回應只核對原 request / jobs / targets。已有 release 的文章不因換 kit、軟體更新或新 render 而自動重發。

## 準備輸出

套件封存 source page、文章 SHA/version、visual-kit SHA、private writing-rules version、完整 storyboard/claims、所有素材大小/SHA、平台文案及每一個 `(account, format)`。

- 教學 deck 約 **6–8 張**、1080×1350。Bluesky 是另做、另核對的 **四張摘要**，不是截去長 deck 的後半。
- 同時產生 PNG 審閱／封存檔與實際 JPEG 交付檔；核准綁定實際交付 bytes。
- 版型明確區分 title、explanation、example、action。字距依真實 CJK glyph 量測，保留文字／數字／條件；溢出 hold，不裁切或縮到閱讀門檻以下。
- 公式使用本機 bundled KaTeX，阻止任意 HTML/URL/macro 指令、阻斷網路；不把 raw LaTeX 當教學圖像。KaTeX lock 與 MIT license 隨 image 保留。
- 摘要影片 1080×1920、約 **45–90 秒**，閱讀時間依內容決定；H.264/yuv420p、30 fps、相容音軌、poster JPEG 與文字稿。無自動旁白、聲音複製或未授權音樂。
- 影片有實際 ffprobe、bounded decode、SHA/大小與有效平台限制檢查。影片是摘要，不宣稱完整朗讀或完整證明。

四個圖卡目的地必選；預設另選 IG/FB 影片。Threads/Bluesky 影片可在核准前選取，但其實際路由能力必須通過。所有被選的格式都要有送達證據，不能以四個帳號的單一成功旗標概括多格式結果。

共用 `myUtils/repurpose/visuals.py` 與 Teaching `repurpose_visuals.py` 是相同公開 bytes。舊 `make_carousel.py` / `make_review_card.py` 只有明確 `--visual-kit ink|cream` 才使用新介面；未 opt-in 的預設像素與 hash 保持 legacy。新素材永遠是新套件／新審閱，不能覆蓋已廣告的 artifact identity。

## 私有規則與資源

`weiwei-senpai-writing` 與 `humanizer` 的 allowlisted 規則及其 references 存在獨立私有目錄。缺規則、模型錯誤、不支持的 claim、未解決的數學問題或未完成 readback 都 hold；不使用 generic blog prompt 或 fail-open humanizer 作為通行證。

產生的 derivatives 使用 Humanizer A；原人類文章受 Mode B 保護且不改動。Style/regex 檢查是輔助，不冒充事實或數學證明。

規則封裝工具只複製 allowlisted 檔案到新 versioned 私有根目錄：

```text
python scripts/repurpose_rules.py --skills-root <installed-skills-root> --out <existing-private-parent>
```

不要把規則本文、文章快照、tokens、OAuth config、provider receipts 或私人 logo/肖像複製到 public repo/image。`SAU_TEACHING_REPURPOSE_ROOT`、規則根目錄與 owner receipt 目錄須在 checkout 外，不能走 symlink/junction、UNC 或跨 scope。

準備用 durable SQLite lease queue；不是 Flask request 中的渲染執行緒，也不在 import 時起 worker。單一 render owner、FFmpeg bounded threads/timeouts、最多 1800 秒工作 deadline。取消／失效 lease 保留 attempts 與歷史；不退款或改為假失敗以便重試。

準備及批准素材複製前都保留 **2 GiB free disk / 4 GiB available RAM**，並計算 staging/output 與 SQLite headroom。容量不足只 hold，絕不刪 active/rollback media。實際交付 bytes 存在既有 protected `data/publish`；private review 檔案缺失不等於可以丟掉尚在排程／未知回執中的素材。

## 預設關閉的配置

| 設定 | 用途 |
|---|---|
| `SAU_TEACHING_REPURPOSE_ENABLED=0` | 準備及 kit mutation 預設關閉 |
| `SAU_TEACHING_REPURPOSE_RELEASE_ENABLED=0` | 新社群交接／provider media fetch 預設關閉 |
| `SAU_TEACHING_REPURPOSE_ROOT` | 私有 package/lease 根目錄，必須在 public checkout 外 |
| `SAU_DB_PATH` | 既有 SAU SQLite；生產 schema 只能走維護流程 |
| `SAU_TEACHING_REPURPOSE_MEDIA_ROOT` | 既有 protected delivery store |
| `SAU_TEACHING_REPURPOSE_PUBLIC_BASE_URL` | 已配置的 SAU provider-fetch origin；不是原文章 URL |
| `SAU_TEACHING_OWNER_ALLOWED_SIGNERS` | 固定 owner principal/namespace 的 existing operator SSH public key allowlist |
| `SAU_TEACHING_OWNER_RECEIPTS_ROOT` | create-only signed handoff/grant history |
| `SAU_REPURPOSE_PRIVATE_GATEWAY` | 只有既有私有 SSH gateway invocation 才能設為 `1` |

本文件不授權修改以上值或啟動服務。維護部署獲准後，standalone preparation worker 的參數是：

```text
python scripts/repurpose_worker.py --db <SAU-db> --root <private-packages> --rules-root <private-rules-version> [--once]
```

owner/publisher 沒有第二個 Notion watcher。Manager 的實際 `current` revision、原服務與排程須在後續部署前重新讀取，不能由 Windows checkout 推定。

## Authority 與回執

普通 HTTP/JSON、prepared 狀態、舊 approval label、published label、checksum 或 HTTP 200 **都不能建立 release authority**。

1. **首次 handoff**：原 Queue/Notion/website owner 對確切 page、文章 SHA、review/package/task、canonical/site evidence 簽名。SAU 用原 operator SSH public key、固定 principal/namespace、strict fields 與不超過 60 秒的 UTC observation 驗證。第一份 handoff 檔案 create-only；不可改舊 timestamp 續期或蓋掉 receipt。
2. **新 provider effect 前**：既有 Manager poll 在 fresh approval/source/live 核對後，對原 release 的確切 job、target、account、format、原 slot 簽署不同的短效 dispatch grant。SAU current pointer 只接受 monotonic observations；每份 signed file 保留，不增加新的 job 或新的 retry budget。
3. **worker**：receipt/unknown-submission 先走原只讀核對。新 effect 在 account/media I/O 與 provider operation checkpoint 前重驗 short-lived grant；missing/stale/withdrawn/changed source 走既有 prerequisite wait，不扣錯 budget、不清 receipts、不早於原 slot。
4. **完成**：只有 valid 原 journal 綁定與 actual provider delivered receipt、target `succeeded/delivered`、physical attempt、已消耗且匹配的 provider route budget、有效 ID/aware delivered time 才計入。Accepted/processing/queued 不算送達；任何 unknown 不重送。

交接一次以 atomic publication group 記錄所有選取 formats、jobs、targets、artifacts 與原 slots。使用既有 calendar/admission 的 **3/day、至少 30 分鐘**，不移動其他 bookings。部分成功保留；aggregate completion 需要每一個選取 delivery 確認。

## 操作介面

Publish → **Teaching 重製**（`#/publish/teaching-repurpose`）包含：

- 唯讀 Notion 文章快照、原 source/version/kit/package SHA。
- 實際 JPEG／PNG、四卡 variant、影片/poster、文字稿、caption、來源參照與 check records。
- ink/cream 新版本選擇、四個圖卡固定目的地、核准前 video 選擇、明確重新準備／取消。
- 原 release/job/target/slot 及確認／等待／未知結果；沒有 approve、blog publish 或 social resend 按鈕。

二進位預覽使用現有 bearer/session/CSRF；沒有 token query URL。MIME、大小與 WebCrypto SHA 一致後才建立 Blob URL。切換工作、取消、離開頁面或 auth revision 變動會 abort/revoke。資料不記錄到 console 或持久文章快取。Flag off、無 generation owner、最新狀態未確認、已交接或 unknown mutation 都停用新 render。Lost response 使用原 request ID 查詢，不自動新造請求。

HTTP 路由都需登入及 Teaching workspace scope；Google 非 owner 只能依權限查看。`/tasks/<id>/regenerate` 的 body 僅有新 `request_id`、`kit_version`、`deliveries`，不接受文章內容或批准旗標。私有 asset 是 `private, no-store`，public provider fetch 只服務原已交接 journal 中的 approved image/video；不服務 manuscript/JSON。

## 檢查與正式啟用門檻

- 新核心／authority／release 測試：`tests/test_teaching_repurpose_*.py`；real local CJK PNG/JPEG/MP4 與 injected offline LLM。不能把 fixture 發到任何公開平台。
- Teaching v1/v2 queue、review、bridge、metadata regression；shared visual default pixels/PNG bytes 與 public-module parity。
- Node **22.21.1**：frontend tests/build。`sau_frontend/scripts/check-teaching-repurpose-ui.mjs <private-real-fixture-manifest> <session-scratch-output>` 是 localhost-only browser check，所有 API writes/outside requests 被拒絕。
- Python **3.12.14 / 3.13.15**、uv **0.12.7** frozen lock、maintained static/undefined names、backend security、isolated migration/readiness。

Windows 本機結果不是 `scripts/release_checks.py record` 的 Linux clean-commit receipt。正式 rollout 還須同一乾淨 source revision 的 Linux dual-version、frontend、Docker image/CVE-policy/non-root gates。保留 `publish.pause` 與 staged release markers；只能走 maintained deployment/resume 與實際繼承的 lock。

完成實作不等於達成真實 pilot。部署、service activation、production migration、一次真正已核准的新文章 pilot 都需要獨立明確批准；不可拿 synthetic public post、已發布文章或歷史核准測試。不做自動 backfill，Goal X／其他 publishing recovery 留在原狀。
