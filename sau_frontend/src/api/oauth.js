import { http } from '@/utils/request'

export const oauthApi = {
  getStatus(platform, accountId = null) {
    const params = { platform }
    if (accountId != null) params.accountId = accountId
    return http.get('/admin/oauth/status', params)
  }
}
