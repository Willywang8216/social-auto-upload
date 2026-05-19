import { http } from '@/utils/request'

export const publishTemplatesApi = {
  list() {
    return http.get('/publish-templates')
  },

  get(templateId) {
    return http.get(`/publish-templates/${templateId}`)
  },

  create(payload) {
    return http.post('/publish-templates', payload)
  },

  update(templateId, payload) {
    return http.patch(`/publish-templates/${templateId}`, payload)
  },

  delete(templateId) {
    return http.delete(`/publish-templates/${templateId}`)
  }
}
