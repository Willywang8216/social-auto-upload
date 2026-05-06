export function buildAccountComparator(sortMode = 'urgency', sortOrder = 'ascending') {
  const compareUrgency = (left, right) => {
    const leftRank = Number(left.urgencyRank ?? 99)
    const rightRank = Number(right.urgencyRank ?? 99)
    if (leftRank !== rightRank) return leftRank - rightRank

    const leftSeconds = left.secondsRemaining ?? Number.POSITIVE_INFINITY
    const rightSeconds = right.secondsRemaining ?? Number.POSITIVE_INFINITY
    if (leftSeconds !== rightSeconds) return leftSeconds - rightSeconds

    const leftProfile = String(left.profileName || '')
    const rightProfile = String(right.profileName || '')
    if (leftProfile !== rightProfile) return leftProfile.localeCompare(rightProfile)

    return String(left.name || '').localeCompare(String(right.name || ''))
  }

  const compareExpiry = (left, right) => {
    const leftSeconds = left.secondsRemaining ?? Number.POSITIVE_INFINITY
    const rightSeconds = right.secondsRemaining ?? Number.POSITIVE_INFINITY
    if (leftSeconds !== rightSeconds) return leftSeconds - rightSeconds
    return compareUrgency(left, right)
  }

  const comparePlatform = (left, right) => {
    const leftPlatform = String(left.platform || '')
    const rightPlatform = String(right.platform || '')
    if (leftPlatform !== rightPlatform) return leftPlatform.localeCompare(rightPlatform)
    return compareUrgency(left, right)
  }

  const compareProfile = (left, right) => {
    const leftProfile = String(left.profileName || '')
    const rightProfile = String(right.profileName || '')
    if (leftProfile !== rightProfile) return leftProfile.localeCompare(rightProfile)
    return compareUrgency(left, right)
  }

  const compareName = (left, right) => {
    const leftName = String(left.name || '')
    const rightName = String(right.name || '')
    if (leftName !== rightName) return leftName.localeCompare(rightName)
    return compareUrgency(left, right)
  }

  const comparatorByMode = {
    urgency: compareUrgency,
    expiry: compareExpiry,
    platform: comparePlatform,
    profile: compareProfile,
    name: compareName
  }

  const baseComparator = comparatorByMode[sortMode] || compareUrgency
  return sortOrder === 'descending'
    ? (left, right) => baseComparator(right, left)
    : baseComparator
}

export function matchesAccountFilters(account, { profileFilter = 'all', riskFilter = 'all', keyword = '' } = {}) {
  if (profileFilter === 'legacy' && account.profileId != null) return false
  if (profileFilter !== 'all' && profileFilter !== 'legacy' && String(account.profileId) !== profileFilter) {
    return false
  }

  if (riskFilter === 'expiring_24h' && !account.isExpiringWithin24h) return false
  if (riskFilter === 'expiring_7d' && !account.isExpiringWithin7d) return false
  if (riskFilter === 'overdue' && !account.isOverdue) return false
  if (riskFilter === 'reconnect_required' && !account.reconnectRequired) return false

  if (!keyword) return true
  return [account.name, account.platform, account.profileName]
    .filter(Boolean)
    .some((value) => String(value).toLowerCase().includes(keyword))
}
