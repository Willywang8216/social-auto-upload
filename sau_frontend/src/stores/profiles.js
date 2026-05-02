import { defineStore } from 'pinia'
import { computed, reactive, ref } from 'vue'

import { profilesApi } from '@/api/profiles'

export const useProfilesStore = defineStore('profiles', () => {
  const profiles = ref([])
  const accountsByProfile = reactive({})
  const activeProfileId = ref(null)

  const activeProfile = computed(() =>
    profiles.value.find((profile) => profile.id === activeProfileId.value) || null
  )

  async function refreshProfiles() {
    const response = await profilesApi.list()
    profiles.value = response?.data || []
    if (!activeProfileId.value && profiles.value.length > 0) {
      activeProfileId.value = profiles.value[0].id
    }
    return profiles.value
  }

  async function fetchAccountsForProfile(profileId, params = {}) {
    const response = await profilesApi.listAccounts(profileId, params)
    accountsByProfile[profileId] = response?.data || []
    return accountsByProfile[profileId]
  }

  function setActiveProfile(profileId) {
    activeProfileId.value = profileId
  }

  return {
    profiles,
    accountsByProfile,
    activeProfile,
    activeProfileId,
    refreshProfiles,
    fetchAccountsForProfile,
    setActiveProfile
  }
})
