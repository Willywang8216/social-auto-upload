import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AxiosError } from 'axios'

const mocks = vi.hoisted(() => ({
  toast: vi.fn(), clearToken: vi.fn(), clearCsrfToken: vi.fn(), clearSession: vi.fn(),
  getToken: vi.fn(() => 'test-bearer'), getCsrfToken: vi.fn(() => 'test-csrf'),
}))
vi.mock('element-plus', () => ({ ElMessage: { error: mocks.toast } }))
vi.mock('@/utils/auth', () => mocks)
vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ clearSession: mocks.clearSession }) }))

let request, http, showRequestError
beforeEach(async () => {
  vi.resetModules()
  vi.clearAllMocks()
  vi.useFakeTimers()
  vi.stubGlobal('window', { location: { hash: '#/jobs' } })
  ;({ default: request, http, showRequestError } = await import('../request'))
})
afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function failingAdapter(status, code = 'ERR_BAD_RESPONSE', message = 'failure') {
  let failure
  return {
    adapter: async (config) => {
      failure = new AxiosError(message, code, config, {}, status ? { status, data: { msg: message }, config } : undefined)
      throw failure
    },
    error: () => failure,
  }
}

describe('request rejection contract (in-memory Axios adapters only)', () => {
  it('rejects HTTP 500 with the original error instead of resolving undefined', async () => {
    const mock = failingAdapter(500)
    await expect(http.get('/test', undefined, mock)).rejects.toMatchObject({ response: { status: 500 } })
    expect(mocks.toast).toHaveBeenCalledOnce()
    showRequestError(mock.error(), 'caller fallback')
    expect(mocks.toast).toHaveBeenCalledOnce()
  })

  it.each(['ECONNABORTED', 'ETIMEDOUT', 'ERR_NETWORK'])('rejects %s', async (code) => {
    await expect(request.get('/test', failingAdapter(null, code))).rejects.toMatchObject({ code })
    expect(mocks.toast).toHaveBeenCalledOnce()
  })

  it('401 clears both authentication modes and redirects once for concurrent failures', async () => {
    const errors = await Promise.allSettled([
      request.get('/test', failingAdapter(401)), request.get('/test', failingAdapter(401)),
    ])
    await vi.dynamicImportSettled()
    expect(errors.every((result) => result.status === 'rejected')).toBe(true)
    expect(mocks.clearToken).toHaveBeenCalledOnce()
    expect(mocks.clearCsrfToken).toHaveBeenCalledOnce()
    expect(mocks.clearSession).toHaveBeenCalledOnce()
    expect(window.location.hash).toBe('#/login')
    expect(mocks.toast).toHaveBeenCalledOnce()
  })

  it('403 rejects but retains bearer/CSRF credentials, session and route', async () => {
    await expect(request.get('/test', failingAdapter(403, 'ERR_BAD_REQUEST', 'permission denied'))).rejects.toMatchObject({ response: { status: 403 } })
    expect(mocks.clearToken).not.toHaveBeenCalled()
    expect(mocks.clearCsrfToken).not.toHaveBeenCalled()
    expect(mocks.clearSession).not.toHaveBeenCalled()
    expect(window.location.hash).toBe('#/jobs')
    expect(mocks.toast).toHaveBeenCalledWith('permission denied')
    await request.get('/next', { adapter: async (config) => {
      expect(config.headers.Authorization).toBe('Bearer test-bearer')
      expect(config.headers['X-CSRF-Token']).toBe('test-csrf')
      return { data: { code: 200 }, status: 200, config }
    } })
  })

  it('silent and cancelled requests still reject without a toast', async () => {
    await expect(request.get('/test', { ...failingAdapter(500), silentError: true })).rejects.toBeTruthy()
    await expect(request.get('/test', failingAdapter(null, 'ERR_CANCELED'))).rejects.toBeTruthy()
    expect(mocks.toast).not.toHaveBeenCalled()
  })

  it('preserves API error status/config and rejects malformed envelopes', async () => {
    await expect(request.get('/test', { silentError: true, adapter: async (config) => ({
      config, status: 200, data: { code: 403, msg: 'not allowed' },
    }) })).rejects.toMatchObject({ response: { data: { code: 403 } }, config: { silentError: true } })
    await expect(request.get('/test', { adapter: async (config) => ({ config, status: 200, data: undefined }) })).rejects.toMatchObject({ code: 'ERR_INVALID_RESPONSE' })
    expect(mocks.clearToken).not.toHaveBeenCalled()
  })

  it('accepts HTTP 202 when the API body code is 200', async () => {
    const body = { code: 200, data: { campaignIds: [7], jobs: [], status: 'preparing' } }
    await expect(http.post('/test', {}, { adapter: async (config) => ({ status: 202, config, data: body }) })).resolves.toEqual(body)
  })
})
