import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import CalendarView from '../CalendarView.vue'
import { useJobsStore } from '@/stores/jobs'
import { findNodes, flush, mount, textContent } from '@/components/__tests__/test-renderer'

const api = vi.hoisted(() => ({ entities: vi.fn(), entity: vi.fn(), error: vi.fn(), replace: vi.fn(), route: { query: {} } }))
vi.mock('@/api/jobs', () => ({ jobsApi: { publishEntities: api.entities, publishEntity: api.entity } }))
vi.mock('vue-router', () => ({ useRoute: () => api.route, useRouter: () => ({ replace: api.replace }) }))
vi.mock('@/utils/request', () => ({ showRequestError: api.error }))
vi.mock('element-plus', () => ({ ElMessage: { success: vi.fn(), error: vi.fn() }, ElMessageBox: {} }))
vi.mock('@/stores/profiles', () => ({ useProfilesStore: () => ({ profiles: [], accountsByProfile: {}, refreshProfiles: async () => {} }) }))

const legacy = (id, status = 'awaiting_delivery') => ({ entityId: `job-${id}`, entityType: 'job', status, campaigns: [], posts: [], mediaItems: [],
  jobs: [{ id, platform: 'twitter', title: `Calendar ${id}`, status, targets: [{ id: 1000 + id, jobId: id, status,
    accountName: `Account ${id}`, fileRef: 'clip.mp4', scheduleAt: `2026-11-${String(1 + id % 28).padStart(2, '0')}T12:00:00`,
    deliveryState: 'awaiting_delivery', providerReceipt: { provider: 'twitter', id: `receipt-${id}`, status: 'processing' }, retryEligibility: { allowed: false },
  }] }],
})
function page(all, { offset, limit }) {
  const items = all.slice(offset, offset + limit)
  return { code: 200, data: { items, offset, limit, total: all.length, hasMore: offset + items.length < all.length } }
}
let view, store
beforeEach(() => {
  vi.useFakeTimers()
  vi.resetAllMocks()
  api.route.query = { month: '2026-11' }
  setActivePinia(createPinia())
  store = useJobsStore()
  api.entities.mockImplementation(async (query) => page([], query))
})
afterEach(() => {
  view?.unmount()
  view = null
  store.$dispose()
  vi.clearAllTimers()
  vi.useRealTimers()
})
async function open() {
  view = mount(CalendarView)
  await flush()
  await flush()
  return view.state
}

describe('calendar entity reliability wiring', () => {
  it.each(['failed', 'cancelled'])('renders a pending destination in a %s group with pending-only enabled', async (status) => {
    const record = legacy(7, status)
    record.jobs[0].targets.push({ ...record.jobs[0].targets[0], id: 1008, status: 'pending', accountName: 'Pending sibling' })
    api.entities.mockImplementation(async (query) => page([record], query))
    const state = await open()
    expect(state.showAll).toBe(false)
    expect(state.events.map((event) => event.targetStatus)).toEqual(['pending'])
    expect(findNodes(view.root, (node) => node.props.class?.includes('cal-ev') && node.props.title?.includes('Pending sibling'))).toHaveLength(1)
    expect(api.entities.mock.calls[0][0]).not.toHaveProperty('status')
    state.showAll = true
    await flush()
    await flush()
    expect(state.events.map((event) => event.targetStatus)).toEqual([status, 'pending'])
  })

  it('loads a 201-entity month instead of silently losing the second page', async () => {
    const all = Array.from({ length: 201 }, (_, index) => legacy(index + 1))
    api.entities.mockImplementation(async (query) => page(all, query))
    const state = await open()
    expect(state.events).toHaveLength(201)
    expect(state.events.some((event) => event.entityId === 'job-201')).toBe(true)
    expect(state.loadWarning).toBe('')
    expect(api.entities.mock.calls.map(([query]) => [query.offset, query.limit])).toEqual([[0, 200], [200, 200]])
    expect(api.entities.mock.calls.every(([query]) => query.month === '2026-11')).toBe(true)
  })

  it.each(['error', 'non-progress'])('keeps loaded pages and displays incomplete feedback on a later %s', async (failure) => {
    const all = Array.from({ length: 201 }, (_, index) => legacy(index + 1))
    api.entities.mockImplementationOnce(async (query) => page(all, query))
    if (failure === 'error') api.entities.mockRejectedValueOnce(new Error('offline'))
    else api.entities.mockResolvedValueOnce({ data: { items: [], offset: 200, limit: 200, total: 201, hasMore: true } })
    const state = await open()
    expect(state.events).toHaveLength(200)
    expect(state.loadWarning).toContain('已載入 200')
    expect(textContent(view.root)).toContain('行事曆尚未完整載入')
    expect(findNodes(view.root, (node) => node.type === 'el-button' && textContent(node).includes('重新載入排程'))).toHaveLength(1)
    expect(api.entities).toHaveBeenCalledTimes(2)
    expect(api.error).toHaveBeenCalledOnce()
  })

  it('renders safe legacy destination/receipt details and the existing job management link', async () => {
    const record = legacy(7)
    record.campaigns = [{ id: 9, status: 'needs_review', last_error: '帳號需重新連線 token=private-campaign-token' }]
    record.jobs[0].targets[0].providerReceipt.request = 'private-receipt-request'
    api.entity.mockResolvedValue({ data: record })
    const state = await open()
    await state.openEvent({ entityId: record.entityId })
    await flush()
    expect(textContent(view.root)).toContain('發佈工作與目的地')
    expect(textContent(view.root)).toContain('等待平台確認')
    expect(textContent(view.root)).toContain('Account 7')
    expect(textContent(view.root)).toContain('receipt-7')
    expect(textContent(view.root)).toContain('原因：帳號需重新連線')
    expect(textContent(view.root)).not.toMatch(/private-campaign-token|private-receipt-request/)
    expect(findNodes(view.root, (node) => node.type === 'router-link' && node.props.to === '/jobs?job=7')).toHaveLength(1)
    expect(findNodes(view.root, (node) => node.type === 'el-button' && textContent(node).includes('重試'))).toHaveLength(0)
  })
})
