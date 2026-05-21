import { http } from '@/utils/request'

export const patreonApi = {
  startOAuth(payload) {
    return http.post('/oauth/patreon/start', payload)
  }
}
