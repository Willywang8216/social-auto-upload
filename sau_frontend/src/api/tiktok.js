import { http } from '@/utils/request'

export const tiktokApi = {
  startOAuth(payload) {
    return http.post('/oauth/tiktok/start', payload)
  },

  getStatus() {
    return http.get('/admin/tiktok/status')
  }
}
