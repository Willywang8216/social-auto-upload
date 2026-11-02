import { http } from '@/utils/request'

export const metaApi = {
  startOAuth(payload) {
    return http.post('/oauth/meta/start', payload)
  },
  completeSelection({ state, selectedId }) {
    // No browser token/page payload is an authority for OAuth completion.
    return http.post('/oauth/meta/select', { state, selectedId })
  }
}
