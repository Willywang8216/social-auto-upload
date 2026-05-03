import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getLegacyPlatformType, getPlatformLabel, getPublishPlatformSlug } from '@/utils/platforms'

export const useAccountStore = defineStore('account', () => {
  const accounts = ref([])

  const mapStatusLabel = (value) => {
    if (value === -1) return '驗證中'
    if (value === 1 || value === 'ok' || value === 'ready') return '正常'
    return '異常'
  }

  const normalizeAccount = (item) => {
    if (Array.isArray(item)) {
      const platformSlug = getPublishPlatformSlug(item[1])
      return {
        id: item[0],
        type: item[1],
        filePath: item[2],
        name: item[3],
        accountName: item[3],
        status: mapStatusLabel(item[4]),
        rawStatus: item[4],
        platformSlug,
        platform: getPlatformLabel(item[1]),
        profileId: null,
        profileName: 'Legacy',
        authType: 'cookie',
        config: {},
        enabled: true,
        isLegacy: true,
        supportsCookieActions: Boolean(item[2]),
        supportsRelogin: getLegacyPlatformType(platformSlug) != null
      }
    }

    const platformSlug = item.platformSlug || item.platform || getPublishPlatformSlug(item.type)
    const accountName = item.accountName || item.account_name || item.name || `帳號 ${item.id}`
    const rawStatus = item.status ?? 0
    const profileId = item.profileId ?? item.profile_id ?? null
    const filePath = item.filePath || item.cookiePath || item.cookie_path || ''
    const isLegacy = item.isLegacy ?? profileId == null
    return {
      id: item.id,
      type: item.type ?? getLegacyPlatformType(platformSlug),
      filePath,
      name: accountName,
      accountName,
      status: mapStatusLabel(rawStatus),
      rawStatus,
      platformSlug,
      platform: getPlatformLabel(platformSlug),
      profileId,
      profileName: item.profileName || item.profile_name || (profileId == null ? 'Legacy' : ''),
      authType: item.authType || item.auth_type || 'cookie',
      config: item.config || {},
      enabled: item.enabled ?? true,
      isLegacy,
      supportsCookieActions: Boolean(filePath),
      supportsRelogin: Boolean(isLegacy && getLegacyPlatformType(platformSlug) != null)
    }
  }

  const setAccounts = (accountsData) => {
    accounts.value = (accountsData || []).map(normalizeAccount)
  }

  const addAccount = (account) => {
    accounts.value.push(normalizeAccount(account))
  }

  const updateAccount = (id, updatedAccount) => {
    const index = accounts.value.findIndex((acc) => acc.id === id)
    if (index !== -1) {
      accounts.value[index] = normalizeAccount({ ...accounts.value[index], ...updatedAccount })
    }
  }

  const deleteAccount = (id) => {
    accounts.value = accounts.value.filter((acc) => acc.id !== id)
  }

  const getAccountsByPlatform = (platform) => {
    return accounts.value.filter((acc) => acc.platform === platform)
  }

  return {
    accounts,
    setAccounts,
    addAccount,
    updateAccount,
    deleteAccount,
    getAccountsByPlatform
  }
})
