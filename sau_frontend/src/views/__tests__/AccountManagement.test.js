import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import AccountManagement from '../AccountManagement.vue'
import { flush, mount, textContent } from '@/components/__tests__/test-renderer'

// The in-memory renderer exercises the real SFC; native DOM v-model listeners
// are irrelevant to these setup/action tests (values are assigned explicitly).
vi.mock('vue', async (original) => ({ ...(await original()), vModelText: {}, vModelSelect: {} }))
const api = vi.hoisted(() => ({ accounts: vi.fn(), profiles: vi.fn(), create: vi.fn(),
  meta: vi.fn(), threads: vi.fn(), other: vi.fn(), select: vi.fn() }))
vi.mock('@/stores/account', () => ({ useAccountStore: () => ({}) }))
vi.mock('@/api/account', () => ({ accountApi: {
  getAccountsApi: api.accounts, getAccountGroups: async () => ({ data: [] }),
  getAccountProfiles: async () => ({ data: [{ id: 1, name: 'First', count: 1 }, { id: 2, name: 'Second', count: 1 }] }),
} }))
vi.mock('@/api/profiles', () => ({ profilesApi: { list: api.profiles, createAccount: api.create } }))
vi.mock('@/api/meta', () => ({ metaApi: { startOAuth: api.meta, completeSelection: api.select } }))
vi.mock('@/api/threads', () => ({ threadsApi: { startOAuth: api.threads } }))
vi.mock('@/api/tiktok', () => ({ tiktokApi: { startOAuth: api.other } }))
vi.mock('@/api/youtube', () => ({ youtubeApi: { startOAuth: api.other } }))
vi.mock('@/api/twitter', () => ({ twitterApi: { startOAuth: api.other } }))
vi.mock('@/api/reddit', () => ({ redditApi: { startOAuth: api.other } }))

let view, popup, listeners, rows
const account = (id, profileId, platform = 'threads') => ({ id, profileId, platform, name: 'same-name',
  profileName: profileId === 1 ? 'First' : 'Second', authType: 'oauth', cookieStatus: 'valid', enabled: true })
beforeEach(() => {
  vi.useFakeTimers()
  vi.resetAllMocks()
  rows = [account(11, 1), account(22, 2)]
  popup = { closed: false, close: vi.fn(() => { popup.closed = true }) }
  listeners = new Map()
  vi.stubGlobal('window', { location: { origin: 'https://ui.example.invalid' }, open: vi.fn(() => popup),
    addEventListener: (type, fn) => listeners.set(type, fn),
    removeEventListener: (type, fn) => { if (listeners.get(type) === fn) listeners.delete(type) },
  })
  api.accounts.mockImplementation(async () => ({ data: rows }))
  api.profiles.mockResolvedValue({ data: [{ id: 1, name: 'First' }, { id: 2, name: 'Second' }, { id: 3, name: 'Empty profile' }] })
  api.create.mockImplementation(async (profileId, payload) => {
    rows.push({ ...account(33, profileId, payload.platform), name: payload.accountName })
    return { data: { id: 33 } }
  })
  for (const call of [api.meta, api.threads, api.other]) call.mockResolvedValue({ code: 200,
    data: { authorizeUrl: 'https://provider.example.invalid/authorize', state: 'bound-state', callbackOrigin: 'https://api.example.invalid' } })
})
afterEach(() => {
  view?.unmount()
  view = null
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})
async function open() {
  view = mount(AccountManagement)
  await flush()
  return view.state
}
const message = (data, overrides = {}) => ({ source: popup, origin: 'https://api.example.invalid', data, ...overrides })

describe('AccountManagement bounded OAuth', () => {
  it('reconnects the exact clicked card, including its profile, without name lookup', async () => {
    const state = await open()
    state.onReauth(state.accounts[1])
    expect(state.connectData.accountId).toBe(22)
    await state.doOAuthConnect()
    expect(api.threads).toHaveBeenCalledWith({ accountId: 22, profileId: 2, accountName: 'same-name' })
    expect(api.create).not.toHaveBeenCalled()
    expect(state.oauthBusy).toBe(true)
  })

  it('does not guess another account when a clicked record disappears or moves profile', async () => {
    const state = await open()
    state.onReauth(state.accounts[1])
    state.accounts = [state.accounts[0]]
    await state.doOAuthConnect()
    expect(api.threads).not.toHaveBeenCalled()
    expect(api.create).not.toHaveBeenCalled()
    expect(state.toast).toContain('exact card')
  })

  it('requires an explicit profile for new accounts and includes empty profiles', async () => {
    const state = await open()
    expect(state.connectProfiles.map(p => p.id)).toContain(3)
    state.openConnect({ platform: 'threads', account: 'new-name' })
    await state.doOAuthConnect()
    expect(api.create).not.toHaveBeenCalled()
    expect(state.toast).toContain('specific profile')
  })

  it('a new flow in another profile never reuses a same-named record', async () => {
    const state = await open()
    state.openConnect({ platform: 'threads', account: 'same-name', profileId: 3 })
    await state.doOAuthConnect()
    expect(api.create).toHaveBeenCalledWith(3, { platform: 'threads', accountName: 'same-name', authType: 'oauth' })
    expect(api.threads).toHaveBeenCalledWith({ accountId: 33, profileId: 3, accountName: 'same-name' })
  })

  it('same profile/name does not become an implicit reconnect', async () => {
    const state = await open()
    state.openConnect({ platform: 'threads', account: 'same-name', profileId: 2 })
    await state.doOAuthConnect()
    expect(api.create).not.toHaveBeenCalled()
    expect(api.threads).not.toHaveBeenCalled()
    expect(state.toast).toContain('Re-auth button')
  })

  it('checks popup source/origin/state and waits for server persistence before picker success', async () => {
    rows = [account(11, 1, 'facebook')]
    const state = await open()
    state.onReauth(state.accounts[0])
    await state.doOAuthConnect()
    const handler = listeners.get('message')
    const selection = { type: 'sau:meta-oauth-selection', data: { state: 'bound-state', selectedId: 'p2', accessToken: 'not-authority' } }
    await handler(message(selection, { origin: 'https://attacker.invalid' }))
    await handler(message(selection, { source: {} }))
    await handler(message({ ...selection, data: { ...selection.data, state: 'other-state' } }))
    expect(api.select).not.toHaveBeenCalled()
    let resolve
    api.select.mockReturnValue(new Promise(done => { resolve = done }))
    const pending = handler(message(selection))
    await flush()
    expect(api.select).toHaveBeenCalledWith({ state: 'bound-state', selectedId: 'p2' })
    expect(state.showConnect).toBe(true)
    expect(popup.close).not.toHaveBeenCalled()
    expect(String(state.toast || '')).not.toContain('Re-authenticated')
    resolve({ code: 200, data: { state: 'bound-state', accountId: 11, status: 'ok' } })
    await pending
    expect(state.showConnect).toBe(false)
    expect(state.toast).toContain('Re-authenticated')
    expect(popup.close).toHaveBeenCalledOnce()
  })

  it('picker failure and old browser token payloads never announce connected', async () => {
    rows = [account(11, 1, 'instagram')]
    const state = await open()
    state.onReauth(state.accounts[0])
    await state.doOAuthConnect()
    const handler = listeners.get('message')
    await handler(message({ type: 'sau:meta-oauth', ok: true,
      data: { state: 'bound-state', accountId: 11, selectedPage: { id: 'p1' }, tokenData: { accessToken: 'forged' } } }))
    expect(state.showConnect).toBe(true)
    expect(popup.close).not.toHaveBeenCalled()
    api.select.mockRejectedValue(new Error('state already used'))
    await handler(message({ type: 'sau:meta-oauth-selection', data: { state: 'bound-state', selectedId: 'p1' } }))
    expect(state.toast).toContain('Selection not saved')
    expect(state.showConnect).toBe(true)
    expect(popup.close).not.toHaveBeenCalled()
  })

  it('separates native reconnect/expiry from unvalidated managed configuration', async () => {
    rows = [{ ...account(11, 1, 'facebook'), nativeCredentials: {
      state: 'reconnect_required', reconnectRequired: true, reason: 'Provider rejected credential (190/460)',
      lastCheckedAt: '2030-01-01T00:00:00', token: { mode: 'never', expiresAt: '2040-01-01' },
      userToken: { mode: 'expiring', expiresAt: '2030-03-01', dataAccessExpiresAt: '2030-02-01' },
    }, publishingRoute: { provider: 'sociamonials', configured: true, enabled: true, state: 'configured',
      validation: 'not_checked', availability: 'unknown' } }]
    const state = await open()
    const card = state.accounts[0]
    expect(card.reconnectRequired).toBe(true)
    expect(card.isOverdue).toBe(false)
    expect(state.cookieStatusLabel(card)).toBe('Reconnect required')
    expect(state.publishingRouteLabel(card)).toBe('Managed configured')
    expect(state.expiryLabel(card)).toBe('No scheduled expiry')
    const text = textContent(view.root)
    expect(text).toContain('Native: Reconnect required')
    expect(text).toContain('Managed connection / availability: not validated')
    expect(text).toContain('User data access: 2030-02-01')
    expect(text).not.toContain('Validated at last check')
    expect(text).not.toContain('Token expired')
    card.nativeCredentials.state = 'unknown'
    card.nativeCredentials.token.mode = 'unknown'
    expect(state.cookieStatusLabel(card)).toBe('Not validated')
    expect(state.expiryLabel(card)).toBe('Unknown')
    card.publishingRoute.enabled = false
    expect(state.publishingRouteLabel(card)).toBe('Disabled')
  })
})
