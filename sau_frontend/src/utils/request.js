import axios from 'axios'
import { ElMessage } from 'element-plus'

import { clearToken, getToken, getCsrfToken, clearCsrfToken } from '@/utils/auth'
import { getApiBaseUrl } from '@/utils/api-url'

// Guard to prevent duplicate 401 handling on concurrent requests
let isRedirectingToLogin = false

// 创建axios实例
const request = axios.create({
  baseURL: getApiBaseUrl(),
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json'
  }
})

// 请求拦截器
request.interceptors.request.use(
  (config) => {
    // Google session: send the cookie and echo the CSRF secret on mutating
    // requests. Presence of a CSRF token means a session is active.
    const csrf = getCsrfToken()
    if (csrf) {
      config.withCredentials = true
      config.headers['X-CSRF-Token'] = csrf
    }
    // Legacy bearer token (harmless alongside a session; unchanged when alone).
    const token = getToken()
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => {
    console.error('請求錯誤:', error)
    return Promise.reject(error)
  }
)

// Callers with an inline error state (polling, submission, health) can set
// `silentError: true`. Other callers can use error.notified to avoid a second toast.
export function showRequestError(error, message = error?.message || '請求失敗') {
  if (error?.notified) return
  ElMessage.error(message)
  if (error && typeof error === 'object') error.notified = true
}

function rejectResponse(error) {
  const status = error.response?.status >= 400
    ? error.response.status
    : Number(error.response?.data?.code)
  const backendMsg = error.response?.data?.msg || error.response?.data?.message
  const silent = error.config?.silentError
  const notify = (message) => { if (!silent) showRequestError(error, message) }

  if (error.code === 'ERR_CANCELED' || error.name === 'CanceledError' || error.name === 'AbortError') {
    return Promise.reject(error)
  }
  if (status === 401) {
    if (!isRedirectingToLogin) {
      isRedirectingToLogin = true
      notify('未授權，請重新登入')
      clearToken()
      clearCsrfToken()
      // Lazy import avoids a module-load cycle.
      import('@/stores/auth')
        .then(({ useAuthStore }) => useAuthStore().clearSession())
        .catch(() => {})
      if (typeof window !== 'undefined' && window.location && !window.location.hash.includes('#/login')) {
        window.location.hash = '#/login'
      }
      setTimeout(() => { isRedirectingToLogin = false }, 1000)
    } else {
      error.notified = true
    }
  } else if (status === 403) {
    // Forbidden is a permission/CSRF failure, not an expired login.
    notify(backendMsg || '沒有執行此操作的權限，請確認權限或重新整理頁面')
  } else if (error.response) {
    notify(backendMsg || (status === 404 ? '找不到請求位址' : status >= 500 ? '伺服器內部錯誤' : '請求失敗'))
  } else if (error.code === 'ECONNABORTED' || error.code === 'ETIMEDOUT') {
    notify('請求逾時，請稍後確認結果')
  } else {
    notify('網路連線失敗，請檢查連線或稍後確認結果')
  }
  return Promise.reject(error)
}

// HTTP 202 is accepted too: the API still returns a body with code: 200.
request.interceptors.response.use(
  (response) => {
    const { data } = response
    if (data && (data.code === 200 || data.success === true)) return data
    const error = new Error(data?.msg || data?.message || '無效的伺服器回應')
    // Preserve status/config for callers deciding whether an error is retryable.
    error.response = response
    error.config = response.config
    error.code = Number(data?.code) >= 400 ? 'ERR_API_RESPONSE' : 'ERR_INVALID_RESPONSE'
    return rejectResponse(error)
  },
  rejectResponse
)

// 封装常用的请求方法
export const http = {
  get(url, params, config = {}) {
    return request.get(url, { ...config, params })
  },
  
  post(url, data, config = {}) {
    return request.post(url, data, config)
  },
  
  put(url, data, config = {}) {
    return request.put(url, data, config)
  },

  patch(url, data, config = {}) {
    return request.patch(url, data, config)
  },
  
  delete(url, params) {
    return request.delete(url, { params })
  },
  
  upload(url, formData, onUploadProgress) {
    return request.post(url, formData, {
      headers: {
        'Content-Type': 'multipart/form-data'
      },
      onUploadProgress
    })
  }
}

export default request
