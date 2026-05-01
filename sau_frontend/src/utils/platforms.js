const PLATFORM_METADATA = [
  {
    accountType: 3,
    publishSlug: 'douyin',
    label: '抖音',
    tagType: 'danger',
    aliases: ['douyin']
  },
  {
    accountType: 4,
    publishSlug: 'kuaishou',
    label: '快手',
    tagType: 'success',
    aliases: ['kuaishou']
  },
  {
    accountType: 2,
    publishSlug: 'tencent',
    label: '視頻號',
    tagType: 'warning',
    aliases: ['tencent', 'channels']
  },
  {
    accountType: 1,
    publishSlug: 'xiaohongshu',
    label: '小紅書',
    tagType: 'info',
    aliases: ['xiaohongshu']
  },
  {
    accountType: 7,
    publishSlug: 'twitter',
    label: 'X / Twitter',
    tagType: 'primary',
    aliases: ['twitter', 'x']
  }
]

const PLATFORM_LOOKUP = new Map()

const normalizePlatformKey = (value) => String(value).trim().toLowerCase()

PLATFORM_METADATA.forEach((platform) => {
  ;[
    platform.accountType,
    String(platform.accountType),
    platform.publishSlug,
    platform.label,
    ...platform.aliases
  ].forEach((value) => {
    PLATFORM_LOOKUP.set(normalizePlatformKey(value), platform)
  })
})

export const PUBLISH_PLATFORM_OPTIONS = PLATFORM_METADATA.map(({ publishSlug, label }) => ({
  value: publishSlug,
  label
}))

export const ACCOUNT_PLATFORM_OPTIONS = PLATFORM_METADATA.map(
  ({ publishSlug, label, accountType, tagType }) => ({
    value: label,
    label,
    publishSlug,
    legacyType: accountType,
    tagType
  })
)

export const SUPPORTED_PLATFORM_TAGS = PLATFORM_METADATA.map(({ label, tagType }) => ({
  label,
  tagType
}))

export const LEGACY_ACCOUNT_PLATFORM_ORDER = [
  'kuaishou',
  'douyin',
  'tencent',
  'xiaohongshu',
  'twitter'
]

export function getPlatformMeta(value) {
  if (value == null || value === '') {
    return null
  }

  return PLATFORM_LOOKUP.get(normalizePlatformKey(value)) || null
}

export function getPlatformLabel(value) {
  return getPlatformMeta(value)?.label || '未知'
}

export function getPlatformTagType(value) {
  return getPlatformMeta(value)?.tagType || 'info'
}

export function getLegacyPlatformType(value) {
  return getPlatformMeta(value)?.accountType ?? null
}

export function getPublishPlatformSlug(value) {
  return getPlatformMeta(value)?.publishSlug || ''
}
