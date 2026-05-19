import { http } from '@/utils/request'

export const publishCenterApi = {
  preview(payload) {
    return http.post('/publish-center/preview', payload)
  },

  regenerate(payload) {
    return http.post('/publish-center/regenerate', payload)
  },

  submit(payload) {
    return http.post('/publish-center/submit', payload)
  }
}
