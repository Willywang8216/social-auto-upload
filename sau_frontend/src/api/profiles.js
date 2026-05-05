import { http } from '@/utils/request'

export const profilesApi = {
  list() {
    return http.get('/profiles')
  },

  create(payload) {
    return http.post('/profiles', payload)
  },

  get(profileId) {
    return http.get(`/profiles/${profileId}`)
  },

  update(profileId, payload) {
    return http.patch(`/profiles/${profileId}`, payload)
  },

  listAccounts(profileId, params = {}) {
    return http.get(`/profiles/${profileId}/accounts`, params)
  },

  createAccount(profileId, payload) {
    return http.post(`/profiles/${profileId}/accounts`, payload)
  },

  validateAccountConfig(payload) {
    return http.post('/accounts/validate-config', payload)
  },

  updateAccount(accountId, payload) {
    return http.patch(`/accounts/${accountId}`, payload)
  },

  refreshAccountToken(accountId) {
    return http.post(`/accounts/${accountId}/refresh-token`)
  },

  checkAccountConnection(accountId) {
    return http.post(`/accounts/${accountId}/check-connection`)
  },

  batchCheckConnections(accountIds) {
    return http.post('/accounts/batch/check-connections', { accountIds })
  },

  batchRefreshTokens(accountIds) {
    return http.post('/accounts/batch/refresh-tokens', { accountIds })
  }
}
