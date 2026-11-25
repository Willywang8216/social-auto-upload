import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import JobsView from '../JobsView.vue'
import { useJobsStore } from '@/stores/jobs'
import { findNodes, flush, mount, textContent } from '@/components/__tests__/test-renderer'

const api = vi.hoisted(() => ({ entities: vi.fn(), entity: vi.fn(), list: vi.fn(), job: vi.fn(), error: vi.fn(), replace: vi.fn() }))
vi.mock('@/api/jobs', () => ({ jobsApi: { publishEntities: api.entities, publishEntity: api.entity, list: api.list, get: api.job } }))
vi.mock('vue-router', () => ({ useRoute: () => ({ query: {} }), useRouter: () => ({ replace: api.replace }) }))
vi.mock('@/utils/request', () => ({ showRequestError: api.error }))
vi.mock('element-plus', () => ({ ElMessage: { success: vi.fn(), error: vi.fn() }, ElMessageBox: {} }))
vi.mock('@/stores/profiles', () => ({ useProfilesStore: () => ({ profiles: [], accountsByProfile: {}, refreshProfiles: async () => {} }) }))
vi.mock('@/components/PublishJobProgress.vue', () => ({ default: { props: ['job', 'polling'], render: () => null } }))

const legacy = (id, revision = 'old') => ({ entityId: `job-${id}`, entityType: 'job', status: 'awaiting_delivery', revision, posts: [], campaigns: [], mediaItems: [],
  jobs: [{ id, platform: 'twitter', profileId: 1, title: `Clip ${id}`, status: 'awaiting_delivery', totalTargets: 1, targets: [
    { id: id + 1000, jobId: id, status: 'awaiting_delivery', accountName: `Account ${id}`, scheduleAt: '2026-11-26T12:00:00',
      deliveryState: 'awaiting_delivery', providerReceipt: { provider: 'twitter', id: `receipt-${id}`, status: 'processing' }, retryEligibility: { allowed: false } },
  ] }],
})
function page(all, { offset, limit }) {
  const items = all.slice(offset, offset + limit)
  return { code: 200, data: { items, offset, limit, total: all.length, hasMore: offset + items.length < all.length } }
}
let view, store
beforeEach(() => {
  vi.useFakeTimers()
  vi.resetAllMocks()
  setActivePinia(createPinia())
  store = useJobsStore()
  api.entities.mockImplementation(async (query) => page([], query))
  api.list.mockResolvedValue({ data: [] })
  api.job.mockImplementation(async (id) => ({ data: legacy(id).jobs[0] }))
})
afterEach(() => {
  view?.unmount()
  view = null
  store.$dispose()
  vi.clearAllTimers()
  vi.useRealTimers()
})
async function open() {
  view = mount(JobsView)
  await flush()
  await flush()
  return view.state
}

describe('queue reliability and management wiring', () => {
  it('renders legacy targets/receipts and opens the existing job detail drawer', async () => {
    const record = legacy(7)
    record.campaigns = [{ id: 9, status: 'needs_review', last_error: '帳號需重新連線 token=private-campaign-token' }]
    record.jobs[0].targets[0].providerReceipt.response = 'private-receipt-response'
    api.entity.mockResolvedValue({ data: record })
    const state = await open()
    await state.openEntity(record)
    await flush()
    expect(textContent(view.root)).toContain('發佈工作與目的地')
    expect(textContent(view.root)).toContain('Account 7')
    expect(textContent(view.root)).toContain('receipt-7')
    expect(textContent(view.root)).toContain('原因：帳號需重新連線')
    expect(textContent(view.root)).not.toMatch(/private-campaign-token|private-receipt-response/)
    const button = findNodes(view.root, (node) => node.type === 'el-button' && textContent(node).includes('開啟任務詳情'))[0]
    expect(button).toBeTruthy()
    button.props.onClick()
    await flush()
    expect(state.drawerVisible).toBe(true)
    expect(state.selectedJobId).toBe(7)
    expect(state.selectedJob.targets[0].status).toBe('awaiting_delivery')
    expect(api.job.mock.calls[0][0]).toBe(7)
  })

  it('makes the work-history button render jobs with a working detail entry point', async () => {
    api.list.mockResolvedValue({ data: [{ ...legacy(7).jobs[0], targets: undefined }] })
    const state = await open()
    const history = findNodes(view.root, (node) => node.type === 'el-button' && textContent(node) === '工作紀錄')[0]
    await history.props.onClick()
    await flush()
    expect(state.historyDrawerVisible).toBe(true)
    expect(api.list).toHaveBeenCalledOnce()
    const detail = findNodes(view.root, (node) => node.type === 'el-button' && textContent(node).includes('任務 #7'))[0]
    expect(detail).toBeTruthy()
    detail.props.onClick()
    await flush()
    expect(state.selectedJobId).toBe(7)
    expect(state.drawerVisible).toBe(true)
    expect(api.job).toHaveBeenCalledOnce()
  })

  it('quietly refreshes all 250 loaded cards in bounded chunks with the same filters and next offset', async () => {
    const all = Array.from({ length: 350 }, (_, index) => legacy(index + 1))
    api.entities.mockImplementation(async (query) => page(all, query))
    const state = await open()
    state.allDates = true
    state.entityStatusFilter = 'awaiting_delivery'
    state.entityPlatformFilters = ['twitter']
    state.entityProfileFilters = [1]
    state.entityKeyword = 'Clip'
    await flush()
    await state.loadEntities(true)
    for (let index = 0; index < 4; index++) await state.loadMoreEntities()
    expect(state.entities).toHaveLength(250)
    expect(state.entityOffset).toBe(250)
    const revised = all.map((record) => ({ ...record, revision: 'new' }))
    api.entities.mockImplementation(async (query) => page(revised, query)).mockClear()
    await state.refreshEntitiesQuietly()
    await flush()
    expect(state.entities).toHaveLength(250)
    expect(state.entities.every((record) => record.revision === 'new')).toBe(true)
    expect(new Set(state.entities.map((record) => record.entityId)).size).toBe(250)
    expect(state.entityOffset).toBe(250)
    expect(state.entityTotal).toBe(350)
    expect(state.entityHasMore).toBe(true)
    expect(api.entities.mock.calls.map(([query]) => [query.offset, query.limit])).toEqual([[0, 200], [200, 50]])
    expect(api.entities.mock.calls.every(([query]) => query.status === 'awaiting_delivery' && query.platforms === 'twitter' && query.profileIds === '1' && query.q === 'Clip' && query.date === undefined)).toBe(true)
  })

  it.each(['error', 'non-progress'])('preserves the full retained list and shows feedback when quiet refresh hits %s', async (failure) => {
    const all = Array.from({ length: 350 }, (_, index) => legacy(index + 1))
    const state = await open()
    state.entities = all.slice(0, 250)
    state.entityOffset = 250
    state.entityTotal = 350
    state.entityHasMore = true
    api.entities.mockClear().mockImplementationOnce(async (query) => page(all, query))
    if (failure === 'error') api.entities.mockRejectedValueOnce(new Error('offline'))
    else api.entities.mockResolvedValueOnce({ data: { items: [], offset: 200, limit: 50, total: 350, hasMore: true } })
    await state.refreshEntitiesQuietly()
    await flush()
    expect(state.entities).toEqual(all.slice(0, 250))
    expect(state.entityOffset).toBe(250)
    expect(state.entityHasMore).toBe(true)
    expect(state.entityTotal).toBe(350)
    expect(textContent(view.root)).toContain('已保留目前清單')
    expect(api.entities).toHaveBeenCalledTimes(2)
  })
})
