import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getLegacyPlatformType, getPlatformLabel, getPublishPlatformSlug } from '@/utils/platforms'

const HEALTH_REFRESH_PLATFORMS = new Set(['tiktok', 'reddit', 'youtube', 'threads'])
const HEALTH_CHECK_PLATFORMS = new Set(['facebook', 'instagram', 'telegram', 'discord'])

export const useAccountStore = defineStore('account', () => {
  const accounts = ref([])

  const parseIsoDate = (value) => {
    if (!value) return null
    try {
      const normalized = String(value).endsWith('Z') ? String(value).replace(/Z$/, '+00:00') : String(value)
      const parsed = new Date(normalized)
      return Number.isNaN(parsed.getTime()) ? null : parsed
    } catch {
      return null
    }
  }

  const deriveExpiryMeta = (platformSlug, config = {}, isLegacy = false) => {
    if (isLegacy) {
      return {
        expiresAt: '',
        secondsRemaining: null,
        isOverdue: false,
        isExpiringWithin24h: false,
        isExpiringWithin7d: false,
        reconnectRequired: false,
        expiryRecommendedAction: ''
      }
    }

    const isMeta = ['facebook', 'instagram'].includes(platformSlug)
    const refreshable = HEALTH_REFRESH_PLATFORMS.has(platformSlug) || (isMeta && Boolean(config.metaUserAccessToken))
    const expiryRaw = isMeta ? (config.metaUserAccessTokenExpiresAt || config.accessTokenExpiresAt || '') : (config.accessTokenExpiresAt || '')
    const expiryDate = parseIsoDate(expiryRaw)
    if (!refreshable || !expiryDate) {
      return {
        expiresAt: expiryRaw || '',
        secondsRemaining: null,
        isOverdue: false,
        isExpiringWithin24h: false,
        isExpiringWithin7d: false,
        reconnectRequired: false,
        expiryRecommendedAction: '',
        urgencyRank: 99,
        urgencyLabel: ''
      }
    }

    const secondsRemaining = Math.floor((expiryDate.getTime() - Date.now()) / 1000)
    const isOverdue = secondsRemaining <= 0
    const isExpiringWithin24h = secondsRemaining > 0 && secondsRemaining <= 24 * 3600
    const isExpiringWithin7d = secondsRemaining > 0 && secondsRemaining <= 7 * 24 * 3600
    const reconnectRequired = Boolean(isMeta && isOverdue)
    let urgencyRank = 99
    let urgencyLabel = ''
    if (reconnectRequired) {
      urgencyRank = 0
      urgencyLabel = 'reconnect_required'
    } else if (isOverdue) {
      urgencyRank = 1
      urgencyLabel = 'overdue'
    } else if (isExpiringWithin24h) {
      urgencyRank = 2
      urgencyLabel = 'expiring_24h'
    } else if (isExpiringWithin7d) {
      urgencyRank = 3
      urgencyLabel = 'expiring_7d'
    }
    return {
      expiresAt: expiryDate.toISOString(),
      secondsRemaining,
      isOverdue,
      isExpiringWithin24h,
      isExpiringWithin7d,
      reconnectRequired,
      expiryRecommendedAction: reconnectRequired ? 'reconnect' : 'refresh',
      urgencyRank,
      urgencyLabel
    }
  }

  const mapStatusLabel = (value) => {
    if (value === -1) return '驗證中'
    if (value === 1 || value === 'ok' || value === 'ready') return '正常'
    return '異常'
  }

  const deriveConnectionMeta = (platformSlug, config = {}, isLegacy = false) => {
    if (isLegacy) {
      return {
        connectionLabel: 'Legacy',
        connectionTagType: 'info',
        connectionTimestamp: '',
        connectionDetail: '',
        supportsHealthAction: false,
        healthActionKind: null
      }
    }

    const timestamp = config.lastConnectionCheckAt || config.lastManualRefreshAt || config.lastAutoRefreshAt || config.connectedAt || config.accessTokenUpdatedAt || ''
    let detail = ''
    if (platformSlug === 'facebook') detail = config.facebookPageName || ''
    else if (platformSlug === 'instagram') detail = config.instagramUserName || ''
    else if (platformSlug === 'threads') detail = config.threadsUserName || ''
    else if (platformSlug === 'telegram') detail = config.telegramChatTitle || config.telegramBotName || ''
    else if (platformSlug === 'discord') detail = config.discordWebhookName || config.discordWebhookChannel || ''
    else if (platformSlug === 'reddit') detail = config.redditUserName || ''
    else if (platformSlug === 'youtube') detail = config.channelTitle || ''
    else if (platformSlug === 'tiktok') detail = config.displayName || config.openId || ''

    const hasCredential = Boolean(
      config.accessToken ||
      config.accessTokenEnv ||
      config.botTokenEnv ||
      config.webhookUrlEnv
    )
    const hasValidatedIdentity = Boolean(detail)

    let connectionLabel = 'Missing'
    let connectionTagType = 'danger'
    if (hasValidatedIdentity || timestamp) {
      connectionLabel = 'Ready'
      connectionTagType = 'success'
    } else if (hasCredential) {
      connectionLabel = 'Configured'
      connectionTagType = 'warning'
    }

    const metaRefreshable = ['facebook', 'instagram'].includes(platformSlug) && Boolean(config.metaUserAccessToken)
    return {
      connectionLabel,
      connectionTagType,
      connectionTimestamp: timestamp,
      connectionDetail: detail,
      supportsHealthAction: HEALTH_REFRESH_PLATFORMS.has(platformSlug) || HEALTH_CHECK_PLATFORMS.has(platformSlug),
      healthActionKind: HEALTH_REFRESH_PLATFORMS.has(platformSlug) || metaRefreshable ? 'refresh' : (HEALTH_CHECK_PLATFORMS.has(platformSlug) ? 'check' : null)
    }
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
    const config = item.config || {}
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
      config,
      enabled: item.enabled ?? true,
      isLegacy,
      supportsCookieActions: Boolean(filePath),
      supportsRelogin: Boolean(isLegacy && getLegacyPlatformType(platformSlug) != null),
      ...deriveConnectionMeta(platformSlug, config, isLegacy),
      ...deriveExpiryMeta(platformSlug, config, isLegacy)
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
