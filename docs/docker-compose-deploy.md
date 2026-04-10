# Docker Compose 部署与 Google Service Account 设置

## 1. 部署文件

本仓库现在包含以下容器化文件：

- `Dockerfile`
- `docker-compose.yml`
- `.env.example`
- `docker/entrypoint.sh`

容器会：

- 构建前端静态文件
- 安装 Python 依赖、Playwright、Patchright、`chromium`、`ffmpeg`、`rclone`
- 启动时自动初始化 SQLite 数据库
- 监听 `5409` 端口

## 2. 目录准备

在仓库根目录准备这些目录：

```bash
mkdir -p data/db data/videoFile data/cookiesFile data/generated_media docker/rclone secrets
```

## 3. Rclone 配置

这个项目里的 OneDrive 上传是通过 `rclone` 完成的。容器内必须能读到你的 `rclone.conf`。

把宿主机上已经可用的 `rclone.conf` 复制到：

```bash
docker/rclone/rclone.conf
```

容器内路径会是：

```bash
/root/.config/rclone/rclone.conf
```

你的 profile 里可以继续使用：

- `remoteName=Onedrive-Yahooforsub-Tao`
- `remotePath=Scripts-ssh-ssl-keys/SocialUpload`

如果你后续要切 DigitalOcean Spaces，也是在 profile 的 storage 设置里改 remote。

## 4. `.env` 键名整理

从 `.env.example` 复制为 `.env`：

```bash
cp .env.example .env
```

当前建议统一使用这些键名：

### 核心运行

- `TZ`
- `LOCAL_CHROME_PATH`
- `LOCAL_CHROME_HEADLESS`
- `DEBUG_MODE`
- `XHS_SERVER`

### LLM

- `SAU_LLM_API_BASE_URL`
- `SAU_LLM_API_KEY`

### Google Sheets

- `SAU_GOOGLE_SERVICE_ACCOUNT_FILE`
- `SAU_GOOGLE_SERVICE_ACCOUNT_JSON`

优先推荐 `SAU_GOOGLE_SERVICE_ACCOUNT_FILE`。

Docker Compose 部署时，建议直接使用：

```env
LOCAL_CHROME_PATH=/usr/bin/chromium
```

这样容器内的 Playwright / Patchright 都会优先走系统 Chromium，而不是依赖额外的 Chrome 安装。

### 可选默认值

- `SAU_DEFAULT_RCLONE_REMOTE`
- `SAU_DEFAULT_RCLONE_PATH`
- `SAU_PUBLIC_URL_TEMPLATE`

### 可选第三方凭证

- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `REDDIT_CLIENT_ID`
- `REDDIT_CLIENT_SECRET`
- `REDDIT_REFRESH_TOKEN`
- `X_APP_ID`
- `X_API_KEY`
- `X_API_KEY_SECRET`
- `X_BEARER_TOKEN`
- `X_ACCESS_TOKEN`
- `X_ACCESS_TOKEN_SECRET`
- `X_CLIENT_ID`
- `X_CLIENT_SECRET`
- `X_2FA_BACKUP_CODE`

## 5. Google Service Account 接法

### 步骤 1：建立 Google Cloud Project

1. 打开 Google Cloud Console
2. 新建或选择一个 Project
3. 启用 **Google Sheets API**
4. 启用 **Google Drive API**

### 步骤 2：建立 Service Account

1. 打开 **IAM & Admin > Service Accounts**
2. 点击 **Create Service Account**
3. 建立完成后，进入该账号
4. 打开 **Keys**
5. 点击 **Add key > Create new key > JSON**
6. 下载 JSON 文件

### 步骤 3：放入仓库

把下载好的 JSON 文件保存为：

```bash
secrets/google-service-account.json
```

然后在 `.env` 中填写：

```env
SAU_GOOGLE_SERVICE_ACCOUNT_FILE=/run/secrets/google-service-account.json
```

### 步骤 4：把目标 Google Sheet 分享给 Service Account

打开你的 Google Sheet，把它分享给 JSON 文件中的：

```text
client_email
```

例如：

```text
social-sheet-bot@your-project.iam.gserviceaccount.com
```

至少给它 **Editor** 权限，否则写入会失败。

### 步骤 5：在 Profile 里填写

在前端 `Profile 管理` 页面里填写：

- `googleSheet.spreadsheetId`
- `googleSheet.worksheetName`

`spreadsheetId` 是 URL 中 `/d/` 与 `/edit` 之间那段。

## 6. 固定 IP 与网络

`docker-compose.yml` 已经固定使用：

- 网络：`1panel-network`
- IP：`172.18.0.45`

前提是这个 external network 已经存在，并且该网段允许分配这个 IP。

例如先确认：

```bash
docker network inspect 1panel-network
```

如果这个网络并不存在，需要由 1Panel 或 Docker 先创建。

## 7. 启动

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f social-auto-upload
```

## 8. 端到端验证步骤

### A. 基础容器验证

1. `docker compose up -d --build`
2. 打开 `http://<服务器IP>:5409`
3. 确认前端可正常加载
4. 确认 `data/db/database.db` 已生成

### B. Profile 配置验证

在 `Profile 管理` 新建一个测试 profile：

- `remoteName = Onedrive-Yahooforsub-Tao`
- `remotePath = Scripts-ssh-ssl-keys/SocialUpload`
- 填入 LLM 模型名
- 填入 `spreadsheetId`
- 填入 `worksheetName`

### C. OneDrive 上传验证

1. 上传一张图片或一个 mp4 素材
2. 在 Profile 里点击“生成内容”
3. 选该素材
4. 先勾选写入 Google Sheet
5. 提交后检查：
   - 容器日志是否出现 `rclone` 相关报错
   - OneDrive 对应目录是否出现文件
   - 回传的 `publicUrl` 是否可以匿名打开

### D. Google Sheet 写入验证

检查目标 worksheet：

- 第 1 行是否为 20 个固定表头
- 后续是否新增 twitter / threads / instagram / facebook / youtube / tiktok 对应的行
- `ImageURL` / `VideoURL` 是否只有一栏被填

### E. AI 文案验证

检查生成结果是否符合：

- `twitter` 和 `threads` 含 emoji
- `twitter` 和 `threads` 各有 3 个 hashtag
- `instagram` / `facebook` 为长文
- 文案中含 `contactDetails`
- 文案中含 CTA

## 9. 常见故障排查

### `rclone link failed`

说明当前 remote 不支持 `rclone link`，或 OneDrive 不能稳定生成公开直链。

解决思路：

1. 在 profile 的 `storage.publicUrlTemplate` 中配置你自己的 CDN / 公开 URL 规则
2. 或改成上传到支持公开文件直链的对象存储

### `Google service account credentials are not configured`

检查：

- `SAU_GOOGLE_SERVICE_ACCOUNT_FILE` 是否存在
- `./secrets/google-service-account.json` 是否已挂载
- JSON 是否为有效内容

### `SpreadsheetNotFound` 或权限错误

检查该 Google Sheet 是否已分享给 service account 的 `client_email`。

### `rclone is not installed` / `ffmpeg is required`

这两个工具已经内置在新 Dockerfile 中；如果仍报错，通常是容器没有重新 build。

重新执行：

```bash
docker compose up -d --build
```

### 浏览器自动化失败

Playwright/patchright 依赖对 `/dev/shm` 较敏感，compose 已设置：

```yaml
shm_size: "1gb"
```

如果仍有浏览器崩溃，可进一步提高。
