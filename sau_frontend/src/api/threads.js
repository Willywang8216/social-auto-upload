import { http } from '@/utils/request'

export const threadsApi = {
  startOAuth(payload) {
    return http.post('/oauth/threads/start', payload)
  }
}
