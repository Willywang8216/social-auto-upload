import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import TeachingRepurpose from '../TeachingRepurpose.vue'
import { flush, mount, textContent } from '@/components/__tests__/test-renderer'

vi.mock('vue', async original => ({ ...(await original()), vModelSelect: {} }))
const calls = vi.hoisted(() => ({ status: vi.fn(), kits: vi.fn(), tasks: vi.fn(), task: vi.fn(), createKit: vi.fn(),
  regenerate: vi.fn(), cancel: vi.fn(), asset: vi.fn(), revision: vi.fn(() => 0) }))
vi.mock('@/api/teaching-repurpose', () => ({ teachingRepurposeApi: calls }))
vi.mock('@/utils/auth', () => ({ getAuthRevision: calls.revision }))
let view, rows, details, enabled, owner
const copy = value => JSON.parse(JSON.stringify(value))
const original = () => ({ id: 1, request_id: 'original-preparation', state: 'prepared', attempts: 1, reason_code: '',
  source: { title: '私有離線測試文章', page_id: '00000000-0000-4000-8000-000000000004', slug: 'private-test',
    manuscript: '<script>not executed</script>\n原文章保持不變。', article_sha256: 'a'.repeat(64), article_version: 'b'.repeat(64) },
  kit_version: 'c'.repeat(64), package_version: 'e'.repeat(64), delivery_ids: ['43:cards', '43:video', '58:cards', '58:video', '61:cards', '125:cards'],
  article_read_only: true, requires_new_notion_review: true, package: { material: { assets: [], checks: [], deliveries: [], storyboard: {} } }, releases: [] })
beforeEach(() => {
  vi.useFakeTimers(); vi.clearAllMocks(); calls.revision.mockReturnValue(0)
  enabled = true; owner = true; rows = [original()]; details = new Map([[1, original()]])
  calls.status.mockImplementation(async () => ({ data: { enabled, generation_owner: owner, profile_id: 4, account_ids: [43, 58, 61, 125] } }))
  calls.kits.mockResolvedValue({ data: [{ version: 'c'.repeat(64), theme: 'ink' }] })
  calls.tasks.mockImplementation(async () => ({ data: copy(rows) }))
  calls.task.mockImplementation(async id => ({ data: copy(details.get(id)) }))
  calls.createKit.mockResolvedValue({ data: { version: 'd'.repeat(64) } })
  calls.regenerate.mockImplementation(async (_id, payload) => {
    const created = { ...original(), id: 2, state: 'queued', attempts: 0, request_id: payload.request_id,
      kit_version: payload.kit_version, delivery_ids: payload.deliveries, package: null, package_version: null }
    rows.unshift(created); details.set(2, created); return { code: 200, data: created }
  })
})
afterEach(() => { view?.unmount(); view = null; vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals() })
async function open() { view = mount(TeachingRepurpose); await flush(); return view.state }

describe('Teaching operational UI, local state only', () => {
  it('shows read-only source and has no article edit, approve or release action', async () => {
    const state = await open(); state.tab = 'article'; await flush()
    expect(textContent(view.root)).toContain('原文章保持不變')
    expect(state.notionLink).toContain('www.notion.so')
    expect(state.task.source.manuscript).toBe(original().source.manuscript)
    expect(Object.keys(calls)).not.toContain('release'); expect(Object.keys(calls)).not.toContain('approve')
  })
  it.each(['disabled', 'non-owner', 'already-released'])('%s prevents generation and does not modify the article', async mode => {
    if (mode === 'disabled') enabled = false
    if (mode === 'non-owner') owner = false
    if (mode === 'already-released') details.get(1).releases = [{ release_id: 'known', state: 'active', outcomes: [] }]
    const state = await open(); await state.regenerate()
    expect(state.canRegenerate).toBe(false); expect(calls.regenerate).not.toHaveBeenCalled(); expect(calls.createKit).not.toHaveBeenCalled()
    expect(state.task.source.manuscript).toBe(original().source.manuscript)
  })
  it('sends only a new immutable preparation request and selected formats', async () => {
    const state = await open(); state.theme = 'cream'; state.videos = [43, 58, 61, 125]
    await state.regenerate()
    const [id, payload] = calls.regenerate.mock.calls[0]
    expect(id).toBe(1); expect(Object.keys(payload).sort()).toEqual(['deliveries', 'kit_version', 'request_id'])
    expect(payload.deliveries).toHaveLength(8); expect(payload.kit_version).toBe('d'.repeat(64))
    expect(payload.request_id).not.toBe('original-preparation')
    expect(state.task.id).toBe(2); expect(state.task.requires_new_notion_review).toBe(true)
    expect(state.task.source.manuscript).toBe(original().source.manuscript)
    expect(state.notice).toContain('重新審閱')
  })
  it('retains the existing complete kit when its theme metadata is unavailable', async () => {
    calls.kits.mockResolvedValue({ data: [] }); const state = await open(); expect(state.theme).toBe('')
    await state.regenerate(); expect(calls.createKit).not.toHaveBeenCalled()
    expect(calls.regenerate.mock.calls[0][1].kit_version).toBe(original().kit_version)
  })
  it('an unknown response keeps the exact original request and never retries on click or poll', async () => {
    calls.regenerate.mockRejectedValue(new Error('response-unknown'))
    const state = await open(); await state.regenerate(); const intent = copy(state.pending)
    expect(intent.request_id).toBeTruthy(); expect(state.canRegenerate).toBe(false)
    await state.regenerate(); await vi.advanceTimersByTimeAsync(30000)
    expect(calls.regenerate).toHaveBeenCalledOnce()
    const existing = { ...original(), id: 2, state: 'queued', attempts: 0, request_id: intent.request_id,
      kit_version: intent.kit_version, delivery_ids: intent.deliveries }
    rows.unshift(existing); details.set(2, existing); await state.refresh()
    expect(state.pending).toBeNull(); expect(state.task.id).toBe(2); expect(state.notice).toContain('原請求')
    expect(calls.regenerate).toHaveBeenCalledOnce()
  })
  it('concurrent clicks create only one request', async () => {
    let resolve; calls.regenerate.mockImplementation(() => new Promise(done => { resolve = done }))
    const state = await open(); const first = state.regenerate(); await state.regenerate()
    expect(calls.regenerate).toHaveBeenCalledOnce()
    const payload = calls.regenerate.mock.calls[0][1]
    resolve({ data: { ...original(), id: 2, request_id: payload.request_id, kit_version: payload.kit_version, delivery_ids: payload.deliveries } })
    await first
  })
  it('a definite refusal remains visible after the read-only refresh', async () => {
    calls.regenerate.mockRejectedValue({ response: { status: 409, data: { msg: 'repurpose_delivery_capacity' } } })
    const state = await open(); await state.regenerate()
    expect(state.pending).toBeNull(); expect(state.error).toContain('資源不足')
    expect(calls.regenerate).toHaveBeenCalledOnce()
  })
  it('partial, queued and invalid aggregate flags cannot become delivery success', async () => {
    const state = await open(); const outcomes = state.task.delivery_ids.map(id => ({ delivery_id: id, status: 'active', actual_delivery_confirmed: false }))
    const release = { state: 'active', receipt_valid: true, errors: [], all_deliveries_confirmed: false, outcomes }
    expect(state.allConfirmed(release)).toBe(false)
    release.state = 'succeeded'; release.all_deliveries_confirmed = true; expect(state.allConfirmed(release)).toBe(false)
    outcomes.forEach(o => { o.status = 'succeeded'; o.actual_delivery_confirmed = true }); expect(state.allConfirmed(release)).toBe(true)
    release.errors = ['receipt_changed']; expect(state.allConfirmed(release)).toBe(false)
    release.errors = []; release.outcomes.pop(); expect(state.allConfirmed(release)).toBe(false)
  })
  it('cancellation retains the original source and attempt counter', async () => {
    details.get(1).state = 'running'; rows[0].state = 'running'
    calls.cancel.mockImplementation(async () => { details.get(1).state = 'cancelled'; return { code: 200 } })
    const state = await open(); await state.cancel()
    expect(calls.cancel.mock.calls[0][0]).toBe(1); expect(state.task.attempts).toBe(1)
    expect(state.task.source.manuscript).toBe(original().source.manuscript)
  })
  it('aborts pending reads and stops polling after unmount', async () => {
    let resolve; calls.task.mockImplementation(() => new Promise(done => { resolve = done }))
    const state = await open(); const signal = calls.task.mock.calls[0][1].signal
    view.unmount(); view = null; resolve({ data: original() }); await flush(); await vi.advanceTimersByTimeAsync(60000)
    expect(signal.aborted).toBe(true); expect(state.task).toBeNull(); expect(calls.task).toHaveBeenCalledOnce()
  })
  it('clears an already loaded private article when authentication changes', async () => {
    const revision = ref(0); calls.revision.mockImplementation(() => revision.value)
    const state = await open(); expect(state.task).not.toBeNull()
    revision.value++; await flush()
    expect(state.task).toBeNull(); expect(state.tasks).toEqual([]); expect(state.status).toBeNull()
    expect(state.canRegenerate).toBeFalsy(); expect(calls.regenerate).not.toHaveBeenCalled()
  })
  it('default-off without private storage shows status without querying absent tables', async () => {
    calls.status.mockResolvedValue({ data: { enabled: false, generation_owner: true, storage_configured: false,
      profile_id: 4, account_ids: [43, 58, 61, 125] } })
    const state = await open()
    expect(state.status.enabled).toBe(false); expect(state.task).toBeNull(); expect(state.canWrite).toBe(false)
    expect(calls.kits).not.toHaveBeenCalled(); expect(calls.tasks).not.toHaveBeenCalled()
    expect(textContent(view.root)).toContain('尚未啟用')
  })
})
