/**
 * URL <-> calendar filter state.
 *
 * The calendar's filters live in the route query so a refresh keeps the view
 * and a link reproduces it. The rule that matters: writes must MERGE into the
 * existing query. A control that builds a fresh query string and sets only its
 * own key silently deletes every sibling filter (and the ?entity= deep link),
 * which surfaces later as "changing one filter clears the others".
 */

const splitList = (value) => (typeof value === 'string' && value ? value.split(',').filter(Boolean) : [])

const splitIds = (value) => splitList(value).map(Number).filter((n) => !Number.isNaN(n))

const joinList = (values) => (Array.isArray(values) ? values.filter((v) => v !== '' && v != null).join(',') : '')

const hasValue = (value) => value !== undefined && value !== null && value !== ''

const isMonth = (value) => typeof value === 'string' && /^\d{4}-(0[1-9]|1[0-2])$/.test(value)

/** The `YYYY-MM` key of the current month, i.e. the calendar's default view. */
export function currentMonthKey(now = new Date()) {
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`
}

/**
 * Read the calendar's filters out of a route query.
 * @returns {{profiles: number[], platforms: string[], accounts: number[], q: string, month: string}}
 *   `month` is '' when absent or malformed.
 */
export function parseCalendarFilters(query = {}) {
  return {
    profiles: splitIds(query.profiles),
    platforms: splitList(query.platforms),
    accounts: splitIds(query.accounts),
    q: typeof query.q === 'string' ? query.q : '',
    month: isMonth(query.month) ? query.month : ''
  }
}

/**
 * Merge calendar filters into a query object, dropping ones at their default
 * so an unfiltered view keeps a clean URL — including the month, which is only
 * written when it is not the current one.
 * @returns {object} a new query object; unrelated keys are preserved
 */
export function applyCalendarFiltersToQuery(currentQuery = {}, filters = {}) {
  const next = { ...currentQuery }
  const setOrDelete = (key, value) => {
    if (value) next[key] = value
    else delete next[key]
  }
  setOrDelete('profiles', joinList(filters.profiles))
  setOrDelete('platforms', joinList(filters.platforms))
  setOrDelete('accounts', joinList(filters.accounts))
  setOrDelete('q', typeof filters.q === 'string' ? filters.q.trim() : '')
  setOrDelete('month', isMonth(filters.month) && filters.month !== currentMonthKey() ? filters.month : '')
  return next
}

/**
 * True when two query objects carry the same effective filters.
 *
 * Used to skip a no-op navigation, which is also what stops the
 * state -> URL -> state watchers from feeding each other.
 */
export function sameCalendarFilterQuery(a = {}, b = {}) {
  const keys = (obj) => Object.keys(obj).filter((key) => hasValue(obj[key]))
  const keysA = keys(a)
  const keysB = keys(b)
  return keysA.length === keysB.length
    && keysA.every((key) => String(a[key]) === String(b[key]))
}
