import { defineStore } from 'pinia'
import { ref } from 'vue'

const platformTypes = {
  1: { key: 'xiaohongshu', label: '小紅書' },
  2: { key: 'channels', label: '影片號' },
  3: { key: 'douyin', label: '抖音' },
  4: { key: 'kuaishou', label: '快手' }
}

const platformLabels = {
  twitter: 'X / Twitter',
  threads: 'Threads',
  instagram: 'Instagram',
  facebook: 'Facebook',
  reddit: 'Reddit',
  tiktok: 'TikTok',
  youtube: 'YouTube',
  xiaohongshu: '小紅書',
  channels: '影片號',
  douyin: '抖音',
  kuaishou: '快手'
}

const normalizeStatus = (statusCode) => {
  if (statusCode === -1) {
    return '驗證中'
  }
  if (statusCode === 1) {
    return '正常'
  }
  return '異常'
}

const normalizeAccount = (item) => {
  if (Array.isArray(item)) {
    const legacyType = Number(item[1] || 0)
    const platformConfig = platformTypes[legacyType] || { key: '', label: '未知平台' }
    const statusCode = Number(item[4] ?? 0)
    return {
      id: item[0],
      type: legacyType,
      legacyType,
      platformKey: platformConfig.key,
      filePath: item[2],
      name: item[3],
      statusCode,
      status: normalizeStatus(statusCode),
      platform: platformConfig.label,
      authMode: legacyType ? 'qr_cookie' : 'manual',
      metadata: {},
      supportsQrLogin: Boolean(legacyType),
      supportsCookieUpload: Boolean(legacyType),
      supportsValidation: Boolean(legacyType),
      isInternational: !legacyType
    }
  }

  const legacyType = Number(item.type ?? item.legacyType ?? 0)
  const statusCode = Number(item.statusCode ?? item.status ?? 0)
  const platformKey = item.platformKey || platformTypes[legacyType]?.key || ''
  return {
    id: item.id,
    type: legacyType,
    legacyType,
    platformKey,
    filePath: item.filePath || '',
    name: item.name || item.userName || '',
    statusCode,
    status: item.statusLabel || item.status || normalizeStatus(statusCode),
    platform: item.platform || platformLabels[platformKey] || platformTypes[legacyType]?.label || '未知平台',
    authMode: item.authMode || (legacyType ? 'qr_cookie' : 'manual'),
    metadata: item.metadata || {},
    supportsQrLogin: Boolean(item.supportsQrLogin),
    supportsCookieUpload: Boolean(item.supportsCookieUpload),
    supportsValidation: Boolean(item.supportsValidation),
    isInternational: Boolean(item.isInternational ?? !legacyType)
  }
}

export const useAccountStore = defineStore('account', () => {
  const accounts = ref([])

  const setAccounts = (accountsData) => {
    accounts.value = (accountsData || []).map(normalizeAccount)
  }

  const addAccount = (account) => {
    accounts.value.unshift(normalizeAccount(account))
  }

  const updateAccount = (id, updatedAccount) => {
    const index = accounts.value.findIndex(acc => acc.id === id)
    if (index !== -1) {
      accounts.value[index] = normalizeAccount({ ...accounts.value[index], ...updatedAccount })
    }
  }

  const deleteAccount = (id) => {
    accounts.value = accounts.value.filter(acc => acc.id !== id)
  }

  const getAccountsByPlatform = (platform) => {
    return accounts.value.filter(acc => acc.platform === platform || acc.platformKey === platform)
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
