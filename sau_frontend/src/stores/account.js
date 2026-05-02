import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getLegacyPlatformType, getPlatformLabel, getPublishPlatformSlug } from '@/utils/platforms'

export const useAccountStore = defineStore('account', () => {
  // 存储所有账号信息
  const accounts = ref([])

  const mapStatusLabel = (value) => {
    if (value === -1) return '驗證中'
    if (value === 1 || value === 'ok' || value === 'ready') return '正常'
    return '異常'
  }

  const normalizeAccount = (item) => {
    if (Array.isArray(item)) {
      return {
        id: item[0],
        type: item[1],
        filePath: item[2],
        name: item[3],
        accountName: item[3],
        status: mapStatusLabel(item[4]),
        rawStatus: item[4],
        platformSlug: getPublishPlatformSlug(item[1]),
        platform: getPlatformLabel(item[1]),
        profileId: null,
        authType: 'cookie',
        config: {},
        enabled: true
      }
    }

    const platformSlug = item.platformSlug || item.platform || getPublishPlatformSlug(item.type)
    const accountName = item.accountName || item.account_name || item.name || `帳號 ${item.id}`
    const rawStatus = item.status ?? 0
    return {
      id: item.id,
      type: item.type ?? getLegacyPlatformType(platformSlug),
      filePath: item.filePath || item.cookiePath || item.cookie_path || '',
      name: accountName,
      accountName,
      status: mapStatusLabel(rawStatus),
      rawStatus,
      platformSlug,
      platform: getPlatformLabel(platformSlug),
      profileId: item.profileId ?? item.profile_id ?? null,
      authType: item.authType || item.auth_type || 'cookie',
      config: item.config || {},
      enabled: item.enabled ?? true
    }
  }

  // 设置账号列表
  const setAccounts = (accountsData) => {
    accounts.value = (accountsData || []).map(normalizeAccount)
  }
  
  // 添加账号
  const addAccount = (account) => {
    accounts.value.push(normalizeAccount(account))
  }
  
  // 更新账号
  const updateAccount = (id, updatedAccount) => {
    const index = accounts.value.findIndex(acc => acc.id === id)
    if (index !== -1) {
      accounts.value[index] = normalizeAccount({ ...accounts.value[index], ...updatedAccount })
    }
  }
  
  // 删除账号
  const deleteAccount = (id) => {
    accounts.value = accounts.value.filter(acc => acc.id !== id)
  }
  
  // 根据平台获取账号
  const getAccountsByPlatform = (platform) => {
    return accounts.value.filter(acc => acc.platform === platform)
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
