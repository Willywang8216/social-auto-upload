import { http } from '@/utils/request'

export const campaignsApi = {
  createMediaGroup(payload) {
    return http.post('/media-groups', payload)
  },

  listMediaGroups() {
    return http.get('/media-groups')
  },

  getMediaGroup(mediaGroupId) {
    return http.get(`/media-groups/${mediaGroupId}`)
  },

  prepareCampaign(payload) {
    return http.post('/campaigns/prepare', payload)
  },

  getCampaign(campaignId) {
    return http.get(`/campaigns/${campaignId}`)
  },

  updateCampaignPost(campaignId, postId, payload) {
    return http.patch(`/campaigns/${campaignId}/posts/${postId}`, payload)
  },

  publishCampaign(campaignId, payload = {}) {
    return http.post(`/campaigns/${campaignId}/publish`, payload)
  }
}
