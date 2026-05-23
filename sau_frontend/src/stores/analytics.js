import { defineStore } from 'pinia'
import { ref, reactive } from 'vue'
import { analyticsApi } from '@/api/analytics'

export const useAnalyticsStore = defineStore('analytics', () => {
  const overview = ref(null)
  const videos = ref([])
  const topVideos = ref([])
  const trends = ref([])
  const syncStatus = ref([])
  const advice = ref(null)

  const loading = reactive({
    overview: false,
    videos: false,
    topVideos: false,
    trends: false,
    engagementTrends: false,
    sync: false,
    advice: false,
  })

  const filters = reactive({
    platform: null,
    accountId: null,
    dateFrom: null,
    dateTo: null,
  })

  async function fetchOverview() {
    loading.overview = true
    try {
      const res = await analyticsApi.getOverview({
        platform: filters.platform,
        accountId: filters.accountId,
        dateFrom: filters.dateFrom,
        dateTo: filters.dateTo,
      })
      overview.value = res?.data
    } finally {
      loading.overview = false
    }
  }

  async function fetchVideos(params = {}) {
    loading.videos = true
    try {
      const res = await analyticsApi.getVideos({
        platform: filters.platform,
        accountId: filters.accountId,
        ...params,
      })
      videos.value = res?.data || []
    } finally {
      loading.videos = false
    }
  }

  async function fetchTopVideos(params = {}) {
    loading.topVideos = true
    try {
      const res = await analyticsApi.getTopVideos({
        platform: filters.platform,
        accountId: filters.accountId,
        ...params,
      })
      topVideos.value = res?.data || []
    } finally {
      loading.topVideos = false
    }
  }

  async function fetchTrends(metric = 'views') {
    loading.trends = true
    try {
      const res = await analyticsApi.getTrends({
        platform: filters.platform,
        accountId: filters.accountId,
        dateFrom: filters.dateFrom,
        dateTo: filters.dateTo,
        metric,
      })
      trends.value = res?.data || []
    } finally {
      loading.trends = false
    }
  }

  async function syncNow(accountId = null) {
    loading.sync = true
    try {
      const res = accountId
        ? await analyticsApi.syncAccount(accountId)
        : await analyticsApi.syncAll()
      const jobId = res?.data?.jobId
      if (!jobId) return res?.data

      // Poll for completion (max 4 minutes)
      let result = null
      for (let i = 0; i < 120; i++) {
        await new Promise(r => setTimeout(r, 2000))
        const poll = await analyticsApi.syncJobStatus(jobId)
        const job = poll?.data
        if (job?.status === 'completed' || job?.status === 'error') {
          result = job.result
          break
        }
      }
      return result
    } finally {
      loading.sync = false
    }
  }

  async function fetchAdvice() {
    loading.advice = true
    try {
      const res = await analyticsApi.getAdvice({
        platform: filters.platform,
        accountId: filters.accountId,
        dateFrom: filters.dateFrom,
        dateTo: filters.dateTo,
      })
      advice.value = res?.data
    } finally {
      loading.advice = false
    }
  }

  async function fetchSyncStatus() {
    const res = await analyticsApi.getSyncStatus({ limit: 10 })
    syncStatus.value = res?.data || []
  }

  async function refreshAll() {
    await Promise.allSettled([
      fetchOverview(),
      fetchTopVideos(),
      fetchTrends(),
      fetchVideos(),
      fetchSyncStatus(),
    ])
  }

  return {
    overview,
    videos,
    topVideos,
    trends,
    syncStatus,
    advice,
    loading,
    filters,
    fetchOverview,
    fetchVideos,
    fetchTopVideos,
    fetchTrends,
    syncNow,
    fetchAdvice,
    fetchSyncStatus,
    refreshAll,
  }
})
