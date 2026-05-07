import { describe, expect, it } from 'vitest'
import {
  availableAccountsForTab,
  buildCampaignPlatformOptions,
  mergeKnownAccounts,
  profileAccountsFor,
  selectedProfilePlatformLabels,
} from '../publish-center-logic'

describe('publish-center-logic', () => {
  it('derives campaign platform options by excluding legacy platforms', () => {
    const result = buildCampaignPlatformOptions(
      [
        { value: 'facebook', label: 'Facebook' },
        { value: 'reddit', label: 'Reddit' },
        { value: 'twitter', label: 'X / Twitter' },
      ],
      [{ value: 'twitter', label: 'X / Twitter' }]
    )

    expect(result).toEqual([
      { value: 'facebook', label: 'Facebook' },
      { value: 'reddit', label: 'Reddit' },
    ])
  })

  it('merges global and per-profile accounts without duplicates', () => {
    const result = mergeKnownAccounts(
      [{ id: 1, name: 'legacy-x' }],
      {
        2: [{ id: 2, name: 'profile-fb' }],
        3: [{ id: 1, name: 'legacy-x' }, { id: 4, name: 'profile-ig' }],
      }
    )

    expect(result.map((item) => item.id)).toEqual([1, 2, 4])
  })

  it('returns only enabled profile accounts and distinct platform labels', () => {
    const accountsByProfile = {
      9: [
        { id: 2, platform: 'Facebook', enabled: true },
        { id: 3, platform: 'Instagram', enabled: false },
        { id: 4, platform: 'Facebook', enabled: true },
      ],
    }

    expect(profileAccountsFor(accountsByProfile, 9).map((item) => item.id)).toEqual([2, 4])
    expect(selectedProfilePlatformLabels(accountsByProfile, 9)).toEqual(['Facebook'])
  })

  it('selects profile accounts in campaign mode and legacy accounts in legacy mode', () => {
    const accountsByProfile = {
      5: [
        { id: 20, platform: 'Facebook', enabled: true },
        { id: 21, platform: 'Reddit', enabled: true },
      ],
    }
    const legacyAccounts = [
      { id: 1, profileId: null, platform: '抖音' },
      { id: 2, profileId: null, platform: '小紅書' },
      { id: 3, profileId: 5, platform: 'Facebook' },
    ]
    const getPlatformLabel = (slug) => ({ douyin: '抖音', xiaohongshu: '小紅書' }[slug] || slug)

    expect(
      availableAccountsForTab({
        currentTab: { selectedProfileId: 5, selectedPlatform: 'douyin' },
        legacyAccounts,
        accountsByProfile,
        getPlatformLabel,
      }).map((item) => item.id)
    ).toEqual([20, 21])

    expect(
      availableAccountsForTab({
        currentTab: { selectedProfileId: null, selectedPlatform: 'xiaohongshu' },
        legacyAccounts,
        accountsByProfile,
        getPlatformLabel,
      }).map((item) => item.id)
    ).toEqual([2])
  })
})
