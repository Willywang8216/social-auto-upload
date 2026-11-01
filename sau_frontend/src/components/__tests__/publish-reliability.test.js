import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import PublishJobProgress from '../PublishJobProgress.vue'
import SystemHealthCard from '../SystemHealthCard.vue'
import { publishStatusLabel } from '@/utils/publish-status'
import { findNodes, flush, mount, textContent } from './test-renderer'

const api = vi.hoisted(() => ({ health: vi.fn(), provider: vi.fn() }))
vi.mock('@/utils/request', () => ({ http: { get: api.health } }))
vi.mock('@/api/tiktok', () => ({ tiktokApi: { getPublishStatus: api.provider } }))
let view
beforeEach(() => {
  vi.useFakeTimers()
  vi.resetAllMocks()
})
afterEach(() => {
  view?.unmount()
  view = null
  vi.clearAllTimers()
  vi.useRealTimers()
})

describe('job progress rendering', () => {
  it.each(['blocked', 'awaiting_delivery', 'needs_review'])('renders %s and the polling error, not success or a blind retry', (status) => {
    const refresh = vi.fn()
    view = mount(PublishJobProgress, {
      job: { id: 7, platform: 'reddit', status, targets: [{ id: 10, status, next_attempt_at: 'later', error_category: 'ambiguous', delivery_state: 'unknown', checkpoint_json: 'private-checkpoint' }] },
      polling: { status: 'error', lastError: 'offline' }, onRefresh: refresh,
    })
    expect(textContent(view.root)).toContain(publishStatusLabel(status))
    expect(textContent(view.root)).toContain('自動更新已停止')
    expect(textContent(view.root)).toContain('offline')
    expect(textContent(view.root)).toContain('下次嘗試：later')
    expect(textContent(view.root)).not.toContain('private-checkpoint')
    expect(findNodes(view.root, (node) => node.type === 'el-tag').every((node) => !['success', 'danger'].includes(node.props.type))).toBe(true)
    const button = findNodes(view.root, (node) => node.type === 'el-button' && textContent(node).includes('更新狀態'))[0]
    button.props.onClick()
    expect(refresh).toHaveBeenCalledOnce()
    expect(api.provider).not.toHaveBeenCalled()
  })

  it('does not overlap a slow provider read and cancels it on unmount', async () => {
    let resolve
    api.provider.mockReturnValue(new Promise((done) => { resolve = done }))
    view = mount(PublishJobProgress, { job: { id: 7, platform: 'tiktok', status: 'awaiting_delivery' } })
    await vi.advanceTimersByTimeAsync(40000)
    expect(api.provider).toHaveBeenCalledOnce()
    const signal = api.provider.mock.calls[0][1].signal
    view.unmount()
    view = null
    expect(signal.aborted).toBe(true)
    resolve({ data: [{ status: 'processing' }] })
    await vi.advanceTimersByTimeAsync(40000)
    expect(api.provider).toHaveBeenCalledOnce()
  })
})

describe('optional publish health', () => {
  const system = { data: { offload: { healthy: true }, publish: { targetsByStatus: { pending: 2 } } } }
  it('uses publish_health.py totals/stale/reconnect schema', async () => {
    api.health.mockImplementation(async (url) => url === '/api/system/health' ? system : { data: {
      timestamp: '2026-10-20T12:00:00', totals: { succeeded: 8, failed: 2, overallSuccessRate: 0.8, blocked: 1, awaiting_delivery: 2, needs_review: 3 },
      staleTargets: [{ targetId: 1 }], accountsNeedingReconnect: [{ id: 3 }], recentFailures: [{ count: 2 }], platforms: [],
    } })
    view = mount(SystemHealthCard)
    await flush()
    expect(textContent(view.root)).toContain('80%')
    expect(textContent(view.root)).toContain('停滯任務：1')
    expect(textContent(view.root)).toContain('需重新連線帳號：1')
    expect(textContent(view.root)).toContain('需人工確認 3')
    expect(view.state.publishCounts.succeeded).toBe(8)
    expect(api.health.mock.calls.every(([, , config]) => config.silentError === true)).toBe(true)
  })

  it.each([404, 503])('degrades gracefully when publish health returns HTTP %s', async (status) => {
    api.health.mockImplementation(async (url) => {
      if (url === '/api/system/health') return system
      throw { response: { status } }
    })
    view = mount(SystemHealthCard)
    await flush()
    expect(textContent(view.root)).toContain('發佈健康摘要暫不可用')
    expect(view.state.publishCounts.pending).toBe(2)
    expect(view.state.offloadLabel).toBe('正常')
    expect(view.state.loading).toBe(false)
  })

  it('does not label malformed health data as healthy', async () => {
    api.health.mockResolvedValue({ data: {} })
    view = mount(SystemHealthCard)
    await flush()
    expect(view.state.publishHealthUnavailable).toBe(true)
    expect(view.state.offloadLabel).toBe('未知')
  })
})
