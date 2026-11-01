import { invalidResponse, isTransientError } from './polling'
import { publishActionHint, publishStatusLabel } from './publish-status'

const KEY_PREFIX = 'sau.publish-submission.v1.'
const LAST_RECEIPT_KEY = `${KEY_PREFIX}last`
const memoryIds = new Map()

function canonicalize(value) {
  if (Array.isArray(value)) return value.map(canonicalize)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort()
      .filter((key) => value[key] !== undefined)
      .map((key) => [key, canonicalize(value[key])]))
  }
  return value
}

export function logicalSubmission(payload) {
  // Snapshot before awaiting: edits while submitting must not change the request
  // behind its key. Account/profile selection is a set; media order is meaningful.
  const { submissionId: _ignored, ...data } = JSON.parse(JSON.stringify(payload))
  for (const key of ['profileIds', 'selectedAccountIds']) {
    if (Array.isArray(data[key])) data[key] = [...new Set(data[key])].sort((a, b) => String(a).localeCompare(String(b)))
  }
  return canonicalize(data)
}

export async function withSubmissionId(payload) {
  const data = logicalSubmission(payload)
  if (!globalThis.crypto?.subtle) {
    throw new Error('無法產生安全提交編號；請使用 HTTPS 或 localhost 後再送出。')
  }
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(data)))
  const fingerprint = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('')
  let submissionId = memoryIds.get(fingerprint)
  if (!submissionId) {
    try {
      const key = `${KEY_PREFIX}${fingerprint}`
      const existing = window.sessionStorage.getItem(key)
      submissionId = existing && /^[\w-]{8,80}$/.test(existing) ? existing : crypto.randomUUID()
      // Persist BEFORE the POST, not just after success. No payload or credentials.
      window.sessionStorage.setItem(key, submissionId)
    } catch {
      // Storage-blocked browsers still reuse the same key after reload.
      submissionId = `pc-${fingerprint.slice(0, 60)}`
    }
    memoryIds.set(fingerprint, submissionId)
  }
  return { ...data, submissionId }
}

const validId = (id) => (typeof id === 'number' && Number.isInteger(id) && id > 0)
  || (typeof id === 'string' && id.trim().length > 0)
const validStatus = (status) => status == null || typeof status === 'string'
const validJob = (job) => job && !Array.isArray(job) && validId(job.id) && validStatus(job.status)
  && (job.targets == null || (Array.isArray(job.targets) && job.targets.every((target) => target && typeof target.status === 'string')))

export function parseSubmissionResponse(response) {
  const data = response?.data
  if (!response || (response.code !== 200 && response.success !== true) || !data || typeof data !== 'object' || Array.isArray(data)) {
    throw invalidResponse('無法確認送出結果；請以相同內容及提交編號確認，勿建立重複發佈。')
  }
  const jobs = data.jobs ?? []
  const campaignIds = data.campaignIds ?? []
  if (!validStatus(data.status) || !Array.isArray(jobs) || !jobs.every(validJob)
      || !Array.isArray(campaignIds) || !campaignIds.every(validId)
      || (!jobs.length && !campaignIds.length && !['blocked', 'needs_review'].includes(data.status))) {
    throw invalidResponse('回應缺少有效的任務或準備編號；結果尚未確認，請勿重複發佈。')
  }
  return { ...data, jobs, campaignIds }
}

export function rememberSubmission(data) {
  if (!data?.submissionId) return
  // Only a receipt is persisted. In particular: no drafts, options, auth headers,
  // provider checkpoints, signed media URLs or account configuration.
  const receipt = {
    submissionId: data.submissionId,
    status: data.status,
    campaignIds: data.campaignIds || [],
    jobs: (data.jobs || []).map(({ id, platform, status }) => ({ id, platform, status })),
  }
  try { window.sessionStorage.setItem(LAST_RECEIPT_KEY, JSON.stringify(receipt)) } catch { /* optional */ }
}

export function lastSubmission() {
  try {
    const data = JSON.parse(window.sessionStorage.getItem(LAST_RECEIPT_KEY) || 'null')
    if (typeof data?.submissionId === 'string' && validStatus(data.status) && Array.isArray(data.jobs) && data.jobs.every(validJob)
      && Array.isArray(data.campaignIds) && data.campaignIds.every(validId)) return data
  } catch { /* storage unavailable or stale receipt */ }
  return null
}

export function createPublishSubmitter(send) {
  const inFlight = new Map()
  return async (payload) => {
    const keyed = await withSubmissionId(payload)
    if (inFlight.has(keyed.submissionId)) return inFlight.get(keyed.submissionId)
    const pending = (async () => {
      rememberSubmission({ submissionId: keyed.submissionId, status: 'uncertain' })
      try {
        const data = parseSubmissionResponse(await send(keyed))
        const result = { ...data, submissionId: keyed.submissionId }
        rememberSubmission(result)
        return result
      } catch (error) {
        error.submissionId = keyed.submissionId
        error.uncertain = error.code === 'ERR_INVALID_RESPONSE' || isTransientError(error)
        rememberSubmission({ submissionId: keyed.submissionId, status: error.uncertain ? 'uncertain' : 'rejected' })
        throw error
      }
    })()
    inFlight.set(keyed.submissionId, pending)
    try { return await pending } finally { inFlight.delete(keyed.submissionId) }
  }
}

export function submissionPresentation(data) {
  if (!data) return { type: 'warning', message: '尚未確認送出結果' }
  if (data.status === 'uncertain') return {
    type: 'warning', message: '送出結果尚未確認。請先檢查發佈佇列；相同內容再次送出會沿用提交編號，不會另建工作。',
  }
  if (data.error || data.status === 'rejected') return { type: 'error', message: data.error || '送出未被接受，請確認權限與設定。' }
  const jobs = data.jobs || []
  const states = [data.status, ...(data.campaigns || []).map((campaign) => campaign.status),
    ...jobs.flatMap((job) => [job.status, ...(job.targets || []).map((target) => target.status)])]
  for (const status of ['needs_review', 'blocked', 'awaiting_delivery']) {
    if (states.includes(status)) return { type: status === 'awaiting_delivery' ? 'info' : 'warning', message: `${publishStatusLabel(status)}。${publishActionHint(status)}` }
  }
  if (states.includes('failed')) return { type: 'error', message: '部分準備或發佈失敗，請檢查各目的地的錯誤。' }
  if (states.includes('cancelled')) return { type: 'warning', message: '部分工作已取消；請確認其餘目的地的狀態。' }
  if (states.includes('preparing') || (!jobs.length && data.campaignIds?.length)) return {
    type: 'info', message: '已排入媒體準備佇列；準備完成後會自動建立發佈工作，尚未發佈成功。',
  }
  if (Array.isArray(data.skipped) && data.skipped.length) return {
    type: 'warning', message: `${data.skipped.length} 個目的地未建立工作；請檢查帳號與設定，其餘目的地請依任務狀態確認。`,
  }
  if (jobs.length && jobs.every((job) => job.status === 'succeeded')
      && jobs.every((job) => (job.targets || []).every((target) => target.status === 'succeeded'))) {
    return { type: 'success', message: `已確認 ${jobs.length} 個發佈工作完成` }
  }
  if (jobs.length) return { type: 'info', message: `已受理 ${jobs.length} 個發佈工作；請依各目的地狀態確認結果。` }
  return { type: 'warning', message: '尚無可確認的發佈工作，請檢查準備狀態。' }
}

export function campaignPreparation(response, campaignId) {
  const campaign = response?.data
  if (!campaign || String(campaign.id) !== String(campaignId) || typeof campaign.status !== 'string') throw invalidResponse()
  const posts = Array.isArray(campaign.posts) ? campaign.posts : []
  if (!posts.every((post) => post && typeof post === 'object')) throw invalidResponse()
  const jobs = Array.isArray(campaign.jobs) ? campaign.jobs.filter(validJob) : []
  // Older campaign responses expose only job IDs on posts.
  const ids = new Set(jobs.map((job) => String(job.id)))
  for (const post of posts) {
    const id = post.jobId ?? post.job_id
    if (validId(id) && !ids.has(String(id))) {
      ids.add(String(id))
      jobs.push({ id, platform: post.platform })
    }
  }
  for (const id of Array.isArray(campaign.jobIds) ? campaign.jobIds : []) {
    if (validId(id) && !ids.has(String(id))) {
      ids.add(String(id))
      jobs.push({ id })
    }
  }
  const ready = posts.filter((post) => ['ready', 'prepared', 'queued', 'publishing', 'published', 'succeeded', 'awaiting_delivery'].includes(post.status)).length
  return { ...campaign, jobs, progressLabel: posts.length ? `已準備 ${ready} / ${posts.length} 個貼文` : '等待媒體準備狀態' }
}
