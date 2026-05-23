import { http } from '@/utils/request'

export const analyticsApi = {
  syncAll() {
    return http.post('/analytics/sync', {})
  },
  syncAccount(accountId) {
    return http.post('/analytics/sync', { accountId })
  },
  syncJobStatus(jobId) {
    return http.get('/analytics/sync/job', { jobId })
  },
  getSyncStatus(params = {}) {
    return http.get('/analytics/sync/status', params)
  },
  getOverview(params = {}) {
    return http.get('/analytics/overview', params)
  },
  getVideos(params = {}) {
    return http.get('/analytics/videos', params)
  },
  getVideoHistory(platformVideoId, params = {}) {
    return http.get(`/analytics/videos/${platformVideoId}/history`, params)
  },
  getTopVideos(params = {}) {
    return http.get('/analytics/top-videos', params)
  },
  getTrends(params = {}) {
    return http.get('/analytics/trends', params)
  },
  getAdvice(params = {}) {
    return http.post('/analytics/advice', params)
  },
}
