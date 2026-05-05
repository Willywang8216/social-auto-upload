import { http } from '@/utils/request'

export const youtubeApi = {
  startOAuth(payload) {
    return http.post('/oauth/youtube/start', payload)
  }
}
