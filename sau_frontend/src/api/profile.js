import { http } from '@/utils/request'
import { buildApiUrl } from '@/utils/apiBase'

export const profileApi = {
  getProfiles() {
    return http.get('/getProfiles')
  },

  saveProfile(data) {
    return http.post('/saveProfile', data)
  },

  deleteProfile(id) {
    return http.get(`/deleteProfile?id=${id}`)
  },

  importProfilesYaml(file) {
    const formData = new FormData()
    formData.append('file', file)
    return http.post('/importProfilesYaml', formData, {
      headers: {
        'Content-Type': 'multipart/form-data'
      }
    })
  },

  getExportProfilesYamlUrl() {
    return buildApiUrl('/exportProfilesYaml')
  },

  generateContent(data) {
    return http.post('/generateProfileContent', data)
  },

  generateBatchContent(data) {
    return http.post('/generateProfileBatchContent', data)
  },

  getGoogleSheetConfig() {
    return http.get('/getGoogleSheetConfig')
  },

  saveGoogleSheetConfig(data) {
    return http.post('/saveGoogleSheetConfig', data)
  },

  validateGoogleSheetConfig(data) {
    return http.post('/validateGoogleSheetConfig', data)
  },

  migrateMedia(data) {
    return http.post('/migrateProfileMedia', data)
  }
}
