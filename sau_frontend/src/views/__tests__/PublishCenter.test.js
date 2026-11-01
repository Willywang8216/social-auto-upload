import { webcrypto } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import PublishCenter from '../PublishCenter.vue'
import { useJobsStore } from '@/stores/jobs'
import { flush, mount, textContent } from '@/components/__tests__/test-renderer'

const api = vi.hoisted(() => ({ submit: vi.fn(), campaign: vi.fn(), job: vi.fn(), toast: vi.fn() }))
vi.mock('@/api/publish-center', () => ({ publishCenterApi: { submit: api.submit } }))
vi.mock('@/api/campaigns', () => ({ campaignsApi: { getCampaign: api.campaign } }))
vi.mock('@/api/jobs', () => ({ jobsApi: { get: api.job } }))
vi.mock('@/api/material', () => ({ materialApi: {} }))
vi.mock('@/api/tiktok', () => ({ tiktokApi: {} }))
vi.mock('@/utils/auth', () => ({ getToken: () => '' }))
vi.mock('element-plus', () => ({ ElMessage: { error: api.toast, success: api.toast }, ElMessageBox: {} }))
vi.mock('@/components/TikTokPostSettings.vue', () => ({ default: { render: () => null } }))
vi.mock('@/components/TikTokReviewModal.vue', () => ({ default: { render: () => null } }))
vi.mock('@/stores/profiles', () => ({ useProfilesStore: () => ({ profiles: [], refreshProfiles: async () => {} }) }))
vi.mock('@/stores/publish-templates', () => ({ usePublishTemplatesStore: () => ({ templates: [], refresh: async () => {} }) }))

let view, store
beforeEach(() => {
  vi.useFakeTimers()
  vi.resetAllMocks()
  const session = new Map()
  vi.stubGlobal('crypto', webcrypto)
  vi.stubGlobal('window', { sessionStorage: {
    getItem: (key) => session.get(key) ?? null,
    setItem: (key, value) => session.set(key, value),
  } })
  setActivePinia(createPinia())
  store = useJobsStore()
  api.job.mockImplementation(async (id) => ({ code: 200, data: { id, status: 'awaiting_delivery', targets: [] } }))
})
afterEach(() => {
  view?.unmount()
  view = null
  store.$dispose()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})
async function open() {
  view = mount(PublishCenter)
  await flush()
  return view.state
}

describe('PublishCenter reliability wiring', () => {
  it.each([undefined, { code: 200, data: {} }])('submitAfterTiktokReview never announces success for %j', async (response) => {
    api.submit.mockResolvedValue(response)
    const state = await open()
    await state.submitAfterTiktokReview()
    await flush()
    expect(state.submitting).toBe(false)
    expect(state.submitResult.status).toBe('uncertain')
    expect(state.submitDisplay.type).toBe('warning')
    expect(textContent(view.root)).toContain('送出結果尚未確認')
    expect(textContent(view.root)).not.toContain('送出成功')
    expect(api.job).not.toHaveBeenCalled()
    expect(api.toast).not.toHaveBeenCalled() // inline error owns presentation
  })

  it('reuses the key after an ambiguous failure and component reload, without bypassing review states', async () => {
    let key
    api.submit.mockImplementationOnce(async (payload) => {
      key = payload.submissionId
      throw Object.assign(new Error('response lost'), { code: 'ECONNABORTED' })
    }).mockImplementationOnce(async (payload) => {
      expect(payload.submissionId).toBe(key)
      return { code: 200, data: { jobs: [{ id: 10, status: 'needs_review' }], status: 'needs_review' } }
    })
    api.job.mockResolvedValue({ code: 200, data: { id: 10, status: 'needs_review', targets: [] } })
    const state = await open()
    await state.submitAfterTiktokReview()
    view.unmount()
    const reloaded = await open()
    expect(reloaded.submitResult.submissionId).toBe(key)
    expect(reloaded.submitResult.status).toBe('uncertain')
    await reloaded.submitAfterTiktokReview()
    await flush()
    expect(reloaded.submitDisplay.type).toBe('warning')
    expect(textContent(view.root)).toContain('需人工確認')
    expect(textContent(view.root)).toContain('勿直接重試')
    expect(store.pollingByJobId[10].status).toBe('paused')
    expect(api.submit.mock.calls.every(([, config]) => config.silentError)).toBe(true)
  })

  it('rejects double clicks and follows asynchronous preparation into provider-pending jobs', async () => {
    api.submit.mockResolvedValue({ code: 200, data: { campaignIds: [7], jobs: [], status: 'preparing' } })
    api.campaign.mockResolvedValueOnce({ code: 200, data: { id: 7, status: 'preparing', posts: [{ status: 'preparing' }] } })
      .mockResolvedValue({ code: 200, data: { id: 7, status: 'queued', jobs: [{ id: 10 }], posts: [{ status: 'ready' }] } })
    const state = await open()
    await Promise.all([state.submitAfterTiktokReview(), state.submitAfterTiktokReview()])
    await flush()
    expect(api.submit).toHaveBeenCalledOnce()
    expect(state.preparations[7].progressLabel).toBe('已準備 0 / 1 個貼文')
    expect(state.submitDisplay.type).toBe('info')
    await vi.advanceTimersByTimeAsync(2500)
    expect(state.submittedJobs[0].id).toBe(10)
    expect(textContent(view.root)).toContain('等待平台確認')
    expect(state.submitDisplay.type).not.toBe('success')
    expect(state.preparationPolling[7].status).toBe('completed')
  })

  it('does not report success while another campaign is still unavailable', async () => {
    api.submit.mockResolvedValue({ code: 200, data: { campaignIds: [7, 8], jobs: [], status: 'preparing' } })
    api.campaign.mockImplementation(async (id) => {
      if (id === 8) throw Object.assign(new Error('offline'), { code: 'ERR_NETWORK' })
      return { data: { id: 7, status: 'queued', jobs: [{ id: 10 }] } }
    })
    api.job.mockResolvedValue({ data: { id: 10, status: 'succeeded', targets: [] } })
    const state = await open()
    await state.submitAfterTiktokReview()
    await flush()
    expect(state.submittedJobs[0].status).toBe('succeeded')
    expect(state.preparationPolling[8].status).toBe('backoff')
    expect(state.submitDisplay.type).not.toBe('success')
    expect(state.submitDisplay.message).toContain('準備')
  })

  it('cancels preparation on unmount and ignores late jobs', async () => {
    let resolve
    api.submit.mockResolvedValue({ code: 200, data: { campaignIds: [7], jobs: [], status: 'preparing' } })
    api.campaign.mockReturnValue(new Promise((done) => { resolve = done }))
    const state = await open()
    await state.submitAfterTiktokReview()
    const signal = api.campaign.mock.calls[0][1].signal
    view.unmount()
    view = null
    expect(signal.aborted).toBe(true)
    resolve({ data: { id: 7, status: 'queued', jobs: [{ id: 10 }] } })
    await vi.advanceTimersByTimeAsync(10000)
    expect(api.job).not.toHaveBeenCalled()
    expect(api.campaign).toHaveBeenCalledOnce()
  })
})
