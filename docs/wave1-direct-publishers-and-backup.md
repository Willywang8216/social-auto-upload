# Wave 1：Direct Publishers、Google Sheet 同步與備份還原

這份說明對應 2026-04-10 完成的 Wave 1 實作，涵蓋：

- Telegram / Discord / Reddit / X 直發
- X 排程用 Google Sheet scheduler copy 與 cleanup / retry 規則
- 一鍵備份與還原

## 1. Direct Publishers 設定

### 後台入口

前往：

- `Profile 管理`
- 右上角 `Direct Publishers`

你可以建立四種 target：

- `Telegram`
- `Discord`
- `Reddit`
- `X / Twitter`

每一個 target 都有自己的 `id`，內容帳號會用 `publisherTargetId` 綁到這裡。

### 各平台需要的欄位

#### Telegram

- `Bot Token`
- `Chat ID`

說明：

- `Chat ID` 可以是 `@channel_name`
- 也可以是群組 / 頻道的數字 ID

#### Discord

- `Webhook URL`
- `Webhook 名稱`（可留空）

說明：

- Wave 1 只支援 webhook，不支援 bot token 模式

#### Reddit

- `Client ID`
- `Client Secret`
- `Refresh Token`
- `Subreddit`

說明：

- 實作使用 refresh token 換 access token，再呼叫 `/api/submit`

#### X / Twitter

- `API Key`
- `API Key Secret`
- `Access Token`
- `Access Token Secret`

說明：

- 實作使用 OAuth 1.0a
- 發文 endpoint 為 `POST https://api.x.com/2/tweets`

### 安全要求

不要重用先前貼在聊天中的 secrets。那些值應視為已洩漏，請重新產生後再填入。

## 2. 內容帳號如何綁定直發 target

前往：

- `Profile 管理`
- 編輯某個 Profile
- 在 `內容帳號設定` 中選擇對應的內容帳號

若平台是以下之一：

- `telegram`
- `discord`
- `reddit`
- `twitter`

表單會出現 `Direct Target` 下拉選單。

這裡選到的 target 會儲存在該 content account 的：

- `publisherTargetId`

發佈 job 執行時會依這個欄位去找真正的 connector 設定。

## 3. Google Sheet 規則

### 3.1 一般 sheet_export job

以下平台仍走既有 `sheet_export`：

- Threads
- Instagram
- Facebook
- YouTube
- TikTok

它們會把列寫進 profile 綁定的 Google Sheet worksheet。

### 3.2 X 的 scheduler copy

X 現在是 `direct_upload`，但為了保留排程協作流程，系統會另外建立一份 scheduler copy 到 Google Sheet。

建立時機：

- `save_publish_jobs()` 儲存 X direct job 時

限制：

- 該 job 必須有 `profileId`
- 該 profile 必須設定 `settings.googleSheet.spreadsheetId`
- 該 job 必須有訊息內容

metadata 會記錄：

- `schedulerSheet.spreadsheetId`
- `schedulerSheet.worksheet`
- `schedulerSheet.rowNumbers`
- `schedulerSheet.scheduledAt`
- `schedulerSheet.state`
- `schedulerSheet.retryCount`

### 3.3 發佈成功後的 cleanup

若 X direct publish 成功：

- job 會標記為 `published`
- 系統會刪除 scheduler copy 對應的 Google Sheet row
- `schedulerSheet.state` 會改成 `deleted`

### 3.4 發佈失敗後的 retry 規則

若 X direct publish 失敗：

- job 會標記為 `failed`
- 系統會把 scheduler copy 改排到 `+7 天`
- 舊 row 會刪除
- 新 row 會寫入新的 worksheet / row number
- `schedulerSheet.state` 會改成 `retry_active`
- `schedulerSheet.retryCount` 會加 1

目前 Wave 1 的 retry 只實作一次自動重建 scheduler copy，不會無限重試。

## 4. 備份與還原

### 4.1 建立備份

在 repo 根目錄執行：

```bash
bash backup.sh
```

這會呼叫後端的 `run_profile_backup()`，並透過 rclone 上傳一個 `.tar.gz` bundle。

### 4.2 備份內容

bundle 目前包含：

- `db/database.db`
- `db/direct_publishers.json`
- `db/google_service_account.json`（若存在）
- `db/profile_backup_config.json`
- `db/profiles.export.yaml`
- `db/backup-manifest.json`

### 4.3 還原最新備份

```bash
bash recover.sh --latest
```

流程：

1. 讀取 `db/profile_backup_config.json`
2. 用 rclone 找出最新的 `profiles-backup-*.tar.gz`
3. 下載到暫存目錄
4. 解壓回專案根目錄

### 4.4 還原指定備份

```bash
bash recover.sh --remote 'Onedrive-Yahooforsub-Tao:Scripts-ssh-ssl-keys/SocialUpload/backups/profile-configs/profiles-backup-20260410-120000.tar.gz'
```

## 5. 執行前需求

### 必要工具

- `rclone`
- Python 執行環境
- 前端建置環境（若要重新 build UI）

### 外部服務需求

- Google service account 已設定，且目標 sheet 已分享給該 account
- X / Telegram / Reddit / Discord 各自的有效 credentials

## 6. 最小驗證清單

### Direct publishers

1. 在 `Direct Publishers` 建立 1 個 target
2. 在某個 content account 綁定 `Direct Target`
3. 生成 publish draft
4. 確認 job metadata 中有 `publisherTargetId`

### X + Google Sheet

1. 建立 1 個 X content account，且 profile 綁定 Google Sheet
2. 儲存排程 job
3. 確認 job metadata 中出現 `schedulerSheet.rowNumbers`
4. 成功發佈後，確認 row 被刪除
5. 模擬失敗後，確認 row 被改排到 `+7 天`

### Backup / recover

1. 執行 `bash backup.sh`
2. 確認 remote 上有新的 `profiles-backup-*.tar.gz`
3. 執行 `bash recover.sh --latest`
4. 確認 `db/` 內資料已被展開回專案
