<template>
  <div class="video-analytics">
    <div class="page-header">
      <h1>影片分析</h1>
    </div>

    <!-- Filters & Sync -->
    <el-card class="filter-card">
      <div class="filter-row">
        <el-select
          v-model="store.filters.platform"
          placeholder="所有平台"
          clearable
          style="width: 160px"
          @change="onFilterChange"
        >
          <el-option label="YouTube" value="youtube" />
          <el-option label="TikTok" value="tiktok" />
          <el-option label="Facebook" value="facebook" />
          <el-option label="Instagram" value="instagram" />
          <el-option label="Threads" value="threads" />
        </el-select>

        <el-select
          v-model="store.filters.accountId"
          placeholder="所有帳號"
          clearable
          filterable
          style="width: 200px"
          @change="onFilterChange"
        >
          <el-option
            v-for="acc in oauthAccounts"
            :key="acc.id"
            :label="`${acc.platform} - ${acc.accountName || acc.name}`"
            :value="acc.id"
          />
        </el-select>

        <el-date-picker
          v-model="dateRange"
          type="daterange"
          range-separator="至"
          start-placeholder="開始日期"
          end-placeholder="結束日期"
          style="width: 280px"
          @change="onDateChange"
        />

        <div class="filter-spacer" />

        <el-button
          type="primary"
          :loading="store.loading.sync"
          @click="handleSync"
        >
          <el-icon v-if="!store.loading.sync"><Refresh /></el-icon>
          {{ store.loading.sync ? '同步中...' : '立即同步' }}
        </el-button>
      </div>
    </el-card>

    <!-- Summary Cards -->
    <el-row :gutter="20" class="summary-row">
      <el-col :span="6">
        <el-card class="stat-card" v-loading="store.loading.overview">
          <div class="stat-card-content">
            <div class="stat-icon views-icon"><el-icon><View /></el-icon></div>
            <div class="stat-info">
              <div class="stat-value">{{ formatNumber(store.overview?.total_views || 0) }}</div>
              <div class="stat-label">總觀看數</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="stat-card" v-loading="store.loading.overview">
          <div class="stat-card-content">
            <div class="stat-icon engagement-icon"><el-icon><Star /></el-icon></div>
            <div class="stat-info">
              <div class="stat-value">{{ formatNumber(totalEngagement) }}</div>
              <div class="stat-label">總互動數</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="stat-card" v-loading="store.loading.overview">
          <div class="stat-card-content">
            <div class="stat-icon rate-icon"><el-icon><TrendCharts /></el-icon></div>
            <div class="stat-info">
              <div class="stat-value">{{ formatPercent(store.overview?.avg_engagement_rate || 0) }}</div>
              <div class="stat-label">平均互動率</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="stat-card" v-loading="store.loading.overview">
          <div class="stat-card-content">
            <div class="stat-icon count-icon"><el-icon><VideoCamera /></el-icon></div>
            <div class="stat-info">
              <div class="stat-value">{{ store.overview?.video_count || 0 }}</div>
              <div class="stat-label">追蹤影片數</div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- Trends Charts -->
    <el-row :gutter="20" class="chart-row">
      <el-col :span="12">
        <el-card>
          <template #header><span>觀看趨勢</span></template>
          <AnalyticsChart :option="viewsChartOption" :height="280" :loading="store.loading.trends" />
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <template #header><span>互動率趨勢</span></template>
          <AnalyticsChart :option="engagementChartOption" :height="280" :loading="store.loading.engagementTrends" />
        </el-card>
      </el-col>
    </el-row>

    <!-- Platform Comparison -->
    <el-card class="section-card">
      <template #header><span>平台比較</span></template>
      <AnalyticsChart :option="platformChartOption" :height="250" :loading="store.loading.overview" />
    </el-card>

    <!-- Top Videos -->
    <el-card class="section-card">
      <template #header><span>表現最佳影片</span></template>
      <el-table :data="store.topVideos" v-loading="store.loading.topVideos" stripe style="width: 100%">
        <el-table-column label="縮圖" width="80">
          <template #default="{ row }">
            <img
              v-if="(row.thumbnail_url || row.platform === 'tiktok') && !failedThumbnails.has(row.platform_video_id)"
              :src="thumbnailSrc(row)"
              class="video-thumbnail"
              alt=""
              @error="failedThumbnails.add(row.platform_video_id)"
            />
            <span v-else class="no-thumbnail">-</span>
          </template>
        </el-table-column>
        <el-table-column prop="title" label="標題" min-width="200" show-overflow-tooltip />
        <el-table-column prop="platform" label="平台" width="100">
          <template #default="{ row }">
            <el-tag size="small">{{ row.platform }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="views" label="觀看" width="100" sortable>
          <template #default="{ row }">{{ formatNumber(row.views) }}</template>
        </el-table-column>
        <el-table-column prop="likes" label="讚" width="80" sortable>
          <template #default="{ row }">{{ formatNumber(row.likes) }}</template>
        </el-table-column>
        <el-table-column prop="comments" label="留言" width="80" sortable>
          <template #default="{ row }">{{ formatNumber(row.comments) }}</template>
        </el-table-column>
        <el-table-column prop="shares" label="分享" width="80" sortable>
          <template #default="{ row }">{{ formatNumber(row.shares) }}</template>
        </el-table-column>
        <el-table-column prop="engagement_rate" label="互動率" width="100" sortable>
          <template #default="{ row }">{{ formatPercent(row.engagement_rate) }}</template>
        </el-table-column>
        <el-table-column prop="published_at" label="發佈日期" width="140">
          <template #default="{ row }">{{ formatDate(row.published_at) }}</template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- All Videos -->
    <el-card class="section-card">
      <template #header><span>所有影片</span></template>
      <el-table :data="store.videos" v-loading="store.loading.videos" stripe style="width: 100%">
        <el-table-column label="縮圖" width="80">
          <template #default="{ row }">
            <img
              v-if="(row.thumbnail_url || row.platform === 'tiktok') && !failedThumbnails.has(row.platform_video_id)"
              :src="thumbnailSrc(row)"
              class="video-thumbnail"
              alt=""
              @error="failedThumbnails.add(row.platform_video_id)"
            />
            <span v-else class="no-thumbnail">-</span>
          </template>
        </el-table-column>
        <el-table-column prop="title" label="標題" min-width="200" show-overflow-tooltip />
        <el-table-column prop="platform" label="平台" width="100">
          <template #default="{ row }">
            <el-tag size="small">{{ row.platform }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="views" label="觀看" width="100" sortable>
          <template #default="{ row }">{{ formatNumber(row.views) }}</template>
        </el-table-column>
        <el-table-column prop="likes" label="讚" width="80" sortable>
          <template #default="{ row }">{{ formatNumber(row.likes) }}</template>
        </el-table-column>
        <el-table-column prop="comments" label="留言" width="80" sortable>
          <template #default="{ row }">{{ formatNumber(row.comments) }}</template>
        </el-table-column>
        <el-table-column prop="shares" label="分享" width="80" sortable>
          <template #default="{ row }">{{ formatNumber(row.shares) }}</template>
        </el-table-column>
        <el-table-column prop="engagement_rate" label="互動率" width="100" sortable>
          <template #default="{ row }">{{ formatPercent(row.engagement_rate) }}</template>
        </el-table-column>
        <el-table-column prop="published_at" label="發佈日期" width="140">
          <template #default="{ row }">{{ formatDate(row.published_at) }}</template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- AI Advisor -->
    <AdvisorPanel
      :advice="store.advice"
      :loading="store.loading.advice"
      @request-advice="handleGetAdvice"
    />
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, View, Star, TrendCharts, VideoCamera } from '@element-plus/icons-vue'
import { useAnalyticsStore } from '@/stores/analytics'
import { useAccountStore } from '@/stores/account'
import { useProfilesStore } from '@/stores/profiles'
import { accountApi } from '@/api/account'
import AnalyticsChart from '@/components/AnalyticsChart.vue'
import AdvisorPanel from '@/components/AdvisorPanel.vue'

const store = useAnalyticsStore()
const accountStore = useAccountStore()
const profilesStore = useProfilesStore()

const dateRange = ref(null)
const engagementTrends = ref([])
const failedThumbnails = reactive(new Set())

const SUPPORTED_ANALYTICS_PLATFORMS = ['youtube', 'tiktok', 'facebook', 'instagram', 'threads']

const oauthAccounts = computed(() => {
  return (accountStore.accounts || []).filter(
    a => a.authType === 'oauth'
      && SUPPORTED_ANALYTICS_PLATFORMS.includes(a.platformSlug)
      && (!store.filters.platform || a.platformSlug === store.filters.platform)
  )
})

const totalEngagement = computed(() => {
  const o = store.overview
  if (!o) return 0
  return (o.total_likes || 0) + (o.total_comments || 0) + (o.total_shares || 0)
})

// --- Chart options ---

const viewsChartOption = computed(() => {
  const data = store.trends || []
  return {
    tooltip: { trigger: 'axis' },
    grid: { left: 50, right: 20, top: 20, bottom: 30 },
    xAxis: { type: 'category', data: data.map(d => d.date) },
    yAxis: { type: 'value', name: '觀看數' },
    series: [{
      type: 'line',
      data: data.map(d => d.value),
      smooth: true,
      areaStyle: { opacity: 0.15 },
      itemStyle: { color: '#409eff' },
    }],
  }
})

const engagementChartOption = computed(() => {
  const data = engagementTrends.value || []
  return {
    tooltip: { trigger: 'axis', formatter: p => `${p[0].axisValue}<br/>${p[0].seriesName}: ${(p[0].value * 100).toFixed(2)}%` },
    grid: { left: 50, right: 20, top: 20, bottom: 30 },
    xAxis: { type: 'category', data: data.map(d => d.date) },
    yAxis: { type: 'value', name: '互動率', axisLabel: { formatter: v => (v * 100).toFixed(1) + '%' } },
    series: [{
      type: 'line',
      data: data.map(d => d.value),
      smooth: true,
      areaStyle: { opacity: 0.15 },
      itemStyle: { color: '#67c23a' },
    }],
  }
})

const platformChartOption = computed(() => {
  const pp = store.overview?.per_platform || {}
  const platforms = Object.keys(pp)
  return {
    tooltip: { trigger: 'axis' },
    grid: { left: 80, right: 20, top: 10, bottom: 30 },
    xAxis: { type: 'value' },
    yAxis: { type: 'category', data: platforms },
    series: [
      { name: '觀看', type: 'bar', data: platforms.map(p => pp[p]?.total_views || 0), itemStyle: { color: '#409eff' } },
      { name: '讚', type: 'bar', data: platforms.map(p => pp[p]?.total_likes || 0), itemStyle: { color: '#67c23a' } },
      { name: '留言', type: 'bar', data: platforms.map(p => pp[p]?.total_comments || 0), itemStyle: { color: '#e6a23c' } },
    ],
  }
})

// --- Formatters ---

function formatNumber(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M'
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K'
  return String(n)
}

function thumbnailSrc(row) {
  if (row.platform === 'tiktok') {
    return `/analytics/thumbnail/${row.platform_video_id}`
  }
  return row.thumbnail_url || ''
}

function formatPercent(r) {
  return (r * 100).toFixed(2) + '%'
}

function formatDate(iso) {
  if (!iso) return '-'
  try {
    return new Date(iso).toLocaleDateString('zh-TW')
  } catch {
    return iso
  }
}

// --- Actions ---

function onFilterChange() {
  store.refreshAll()
  fetchEngagementTrends()
}

function onDateChange(val) {
  if (val) {
    store.filters.dateFrom = val[0].toISOString()
    store.filters.dateTo = val[1].toISOString()
  } else {
    store.filters.dateFrom = null
    store.filters.dateTo = null
  }
  store.refreshAll()
  fetchEngagementTrends()
}

async function handleSync() {
  try {
    const result = await store.syncNow()
    const synced = result?.synced ?? 0
    const skipped = result?.skipped ?? 0
    const errorCount = result?.errors?.length ?? 0
    if (synced > 0) {
      ElMessage.success(`同步完成：${synced} 個帳號已同步`)
    } else if (skipped > 0) {
      const detail = result.skipped_details?.slice(0, 2).join('\n') || ''
      ElMessage.warning({ message: `${skipped} 個帳號被略過${detail ? ':\n' + detail : ''}`, duration: 8000, showClose: true })
    } else {
      ElMessage.info('無可同步的帳號')
    }
    if (errorCount > 0) {
      const errorDetail = result.errors?.slice(0, 3).join('\n') || ''
      ElMessage.warning({ message: `同步完成但有 ${errorCount} 個錯誤:\n${errorDetail}`, duration: 10000, showClose: true })
    }
    await store.refreshAll()
    await fetchEngagementTrends()
  } catch (e) {
    ElMessage.error('同步失敗：' + (e.message || e))
  }
}

async function handleGetAdvice() {
  try {
    await store.fetchAdvice()
  } catch (e) {
    ElMessage.error('取得建議失敗：' + (e.message || e))
  }
}

async function fetchEngagementTrends() {
  store.loading.engagementTrends = true
  try {
    const { analyticsApi } = await import('@/api/analytics')
    const res = await analyticsApi.getTrends({
      platform: store.filters.platform,
      accountId: store.filters.accountId,
      dateFrom: store.filters.dateFrom,
      dateTo: store.filters.dateTo,
      metric: 'engagement_rate',
    })
    engagementTrends.value = res?.data || []
  } catch {
    engagementTrends.value = []
  } finally {
    store.loading.engagementTrends = false
  }
}

// --- Init ---

onMounted(async () => {
  // Always load fresh accounts for analytics (never rely on stale store data)
  try {
    const profiles = await profilesStore.refreshProfiles()
    const legacyResponse = await accountApi.getAccounts()
    const legacyAccounts = legacyResponse?.data || []
    const structuredGroups = await Promise.all(
      profiles.map(async (profile) => {
        const items = await profilesStore.fetchAccountsForProfile(profile.id)
        return items.map((item) => ({ ...item, profileName: profile.name }))
      })
    )
    accountStore.setAccounts([...legacyAccounts, ...structuredGroups.flat()])
  } catch (e) {
    console.error('Failed to load accounts for analytics:', e)
  }
  await store.refreshAll()
  await fetchEngagementTrends()
})
</script>

<style scoped lang="scss">
@use '@/styles/variables.scss' as *;

.video-analytics {
  padding: 20px;
}

.page-header {
  margin-bottom: 20px;

  h1 {
    font-size: 24px;
    font-weight: 600;
    color: #303133;
  }
}

.filter-card {
  margin-bottom: 20px;
}

.filter-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.filter-spacer {
  flex: 1;
}

.summary-row {
  margin-bottom: 20px;
}

.stat-card {
  .stat-card-content {
    display: flex;
    align-items: center;
    gap: 16px;
  }

  .stat-icon {
    width: 48px;
    height: 48px;
    border-radius: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 24px;

    &.views-icon {
      background: #ecf5ff;
      color: #409eff;
    }
    &.engagement-icon {
      background: #fdf6ec;
      color: #e6a23c;
    }
    &.rate-icon {
      background: #f0f9eb;
      color: #67c23a;
    }
    &.count-icon {
      background: #fef0f0;
      color: #f56c6c;
    }
  }

  .stat-value {
    font-size: 24px;
    font-weight: 700;
    color: #303133;
  }

  .stat-label {
    font-size: 13px;
    color: #909399;
    margin-top: 4px;
  }
}

.chart-row {
  margin-bottom: 20px;
}

.section-card {
  margin-bottom: 20px;
}

.video-thumbnail {
  width: 56px;
  height: 32px;
  object-fit: cover;
  border-radius: 4px;
}

.no-thumbnail {
  color: #c0c4cc;
}
</style>
