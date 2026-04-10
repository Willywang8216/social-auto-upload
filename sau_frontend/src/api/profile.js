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

  previewProfilesYaml(yamlContent) {
    return http.post('/previewImportProfilesYaml', { yamlContent })
  },

  importProfilesYaml(input) {
    if (typeof input === 'string') {
      return http.post('/importProfilesYaml', { yamlContent: input })
    }

    const formData = new FormData()
    formData.append('file', input)
    return http.post('/importProfilesYaml', formData, {
      headers: {
        'Content-Type': 'multipart/form-data'
      }
    })
  },

  getExportProfilesYamlUrl() {
    return buildApiUrl('/exportProfilesYaml')
  },

  getExampleProfilesYamlUrl() {
    return buildApiUrl('/downloadProfileConfigExample')
  },

  getProfileBackupConfig() {
    return http.get('/getProfileBackupConfig')
  },

  saveProfileBackupConfig(data) {
    return http.post('/saveProfileBackupConfig', data)
  },

  runProfileBackup() {
    return http.post('/runProfileBackup', {})
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

  getDirectPublishersConfig() {
    return http.get('/getDirectPublishersConfig')
  },

  saveDirectPublishersConfig(data) {
    return http.post('/saveDirectPublishersConfig', data)
  },

  migrateMedia(data) {
    return http.post('/migrateProfileMedia', data)
  }
}
