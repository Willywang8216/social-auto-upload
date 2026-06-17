<template>
  <div class="dashboard page-container">
    <!-- Page Header -->
    <div class="page-header">
      <h1>Dashboard</h1>
      <p>Overview of your social media accounts, materials, and publishing status.</p>
    </div>

    <!-- Stat Cards -->
    <el-row :gutter="16" class="stat-row">
      <el-col :xs="24" :sm="12" :lg="6">
        <div class="stat-card">
          <div class="stat-icon primary">
            <el-icon><User /></el-icon>
          </div>
          <div>
            <div class="stat-value">{{ accountStats.total }}</div>
            <div class="stat-label">Total Accounts</div>
          </div>
        </div>
      </el-col>
      <el-col :xs="24" :sm="12" :lg="6">
        <div class="stat-card">
          <div class="stat-icon success">
            <el-icon><Connection /></el-icon>
          </div>
          <div>
            <div class="stat-value">{{ healthSummary.ready }}</div>
            <div class="stat-label">Ready Connections</div>
          </div>
        </div>
      </el-col>
      <el-col :xs="24" :sm="12" :lg="6">
        <div class="stat-card">
          <div class="stat-icon warning">
            <el-icon><Document /></el-icon>
          </div>
          <div>
            <div class="stat-value">{{ contentStats.total }}</div>
            <div class="stat-label">Materials</div>
          </div>
        </div>
      </el-col>
      <el-col :xs="24" :sm="12" :lg="6">
        <div class="stat-card">
          <div class="stat-icon" :class="maintenanceStatus.running ? 'success' : (maintenanceStatus.enabled ? 'primary' : 'warning')">
            <el-icon><Timer /></el-icon>
          </div>
          <div>
            <div class="stat-value">{{ maintenanceStatus.running ? 'Running' : (maintenanceStatus.enabled ? 'Ready' : 'Off') }}</div>
            <div class="stat-label">Maintenance</div>
          </div>
        </div>
      </el-col>
    </el-row>

    <!-- Credential Expiry Alerts -->
    <div class="section" v-if="hasExpiryAlerts">
      <div class="data-table-card">
        <div class="data-table-header">
          <h2>Credential Expiry Alerts</h2>
          <el-button text size="small" @click="goToAccountQueue({ sort: 'urgency' })">View All</el-button>
        </div>
        <div style="padding: 16px 24px;">
          <div class="expiry-badges">
            <el-tag type="danger" v-if="healthSummary.expirySummary?.overdue">Overdue: {{ healthSummary.expirySummary.overdue }}</el-tag>
            <el-tag type="warning" v-if="healthSummary.expirySummary?.expiringWithin24h">24h: {{ healthSummary.expirySummary.expiringWithin24h }}</el-tag>
            <el-tag v-if="healthSummary.expirySummary?.expiringWithin7d">7d: {{ healthSummary.expirySummary.expiringWithin7d }}</el-tag>
            <el-tag type="danger" v-if="healthSummary.expirySummary?.reconnectRequired">Reconnect: {{ healthSummary.expirySummary.reconnectRequired }}</el-tag>
          </div>
        </div>
        <el-table :data="healthSummary.expiringAccounts || []" v-loading="loading" stripe>
          <el-table-column prop="platform" label="Platform" width="120" />
          <el-table-column prop="accountName" label="Account" min-width="180" />
          <el-table-column prop="expiresAt" label="Expires" width="220" />
          <el-table-column label="Remaining" width="120">
            <template #default="{ row }">
              {{ formatRemaining(row.secondsRemaining) }}
            </template>
          </el-table-column>
          <el-table-column label="Action" width="120">
            <template #default="{ row }">
              <el-button text size="small" @click="goToAccountQueue({ risk: row.requiresReconnect ? 'reconnect_required' : (row.secondsRemaining <= 24 * 3600 ? 'expiring_24h' : 'expiring_7d'), sort: row.requiresReconnect ? 'urgency' : 'expiry', platform: row.platform })">
                Go
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </div>

    <!-- Quick Actions -->
    <div class="section">
      <div class="section-title">Quick Actions</div>
      <el-row :gutter="16">
        <el-col :xs="12" :sm="8" :lg="4" v-for="action in quickActions" :key="action.path">
          <div class="action-card" @click="navigateTo(action.path)">
            <div class="action-icon">
              <el-icon><component :is="action.icon" /></el-icon>
            </div>
            <div class="action-title">{{ action.title }}</div>
            <div class="action-desc">{{ action.desc }}</div>
          </div>
        </el-col>
      </el-row>
    </div>

    <!-- Recent Materials -->
    <div class="section">
      <div class="data-table-card">
        <div class="data-table-header">
          <h2>Recent Materials</h2>
          <el-button text size="small" @click="navigateTo('/material-management')">View All</el-button>
        </div>
        <el-table :data="recentMaterials" v-loading="loading" stripe>
          <el-table-column prop="filename" label="Filename" min-width="280" />
          <el-table-column prop="filesize" label="Size" width="100">
            <template #default="{ row }">{{ row.filesize }} MB</template>
          </el-table-column>
          <el-table-column prop="upload_time" label="Uploaded" width="200" />
          <el-table-column label="Type" width="100">
            <template #default="{ row }">
              <el-tag :type="getFileTypeTag(row.filename)" size="small">{{ getFileType(row.filename) }}</el-tag>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-if="!loading && recentMaterials.length === 0" description="No materials yet" />
      </div>
    </div>

    <!-- Recent Account Events -->
    <div class="section">
      <div class="data-table-card">
        <div class="data-table-header">
          <h2>Recent Account Events</h2>
          <el-button text size="small" @click="navigateTo('/account-management')">Manage</el-button>
        </div>
        <el-table :data="healthSummary.recentEvents || []" v-loading="loading" stripe>
          <el-table-column prop="created_at" label="Time" width="180" />
          <el-table-column prop="platform" label="Platform" width="120" />
          <el-table-column prop="account_name" label="Account" min-width="180" />
          <el-table-column prop="action" label="Action" width="140" />
          <el-table-column label="Status" width="100">
            <template #default="{ row }">
              <el-tag :type="row.status === 'ok' ? 'success' : 'danger'" size="small">{{ row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="summary" label="Summary" min-width="240" />
        </el-table>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import {
  User, UserFilled, Document,
  Upload, Timer, DataAnalysis, Connection
} from '@element-plus/icons-vue'
import { accountApi } from '@/api/account'
import { materialApi } from '@/api/material'
import { useAccountStore } from '@/stores/account'
import { useAppStore } from '@/stores/app'
import { useProfilesStore } from '@/stores/profiles'
import { SUPPORTED_PLATFORM_TAGS } from '@/utils/platforms'
import { buildAccountQueueNavigationQuery } from '@/utils/accountQueueRouting'

const router = useRouter()
const accountStore = useAccountStore()
const appStore = useAppStore()
const profilesStore = useProfilesStore()
const loading = ref(false)
const healthSummary = ref({
  total: 0, ready: 0, configured: 0, missing: 0, refreshable: 0, checkable: 0,
  expirySummary: { overdue: 0, expiringWithin24h: 0, expiringWithin7d: 0, reconnectRequired: 0 },
  recentEventTotals: { total: 0, ok: 0, error: 0 },
  expiringAccounts: [], recentEvents: []
})
const maintenanceStatus = ref({ enabled: false, running: false, intervalSeconds: 0, lastFinishedAt: '', lastStartedAt: '', lastResult: null, lastError: null })

const hasExpiryAlerts = computed(() => {
  const s = healthSummary.value.expirySummary
  return (s?.overdue || s?.expiringWithin24h || s?.expiringWithin7d || s?.reconnectRequired) > 0
})

const quickActions = [
  { path: '/account-management', title: 'Accounts', desc: 'Manage accounts', icon: UserFilled },
  { path: '/material-management', title: 'Materials', desc: 'Upload media', icon: Upload },
  { path: '/publish-center', title: 'Publish', desc: 'Publish content', icon: Timer },
  { path: '/campaign-builder', title: 'Campaigns', desc: 'Build campaigns', icon: Document },
  { path: '/oauth-review', title: 'OAuth', desc: 'Connection status', icon: Connection },
  { path: '/about', title: 'About', desc: 'System info', icon: DataAnalysis },
]

const videoExtensions = ['.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv']
const imageExtensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']

const accountStats = computed(() => {
  const accounts = accountStore.accounts
  const normal = accounts.filter(a => a.status === '正常').length
  const abnormal = accounts.filter(a => a.status !== '正常' && a.status !== '驗證中').length
  return { total: accounts.length, normal, abnormal }
})

const contentStats = computed(() => {
  const materials = appStore.materials
  const videos = materials.filter(m => videoExtensions.some(ext => m.filename.toLowerCase().endsWith(ext))).length
  const images = materials.filter(m => imageExtensions.some(ext => m.filename.toLowerCase().endsWith(ext))).length
  return { total: materials.length, videos, images, others: materials.length - videos - images }
})

const recentMaterials = computed(() => {
  return [...appStore.materials].sort((a, b) => new Date(b.upload_time) - new Date(a.upload_time)).slice(0, 5)
})

const getFileType = (filename) => {
  if (videoExtensions.some(ext => filename.toLowerCase().endsWith(ext))) return 'Video'
  if (imageExtensions.some(ext => filename.toLowerCase().endsWith(ext))) return 'Image'
  return 'Other'
}

const getFileTypeTag = (filename) => {
  return { Video: 'success', Image: 'warning', Other: 'info' }[getFileType(filename)] || 'info'
}

const formatRemaining = (seconds) => {
  if (seconds == null) return '—'
  if (seconds <= 0) return 'expired'
  const hours = Math.floor(seconds / 3600)
  if (hours < 24) return `${hours}h`
  const days = Math.floor(hours / 24)
  return `${days}d`
}

const navigateTo = (path) => router.push(path)

const platformValueByLabel = computed(() => Object.fromEntries(
  SUPPORTED_PLATFORM_TAGS.map((p) => [p.label, p.key])
))

const goToAccountQueue = ({ risk = 'all', platform = 'all', profile = 'all', sort = 'urgency', sortOrder = 'ascending' } = {}) => {
  const query = buildAccountQueueNavigationQuery({ risk, platform, profile, sort, sortOrder, platformValueByLabel: platformValueByLabel.value })
  router.push({ path: '/account-management', query })
}

const nextMaintenanceRunLabel = computed(() => {
  const intervalSeconds = Number(maintenanceStatus.value?.intervalSeconds || 0)
  if (!maintenanceStatus.value?.enabled || intervalSeconds <= 0) return '—'
  if (maintenanceStatus.value?.running) return 'running now'
  const reference = maintenanceStatus.value?.lastFinishedAt || maintenanceStatus.value?.lastStartedAt
  if (!reference) return 'waiting for first run'
  const parsed = new Date(reference)
  if (Number.isNaN(parsed.getTime())) return '—'
  return new Date(parsed.getTime() + intervalSeconds * 1000).toISOString()
})

const fetchDashboardData = async () => {
  loading.value = true
  try {
    const [accountRes, materialRes, healthRes, maintenanceRes, profilesRes] = await Promise.allSettled([
      accountApi.getAccounts(),
      materialApi.getAllMaterials(),
      accountApi.getHealthSummary(),
      accountApi.getMaintenanceStatus(),
      profilesStore.refreshProfiles()
    ])

    const legacyAccounts = accountRes.status === 'fulfilled' && accountRes.value.code === 200
      ? (accountRes.value.data || []) : []
    let structuredAccounts = []
    if (profilesRes.status === 'fulfilled') {
      const structuredGroups = await Promise.all(
        (profilesRes.value || []).map(async (profile) => {
          const items = await profilesStore.fetchAccountsForProfile(profile.id)
          return items.map((item) => ({ ...item, profileName: profile.name }))
        })
      )
      structuredAccounts = structuredGroups.flat()
    }
    accountStore.setAccounts([...legacyAccounts, ...structuredAccounts])
    if (materialRes.status === 'fulfilled' && materialRes.value.code === 200) appStore.setMaterials(materialRes.value.data)
    if (healthRes.status === 'fulfilled' && healthRes.value.code === 200) healthSummary.value = healthRes.value.data
    if (maintenanceRes.status === 'fulfilled' && maintenanceRes.value.code === 200) maintenanceStatus.value = maintenanceRes.value.data
  } catch (error) {
    console.error('Dashboard fetch failed:', error)
  } finally {
    loading.value = false
  }
}

onMounted(() => fetchDashboardData())
</script>

<style lang="scss" scoped>
.dashboard {
  .stat-row {
    margin-bottom: var(--space-8);

    .el-col {
      margin-bottom: var(--space-4);
    }
  }

  .section {
    margin-bottom: var(--space-8);
  }

  .expiry-badges {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .action-card {
    margin-bottom: var(--space-4);
  }
}
</style>
