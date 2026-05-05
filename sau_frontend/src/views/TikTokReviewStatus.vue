<template>
  <div class="tiktok-review-status">
    <div class="page-header">
      <h1>TikTok callback status</h1>
      <div class="header-actions">
      <el-button plain @click="refreshStaleTokens" :loading="refreshingStale">Refresh stale tokens</el-button>
      <el-button type="primary" @click="refreshStatus" :loading="loading">Refresh</el-button>
    </div>
    </div>

    <el-row :gutter="20">
      <el-col :span="12">
        <el-card>
          <template #header>Configured URIs</template>
          <div class="kv"><span>Domain</span><code>{{ status.domain || '—' }}</code></div>
          <div class="kv"><span>Redirect URI</span><code>{{ status.redirectUri || '—' }}</code></div>
          <div class="kv"><span>Webhook URI</span><code>{{ status.webhookUri || '—' }}</code></div>
          <div class="kv"><span>Account filter</span><code>{{ status.accountId || 'global' }}</code></div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <template #header>Selected products and scopes</template>
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
          <template #header>Last request / callback</template>
          <div v-if="status.lastCallback || status.lastRequest" class="event-card">
            <div class="kv"><span>Last request</span><strong>{{ status.lastRequest?.requestedAt || '—' }}</strong></div>
            <div class="kv"><span>Request status</span><strong>{{ status.lastRequest?.status || '—' }}</strong></div>
            <div class="kv"><span>Received</span><strong>{{ status.lastCallback?.receivedAt || '—' }}</strong></div>
            <div class="kv"><span>Status</span><strong>{{ status.lastCallback?.status || '—' }}</strong></div>
            <div class="kv"><span>Account</span><strong>{{ status.lastCallback?.accountName || status.lastRequest?.accountName || '—' }}</strong></div>
            <div class="kv"><span>Display name</span><strong>{{ status.lastCallback?.displayName || '—' }}</strong></div>
            <div class="kv"><span>Open ID</span><code>{{ status.lastCallback?.openId || '—' }}</code></div>
            <div class="kv"><span>Scope</span><code>{{ status.lastCallback?.scope || status.lastRequest?.scopes?.join(', ') || '—' }}</code></div>
          </div>
          <el-empty v-else description="No callback received yet" />
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <template #header>Last refresh / webhook</template>
          <div v-if="status.lastWebhook || status.lastRefresh" class="event-card">
            <div class="kv"><span>Last refresh</span><strong>{{ status.lastRefresh?.receivedAt || '—' }}</strong></div>
            <div class="kv"><span>Refresh status</span><strong>{{ status.lastRefresh?.status || '—' }}</strong></div>
            <div class="kv"><span>Received</span><strong>{{ status.lastWebhook?.receivedAt || '—' }}</strong></div>
            <div class="kv"><span>Signature</span><strong>{{ status.lastWebhook?.signatureStatus || '—' }}</strong></div>
            <div class="kv"><span>Verified</span><strong>{{ status.lastWebhook?.signatureVerified ? 'yes' : 'no' }}</strong></div>
            <div class="payload-preview">
              <pre>{{ pretty(status.lastWebhook?.payload) }}</pre>
            </div>
          </div>
          <el-empty v-else description="No webhook or refresh yet" />
        </el-card>
      </el-col>
    </el-row>

    <el-card class="events-card">
      <template #header>Recent TikTok events</template>
      <el-table :data="status.recentEvents || []" style="width: 100%">
        <el-table-column prop="receivedAt" label="Received" width="180" />
        <el-table-column prop="type" label="Type" width="120" />
        <el-table-column prop="status" label="Status" width="120" />
        <el-table-column label="Summary">
          <template #default="scope">
            <span>{{ eventSummary(scope.row) }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute } from 'vue-router'
import { tiktokApi } from '@/api/tiktok'

const route = useRoute()
const loading = ref(false)
const refreshingStale = ref(false)
const status = reactive({
  domain: '',
  redirectUri: '',
  webhookUri: '',
  selectedProducts: [],
  selectedScopes: [],
  accountId: null,
  lastRequest: null,
  lastCallback: null,
  lastRefresh: null,
  lastWebhook: null,
  recentEvents: []
})
let timer = null

function pretty(value) {
  if (!value) return '—'
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function eventSummary(event) {
  if (event.type === 'start') return `${event.accountName || 'TikTok'} requested ${event.scopes?.join(', ') || ''}`
  if (event.type === 'callback') return `${event.accountName || 'TikTok'} ${event.status}`
  if (event.type === 'refresh') return `${event.accountName || 'TikTok'} token refresh ${event.status}`
  if (event.type === 'webhook') return `${event.signatureStatus || 'received'} webhook event`
  return '—'
}

const scopedAccountId = computed(() => {
  const raw = route.query.accountId
  if (!raw) return null
  const value = Array.isArray(raw) ? raw[0] : raw
  return value ? Number(value) : null
})

async function refreshStaleTokens() {
  refreshingStale.value = true
  try {
    const response = await tiktokApi.refreshStaleTokens(
      scopedAccountId.value ? { accountId: scopedAccountId.value } : {}
    )
    const data = response?.data || {}
    ElMessage.success(`Checked ${data.examined || 0} account(s), refreshed ${data.refreshed || 0}`)
    await refreshStatus()
  } catch (error) {
    console.error('刷新 stale TikTok tokens 失敗:', error)
    ElMessage.error(error?.message || '刷新 stale TikTok tokens 失敗')
  } finally {
    refreshingStale.value = false
  }
}

async function refreshStatus() {
  loading.value = true
  try {
    const response = await tiktokApi.getStatus(scopedAccountId.value)
    Object.assign(status, response?.data || {})
  } catch (error) {
    console.error('載入 TikTok callback status 失敗:', error)
    ElMessage.error('載入 TikTok callback status 失敗')
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  refreshStatus()
  timer = window.setInterval(refreshStatus, 5000)
})

watch(scopedAccountId, () => {
  refreshStatus()
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<style lang="scss" scoped>
.tiktok-review-status {
  .page-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 20px;

    h1 {
      margin: 0;
      font-size: 24px;
    }

    .header-actions {
      display: flex;
      gap: 12px;
      align-items: center;
    }
  }

  .status-row,
  .events-card {
    margin-top: 20px;
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

  .payload-preview {
    margin-top: 12px;

    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      background: #f5f7fa;
      padding: 12px;
      border-radius: 6px;
      font-size: 12px;
      line-height: 1.6;
    }
  }
}
</style>
