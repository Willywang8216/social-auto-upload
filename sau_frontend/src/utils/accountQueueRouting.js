export const ACCOUNT_QUEUE_DEFAULTS = {
  risk: 'all',
  profile: 'all',
  platform: 'all',
  sort: 'urgency',
  sortOrder: 'ascending',
}

const ALLOWED_RISKS = new Set(['all', 'expiring_24h', 'expiring_7d', 'overdue', 'reconnect_required'])
const ALLOWED_SORTS = new Set(['urgency', 'expiry', 'platform', 'profile', 'name'])
const ALLOWED_SORT_ORDERS = new Set(['ascending', 'descending'])

function readQueryScalar(value) {
  return Array.isArray(value) ? value[0] : value
}

export function normalizeAccountManagementRouteQuery(routeQuery = {}, accountPlatformTabs = []) {
  const risk = readQueryScalar(routeQuery.risk)
  const profile = readQueryScalar(routeQuery.profile)
  const platform = readQueryScalar(routeQuery.platform)
  const sort = readQueryScalar(routeQuery.sort)
  const sortOrder = readQueryScalar(routeQuery.sortOrder)

  const allowedPlatforms = new Set(['all', ...accountPlatformTabs.map((item) => item.value)])

  return {
    selectedRiskFilter: ALLOWED_RISKS.has(String(risk || ACCOUNT_QUEUE_DEFAULTS.risk))
      ? String(risk || ACCOUNT_QUEUE_DEFAULTS.risk)
      : ACCOUNT_QUEUE_DEFAULTS.risk,
    selectedProfileFilter: profile === 'legacy' || profile === 'all'
      ? String(profile || ACCOUNT_QUEUE_DEFAULTS.profile)
      : (profile != null && profile !== '' ? String(profile) : ACCOUNT_QUEUE_DEFAULTS.profile),
    activeTab: allowedPlatforms.has(String(platform || ACCOUNT_QUEUE_DEFAULTS.platform))
      ? String(platform || ACCOUNT_QUEUE_DEFAULTS.platform)
      : ACCOUNT_QUEUE_DEFAULTS.platform,
    selectedSortMode: ALLOWED_SORTS.has(String(sort || ACCOUNT_QUEUE_DEFAULTS.sort))
      ? String(sort || ACCOUNT_QUEUE_DEFAULTS.sort)
      : ACCOUNT_QUEUE_DEFAULTS.sort,
    selectedSortOrder: ALLOWED_SORT_ORDERS.has(String(sortOrder || ACCOUNT_QUEUE_DEFAULTS.sortOrder))
      ? String(sortOrder || ACCOUNT_QUEUE_DEFAULTS.sortOrder)
      : ACCOUNT_QUEUE_DEFAULTS.sortOrder,
  }
}

export function buildAccountManagementRouteQuery({
  selectedRiskFilter = ACCOUNT_QUEUE_DEFAULTS.risk,
  selectedProfileFilter = ACCOUNT_QUEUE_DEFAULTS.profile,
  activeTab = ACCOUNT_QUEUE_DEFAULTS.platform,
  selectedSortMode = ACCOUNT_QUEUE_DEFAULTS.sort,
  selectedSortOrder = ACCOUNT_QUEUE_DEFAULTS.sortOrder,
  currentQuery = {},
} = {}) {
  const nextQuery = { ...currentQuery }

  if (selectedRiskFilter !== ACCOUNT_QUEUE_DEFAULTS.risk) nextQuery.risk = selectedRiskFilter
  else delete nextQuery.risk

  if (selectedProfileFilter !== ACCOUNT_QUEUE_DEFAULTS.profile) nextQuery.profile = selectedProfileFilter
  else delete nextQuery.profile

  if (activeTab !== ACCOUNT_QUEUE_DEFAULTS.platform) nextQuery.platform = activeTab
  else delete nextQuery.platform

  if (selectedSortMode !== ACCOUNT_QUEUE_DEFAULTS.sort) nextQuery.sort = selectedSortMode
  else delete nextQuery.sort

  if (selectedSortOrder !== ACCOUNT_QUEUE_DEFAULTS.sortOrder) nextQuery.sortOrder = selectedSortOrder
  else delete nextQuery.sortOrder

  const unchanged = ['risk', 'profile', 'platform', 'sort', 'sortOrder'].every(
    (key) => String((currentQuery || {})[key] || '') === String(nextQuery[key] || '')
  )

  return { nextQuery, unchanged }
}

export function buildAccountQueueNavigationQuery({
  risk = ACCOUNT_QUEUE_DEFAULTS.risk,
  platform = ACCOUNT_QUEUE_DEFAULTS.platform,
  profile = ACCOUNT_QUEUE_DEFAULTS.profile,
  sort = ACCOUNT_QUEUE_DEFAULTS.sort,
  sortOrder = ACCOUNT_QUEUE_DEFAULTS.sortOrder,
  platformValueByLabel = {},
} = {}) {
  const query = {}
  if (risk && risk !== ACCOUNT_QUEUE_DEFAULTS.risk) query.risk = risk

  const normalizedPlatform = platform && platform !== ACCOUNT_QUEUE_DEFAULTS.platform
    ? (platformValueByLabel[platform] || platform)
    : ACCOUNT_QUEUE_DEFAULTS.platform
  if (normalizedPlatform !== ACCOUNT_QUEUE_DEFAULTS.platform) query.platform = normalizedPlatform

  if (profile && profile !== ACCOUNT_QUEUE_DEFAULTS.profile) query.profile = profile
  if (sort && sort !== ACCOUNT_QUEUE_DEFAULTS.sort) query.sort = sort
  if (sortOrder && sortOrder !== ACCOUNT_QUEUE_DEFAULTS.sortOrder) query.sortOrder = sortOrder
  return query
}
