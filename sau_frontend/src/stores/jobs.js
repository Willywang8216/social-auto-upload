import { defineStore } from 'pinia'
import { computed, onScopeDispose, reactive, ref } from 'vue'

import { jobsApi } from '@/api/jobs'

import { createPoller, invalidResponse } from '@/utils/polling'
import { JOB_STATUS, TARGET_STATUS, canRetryTarget, isTerminalJobStatus, needsPublishAction, normalizeTarget } from '@/utils/publish-status'

export { JOB_STATUS, TARGET_STATUS }

function jobSnapshot(response, jobId) {
  const job = response?.data
  if (!job || job.id == null || (jobId != null && String(job.id) !== String(jobId)) || typeof job.status !== 'string') {
    throw invalidResponse()
  }
  if (job.targets != null && (!Array.isArray(job.targets)
    || !job.targets.every((target) => target && typeof target.status === 'string'))) throw invalidResponse()
  return job
}

export const useJobsStore = defineStore('jobs', () => {
  // jobsById holds the most recent server snapshot of each job we know
  // about. Targets are kept on the same object when /jobs/<id> is fetched.
  const jobsById = reactive({})
  // Runtime handles stay private; only serializable status is exposed to the UI.
  const pollers = new Map()
  const pollingByJobId = reactive({})
  const recentJobIds = ref([])

  const jobs = computed(() =>
    recentJobIds.value
      .map((id) => jobsById[id])
      .filter(Boolean)
  )

  function _store(job) {
    if (!job || job.id == null) return null
    const previous = jobsById[job.id] || {}
    // Preserve `targets` when the list endpoint returns a job summary
    // without a targets array — only overwrite when the new payload
    // actually carries them.
    const merged = {
      ...previous,
      ...job,
      targets: (job.targets ?? previous.targets ?? []).map(normalizeTarget)
    }
    jobsById[job.id] = merged
    if (!recentJobIds.value.includes(job.id)) {
      recentJobIds.value.unshift(job.id)
    }
    return merged
  }

  async function refreshList({ status, platform, limit = 50, offset = 0 } = {}) {
    const response = await jobsApi.list({ status, platform, limit, offset })
    const data = response?.data
    if (!Array.isArray(data)) throw invalidResponse()
    const previousIds = [...recentJobIds.value]
    data.forEach((job) => _store(job))
    if (offset === 0) {
      recentJobIds.value = data.map((job) => job.id)
    } else {
      recentJobIds.value = [...new Set([...previousIds, ...data.map((job) => job.id)])]
    }
    return { items: data, hasMore: data.length === limit }
  }

  async function fetchJob(jobId, config = {}) {
    const response = await jobsApi.get(jobId, config)
    return _store(jobSnapshot(response, jobId))
  }

  async function createJob(payload) {
    const response = await jobsApi.create(payload)
    const job = _store(jobSnapshot(response))
    if (job && !isTerminalJobStatus(job.status)) {
      startPolling(job.id)
    }
    return job
  }

  async function cancelJob(jobId) {
    const response = await jobsApi.cancel(jobId)
    const job = _store(jobSnapshot(response, jobId))
    if (isTerminalJobStatus(job.status)) stopPolling(jobId)
    return job
  }

  async function refreshEntities(params = {}, config = {}) {
    const response = await jobsApi.publishEntities(params, config)
    return response?.data || { items: [], total: 0, hasMore: false }
  }

  async function fetchEntity(entityId) {
    const response = await jobsApi.publishEntity(entityId)
    return response?.data || null
  }

  async function updateEntityPost(entityId, postId, draft) {
    const response = await jobsApi.updateEntityPost(entityId, postId, { draft })
    return response?.data
  }

  // --- Calendar / schedule management (2026-09-15) ---

  // Returns the raw list of scheduled targets for the calendar view.
  async function refreshCalendar({ month, platform, status } = {}) {
    const response = await jobsApi.calendar({ month, platform, status })
    const data = response?.data || {}
    return data.items || []
  }

  async function rescheduleTarget(targetId, scheduleAt, parentJobId) {
    const response = await jobsApi.rescheduleTarget(targetId, scheduleAt)
    if (parentJobId != null) {
      await fetchJob(parentJobId)  // refresh job counters/counts
    }
    return response?.data
  }

  async function cancelTarget(targetId, parentJobId) {
    const response = await jobsApi.cancelTarget(targetId)
    if (parentJobId != null) {
      await fetchJob(parentJobId)
    }
    return response?.data
  }

  async function resubmitTarget(targetId, parentJobId) {
    // Re-read before retrying: a stale failed row may now require reconciliation.
    const job = parentJobId != null ? await fetchJob(parentJobId) : null
    const target = job?.targets?.find((item) => String(item.id) === String(targetId))
    if (!canRetryTarget(target)) {
      throw new Error('無法安全重試；請先更新任務並至平台確認交付結果。')
    }
    const response = await jobsApi.resubmitTarget(targetId)
    if (parentJobId != null) {
      await fetchJob(parentJobId)
    }
    return response?.data
  }

  function startPolling(jobId, options = {}) {
    const key = String(jobId)
    if (!pollers.has(key)) {
      pollers.set(key, createPoller(
        (signal) => jobsApi.get(jobId, { signal, silentError: true }),
        {
          ...options,
          onState: (state) => { pollingByJobId[key] = state },
          onResult: (response) => {
            const job = _store(jobSnapshot(response, jobId))
            if (isTerminalJobStatus(job.status)) return 'completed'
            // These are action states, not success/failure and not blind retries.
            if (needsPublishAction(job.status)) return 'paused'
          },
        }
      ))
    }
    pollers.get(key).start()
  }

  function stopPolling(jobId) {
    pollers.get(String(jobId))?.stop()
  }

  function stopAllPolling() {
    pollers.forEach((poller) => poller.stop())
  }

  onScopeDispose(stopAllPolling)

  return {
    JOB_STATUS,
    TARGET_STATUS,
    jobs,
    jobsById,
    pollingByJobId,
    createJob,
    cancelJob,
    fetchJob,
    refreshList,
    refreshCalendar,
    refreshEntities,
    fetchEntity,
    updateEntityPost,
    rescheduleTarget,
    cancelTarget,
    resubmitTarget,
    startPolling,
    stopPolling,
    stopAllPolling
  }
})
