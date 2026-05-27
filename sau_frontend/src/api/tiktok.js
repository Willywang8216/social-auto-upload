import { http } from '@/utils/request'

export const tiktokApi = {
  startOAuth(payload) {
    return http.post('/oauth/tiktok/start', payload)
  },

  getStatus(accountId = null) {
    return http.get('/admin/tiktok/status', accountId ? { accountId } : undefined)
  },

  getCreatorInfo(accountId) {
    return http.get(`/tiktok/creator-info/${accountId}`)
  },

  refreshStaleTokens(payload = {}) {
    return http.post('/accounts/tiktok/refresh-stale', payload)
  },

  getVideoInfo(filePath) {
    return http.post('/media/video-info', { file_path: filePath })
  },

  getPublishStatus(jobId) {
    return http.get(`/tiktok/publish-status/${jobId}`)
  }
}
