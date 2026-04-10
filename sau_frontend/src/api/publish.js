import { http } from '@/utils/request'

export const publishApi = {
  generateBatchDrafts(data) {
    return http.post('/generatePublishBatchDrafts', data)
  },

  saveJobs(data) {
    return http.post('/savePublishJobs', data)
  },

  getJobs(params) {
    return http.get('/getPublishJobs', params)
  },

  getCalendarEntries(params) {
    return http.get('/getPublishCalendarEntries', params)
  },

  updateJob(data) {
    return http.post('/updatePublishJob', data)
  },

  regenerateJob(data) {
    return http.post('/regeneratePublishJob', data)
  },

  runJobNow(jobId) {
    return http.post('/runPublishJobNow', { jobId })
  },

  cancelJob(jobId) {
    return http.post('/cancelPublishJob', { jobId })
  },

  completeManualJob(jobId) {
    return http.post('/completeManualPublishJob', { jobId })
  }
}
