import { http } from '@/utils/request'

export const watermarkConfigApi = {
  // List watermark configs
  listConfigs: (params = {}) => {
    return http.get('/api/watermark-configs', { params })
  },

  // Create config
  createConfig: (data) => {
    return http.post('/api/watermark-configs', data)
  },

  // Get config
  getConfig: (id) => {
    return http.get(`/api/watermark-configs/${id}`)
  },

  // Update config
  updateConfig: (id, data) => {
    return http.patch(`/api/watermark-configs/${id}`, data)
  },

  // Delete config
  deleteConfig: (id) => {
    return http.delete(`/api/watermark-configs/${id}`)
  },
}
