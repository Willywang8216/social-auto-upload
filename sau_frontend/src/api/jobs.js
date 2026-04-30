import { http } from '@/utils/request'

// Publish-job runtime API. Mirrors the Flask backend's /jobs surface.
//
// The legacy synchronous /postVideo endpoint is still wired up in the
// backend, but new UI flows should go through this module so we get
// idempotency, per-target progress, retries, and history for free.
export const jobsApi = {
  // Enqueue a publish job. The body shape matches /postVideo so existing
  // callers can switch over with a one-line edit:
  //   - type (legacy numeric code) or platform (slug)
  //   - title, tags
  //   - fileList, accountList
  //   - enableTimer, videosPerDay, dailyTimes, startDays
  //   - thumbnail, productLink, productTitle, isDraft
  // Optionally include `idempotencyKey` to make retries safe.
  create(payload) {
    return http.post('/jobs', payload)
  },

  // List recent jobs. Filters are optional.
  list({ status, platform, limit = 50 } = {}) {
    return http.get('/jobs', { status, platform, limit })
  },

  // Fetch a single job along with its targets array.
  get(jobId) {
    return http.get(`/jobs/${jobId}`)
  },

  // Cancel a non-terminal job. Idempotent: cancelling an already-terminal
  // job is a no-op that returns the existing status.
  cancel(jobId) {
    return http.post(`/jobs/${jobId}/cancel`)
  },

  // Drain the queue synchronously in the backend process. Useful in
  // single-process dev mode where there is no separate worker.
  runDrain() {
    return http.post('/jobs/run')
  }
}
