<template>
  <div class="oauth-review-status">
    <div class="page-header">
      <h1>{{ title }}</h1>
      <el-button type="primary" @click="refreshStatus" :loading="loading">Refresh</el-button>
    </div>

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
          <template #header>Last request / callback</template>
          <div v-if="status.lastRequest || status.lastCallback" class="event-card">
            <div class="kv"><span>Last request</span><strong>{{ status.lastRequest?.requestedAt || '—' }}</strong></div>
            <div class="kv"><span>Request status</span><strong>{{ status.lastRequest?.status || status.lastStart?.status || '—' }}</strong></div>
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
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { oauthApi } from '@/api/oauth'

const route = useRoute()
const loading = ref(false)
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
  recentEvents: []
})

const platform = computed(() => {
  const raw = route.query.platform || route.params.platform
  return Array.isArray(raw) ? raw[0] : (raw || '')
})
const accountId = computed(() => {
  const raw = route.query.accountId
  if (!raw) return null
  const value = Array.isArray(raw) ? raw[0] : raw
  return value ? Number(value) : null
})
const title = computed(() => `${platform.value || 'OAuth'} status`)

async function refreshStatus() {
  if (!platform.value) return
  loading.value = true
  try {
    const response = await oauthApi.getStatus(platform.value, accountId.value)
    Object.assign(status, response?.data || {})
  } catch (error) {
    console.error('載入 OAuth status 失敗:', error)
    ElMessage.error(error?.message || '載入 OAuth status 失敗')
  } finally {
    loading.value = false
  }
}

onMounted(refreshStatus)
watch([platform, accountId], refreshStatus)
</script>

<style lang="scss" scoped>
.oauth-review-status {
  .page-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 20px;

    h1 {
      margin: 0;
      font-size: 24px;
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
}
</style>
