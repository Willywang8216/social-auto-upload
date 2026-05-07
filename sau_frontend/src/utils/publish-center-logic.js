export function buildCampaignPlatformOptions(profilePlatformOptions = [], legacyPlatformOptions = []) {
  return profilePlatformOptions.filter(
    (platform) => !legacyPlatformOptions.some((legacy) => legacy.value === platform.value)
  )
}

export function mergeKnownAccounts(storeAccounts = [], accountsByProfile = {}) {
  const merged = [...storeAccounts]
  Object.values(accountsByProfile || {}).forEach((items) => {
    for (const item of items || []) {
      if (!merged.some((existing) => existing.id === item.id)) {
        merged.push(item)
      }
    }
  })
  return merged
}

export function profileAccountsFor(accountsByProfile = {}, profileId = null) {
  if (!profileId) return []
  return (accountsByProfile[profileId] || []).filter((acc) => acc.enabled !== false)
}

export function selectedProfilePlatformLabels(accountsByProfile = {}, profileId = null) {
  const labels = profileAccountsFor(accountsByProfile, profileId)
    .map((account) => account.platform || '')
    .filter(Boolean)
  return [...new Set(labels)]
}

export function availableAccountsForTab({ currentTab = null, legacyAccounts = [], accountsByProfile = {}, getPlatformLabel }) {
  if (!currentTab) return []
  if (currentTab.selectedProfileId) {
    return profileAccountsFor(accountsByProfile, currentTab.selectedProfileId)
  }
  const currentPlatform = currentTab.selectedPlatform ? getPlatformLabel(currentTab.selectedPlatform) : null
  return currentPlatform
    ? legacyAccounts.filter((acc) => acc.profileId == null && acc.platform === currentPlatform)
    : []
}
