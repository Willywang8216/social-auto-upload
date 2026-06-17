<template>
  <div class="about">
    <el-card class="about-card">
      <div class="about-header">
        <h1>Socialupload</h1>
        <p class="version">social-auto-upload</p>
      </div>

      <el-divider />

      <div class="about-section">
        <h3>系統簡介</h3>
        <p>
          本系統是一款自動化工具，協助內容創作者與營運人員一鍵將影片內容高效發佈到多個國內外主流社群平台。
          支援影片上傳、排程發佈等功能。
        </p>
      </div>

      <div class="about-section">
        <h3>支援平台</h3>
        <div class="platform-tags">
          <el-tag
            v-for="platform in supportedPlatforms"
            :key="platform.label"
            :type="platform.tagType"
          >
            {{ platform.label }}
          </el-tag>
        </div>
      </div>

      <div class="about-section">
        <h3>核心功能</h3>
        <ul class="feature-list">
          <li>多平台帳號管理與登入狀態維護</li>
          <li>影片素材上傳與管理</li>
          <li>一鍵多平台發佈</li>
          <li>排程發佈與批次發佈</li>
          <li>Cookie 匯入與匯出</li>
          <li>影片數據分析與 AI 建議</li>
          <li>遠端儲存空間 (DO Spaces) 整合</li>
        </ul>
      </div>

      <div class="about-section">
        <h3>技術棧</h3>
        <div class="tech-tags">
          <el-tag effect="plain">Vue 3</el-tag>
          <el-tag effect="plain">Element Plus</el-tag>
          <el-tag effect="plain">Pinia</el-tag>
          <el-tag effect="plain">Flask</el-tag>
          <el-tag effect="plain">Playwright</el-tag>
          <el-tag effect="plain">SQLite</el-tag>
          <el-tag effect="plain">DigitalOcean Spaces</el-tag>
        </div>
      </div>
    </el-card>

    <!-- API Reference -->
    <el-card class="about-card api-card">
      <template #header>
        <div class="api-header">
          <h2>API 參考文件</h2>
          <p class="api-subtitle">所有 API 端點都需要 Bearer Token 驗證</p>
        </div>
      </template>

      <div class="api-intro">
        <p><strong>Base URL:</strong> <code>https://socialupload.iamwillywang.com</code></p>
        <p><strong>驗證方式：</strong> 所有請求需帶上 <code>Authorization: Bearer &lt;your-api-token&gt;</code> 標頭</p>
        <p><strong>回應格式：</strong> JSON，包含 <code>code</code>、<code>msg</code>、<code>data</code> 三個欄位</p>
      </div>

      <el-collapse v-model="activeSections">
        <!-- Files / Materials -->
        <el-collapse-item name="files">
          <template #title>
            <span class="collapse-title">檔案 / 素材管理</span>
          </template>
          <div class="endpoint-group">
            <div class="endpoint">
              <span class="method post">POST</span>
              <code>/upload</code>
              <span class="desc">上傳檔案（不建立資料庫記錄）</span>
            </div>
            <div class="endpoint">
              <span class="method post">POST</span>
              <code>/uploadSave</code>
              <span class="desc">上傳檔案並記錄到資料庫，檔案會自動上傳到遠端儲存空間</span>
            </div>
            <div class="endpoint">
              <span class="method get">GET</span>
              <code>/getFile?filename=&lt;filename&gt;</code>
              <span class="desc">下載或預覽檔案（本地檔案不存在時自動轉址到 CDN）</span>
            </div>
            <div class="endpoint">
              <span class="method get">GET</span>
              <code>/getFiles</code>
              <span class="desc">列出所有已上傳的檔案</span>
            </div>
            <div class="endpoint">
              <span class="method get">GET</span>
              <code>/deleteFile?id=&lt;id&gt;</code>
              <span class="desc">刪除單一檔案（本地 + 遠端儲存空間）</span>
            </div>
            <div class="endpoint">
              <span class="method post">POST</span>
              <code>/deleteFiles</code>
              <span class="desc">批次刪除檔案</span>
            </div>
            <pre class="code-block"><code># 上傳檔案
curl -X POST https://socialupload.iamwillywang.com/uploadSave \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "file=@video.mp4"

# 列出所有檔案
curl https://socialupload.iamwillywang.com/getFiles \
  -H "Authorization: Bearer YOUR_TOKEN"

# 下載檔案
curl https://socialupload.iamwillywang.com/getFile?filename=abc123_video.mp4 \
  -H "Authorization: Bearer YOUR_TOKEN" -o video.mp4

# 刪除檔案
curl "https://socialupload.iamwillywang.com/deleteFile?id=1" \
  -H "Authorization: Bearer YOUR_TOKEN"

# 批次刪除
curl -X POST https://socialupload.iamwillywang.com/deleteFiles \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"ids": [1, 2, 3]}'</code></pre>
          </div>
        </el-collapse-item>

        <!-- Profiles -->
        <el-collapse-item name="profiles">
          <template #title>
            <span class="collapse-title">設定檔管理</span>
          </template>
          <div class="endpoint-group">
            <div class="endpoint">
              <span class="method get">GET</span>
              <code>/profiles</code>
              <span class="desc">列出所有設定檔</span>
            </div>
            <div class="endpoint">
              <span class="method post">POST</span>
              <code>/profiles</code>
              <span class="desc">建立新設定檔</span>
            </div>
            <div class="endpoint">
              <span class="method get">GET</span>
              <code>/profiles/&lt;id&gt;</code>
              <span class="desc">取得單一設定檔</span>
            </div>
            <div class="endpoint">
              <span class="method patch">PATCH</span>
              <code>/profiles/&lt;id&gt;</code>
              <span class="desc">更新設定檔</span>
            </div>
            <div class="endpoint">
              <span class="method delete">DELETE</span>
              <code>/profiles/&lt;id&gt;</code>
              <span class="desc">刪除設定檔</span>
            </div>
            <div class="endpoint">
              <span class="method get">GET</span>
              <code>/profiles/&lt;id&gt;/accounts</code>
              <span class="desc">列出設定檔中的帳號</span>
            </div>
            <div class="endpoint">
              <span class="method post">POST</span>
              <code>/profiles/&lt;id&gt;/accounts</code>
              <span class="desc">新增帳號到設定檔</span>
            </div>
            <pre class="code-block"><code># 列出設定檔
curl https://socialupload.iamwillywang.com/profiles \
  -H "Authorization: Bearer YOUR_TOKEN"

# 建立設定檔
curl -X POST https://socialupload.iamwillywang.com/profiles \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "我的設定檔", "description": "主要帳號"}'

# 取得設定檔中的帳號
curl https://socialupload.iamwillywang.com/profiles/1/accounts \
  -H "Authorization: Bearer YOUR_TOKEN"</code></pre>
          </div>
        </el-collapse-item>

        <!-- Accounts -->
        <el-collapse-item name="accounts">
          <template #title>
            <span class="collapse-title">帳號管理</span>
          </template>
          <div class="endpoint-group">
            <div class="endpoint">
              <span class="method get">GET</span>
              <code>/getAccounts</code>
              <span class="desc">列出所有社群帳號</span>
            </div>
            <div class="endpoint">
              <span class="method get">GET</span>
              <code>/getValidAccounts</code>
              <span class="desc">列出登入狀態有效的帳號</span>
            </div>
            <div class="endpoint">
              <span class="method post">POST</span>
              <code>/accounts/&lt;id&gt;/check-connection</code>
              <span class="desc">檢查帳號連線狀態</span>
            </div>
            <div class="endpoint">
              <span class="method post">POST</span>
              <code>/accounts/&lt;id&gt;/refresh-token</code>
              <span class="desc">重新整理帳號 Token</span>
            </div>
            <div class="endpoint">
              <span class="method post">POST</span>
              <code>/accounts/batch/check-connections</code>
              <span class="desc">批次檢查帳號連線狀態</span>
            </div>
            <div class="endpoint">
              <span class="method post">POST</span>
              <code>/accounts/batch/refresh-tokens</code>
              <span class="desc">批次重新整理 Token</span>
            </div>
            <div class="endpoint">
              <span class="method get">GET</span>
              <code>/accounts/health-summary</code>
              <span class="desc">帳號健康狀態摘要</span>
            </div>
            <div class="endpoint">
              <span class="method post">POST</span>
              <code>/uploadCookie</code>
              <span class="desc">上傳 Cookie 檔案</span>
            </div>
            <pre class="code-block"><code># 列出所有帳號
curl https://socialupload.iamwillywang.com/getAccounts \
  -H "Authorization: Bearer YOUR_TOKEN"

# 檢查帳號連線
curl -X POST https://socialupload.iamwillywang.com/accounts/1/check-connection \
  -H "Authorization: Bearer YOUR_TOKEN"

# 批次檢查連線
curl -X POST https://socialupload.iamwillywang.com/accounts/batch/check-connections \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"account_ids": [1, 2, 3]}'

# 帳號健康摘要
curl https://socialupload.iamwillywang.com/accounts/health-summary \
  -H "Authorization: Bearer YOUR_TOKEN"</code></pre>
          </div>
        </el-collapse-item>

        <!-- Publishing -->
        <el-collapse-item name="publishing">
          <template #title>
            <span class="collapse-title">發佈</span>
          </template>
          <div class="endpoint-group">
            <div class="endpoint">
              <span class="method post">POST</span>
              <code>/publish-center/submit</code>
              <span class="desc">提交發佈任務（透過發佈中心）</span>
            </div>
            <div class="endpoint">
              <span class="method post">POST</span>
              <code>/publish-center/preview</code>
              <span class="desc">預覽發佈內容</span>
            </div>
            <div class="endpoint">
              <span class="method post">POST</span>
              <code>/postVideo</code>
              <span class="desc">直接發佈影片到指定平台</span>
            </div>
            <pre class="code-block"><code># 提交發佈任務
curl -X POST https://socialupload.iamwillywang.com/publish-center/submit \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "profileId": 1,
    "mediaFilePaths": ["abc123_video.mp4"],
    "selectedAccountIds": [1, 2],
    "title": "我的影片",
    "tags": ["tag1", "tag2"]
  }'

# 直接發佈影片
curl -X POST https://socialupload.iamwillywang.com/postVideo \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "file": "abc123_video.mp4",
    "title": "影片標題",
    "accounts": [1],
    "tags": ["測試"]
  }'</code></pre>
          </div>
        </el-collapse-item>

        <!-- Campaigns -->
        <el-collapse-item name="campaigns">
          <template #title>
            <span class="collapse-title">行銷活動 (Campaigns)</span>
          </template>
          <div class="endpoint-group">
            <div class="endpoint">
              <span class="method post">POST</span>
              <code>/campaigns/prepare</code>
              <span class="desc">準備行銷活動（規劃跨平台貼文）</span>
            </div>
            <div class="endpoint">
              <span class="method get">GET</span>
              <code>/campaigns/&lt;id&gt;</code>
              <span class="desc">取得行銷活動詳情</span>
            </div>
            <div class="endpoint">
              <span class="method patch">PATCH</span>
              <code>/campaigns/&lt;id&gt;/posts/&lt;post_id&gt;</code>
              <span class="desc">更新活動中的單一貼文</span>
            </div>
            <div class="endpoint">
              <span class="method post">POST</span>
              <code>/campaigns/&lt;id&gt;/publish</code>
              <span class="desc">執行/發佈行銷活動</span>
            </div>
            <pre class="code-block"><code># 準備行銷活動
curl -X POST https://socialupload.iamwillywang.com/campaigns/prepare \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "profileId": 1,
    "mediaGroupId": 1,
    "selectedAccountIds": [1, 2, 3]
  }'

# 取得活動詳情
curl https://socialupload.iamwillywang.com/campaigns/1 \
  -H "Authorization: Bearer YOUR_TOKEN"

# 執行活動
curl -X POST https://socialupload.iamwillywang.com/campaigns/1/publish \
  -H "Authorization: Bearer YOUR_TOKEN"</code></pre>
          </div>
        </el-collapse-item>

        <!-- Analytics -->
        <el-collapse-item name="analytics">
          <template #title>
            <span class="collapse-title">數據分析</span>
          </template>
          <div class="endpoint-group">
            <div class="endpoint">
              <span class="method post">POST</span>
              <code>/analytics/sync</code>
              <span class="desc">觸發數據同步（所有帳號或指定帳號）</span>
            </div>
            <div class="endpoint">
              <span class="method get">GET</span>
              <code>/analytics/overview</code>
              <span class="desc">取得分析總覽（可選 ?platform=&amp;accountId=&amp;dateFrom=&amp;dateTo=）</span>
            </div>
            <div class="endpoint">
              <span class="method get">GET</span>
              <code>/analytics/videos</code>
              <span class="desc">列出影片及其分析數據</span>
            </div>
            <div class="endpoint">
              <span class="method get">GET</span>
              <code>/analytics/top-videos</code>
              <span class="desc">表現最佳的影片</span>
            </div>
            <div class="endpoint">
              <span class="method get">GET</span>
              <code>/analytics/trends</code>
              <span class="desc">趨勢數據（可選 ?metric=views|engagement_rate）</span>
            </div>
            <div class="endpoint">
              <span class="method post">POST</span>
              <code>/analytics/advice</code>
              <span class="desc">取得 AI 分析建議</span>
            </div>
            <div class="endpoint">
              <span class="method get">GET</span>
              <code>/analytics/sync/status</code>
              <span class="desc">查看同步狀態紀錄</span>
            </div>
            <pre class="code-block"><code># 觸發數據同步
curl -X POST https://socialupload.iamwillywang.com/analytics/sync \
  -H "Authorization: Bearer YOUR_TOKEN"

# 只同步特定帳號
curl -X POST https://socialupload.iamwillywang.com/analytics/sync \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"account_id": 5}'

# 取得分析總覽
curl https://socialupload.iamwillywang.com/analytics/overview \
  -H "Authorization: Bearer YOUR_TOKEN"

# 取得趨勢數據
curl "https://socialupload.iamwillywang.com/analytics/trends?metric=views" \
  -H "Authorization: Bearer YOUR_TOKEN"

# 取得表現最佳影片
curl https://socialupload.iamwillywang.com/analytics/top-videos \
  -H "Authorization: Bearer YOUR_TOKEN"

# 取得 AI 建議
curl -X POST https://socialupload.iamwillywang.com/analytics/advice \
  -H "Authorization: Bearer YOUR_TOKEN"</code></pre>
          </div>
        </el-collapse-item>

        <!-- Jobs -->
        <el-collapse-item name="jobs">
          <template #title>
            <span class="collapse-title">背景任務 (Jobs)</span>
          </template>
          <div class="endpoint-group">
            <div class="endpoint">
              <span class="method get">GET</span>
              <code>/jobs</code>
              <span class="desc">列出所有背景任務</span>
            </div>
            <div class="endpoint">
              <span class="method get">GET</span>
              <code>/jobs/&lt;id&gt;</code>
              <span class="desc">取得任務狀態</span>
            </div>
            <div class="endpoint">
              <span class="method post">POST</span>
              <code>/jobs/&lt;id&gt;/cancel</code>
              <span class="desc">取消任務</span>
            </div>
            <pre class="code-block"><code># 列出任務
curl https://socialupload.iamwillywang.com/jobs \
  -H "Authorization: Bearer YOUR_TOKEN"

# 查看任務狀態
curl https://socialupload.iamwillywang.com/jobs/42 \
  -H "Authorization: Bearer YOUR_TOKEN"

# 取消任務
curl -X POST https://socialupload.iamwillywang.com/jobs/42/cancel \
  -H "Authorization: Bearer YOUR_TOKEN"</code></pre>
          </div>
        </el-collapse-item>

        <!-- Media Groups -->
        <el-collapse-item name="media-groups">
          <template #title>
            <span class="collapse-title">媒體群組</span>
          </template>
          <div class="endpoint-group">
            <div class="endpoint">
              <span class="method post">POST</span>
              <code>/media-groups</code>
              <span class="desc">建立媒體群組（將多個檔案分組）</span>
            </div>
            <div class="endpoint">
              <span class="method get">GET</span>
              <code>/media-groups</code>
              <span class="desc">列出所有媒體群組</span>
            </div>
            <div class="endpoint">
              <span class="method get">GET</span>
              <code>/media-groups/&lt;id&gt;</code>
              <span class="desc">取得媒體群組詳情</span>
            </div>
            <pre class="code-block"><code># 建立媒體群組
curl -X POST https://socialupload.iamwillywang.com/media-groups \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "週末特輯",
    "fileIds": [1, 2, 3],
    "roles": ["video", "image", "thumbnail"]
  }'

# 列出媒體群組
curl https://socialupload.iamwillywang.com/media-groups \
  -H "Authorization: Bearer YOUR_TOKEN"</code></pre>
          </div>
        </el-collapse-item>
      </el-collapse>
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { SUPPORTED_PLATFORM_TAGS } from '@/utils/platforms'

const supportedPlatforms = SUPPORTED_PLATFORM_TAGS
const activeSections = ref([])
</script>

<style lang="scss" scoped>
.about {
  max-width: 900px;
  margin: 0 auto;

  .about-card {
    margin-bottom: 20px;

    .about-header {
      text-align: center;

      h1 {
        color: var(--color-text);
        margin: 0 0 8px 0;
        font-size: 24px;
      }

      .version {
        color: var(--color-text-secondary);
        font-size: 14px;
        margin: 0;
      }
    }

    .about-section {
      margin-bottom: 24px;

      h3 {
        font-size: 16px;
        color: var(--color-text);
        margin: 0 0 12px 0;
      }

      p {
        color: var(--color-text-secondary);
        line-height: 1.8;
        margin: 0;
      }

      .platform-tags,
      .tech-tags {
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
      }

      .feature-list {
        margin: 0;
        padding-left: 20px;
        color: var(--color-text-secondary);
        line-height: 2;
      }
    }
  }

  .api-card {
    .api-header {
      h2 {
        margin: 0 0 4px 0;
        font-size: 20px;
        color: var(--color-text);
      }
      .api-subtitle {
        margin: 0;
        font-size: 13px;
        color: var(--color-text-secondary);
      }
    }

    .api-intro {
      margin-bottom: 20px;
      p {
        margin: 0 0 6px 0;
        font-size: 13px;
        color: var(--color-text-secondary);
        line-height: 1.6;
      }
      code {
        background: #f5f7fa;
        padding: 2px 6px;
        border-radius: 3px;
        font-size: 12px;
        color: #e6a23c;
      }
    }

    .collapse-title {
      font-weight: 600;
      font-size: 15px;
    }

    .endpoint-group {
      .endpoint {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 6px 0;
        border-bottom: 1px solid #f0f0f0;

        &:last-of-type {
          border-bottom: none;
        }

        .method {
          display: inline-block;
          width: 60px;
          text-align: center;
          font-size: 11px;
          font-weight: 700;
          padding: 2px 0;
          border-radius: 3px;
          color: #fff;
          flex-shrink: 0;

          &.get { background: #67c23a; }
          &.post { background: #409eff; }
          &.patch { background: #e6a23c; }
          &.delete { background: #f56c6c; }
        }

        code {
          font-size: 13px;
          color: #303133;
          background: #f5f7fa;
          padding: 2px 6px;
          border-radius: 3px;
          white-space: nowrap;
        }

        .desc {
          font-size: 13px;
          color: var(--color-text-secondary);
          margin-left: auto;
        }
      }

      .code-block {
        margin: 12px 0 4px 0;
        padding: 14px 16px;
        background: #1e1e1e;
        border-radius: 6px;
        overflow-x: auto;

        code {
          color: #d4d4d4;
          font-size: 12px;
          line-height: 1.6;
          white-space: pre;
          background: none;
          padding: 0;
        }
      }
    }
  }
}
</style>
