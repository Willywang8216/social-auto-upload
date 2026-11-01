import { webcrypto } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { canRetryTarget, publishActionHint, publishStatusLabel, publishStatusType, targetReliabilityDetails } from '../publish-status'

let logic, stored
const form = () => ({
  profileIds: [2, 1], selectedAccountIds: [9, 8], mediaFilePaths: ['one.mp4', 'two.mp4'],
  brief: 'Private draft text', options: { watermark: true }, schedule: { publishNow: true },
  accountDrafts: { 8: { title: 'A', message: 'B' } }, tiktokPostSettings: {},
})
beforeEach(async () => {
  vi.resetModules()
  stored = new Map()
  vi.stubGlobal('crypto', webcrypto)
  vi.stubGlobal('window', { sessionStorage: {
    getItem: (key) => stored.get(key) ?? null,
    setItem: (key, value) => stored.set(key, value),
  } })
  logic = await import('../publish-submission')
})
afterEach(() => vi.unstubAllGlobals())

describe('stable PublishCenter submission identity', () => {
  it('canonicalizes object keys and selection order but preserves ordered media and changed content', async () => {
    const first = await logic.withSubmissionId(form())
    const equivalent = await logic.withSubmissionId({ ...form(), profileIds: [1, 2, 1], selectedAccountIds: [8, 9], accountDrafts: { 8: { message: 'B', title: 'A' } } })
    expect(equivalent).toEqual(first)
    const changed = await logic.withSubmissionId({ ...form(), brief: 'Changed draft' })
    const reordered = await logic.withSubmissionId({ ...form(), mediaFilePaths: ['two.mp4', 'one.mp4'] })
    expect(changed.submissionId).not.toBe(first.submissionId)
    expect(reordered.submissionId).not.toBe(first.submissionId)
  })

  it('does not create duplicate work after an ambiguous error, including a reload', async () => {
    const campaigns = new Map()
    let uncertain = true
    const send = vi.fn(async (payload) => {
      // Models the backend's atomic replay contract; no HTTP or external calls.
      if (!campaigns.has(payload.submissionId)) campaigns.set(payload.submissionId, { campaignIds: [7], jobs: [], status: 'preparing' })
      expect([...stored.values()]).toContain(payload.submissionId) // saved before POST
      if (uncertain) {
        uncertain = false
        throw Object.assign(new Error('response lost after enqueue'), { code: 'ECONNABORTED' })
      }
      return { code: 200, data: campaigns.get(payload.submissionId) }
    })
    await expect(logic.createPublishSubmitter(send)(form())).rejects.toMatchObject({ uncertain: true })
    const beforeReload = logic.lastSubmission()
    expect(beforeReload.status).toBe('uncertain')
    vi.resetModules()
    logic = await import('../publish-submission')
    const result = await logic.createPublishSubmitter(send)(form())
    expect(result.submissionId).toBe(beforeReload.submissionId)
    expect(result.campaignIds).toEqual([7])
    expect(campaigns.size).toBe(1)
    const receipt = logic.lastSubmission()
    expect(receipt.status).toBe('preparing')
    expect([...stored.keys(), ...stored.values()].join(' ')).not.toMatch(/Private draft|one\.mp4|Authorization|test-secret/)
  })

  it('shares an in-flight POST for identical concurrent submissions', async () => {
    let resolve
    const send = vi.fn(() => new Promise((done) => { resolve = done }))
    const submit = logic.createPublishSubmitter(send)
    const first = submit(form())
    const second = submit(form())
    await vi.waitFor(() => expect(send).toHaveBeenCalledOnce())
    // Give both digests time to settle while the POST remains unresolved.
    await new Promise((done) => setTimeout(done, 10))
    expect(send).toHaveBeenCalledOnce()
    resolve({ code: 200, data: { jobs: [{ id: 10, status: 'pending' }] } })
    expect(await first).toEqual(await second)
  })

  it('snapshots the exact payload before awaiting its fingerprint', async () => {
    const payload = form()
    const pending = logic.withSubmissionId(payload)
    payload.options.watermark = false
    payload.mediaFilePaths.push('extra.mp4')
    const result = await pending
    expect(result.options.watermark).toBe(true)
    expect(result.mediaFilePaths).toEqual(['one.mp4', 'two.mp4'])
  })

  it('keeps a deterministic key across reload when sessionStorage is blocked', async () => {
    window.sessionStorage.getItem = () => { throw new Error('denied') }
    const first = await logic.withSubmissionId(form())
    vi.resetModules()
    logic = await import('../publish-submission')
    expect((await logic.withSubmissionId(form())).submissionId).toBe(first.submissionId)
  })

  it('does not send without a safe key if Web Crypto is unavailable', async () => {
    vi.stubGlobal('crypto', {})
    const send = vi.fn()
    await expect(logic.createPublishSubmitter(send)(form())).rejects.toThrow('HTTPS')
    expect(send).not.toHaveBeenCalled()
  })
})

describe('submission validation and visible lifecycle states', () => {
  it.each([
    undefined, null, {}, { code: 200 }, { code: 200, data: {} },
    { code: 200, data: { jobs: 'not an array' } },
    { code: 200, data: { jobs: [{}] } },
    { code: 200, data: { jobs: [{ id: 1, status: 'succeeded', targets: 'bad' }] } },
    { code: 200, data: { campaignIds: [null], status: 'preparing' } },
  ])('rejects malformed/undefined responses instead of showing success: %j', async (response) => {
    await expect(logic.createPublishSubmitter(async () => response)(form())).rejects.toMatchObject({ code: 'ERR_INVALID_RESPONSE', uncertain: true })
    expect(logic.submissionPresentation(logic.lastSubmission()).type).toBe('warning')
  })

  it.each(['blocked', 'awaiting_delivery', 'needs_review'])('keeps %s distinct and non-retryable at job and target level', (status) => {
    expect(publishStatusLabel(status)).not.toBe(status)
    expect(publishStatusType(status)).not.toBe('success')
    expect(publishStatusType(status)).not.toBe('danger')
    expect(canRetryTarget({ status })).toBe(false)
    const result = logic.submissionPresentation({ jobs: [{ id: 1, status: 'succeeded', targets: [{ status }] }] })
    expect(result.type).not.toBe('success')
    expect(result.message).toContain(publishStatusLabel(status))
    expect(publishActionHint(status)).toBeTruthy()
  })

  it('only shows success for confirmed completed jobs, not queued/preparing/empty work', () => {
    expect(logic.submissionPresentation({ jobs: [{ id: 1, status: 'pending' }] }).type).toBe('info')
    expect(logic.submissionPresentation({ status: 'preparing', campaignIds: [7], jobs: [] }).type).toBe('info')
    expect(logic.submissionPresentation({}).type).toBe('warning')
    expect(logic.submissionPresentation({ jobs: [{ id: 1, status: 'succeeded' }] }).type).toBe('success')
  })

  it('renders both target field spellings without leaking checkpoint_json', () => {
    const camel = { status: 'needs_review', nextAttemptAt: '2026-10-20T10:00:00Z', errorCategory: 'ambiguous', deliveryState: 'unknown' }
    const snake = { status: 'needs_review', next_attempt_at: camel.nextAttemptAt, error_category: 'ambiguous', delivery_state: 'unknown', checkpoint_json: 'test-secret' }
    expect(targetReliabilityDetails(camel)).toBe(targetReliabilityDetails(snake))
    expect(targetReliabilityDetails(snake)).toContain(camel.nextAttemptAt)
    expect(targetReliabilityDetails(snake)).not.toContain('test-secret')
    expect(canRetryTarget({ status: 'failed', delivery_state: 'unknown' })).toBe(false)
    expect(canRetryTarget({ status: 'failed', delivery_state: 'not_sent' })).toBe(true)
  })

  it('derives real preparation counts and job references from the campaign read API', () => {
    const campaign = logic.campaignPreparation({ data: { id: 7, status: 'preparing', posts: [
      { status: 'ready', jobId: 10, platform: 'tiktok' }, { status: 'preparing' },
    ], jobs: [{ id: 10, platform: 'tiktok' }], jobIds: [10, 11] } }, 7)
    expect(campaign.progressLabel).toBe('已準備 1 / 2 個貼文')
    expect(campaign.jobs.map((job) => job.id)).toEqual([10, 11])
    expect(() => logic.campaignPreparation(undefined, 7)).toThrow()
  })
})
