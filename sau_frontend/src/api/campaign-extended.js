import { http } from '@/utils/request'

export const campaignExtendedApi = {
  // Generate content for campaign
  generate: (campaignId, data = {}) => {
    return http.post(`/api/campaigns/${campaignId}/generate`, data)
  },

  // Validate campaign posts
  validate: (campaignId) => {
    return http.post(`/api/campaigns/${campaignId}/validate`)
  },

  // Approve campaign posts
  approve: (campaignId, data = {}) => {
    return http.post(`/api/campaigns/${campaignId}/approve`, data)
  },

  // List prepared posts
  listPosts: (campaignId, params = {}) => {
    return http.get(`/api/campaigns/${campaignId}/posts`, { params })
  },

  // Update prepared post
  updatePost: (campaignId, postId, data) => {
    return http.patch(`/api/campaigns/${campaignId}/posts/${postId}`, data)
  },

  // Export to Google Sheet
  exportSheet: (campaignId, data = {}) => {
    return http.post(`/api/campaigns/${campaignId}/export/google-sheet`, data)
  },

  // Export CSV (returns URL for download)
  exportCsvUrl: (campaignId) => {
    return `/api/campaigns/${campaignId}/export/csv`
  },
}
