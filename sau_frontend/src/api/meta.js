import { http } from '@/utils/request'

export const metaApi = {
  startOAuth(payload) {
    return http.post('/oauth/meta/start', payload)
  }
}
