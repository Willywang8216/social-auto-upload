import { http } from '@/utils/request'

// SAU-Inbox queue API. Mirrors the Flask backend's /api/inbox surface:
//   GET  /api/inbox                        -> {ready, pending, quarantined}
//   POST /api/inbox/items/<id>/approve     -> {item}
//   POST /api/inbox/items/<id>/reject      -> {item}   body {reason}
export const inboxApi = {
  // Grouped inbox listing.
  list() {
    return http.get('/api/inbox')
  },

  // Approve a ready item (moves it to processed).
  approve(id) {
    return http.post(`/api/inbox/items/${encodeURIComponent(id)}/approve`)
  },

  // Reject a ready item (moves it to quarantined) with an optional reason.
  reject(id, { reason = '' } = {}) {
    return http.post(`/api/inbox/items/${encodeURIComponent(id)}/reject`, { reason })
  }
}