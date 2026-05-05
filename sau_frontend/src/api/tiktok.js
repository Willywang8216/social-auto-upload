import { http } from '@/utils/request'

export const tiktokApi = {
  startOAuth(payload) {
    return http.post('/oauth/tiktok/start', payload)
  },

  getStatus(accountId = null) {
    return http.get('/admin/tiktok/status', accountId ? { accountId } : undefined)
  },

  refreshStaleTokens(payload = {}) {
    return http.post('/accounts/tiktok/refresh-stale', payload)
  }
}
