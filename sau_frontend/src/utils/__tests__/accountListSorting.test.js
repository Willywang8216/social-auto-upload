import { describe, expect, it } from 'vitest'
import { buildAccountComparator, matchesAccountFilters } from '../account-list-sorting'

describe('account-list-sorting', () => {
  const accounts = [
    {
      name: 'Zulu',
      platform: 'YouTube',
      profileName: 'Brand B',
      urgencyRank: 3,
      secondsRemaining: 86400,
      profileId: 2,
      isExpiringWithin24h: true,
      isExpiringWithin7d: true,
      isOverdue: false,
      reconnectRequired: false,
    },
    {
      name: 'Alpha',
      platform: 'Facebook',
      profileName: 'Brand A',
      urgencyRank: 0,
      secondsRemaining: -1,
      profileId: 1,
      isExpiringWithin24h: false,
      isExpiringWithin7d: false,
      isOverdue: true,
      reconnectRequired: true,
    },
    {
      name: 'Beta',
      platform: 'Discord',
      profileName: 'Legacy',
      urgencyRank: 99,
      secondsRemaining: null,
      profileId: null,
      isExpiringWithin24h: false,
      isExpiringWithin7d: false,
      isOverdue: false,
      reconnectRequired: false,
    },
  ]

  it('sorts by urgency by default', () => {
    const comparator = buildAccountComparator()
    const sorted = [...accounts].sort(comparator)
    expect(sorted.map((item) => item.name)).toEqual(['Alpha', 'Zulu', 'Beta'])
  })

  it('sorts by name descending when requested', () => {
    const comparator = buildAccountComparator('name', 'descending')
    const sorted = [...accounts].sort(comparator)
    expect(sorted.map((item) => item.name)).toEqual(['Zulu', 'Beta', 'Alpha'])
  })

  it('matches reconnect-required filter', () => {
    expect(matchesAccountFilters(accounts[0], { riskFilter: 'reconnect_required' })).toBe(false)
    expect(matchesAccountFilters(accounts[1], { riskFilter: 'reconnect_required' })).toBe(true)
  })

  it('matches legacy profile filter and keyword filter together', () => {
    expect(matchesAccountFilters(accounts[2], { profileFilter: 'legacy', keyword: 'disc' })).toBe(true)
    expect(matchesAccountFilters(accounts[0], { profileFilter: 'legacy', keyword: 'you' })).toBe(false)
  })
})
