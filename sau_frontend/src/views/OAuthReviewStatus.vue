<template>
  <div class="oauth-review-status">
    <div class="page-header">
      <div class="page-header-left">
        <el-button v-if="platform" text @click="backToAll">
          <el-icon><ArrowLeft /></el-icon> 返回總覽
        </el-button>
        <h1>{{ title }}</h1>
        <el-select
          v-if="!platform"
          v-model="selectedPlatform"
          placeholder="選擇平台篩選"
          style="width: 200px; margin-left: 16px;"
          clearable
          @change="onPlatformChange"
        >
          <el-option v-for="p in OAUTH_PLATFORMS" :key="p.value" :label="p.label" :value="p.value" />
        </el-select>
      </div>
      <div class="header-actions">
        <el-button
          v-if="!platform && Object.keys(allStatuses).length > 0"
          type="warning"
          :loading="bulkRefreshing"
          @click="handleBulkReauth"
        >
          <el-icon><Refresh /></el-icon> 重新驗證全部
        </el-button>
        <el-button type="primary" @click="refreshStatus" :loading="loading">重新載入</el-button>
      </div>
    </div>

    <!-- All-platforms summary view -->
    <template v-if="!platform">
      <el-row :gutter="20">
        <el-col v-for="p in OAUTH_PLATFORMS" :key="p.value" :span="8" style="margin-bottom: 16px;">
          <el-card
            shadow="hover"
            :class="['platform-summary-card', { clickable: allStatuses[p.value] }]"
            @click="allStatuses[p.value] && router.push({ path: `/oauth-review/${p.value}` })"
          >
            <template #header>
              <div class="platform-card-header">
                <span>{{ p.label }}</span>
                <el-tag
                  v-if="allStatuses[p.value]"
                  :type="summaryTagType(allStatuses[p.value])"
                  size="small"
                >
                  {{ summaryTagLabel(allStatuses[p.value]) }}
                </el-tag>
              </div>
            </template>
            <template v-if="allStatuses[p.value]">
              <div class="kv compact"><span>Account</span><strong>{{ allStatuses[p.value].account?.account_name || '—' }}</strong></div>
              <div class="kv compact"><span>Last check</span><strong>{{ allStatuses[p.value].lastCallback?.created_at || '—' }}</strong></div>
              <div class="kv compact"><span>Expires</span><strong>{{ allStatuses[p.value].expiresAt || '—' }}</strong></div>
              <div v-if="allStatuses[p.value].recommendedAction" class="kv compact"><span>Action</span><strong>{{ actionLabel(allStatuses[p.value].recommendedAction) }}</strong></div>
            </template>
            <el-empty v-else description="尚未設定" :image-size="60" />
          </el-card>
        </el-col>
      </el-row>
    </template>

    <!-- Single-platform detail view -->
    <template v-if="platform">
    <el-row :gutter="20">
      <el-col :span="12">
        <el-card>
          <template #header>Configured OAuth</template>
          <div class="kv"><span>Platform</span><code>{{ status.platform || '—' }}</code></div>
          <div class="kv"><span>Redirect URI</span><code>{{ status.redirectUri || '—' }}</code></div>
          <div class="kv"><span>Account filter</span><code>{{ status.accountId || 'global' }}</code></div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <template #header>Products and scopes</template>
          <div class="chip-row">
            <el-tag v-for="product in status.selectedProducts || []" :key="product">{{ product }}</el-tag>
          </div>
          <div class="chip-row scopes">
            <el-tag v-for="scope in status.selectedScopes || []" :key="scope" type="success">{{ scope }}</el-tag>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" class="status-row">
      <el-col :span="12">
        <el-card>
          <template #header>Account lifecycle</template>
          <div v-if="status.account" class="event-card">
            <div class="kv"><span>Account</span><strong>{{ status.account.account_name || status.account.accountName || '—' }}</strong></div>
            <div class="kv"><span>Connected</span><strong>{{ status.account.config?.connectedAt || '—' }}</strong></div>
            <div class="kv"><span>Expires</span><strong>{{ status.expiresAt || '—' }}</strong></div>
            <div class="kv"><span>Reconnect</span><strong>{{ status.reconnectRequired ? 'yes' : 'no' }}</strong></div>
            <div class="kv"><span>Action</span><strong>{{ status.recommendedAction || '—' }}</strong></div>
            <div class="jump-actions">
              <el-button
                v-if="status.accountId && status.recommendedAction === 'refresh'"
                type="warning"
                size="small"
                :loading="refreshingToken"
                @click="handleRefreshToken"
              >
                Refresh Token
              </el-button>
              <el-button
                v-if="status.accountId && status.recommendedAction === 'reconnect'"
                type="danger"
                size="small"
                @click="goToAccountQueue"
              >
                重新連線
              </el-button>
              <el-button plain size="small" @click="goToAccountQueue">Open account queue</el-button>
            </div>
          </div>
          <el-empty v-else description="No account snapshot available" />
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <template #header>Credential snapshot</template>
          <div v-if="credentialRows.length > 0" class="event-card">
            <div v-for="row in credentialRows" :key="row.label" class="kv"><span>{{ row.label }}</span><strong>{{ row.value || '—' }}</strong></div>
          </div>
          <el-empty v-else description="No credential details available" />
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <template #header>Last request / callback</template>
          <div v-if="status.lastRequest || status.lastCallback" class="event-card">
            <div class="kv"><span>Last request</span><strong>{{ status.lastRequest?.requestedAt || '—' }}</strong></div>
            <div class="kv"><span>Request status</span><strong>{{ requestStatusLabel(status.lastRequest?.status || status.lastStart?.status) }}</strong></div>
            <div class="kv"><span>Last callback</span><strong>{{ status.lastCallback?.created_at || '—' }}</strong></div>
            <div class="kv"><span>Callback status</span><strong>{{ status.lastCallback?.status || '—' }}</strong></div>
            <div class="kv"><span>Summary</span><strong>{{ status.lastCallback?.summary || '—' }}</strong></div>
          </div>
          <el-empty v-else description="No OAuth activity yet" />
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <template #header>Last refresh</template>
          <div v-if="status.lastRefresh" class="event-card">
            <div class="kv"><span>Time</span><strong>{{ status.lastRefresh.created_at || '—' }}</strong></div>
            <div class="kv"><span>Status</span><strong>{{ status.lastRefresh.status || '—' }}</strong></div>
            <div class="kv"><span>Summary</span><strong>{{ status.lastRefresh.summary || '—' }}</strong></div>
          </div>
          <el-empty v-else description="No refresh yet" />
        </el-card>
      </el-col>
    </el-row>

    <el-card class="events-card">
      <template #header>Recent OAuth events</template>
      <el-table :data="status.recentEvents || []" style="width: 100%">
        <el-table-column prop="created_at" label="Time" width="180" />
        <el-table-column prop="action" label="Action" width="140" />
        <el-table-column prop="status" label="Status" width="100" />
        <el-table-column prop="summary" label="Summary" min-width="260" />
      </el-table>
    </el-card>
    </template>
  </div>
</template>

<script setup>
import { ArrowLeft, Refresh } from '@element-plus/icons-vue'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { oauthApi } from '@/api/oauth'
import { profilesApi } from '@/api/profiles'
import { buildAccountQueueNavigationQuery } from '@/utils/accountQueueRouting'

const OAUTH_PLATFORMS = [
  { value: 'youtube', label: 'YouTube' },
  { value: 'reddit', label: 'Reddit' },
  { value: 'facebook', label: 'Facebook' },
  { value: 'instagram', label: 'Instagram' },
  { value: 'threads', label: 'Threads' },
  { value: 'tiktok', label: 'TikTok' },
]

const route = useRoute()
const router = useRouter()
const loading = ref(false)
const bulkRefreshing = ref(false)
const refreshingToken = ref(false)
const selectedPlatform = ref('')
const allStatuses = reactive({})
const status = reactive({
  platform: '',
  accountId: null,
  redirectUri: '',
  selectedProducts: [],
  selectedScopes: [],
  lastRequest: null,
  lastStart: null,
  lastCallback: null,
  lastRefresh: null,
  recentEvents: [],
  account: null,
  expiresAt: '',
  reconnectRequired: false,
  recommendedAction: ''
})

const platform = computed(() => {
  const raw = route.query.platform || route.params.platform || ''
  return Array.isArray(raw) ? raw[0] : (raw || '')
})
const accountId = computed(() => {
  const raw = route.query.accountId
  if (!raw) return null
  const value = Array.isArray(raw) ? raw[0] : raw
  return value ? Number(value) : null
})
const credentialRows = computed(() => {
  const config = status.account?.config || {}
  const rows = [
    { label: 'Access token updated', value: config.accessTokenUpdatedAt || '' },
    { label: 'Last manual refresh', value: config.lastManualRefreshAt || '' },
    { label: 'Last auto refresh', value: config.lastAutoRefreshAt || '' },
    { label: 'Last connection check', value: config.lastConnectionCheckAt || '' }
  ]

  if (platform.value === 'reddit') {
    rows.unshift(
      { label: 'Username', value: config.redditUserName || '' },
      { label: 'Scope', value: config.scope || '' }
    )
  } else if (platform.value === 'youtube') {
    rows.unshift(
      { label: 'Channel title', value: config.channelTitle || '' },
      { label: 'Channel ID', value: config.channelId || '' }
    )
  } else if (platform.value === 'facebook') {
    rows.unshift(
      { label: 'Page name', value: config.facebookPageName || '' },
      { label: 'Page ID', value: config.pageId || '' }
    )
  } else if (platform.value === 'instagram') {
    rows.unshift(
      { label: 'Username', value: config.instagramUserName || '' },
      { label: 'IG user ID', value: config.igUserId || '' }
    )
  } else if (platform.value === 'threads') {
    rows.unshift(
      { label: 'Username', value: config.threadsUserName || '' },
      { label: 'User ID', value: config.threadUserId || config.userId || '' }
    )
  } else if (platform.value === 'tiktok') {
    rows.unshift(
      { label: 'Display name', value: config.tiktokDisplayName || config.displayName || '' },
      { label: 'Open ID', value: config.tiktokOpenId || config.openId || '' }
    )
  }

  return rows.filter((row) => row.value)
})

const title = computed(() => platform.value ? `${platform.value} status` : 'OAuth 狀態總覽')

function backToAll() {
  router.replace({ path: '/oauth-review' })
}

const STATUS_LABELS = {
  completed: '已完成',
  pending_ig_selection: '等待選擇 IG 帳號',
  pending_page_selection: '等待選擇粉絲專頁',
  started: '進行中',
  error: '失敗',
}

const ACTION_LABELS = {
  reconnect: '需重新連線',
  refresh: '需重新整理 Token',
  connect: '需連線帳號',
}

function requestStatusLabel(status) {
  return STATUS_LABELS[status] || status || '—'
}

function actionLabel(action) {
  return ACTION_LABELS[action] || action || '—'
}

function summaryTagType(s) {
  if (!s) return 'info'
  if (s.account) {
    return s.reconnectRequired ? 'danger' : 'success'
  }
  const cbStatus = s.lastCallback?.status
  if (cbStatus === 'ok') return 'success'
  if (cbStatus && isPendingStatus(s.lastRequest?.status)) return 'warning'
  if (cbStatus) return 'danger'
  return 'info'
}

function summaryTagLabel(s) {
  if (!s) return '無資料'
  if (s.account) {
    return s.reconnectRequired ? '需重新連線' : '已連線'
  }
  const cbStatus = s.lastCallback?.status
  if (cbStatus === 'ok') return '連線正常'
  if (cbStatus && isPendingStatus(s.lastRequest?.status)) return '進行中'
  if (cbStatus) return '連線失敗'
  return '無資料'
}

function isPendingStatus(status) {
  return status === 'pending_ig_selection' || status === 'pending_page_selection'
}

function goToAccountQueue() {
  const query = buildAccountQueueNavigationQuery({
    risk: status.reconnectRequired
      ? 'reconnect_required'
      : (status.recommendedAction === 'refresh' && status.expiresAt ? 'expiring_7d' : 'all'),
    platform: platform.value || 'all',
    profile: status.account?.profile_id != null ? String(status.account.profile_id) : 'all',
    sort: status.reconnectRequired ? 'urgency' : 'expiry',
  })
  router.push({ path: '/account-management', query })
}

async function handleBulkReauth() {
  const accountsToRefresh = OAUTH_PLATFORMS
    .filter(p => allStatuses[p.value]?.accountId)
    .map(p => allStatuses[p.value].accountId)
  if (!accountsToRefresh.length) {
    ElMessage.warning('目前沒有可重新驗證的 OAuth 帳號')
    return
  }
  bulkRefreshing.value = true
  try {
    await profilesApi.batchRefreshTokens(accountsToRefresh)
    ElMessage.success(`已對 ${accountsToRefresh.length} 個帳號發送重新驗證要求`)
    await fetchAllStatuses()
  } catch (e) {
    ElMessage.error('批量重新驗證失敗：' + (e.message || e))
  } finally {
    bulkRefreshing.value = false
  }
}

async function handleRefreshToken() {
  if (!status.accountId) return
  refreshingToken.value = true
  try {
    await profilesApi.refreshAccountToken(status.accountId)
    ElMessage.success('Token 已重新整理')
    await refreshStatus()
  } catch (e) {
    ElMessage.error('Token 重新整理失敗：' + (e.message || e))
  } finally {
    refreshingToken.value = false
  }
}

function onPlatformChange(value) {
  if (value) {
    router.push({ path: `/oauth-review/${value}` })
  }
}

async function fetchAllStatuses() {
  const results = await Promise.allSettled(
    OAUTH_PLATFORMS.map(async (p) => {
      try {
        const response = await oauthApi.getStatus(p.value, null)
        if (response?.data) {
          allStatuses[p.value] = response.data
        }
      } catch {
        allStatuses[p.value] = null
      }
    })
  )
}

async function refreshStatus() {
  loading.value = true
  try {
    if (platform.value) {
      const response = await oauthApi.getStatus(platform.value, accountId.value)
      Object.assign(status, response?.data || {})
    } else {
      await fetchAllStatuses()
    }
  } catch (error) {
    console.error('載入 OAuth status 失敗:', error)
    ElMessage.error(error?.message || '載入 OAuth status 失敗')
  } finally {
    loading.value = false
  }
}

onMounted(refreshStatus)
watch([platform, accountId], () => {
  if (platform.value) {
    refreshStatus()
  }
})
</script>

<style lang="scss" scoped>
.oauth-review-status {
  .page-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 20px;

    .page-header-left {
      display: flex;
      align-items: center;
    }

    h1 {
      margin: 0;
      font-size: 24px;
    }
  }

  .header-actions {
    display: flex;
    align-items: center;
    gap: var(--space-3);
  }

  .platform-summary-card {
    cursor: default;

    &.clickable {
      cursor: pointer;
    }

    .platform-card-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
    }

    .kv.compact {
      margin-bottom: 6px;
      font-size: 13px;

      span {
        width: 80px;
      }
    }
  }

  .status-row,
  .events-card {
    margin-top: 20px;
  }

  .jump-actions {
    margin-top: 12px;
  }

  .kv {
    display: flex;
    gap: 12px;
    align-items: flex-start;
    margin-bottom: 10px;

    span {
      width: 110px;
      color: #909399;
      flex-shrink: 0;
    }

    code,
    strong {
      word-break: break-all;
    }
  }

  .chip-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 12px;
  }
}
</style>