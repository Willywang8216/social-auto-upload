import { defineStore } from 'pinia'
import { reactive } from 'vue'

import { campaignsApi } from '@/api/campaigns'

export const useCampaignsStore = defineStore('campaigns', () => {
  const campaignsById = reactive({})

  function storeCampaign(campaign) {
    if (!campaign || campaign.id == null) return null
    campaignsById[campaign.id] = campaign
    return campaign
  }

  async function createMediaGroup(payload) {
    const response = await campaignsApi.createMediaGroup(payload)
    return response?.data
  }

  async function prepareCampaign(payload) {
    const response = await campaignsApi.prepareCampaign(payload)
    return storeCampaign(response?.data)
  }

  async function fetchCampaign(campaignId) {
    const response = await campaignsApi.getCampaign(campaignId)
    return storeCampaign(response?.data)
  }

  async function updateCampaignPost(campaignId, postId, payload) {
    const response = await campaignsApi.updateCampaignPost(campaignId, postId, payload)
    const campaign = campaignsById[campaignId]
    if (campaign && Array.isArray(campaign.posts)) {
      const index = campaign.posts.findIndex((item) => item.id === postId)
      if (index !== -1) {
        campaign.posts[index] = response?.data
      }
    }
    return response?.data
  }

  async function publishCampaign(campaignId, payload = {}) {
    const response = await campaignsApi.publishCampaign(campaignId, payload)
    const campaign = response?.data?.campaign
    if (campaign) {
      storeCampaign(campaign)
    }
    return response?.data
  }

  return {
    campaignsById,
    createMediaGroup,
    prepareCampaign,
    fetchCampaign,
    updateCampaignPost,
    publishCampaign
  }
})
