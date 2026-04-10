import { http } from '@/utils/request'
import { buildApiUrl } from '@/utils/apiBase'

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

  uploadCookie({ id, platform, file }) {
    const formData = new FormData()
    formData.append('id', id)
    formData.append('platform', platform)
    formData.append('file', file)
    return http.post('/uploadCookie', formData, {
      headers: {
        'Content-Type': 'multipart/form-data'
      }
    })
  },

  getDownloadCookieUrl(filePath) {
    return buildApiUrl(`/downloadCookie?filePath=${encodeURIComponent(filePath)}`)
  },

  // 删除账号
  deleteAccount(id) {
    return http.get(`/deleteAccount?id=${id}`)
  }
}
