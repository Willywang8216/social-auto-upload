import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useJobsStore, JOB_STATUS, TARGET_STATUS } from '../jobs'

const api = vi.hoisted(() => ({ get: vi.fn(), list: vi.fn(), create: vi.fn(), cancel: vi.fn(), resubmitTarget: vi.fn() }))
vi.mock('@/api/jobs', () => ({ jobsApi: api }))
const snapshot = (id = 1, status = 'running', targets = []) => ({ code: 200, data: { id, status, targets } })
const offline = () => Object.assign(new Error('offline'), { code: 'ERR_NETWORK' })
function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}
let store
beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(0)
  vi.resetAllMocks()
  setActivePinia(createPinia())
  store = useJobsStore()
  api.get.mockImplementation(async (id) => snapshot(id))
})
afterEach(() => {
  store.$dispose()
  vi.clearAllTimers()
  vi.useRealTimers()
})

describe('per-job read polling', () => {
  it('never overlaps a slow request and deduplicates repeated starts', async () => {
    const slow = deferred()
    api.get.mockReturnValueOnce(slow.promise)
    store.startPolling(1)
    store.startPolling('1')
    await vi.advanceTimersByTimeAsync(60000)
    expect(api.get).toHaveBeenCalledOnce()
    expect(store.pollingByJobId[1].status).toBe('polling')
    slow.resolve(snapshot())
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(1499)
    expect(api.get).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(1)
    expect(api.get).toHaveBeenCalledTimes(2)
  })

  it('aborts and ignores an in-flight response after stop, without scheduling another read', async () => {
    const slow = deferred()
    api.get.mockReturnValueOnce(slow.promise)
    store.startPolling(1)
    const config = api.get.mock.calls[0][1]
    expect(config.silentError).toBe(true)
    store.stopPolling(1)
    expect(config.signal.aborted).toBe(true)
    slow.resolve(snapshot())
    await vi.advanceTimersByTimeAsync(30000)
    expect(store.jobsById[1]).toBeUndefined()
    expect(store.pollingByJobId[1].status).toBe('stopped')
    expect(api.get).toHaveBeenCalledOnce()
  })

  it('waits for an aborted adapter on stop/start and never commits its stale result', async () => {
    const slow = deferred()
    const current = deferred()
    api.get.mockReturnValueOnce(slow.promise).mockReturnValueOnce(current.promise)
    store.startPolling(1)
    store.stopPolling(1)
    store.startPolling(1)
    expect(api.get).toHaveBeenCalledOnce()
    slow.resolve(snapshot(1, 'failed'))
    await vi.advanceTimersByTimeAsync(0)
    expect(api.get).toHaveBeenCalledTimes(2)
    expect(store.jobsById[1]).toBeUndefined()
    current.resolve(snapshot(1, 'awaiting_delivery'))
    await vi.advanceTimersByTimeAsync(0)
    expect(store.jobsById[1].status).toBe('awaiting_delivery')
  })

  it('backs off exponentially to the cap, keeps the last error visible, then resets after recovery', async () => {
    api.get.mockRejectedValueOnce(offline()).mockRejectedValueOnce(offline())
    store.startPolling(1, { interval: 100, maxInterval: 300 })
    await vi.advanceTimersByTimeAsync(0)
    expect(store.pollingByJobId[1]).toMatchObject({ status: 'backoff', consecutiveErrors: 1, lastError: 'offline', nextPollAt: 200 })
    await vi.advanceTimersByTimeAsync(199)
    expect(api.get).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(1)
    expect(store.pollingByJobId[1]).toMatchObject({ status: 'backoff', consecutiveErrors: 2, nextPollAt: 500 })
    await vi.advanceTimersByTimeAsync(300)
    expect(store.pollingByJobId[1]).toMatchObject({ status: 'waiting', consecutiveErrors: 0, lastError: null, nextPollAt: 600 })
    await vi.advanceTimersByTimeAsync(100)
    expect(api.get).toHaveBeenCalledTimes(4)
  })

  it('bounds consecutive transient retries and retains the last server snapshot', async () => {
    api.get.mockResolvedValueOnce(snapshot()).mockRejectedValue(offline())
    store.startPolling(1, { interval: 100, maxInterval: 200, maxErrors: 3 })
    await vi.advanceTimersByTimeAsync(10000)
    expect(api.get).toHaveBeenCalledTimes(4)
    expect(store.jobsById[1].status).toBe('running')
    expect(store.pollingByJobId[1]).toMatchObject({ status: 'error', consecutiveErrors: 3, lastError: 'offline', nextPollAt: null })
    api.get.mockResolvedValue(snapshot(1, 'succeeded'))
    store.startPolling(1)
    await vi.advanceTimersByTimeAsync(0)
    expect(store.pollingByJobId[1].status).toBe('completed')
  })

  it.each([408, 429, 500, 502, 503])('retries transient HTTP %s', async (status) => {
    api.get.mockRejectedValueOnce(Object.assign(new Error('temporary'), { response: { status } }))
    store.startPolling(1, { interval: 100 })
    await vi.advanceTimersByTimeAsync(200)
    expect(api.get).toHaveBeenCalledTimes(2)
    expect(store.pollingByJobId[1].status).toBe('waiting')
  })

  it.each([401, 403, 404, 422])('stops on terminal HTTP %s without retrying', async (status) => {
    api.get.mockRejectedValue(Object.assign(new Error('denied'), { response: { status } }))
    store.startPolling(1)
    await vi.advanceTimersByTimeAsync(60000)
    expect(api.get).toHaveBeenCalledOnce()
    expect(store.pollingByJobId[1]).toMatchObject({ status: 'error', lastError: 'denied' })
  })

  it.each([undefined, { data: {} }, { data: { id: 2, status: 'running' } }, snapshot(1, 'running', [null])])('stops on malformed snapshots: %j', async (response) => {
    api.get.mockResolvedValue(response)
    store.startPolling(1)
    await vi.advanceTimersByTimeAsync(60000)
    expect(api.get).toHaveBeenCalledOnce()
    expect(store.pollingByJobId[1].status).toBe('error')
    expect(store.jobsById[1]).toBeUndefined()
  })

  it.each(['succeeded', 'failed', 'cancelled', 'needs_review', 'blocked'])('settles %s without treating action states as success', async (status) => {
    api.get.mockResolvedValue(snapshot(1, status))
    store.startPolling(1)
    await vi.advanceTimersByTimeAsync(60000)
    expect(api.get).toHaveBeenCalledOnce()
    expect(store.jobsById[1].status).toBe(status)
    expect(store.pollingByJobId[1].status).toBe(['needs_review', 'blocked'].includes(status) ? 'paused' : 'completed')
  })

  it('continues awaiting_delivery and keeps cancellation isolated per job', async () => {
    api.get.mockImplementation(async (id) => snapshot(id, 'awaiting_delivery'))
    store.startPolling(1, { interval: 100 })
    store.startPolling(2, { interval: 100 })
    await vi.advanceTimersByTimeAsync(0)
    store.stopPolling(1)
    await vi.advanceTimersByTimeAsync(100)
    expect(api.get.mock.calls.map(([id]) => id)).toEqual([1, 2, 2])
    expect(JOB_STATUS.AWAITING_DELIVERY).toBe('awaiting_delivery')
    expect(TARGET_STATUS.NEEDS_REVIEW).toBe('needs_review')
  })

  it('normalizes target reliability fields and preserves them across list summaries', async () => {
    api.get.mockResolvedValue(snapshot(1, 'blocked', [{ id: 10, status: 'blocked', next_attempt_at: 'later', error_category: 'credentials', delivery_state: 'not_sent', checkpoint_json: '{private}' }]))
    await store.fetchJob(1)
    api.list.mockResolvedValue({ data: [{ id: 1, status: 'blocked' }] })
    await store.refreshList()
    expect(store.jobsById[1].targets[0]).toMatchObject({ nextAttemptAt: 'later', errorCategory: 'credentials', deliveryState: 'not_sent' })
  })

  it('rechecks a stale failed target and refuses a blind needs_review retry', async () => {
    store.jobsById[1] = { id: 1, status: 'failed', targets: [{ id: 10, status: 'failed' }] }
    api.get.mockResolvedValue(snapshot(1, 'needs_review', [{ id: 10, status: 'needs_review' }]))
    await expect(store.resubmitTarget(10, 1)).rejects.toThrow('無法安全重試')
    expect(api.resubmitTarget).not.toHaveBeenCalled()
  })

  it('cancels all timers and requests when the store scope is disposed', async () => {
    const slow = deferred()
    api.get.mockReturnValue(slow.promise)
    store.startPolling(1)
    store.startPolling(2)
    store.$dispose()
    expect(api.get.mock.calls.every(([, config]) => config.signal.aborted)).toBe(true)
    slow.resolve(snapshot())
    await vi.advanceTimersByTimeAsync(60000)
    expect(api.get).toHaveBeenCalledTimes(2)
    expect(store.jobsById[1]).toBeUndefined()
  })
})
