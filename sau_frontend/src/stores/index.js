import { createPinia } from 'pinia'
import { useUserStore } from './user'
import { useAccountStore } from './account'
import { useAppStore } from './app'
import { useProfilesStore } from './profiles'
import { useCampaignsStore } from './campaigns'

const pinia = createPinia()

export default pinia
export { useUserStore, useAccountStore, useAppStore, useProfilesStore, useCampaignsStore }
