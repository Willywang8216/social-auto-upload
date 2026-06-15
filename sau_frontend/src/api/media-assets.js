import { http } from '@/utils/request'

export const mediaAssetApi = {
  // List media assets
  listAssets: (params = {}) => {
    return http.get('/api/media/assets', { params })
  },

  // Get single asset
  getAsset: (id) => {
    return http.get(`/api/media/assets/${id}`)
  },

  // Delete asset
  deleteAsset: (id) => {
    return http.delete(`/api/media/assets/${id}`)
  },

  // Batch upload files
  uploadBatch: (formData, onProgress) => {
    return http.upload('/api/media/upload/batch', formData, onProgress)
  },

  // Process asset (watermark, thumbnail, audio)
  processAsset: (id, data = {}) => {
    return http.post(`/api/media/assets/${id}/process`, data)
  },

  // Upload to rclone
  uploadToRclone: (id, data = {}) => {
    return http.post(`/api/media/assets/${id}/upload-rclone`, data)
  },
}
