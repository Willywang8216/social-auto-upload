<template>
  <div class="api-docs">
    <el-card>
      <template #header>
        <div class="card-header">
          <h2>API Documentation</h2>
          <el-tag type="info">Base URL: http://localhost:5409</el-tag>
        </div>
      </template>

      <el-alert type="info" :closable="false" style="margin-bottom: 20px">
        All endpoints require <code>Authorization: Bearer YOUR_TOKEN</code> header.
        Get your token from <code>.env</code> → <code>SAU_API_TOKENS</code>.
      </el-alert>

      <div v-for="section in sections" :key="section.title" class="api-section">
        <h3 @click="section.open = !section.open" class="section-title">
          <el-icon><ArrowDown v-if="section.open" /><ArrowRight v-else /></el-icon>
          {{ section.title }}
        </h3>
        <div v-show="section.open">
          <div v-for="ep in section.endpoints" :key="ep.path" class="endpoint">
            <div class="endpoint-header">
              <el-tag :type="methodColor(ep.method)" size="small">{{ ep.method }}</el-tag>
              <code class="path">{{ ep.path }}</code>
              <span class="desc">{{ ep.description }}</span>
            </div>
            <div v-if="ep.params" class="endpoint-detail">
              <strong>Parameters:</strong>
              <ul>
                <li v-for="p in ep.params" :key="p.name">
                  <code>{{ p.name }}</code> ({{ p.type }}) — {{ p.desc }}
                </li>
              </ul>
            </div>
            <div v-if="ep.example" class="endpoint-detail">
              <strong>Example:</strong>
              <pre>{{ ep.example }}</pre>
            </div>
            <div v-if="ep.response" class="endpoint-detail">
              <strong>Response:</strong>
              <pre>{{ ep.response }}</pre>
            </div>
          </div>
        </div>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ArrowDown, ArrowRight } from '@element-plus/icons-vue'

function methodColor(m) {
  const colors = { GET: 'success', POST: 'primary', PATCH: 'warning', DELETE: 'danger' }
  return colors[m] || 'info'
}

const sections = ref([
  {
    title: '📋 Profiles',
    open: true,
    endpoints: [
      { method: 'GET', path: '/profiles', description: 'List all profiles' },
      { method: 'POST', path: '/profiles', description: 'Create a profile',
        params: [
          { name: 'name', type: 'string', desc: 'Profile display name' },
          { name: 'description', type: 'string', desc: 'Profile description' },
          { name: 'system_prompt', type: 'string', desc: 'LLM system prompt for content generation' },
          { name: 'writing_style_prompt', type: 'string', desc: 'Writing style instructions' },
          { name: 'contact_details', type: 'string', desc: 'Contact info to include in posts' },
          { name: 'default_cta', type: 'string', desc: 'Default call-to-action text' },
          { name: 'default_hashtags', type: 'string', desc: 'Default hashtags' },
          { name: 'default_link', type: 'string', desc: 'Default website link' },
          { name: 'default_language', type: 'string', desc: 'Language code (e.g. "en")' },
          { name: 'timezone', type: 'string', desc: 'Timezone (e.g. "UTC")' },
        ],
        example: `curl -X POST /profiles \\
  -H "Authorization: Bearer TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"name":"My Brand","system_prompt":"You are a social media manager."}'`,
        response: `{"code":200,"data":{"id":1,"name":"My Brand","slug":"my-brand",...},"msg":"created"}`
      },
      { method: 'GET', path: '/profiles/:id', description: 'Get profile details' },
      { method: 'PATCH', path: '/profiles/:id', description: 'Update profile fields' },
      { method: 'DELETE', path: '/profiles/:id', description: 'Delete profile and all accounts' },
      { method: 'GET', path: '/profiles/:id/accounts', description: 'List accounts for profile' },
      { method: 'POST', path: '/profiles/:id/accounts', description: 'Add account to profile',
        params: [
          { name: 'platform', type: 'string', desc: 'Platform name (tiktok, youtube, reddit, etc.)' },
          { name: 'account_name', type: 'string', desc: 'Account display name' },
          { name: 'auth_type', type: 'string', desc: 'Auth method: cookie, oauth, env, webhook, manual' },
          { name: 'config', type: 'object', desc: 'Platform-specific config (subreddits, channel_id, etc.)' },
        ]
      },
    ]
  },
  {
    title: '🎬 Media Assets',
    open: false,
    endpoints: [
      { method: 'GET', path: '/api/media/assets', description: 'List media assets',
        params: [
          { name: 'media_type', type: 'string', desc: 'Filter: video, image, audio' },
          { name: 'upload_status', type: 'string', desc: 'Filter: pending, uploaded, failed' },
          { name: 'processing_status', type: 'string', desc: 'Filter: pending, processing, processed, failed' },
          { name: 'limit', type: 'int', desc: 'Max results (1-1000, default 200)' },
          { name: 'offset', type: 'int', desc: 'Pagination offset' },
        ]
      },
      { method: 'GET', path: '/api/media/assets/:id', description: 'Get asset details' },
      { method: 'POST', path: '/api/media/upload/batch', description: 'Upload multiple files',
        params: [
          { name: 'files', type: 'multipart', desc: 'One or more files (images/videos/audio)' },
        ],
        example: `curl -X POST /api/media/upload/batch \\
  -H "Authorization: Bearer TOKEN" \\
  -F "files=@video1.mp4" \\
  -F "files=@photo1.jpg" \\
  -F "files=@photo2.png"`,
        response: `{"assets":[{"id":1,"original_filename":"video1.mp4","media_type":"video",...}], "count":3}`
      },
      { method: 'POST', path: '/api/media/assets/:id/process', description: 'Process asset (watermark, thumbnail, audio)',
        params: [
          { name: 'watermark_config_id', type: 'int', desc: 'Optional watermark config to apply' },
        ]
      },
      { method: 'POST', path: '/api/media/assets/:id/upload-rclone', description: 'Upload to rclone remote (OneDrive)',
        params: [
          { name: 'profile_slug', type: 'string', desc: 'Profile slug for path organization' },
        ]
      },
      { method: 'DELETE', path: '/api/media/assets/:id', description: 'Delete media asset' },
    ]
  },
  {
    title: '🖼️ Media Groups',
    open: false,
    endpoints: [
      { method: 'GET', path: '/media-groups', description: 'List all media groups' },
      { method: 'POST', path: '/media-groups', description: 'Create media group',
        params: [
          { name: 'name', type: 'string', desc: 'Group name' },
          { name: 'notes', type: 'string', desc: 'Group notes' },
          { name: 'profile_id', type: 'int', desc: 'Associated profile' },
          { name: 'group_type', type: 'string', desc: 'images, video, mixed' },
          { name: 'content_theme', type: 'string', desc: 'Theme (teaching, lifestyle, etc.)' },
          { name: 'user_notes', type: 'string', desc: 'Additional user notes' },
        ]
      },
      { method: 'GET', path: '/media-groups/:id', description: 'Get media group with items' },
      { method: 'PATCH', path: '/api/media-groups/:id', description: 'Update media group fields' },
      { method: 'PATCH', path: '/api/media-groups/:id/items/reorder', description: 'Reorder items in group',
        params: [
          { name: 'items', type: 'array', desc: '[{"id":1,"sort_order":0}, {"id":2,"sort_order":1}]' },
        ]
      },
      { method: 'DELETE', path: '/api/media-groups/:id', description: 'Delete media group' },
    ]
  },
  {
    title: '💧 Watermark Configs',
    open: false,
    endpoints: [
      { method: 'GET', path: '/api/watermark-configs', description: 'List watermark configs',
        params: [
          { name: 'profile_id', type: 'int', desc: 'Filter by profile' },
        ]
      },
      { method: 'POST', path: '/api/watermark-configs', description: 'Create watermark config',
        params: [
          { name: 'name', type: 'string', desc: 'Config name' },
          { name: 'watermark_type', type: 'string', desc: 'text, image, combined' },
          { name: 'text', type: 'string', desc: 'Watermark text (for text type)' },
          { name: 'image_path', type: 'string', desc: 'Path to watermark image' },
          { name: 'opacity', type: 'float', desc: '0.0-1.0 (default 0.3)' },
          { name: 'scale', type: 'float', desc: 'Image scale relative to source (default 0.15)' },
          { name: 'margin', type: 'int', desc: 'Margin from edges in pixels (default 24)' },
          { name: 'randomize_position', type: 'bool', desc: 'Random position per image' },
          { name: 'video_dynamic_position', type: 'bool', desc: 'Change position every 1-5s in video' },
          { name: 'video_position_change_min_seconds', type: 'int', desc: 'Min seconds between changes' },
          { name: 'video_position_change_max_seconds', type: 'int', desc: 'Max seconds between changes' },
          { name: 'allowed_positions', type: 'array', desc: '["top_left","top_right","bottom_left","bottom_right","center"]' },
          { name: 'font_family', type: 'string', desc: 'Font file path' },
          { name: 'font_size', type: 'int', desc: 'Font size (0=auto)' },
          { name: 'font_color', type: 'string', desc: 'Color name or hex (default "white")' },
          { name: 'enabled', type: 'bool', desc: 'Enable/disable (default true)' },
        ],
        example: `curl -X POST /api/watermark-configs \\
  -H "Authorization: Bearer TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{
    "name": "brand-watermark",
    "watermark_type": "text",
    "text": "© MyBrand",
    "opacity": 0.4,
    "video_dynamic_position": true,
    "allowed_positions": ["top_left","top_right","bottom_left","bottom_right"]
  }'`
      },
      { method: 'GET', path: '/api/watermark-configs/:id', description: 'Get config details' },
      { method: 'PATCH', path: '/api/watermark-configs/:id', description: 'Update config' },
      { method: 'DELETE', path: '/api/watermark-configs/:id', description: 'Delete config' },
    ]
  },
  {
    title: '📢 Campaigns',
    open: false,
    endpoints: [
      { method: 'POST', path: '/campaigns/prepare', description: 'Create campaign from profile + media group',
        params: [
          { name: 'profile_id', type: 'int', desc: 'Profile ID' },
          { name: 'media_group_id', type: 'int', desc: 'Media group ID' },
          { name: 'selected_account_ids', type: 'array', desc: 'Account IDs to target (empty=all)' },
        ]
      },
      { method: 'GET', path: '/campaigns/:id', description: 'Get campaign details' },
      { method: 'POST', path: '/api/campaigns/:id/generate', description: 'Generate platform-specific content using LLM',
        params: [
          { name: 'platforms', type: 'array', desc: 'Optional: ["twitter","instagram"] to limit generation' },
        ],
        example: `curl -X POST /api/campaigns/1/generate \\
  -H "Authorization: Bearer TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"platforms":["twitter","instagram","facebook"]}'`,
        response: `{"posts":[{"platform":"twitter","message":"✨ Great post! #a #b #c","status":"generated",...}], "count":3}`
      },
      { method: 'POST', path: '/api/campaigns/:id/validate', description: 'Validate all posts against platform rules',
        response: `{"results":[{"id":1,"platform":"twitter","errors":[],"valid":true}], "total":3, "valid":3}`
      },
      { method: 'POST', path: '/api/campaigns/:id/approve', description: 'Approve valid posts for export/publish',
        params: [
          { name: 'post_ids', type: 'array', desc: 'Optional: specific post IDs to approve (null=all valid)' },
        ]
      },
      { method: 'GET', path: '/api/campaigns/:id/posts', description: 'List prepared posts',
        params: [
          { name: 'platform', type: 'string', desc: 'Filter by platform' },
          { name: 'status', type: 'string', desc: 'Filter by status' },
        ]
      },
      { method: 'PATCH', path: '/api/campaigns/:id/posts/:post_id', description: 'Edit a prepared post',
        params: [
          { name: 'message', type: 'string', desc: 'Updated message text' },
          { name: 'title', type: 'string', desc: 'Updated title' },
          { name: 'description', type: 'string', desc: 'Updated description' },
          { name: 'first_comment', type: 'string', desc: 'Updated first comment' },
        ]
      },
      { method: 'POST', path: '/api/campaigns/:id/export/google-sheet', description: 'Export approved posts to Google Sheet',
        params: [
          { name: 'profile_slug', type: 'string', desc: 'Profile slug for sheet name (YYYY-MM-DD_SLUG)' },
          { name: 'spreadsheet_id', type: 'string', desc: 'Optional: update existing sheet' },
          { name: 'folder_id', type: 'string', desc: 'Optional: Google Drive folder ID' },
        ],
        response: `{"id":1,"sheet_name":"2026-06-15_my-brand","spreadsheet_url":"https://docs.google.com/...", "row_count":5}`
      },
      { method: 'GET', path: '/api/campaigns/:id/export/csv', description: 'Download approved posts as CSV',
        example: `curl -H "Authorization: Bearer TOKEN" \\
  /api/campaigns/1/export/csv -o export.csv`
      },
      { method: 'POST', path: '/api/campaigns/:id/publish', description: 'Direct publish to platforms (existing)' },
    ]
  },
  {
    title: '📊 Sheet Exports',
    open: false,
    endpoints: [
      { method: 'GET', path: '/api/sheet-exports', description: 'List export history',
        params: [
          { name: 'campaign_id', type: 'int', desc: 'Filter by campaign' },
          { name: 'profile_id', type: 'int', desc: 'Filter by profile' },
        ]
      },
    ]
  },
  {
    title: '⚙️ Jobs',
    open: false,
    endpoints: [
      { method: 'GET', path: '/jobs', description: 'List all jobs' },
      { method: 'GET', path: '/jobs/:id', description: 'Get job details with targets' },
      { method: 'POST', path: '/jobs/:id/cancel', description: 'Cancel a running job' },
      { method: 'POST', path: '/jobs/:id/retry', description: 'Retry failed targets' },
      { method: 'POST', path: '/jobs/run', description: 'Run pending jobs immediately' },
    ]
  },
  {
    title: '🔐 OAuth',
    open: false,
    endpoints: [
      { method: 'POST', path: '/oauth/tiktok/start', description: 'Start TikTok OAuth flow' },
      { method: 'POST', path: '/oauth/youtube/start', description: 'Start YouTube OAuth flow' },
      { method: 'POST', path: '/oauth/reddit/start', description: 'Start Reddit OAuth flow' },
      { method: 'POST', path: '/oauth/meta/start', description: 'Start Meta (FB/IG) OAuth flow' },
      { method: 'POST', path: '/oauth/threads/start', description: 'Start Threads OAuth flow' },
      { method: 'POST', path: '/oauth/twitter/start', description: 'Start Twitter/X OAuth flow' },
      { method: 'POST', path: '/oauth/patreon/start', description: 'Start Patreon OAuth flow' },
      { method: 'GET', path: '/admin/oauth/status', description: 'Check all OAuth statuses' },
    ]
  },
  {
    title: '🟠 Reddit Config',
    open: false,
    endpoints: [
      { method: 'POST', path: '/profiles/:id/accounts', description: 'Create Reddit account with subreddits',
        params: [
          { name: 'platform', type: 'string', desc: '"reddit"' },
          { name: 'account_name', type: 'string', desc: 'Account display name' },
          { name: 'auth_type', type: 'string', desc: '"oauth" or "cookie"' },
          { name: 'config.subreddits', type: 'array', desc: 'Array of subreddit names, e.g. ["videos", "funny"]' },
          { name: 'config.redditAuthType', type: 'string', desc: '"api" (OAuth) or "cookie"' },
          { name: 'config.clientIdEnv', type: 'string', desc: 'Env var name for Reddit client ID' },
          { name: 'config.clientSecretEnv', type: 'string', desc: 'Env var name for Reddit client secret' },
          { name: 'config.refreshTokenEnv', type: 'string', desc: 'Env var name for Reddit refresh token' },
        ]
      },
      { method: 'POST', path: '/publish-center/submit', description: 'Publish to Reddit (subreddits override)',
        params: [
          { name: 'accountDrafts[id].subreddits', type: 'array', desc: 'Override subreddits per account, e.g. ["gaming", "videos"]' },
          { name: 'accountDrafts[id].title', type: 'string', desc: 'Post title (max 300 chars)' },
          { name: 'accountDrafts[id].message', type: 'string', desc: 'Post body text' },
        ]
      },
    ]
  },
  {
    title: '📈 Analytics',
    open: false,
    endpoints: [
      { method: 'POST', path: '/analytics/sync', description: 'Trigger analytics sync' },
      { method: 'GET', path: '/analytics/overview', description: 'Get analytics overview' },
      { method: 'GET', path: '/analytics/videos', description: 'List analytics videos' },
      { method: 'GET', path: '/analytics/top-videos', description: 'Get top performing videos' },
      { method: 'POST', path: '/analytics/advice', description: 'Get AI-powered analytics advice' },
    ]
  },
])
</script>

<style scoped>
.api-docs {
  padding: 20px;
  max-width: 1000px;
  margin: 0 auto;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.api-section {
  margin-bottom: 16px;
  border: 1px solid var(--el-border-color, #ebeef5);
  border-radius: 8px;
  padding: 12px 16px;
}
.section-title {
  cursor: pointer;
  user-select: none;
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
}
.endpoint {
  margin: 8px 0 16px 0;
  padding: 12px;
  background: var(--el-fill-color-light, #f5f7fa);
  border-radius: 6px;
}
.endpoint-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.path {
  font-weight: 600;
  font-size: 14px;
}
.desc {
  color: var(--el-text-color-regular, #606266);
  font-size: 13px;
}
.endpoint-detail {
  margin-top: 8px;
  font-size: 13px;
}
.endpoint-detail pre {
  background: var(--el-fill-color-dark, #1e1e1e);
  color: var(--el-text-color-primary, #d4d4d4);
  padding: 12px;
  border-radius: 4px;
  overflow-x: auto;
  font-size: 12px;
  line-height: 1.5;
}
.endpoint-detail ul {
  margin: 4px 0;
  padding-left: 20px;
}
.endpoint-detail li {
  margin: 2px 0;
}
code {
  background: var(--el-fill-color, #e8e8e8);
  color: var(--el-text-color-primary);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 12px;
}
</style>
