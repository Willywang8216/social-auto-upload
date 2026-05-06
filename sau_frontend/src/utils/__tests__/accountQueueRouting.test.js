import { describe, expect, it } from 'vitest'
import {
  buildAccountManagementRouteQuery,
  buildAccountQueueNavigationQuery,
  normalizeAccountManagementRouteQuery,
} from '../accountQueueRouting'

describe('accountQueueRouting', () => {
  it('normalizes route query state with arrays and invalid values', () => {
    const result = normalizeAccountManagementRouteQuery(
      {
        risk: ['expiring_24h'],
        profile: ['legacy'],
        platform: ['youtube'],
        sort: ['expiry'],
        sortOrder: ['descending'],
      },
      [{ value: 'youtube' }, { value: 'reddit' }]
    )

    expect(result).toEqual({
      selectedRiskFilter: 'expiring_24h',
      selectedProfileFilter: 'legacy',
      activeTab: 'youtube',
      selectedSortMode: 'expiry',
      selectedSortOrder: 'descending',
    })
  })

  it('falls back to defaults for unsupported route values', () => {
    const result = normalizeAccountManagementRouteQuery(
      {
        risk: 'not-real',
        profile: '',
        platform: 'unknown-platform',
        sort: 'bad-sort',
        sortOrder: 'sideways',
      },
      [{ value: 'youtube' }]
    )

    expect(result).toEqual({
      selectedRiskFilter: 'all',
      selectedProfileFilter: 'all',
      activeTab: 'all',
      selectedSortMode: 'urgency',
      selectedSortOrder: 'ascending',
    })
  })

  it('builds a compact account-management query and preserves unrelated query params', () => {
    const { nextQuery, unchanged } = buildAccountManagementRouteQuery({
      selectedRiskFilter: 'reconnect_required',
      selectedProfileFilter: '12',
      activeTab: 'facebook',
      selectedSortMode: 'expiry',
      selectedSortOrder: 'descending',
      currentQuery: { foo: 'bar' },
    })

    expect(unchanged).toBe(false)
    expect(nextQuery).toEqual({
      foo: 'bar',
      risk: 'reconnect_required',
      profile: '12',
      platform: 'facebook',
      sort: 'expiry',
      sortOrder: 'descending',
    })
  })

  it('omits default navigation values and maps platform labels to slugs', () => {
    const query = buildAccountQueueNavigationQuery({
      risk: 'expiring_7d',
      platform: 'YouTube',
      profile: '7',
      sort: 'expiry',
      platformValueByLabel: {
        YouTube: 'youtube',
      },
    })

    expect(query).toEqual({
      risk: 'expiring_7d',
      platform: 'youtube',
      profile: '7',
      sort: 'expiry',
    })
  })
})
