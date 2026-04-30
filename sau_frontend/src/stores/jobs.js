import { defineStore } from 'pinia'
import { computed, reactive, ref } from 'vue'

import { jobsApi } from '@/api/jobs'

// Job lifecycle states. Kept as a plain object rather than an enum so
// templates can reference JOB_STATUS.SUCCEEDED directly.
export const JOB_STATUS = Object.freeze({
  PENDING: 'pending',
  RUNNING: 'running',
  SUCCEEDED: 'succeeded',
  FAILED: 'failed',
  CANCELLED: 'cancelled'
})

export const TARGET_STATUS = Object.freeze({
  PENDING: 'pending',
  RUNNING: 'running',
  RETRYING: 'retrying',
  SUCCEEDED: 'succeeded',
  FAILED: 'failed',
  CANCELLED: 'cancelled'
})

const JOB_TERMINAL = new Set([
  JOB_STATUS.SUCCEEDED,
  JOB_STATUS.FAILED,
  JOB_STATUS.CANCELLED
])

const DEFAULT_POLL_INTERVAL = 1500

export const useJobsStore = defineStore('jobs', () => {
  // jobsById holds the most recent server snapshot of each job we know
  // about. Targets are kept on the same object when /jobs/<id> is fetched.
  const jobsById = reactive({})
  // Per-job polling timers so we don't double-poll the same job.
  const pollers = reactive({})
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
      targets: job.targets ?? previous.targets ?? []
    }
    jobsById[job.id] = merged
    if (!recentJobIds.value.includes(job.id)) {
      recentJobIds.value.unshift(job.id)
    }
    return merged
  }

  async function refreshList({ status, platform, limit = 50 } = {}) {
    const response = await jobsApi.list({ status, platform, limit })
    const data = response?.data || []
    // Reset known-recent ids to the server order so cancelled/old jobs
    // drop out of the dashboard naturally.
    recentJobIds.value = data.map((job) => job.id)
    data.forEach((job) => _store(job))
    return data
  }

  async function fetchJob(jobId) {
    const response = await jobsApi.get(jobId)
    return _store(response?.data)
  }

  async function createJob(payload) {
    const response = await jobsApi.create(payload)
    const job = _store(response?.data)
    if (job && !JOB_TERMINAL.has(job.status)) {
      startPolling(job.id)
    }
    return job
  }

  async function cancelJob(jobId) {
    const response = await jobsApi.cancel(jobId)
    return _store(response?.data)
  }

  // Drives a single job to completion via repeated /jobs/<id> calls.
  // The poller stops itself once the job reaches a terminal status, or
  // when the caller explicitly cancels via stopPolling.
  function startPolling(jobId, { interval = DEFAULT_POLL_INTERVAL } = {}) {
    if (pollers[jobId]) return // already polling

    const tick = async () => {
      try {
        const job = await fetchJob(jobId)
        if (job && JOB_TERMINAL.has(job.status)) {
          stopPolling(jobId)
        }
      } catch (error) {
        // Stop polling on persistent errors so we don't spam the backend.
        // The user can re-trigger via the UI.
        console.error(`Failed to poll job ${jobId}:`, error)
        stopPolling(jobId)
      }
    }

    // Fire once immediately so the UI shows fresh state without a delay.
    tick()
    pollers[jobId] = window.setInterval(tick, interval)
  }

  function stopPolling(jobId) {
    if (pollers[jobId]) {
      window.clearInterval(pollers[jobId])
      delete pollers[jobId]
    }
  }

  function stopAllPolling() {
    Object.keys(pollers).forEach((jobId) => stopPolling(jobId))
  }

  return {
    JOB_STATUS,
    TARGET_STATUS,
    jobs,
    jobsById,
    createJob,
    cancelJob,
    fetchJob,
    refreshList,
    startPolling,
    stopPolling,
    stopAllPolling
  }
})
