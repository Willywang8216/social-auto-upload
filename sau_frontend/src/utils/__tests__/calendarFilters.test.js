import { describe, expect, it } from 'vitest'

import {
  applyCalendarFiltersToQuery,
  currentMonthKey,
  parseCalendarFilters,
  sameCalendarFilterQuery
} from '../calendarFilters'

describe('parseCalendarFilters', () => {
  it('reads comma-separated lists and drops empties', () => {
    expect(parseCalendarFilters({ profiles: '1,2,,3', platforms: 'douyin,', accounts: '7' }))
      .toEqual({ profiles: [1, 2, 3], platforms: ['douyin'], accounts: [7], q: '', month: '' })
  })

  it('ignores non-numeric ids rather than emitting NaN', () => {
    expect(parseCalendarFilters({ profiles: '1,abc' }).profiles).toEqual([1])
  })

  it('defaults to an empty filter set', () => {
    expect(parseCalendarFilters()).toEqual({ profiles: [], platforms: [], accounts: [], q: '', month: '' })
  })
})

describe('applyCalendarFiltersToQuery', () => {
  it('merges into the current query instead of rebuilding it', () => {
    // The classic bug: a control that sets only its own key wipes ?entity=.
    const next = applyCalendarFiltersToQuery({ entity: 'mg-42' }, { q: 'hello' })
    expect(next).toEqual({ entity: 'mg-42', q: 'hello' })
  })

  it('adds the filter keys as comma-separated values', () => {
    const next = applyCalendarFiltersToQuery({}, { profiles: [1, 2], platforms: ['douyin'], accounts: [7] })
    expect(next).toEqual({ profiles: '1,2', platforms: 'douyin', accounts: '7' })
  })

  it('removes a filter that is back at its default so the URL stays clean', () => {
    const next = applyCalendarFiltersToQuery({ profiles: '1', entity: 'mg-9' }, { profiles: [] })
    expect(next).toEqual({ entity: 'mg-9' })
  })

  it('trims the keyword and drops a whitespace-only one', () => {
    expect(applyCalendarFiltersToQuery({}, { q: '  hi  ' }).q).toBe('hi')
    expect(applyCalendarFiltersToQuery({ q: 'hi' }, { q: '   ' })).toEqual({})
  })
})

describe('sameCalendarFilterQuery', () => {
  it('treats equivalent queries as equal so no navigation is made', () => {
    expect(sameCalendarFilterQuery({ q: 'a', profiles: '1' }, { profiles: '1', q: 'a' })).toBe(true)
  })

  it('distinguishes different filter values', () => {
    expect(sameCalendarFilterQuery({ q: 'a' }, { q: 'b' })).toBe(false)
  })

  it('ignores keys with no value', () => {
    expect(sameCalendarFilterQuery({ q: 'a' }, { q: 'a', profiles: '', accounts: undefined })).toBe(true)
  })
})

describe('month in the query', () => {
  it('formats the current month as YYYY-MM', () => {
    expect(currentMonthKey(new Date(2026, 9, 1))).toBe('2026-10')
    expect(currentMonthKey(new Date(2026, 0, 31))).toBe('2026-01')
  })

  it('reads a valid month and rejects a malformed one', () => {
    expect(parseCalendarFilters({ month: '2026-09' }).month).toBe('2026-09')
    expect(parseCalendarFilters({ month: '2026-13' }).month).toBe('')
    expect(parseCalendarFilters({ month: 'nonsense' }).month).toBe('')
    expect(parseCalendarFilters({}).month).toBe('')
  })

  it('writes a non-current month and omits the current one as the default', () => {
    expect(applyCalendarFiltersToQuery({}, { month: '1999-01' }).month).toBe('1999-01')
    expect(applyCalendarFiltersToQuery({}, { month: currentMonthKey() }).month).toBeUndefined()
  })

  it('drops the month when the view returns to the current month', () => {
    expect(applyCalendarFiltersToQuery({ month: '2026-09' }, { month: currentMonthKey() }))
      .toEqual({})
  })

  it('carries the month through a round trip with the other filters', () => {
    const state = { profiles: [1], platforms: [], accounts: [], q: 'launch', month: '1999-01' }
    const query = applyCalendarFiltersToQuery({ entity: 'mg-1' }, state)
    expect(parseCalendarFilters(query)).toEqual(state)
    expect(query.entity).toBe('mg-1')
  })
})

describe('filter round-trip', () => {
  it('survives state -> URL -> state unchanged', () => {
    const state = { profiles: [1, 2], platforms: ['douyin', 'bilibili'], accounts: [7], q: 'launch', month: '' }
    const query = applyCalendarFiltersToQuery({ entity: 'mg-1' }, state)
    const parsed = parseCalendarFilters(query)
    expect(parsed).toEqual(state)
    expect(query.entity).toBe('mg-1')
  })
})
