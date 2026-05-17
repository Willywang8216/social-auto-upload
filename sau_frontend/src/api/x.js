import { http } from '@/utils/request'

export const twitterApi = {
  startOAuth(payload) {
    return http.post('/oauth/twitter/start', payload)
  },
}
