import { http } from '@/utils/request'

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

  generateContent(data) {
    return http.post('/generateProfileContent', data)
  },

  generateBatchContent(data) {
    return http.post('/generateProfileBatchContent', data)
  },

  migrateMedia(data) {
    return http.post('/migrateProfileMedia', data)
  }
}
