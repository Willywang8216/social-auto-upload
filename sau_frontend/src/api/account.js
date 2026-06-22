import { http } from '@/utils/request'

// 账号管理相关API
export const accountApi = {
  // 获取有效账号列表（带验证）
  getValidAccounts() {
    return http.get('/getValidAccounts')
  },

  // 获取账号列表（不带验证，快速加载）
  getAccounts() {
    return http.get('/getAccounts')
  },

  // 添加账号
  addAccount(data) {
    return http.post('/account', data)
  },

  // 更新账号
  updateAccount(data) {
    return http.post('/updateUserinfo', data)
  },

  // 删除账号
  deleteAccount(id) {
    return http.get(`/deleteAccount?id=${id}`)
  },

  getHealthSummary() {
    return http.get('/accounts/health-summary')
  },

  getRecentEvents(params = {}) {
    return http.get('/accounts/events', params)
  },

  runMaintenance(payload = {}) {
    return http.post('/accounts/maintenance/run', payload)
  },

  getMaintenanceStatus() {
    return http.get('/accounts/maintenance/status')
  },

  // --- New endpoints for the Control Room redesign ---

  // Export decrypted cookies for an account
  exportCookies(accountId) {
    return http.get(`/accounts/${accountId}/export-cookies`)
  },

  // Import cookies (JSON or Netscape format)
  importCookies(platform, account, profile, format, payload) {
    return http.post('/accounts/import-cookies', { platform, account, profile, format, payload })
  },

  // Get all accounts via the new /api/accounts endpoint (returns flat array)
  getAccountsApi() {
    return http.get('/api/accounts')
  },

  // Check a single account's connection / cookie validity
  checkConnection(accountId) {
    return http.post(`/accounts/${accountId}/check-connection`)
  },

  // Start a QR/browser login session
  startLogin(platform, account, profile) {
    return http.post('/auth/login/start', { platform, account, profile })
  },

  // Poll login session status
  pollLogin(sessionId) {
    return http.get(`/auth/login/${sessionId}`)
  },

  // Cancel a login session
  cancelLogin(sessionId) {
    return http.post(`/auth/login/${sessionId}/cancel`)
  }
}
