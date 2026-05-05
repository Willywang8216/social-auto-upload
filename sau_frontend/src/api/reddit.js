import { http } from '@/utils/request'

export const redditApi = {
  startOAuth(payload) {
    return http.post('/oauth/reddit/start', payload)
  }
}
