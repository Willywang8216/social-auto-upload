import { http } from '@/utils/request'

export const socialApi = {
  publish(payload) {
    return http.post('/publishSocial', payload)
  }
}
