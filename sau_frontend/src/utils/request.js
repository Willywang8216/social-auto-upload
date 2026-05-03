import axios from 'axios'
import { ElMessage } from 'element-plus'

import { clearToken, getToken } from '@/utils/auth'
import { getApiBaseUrl } from '@/utils/api-url'

// 创建axios实例
const request = axios.create({
  baseURL: getApiBaseUrl(),
  headers: {
    'Content-Type': 'application/json'
  }
})

// 请求拦截器
request.interceptors.request.use(
  (config) => {
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

// 响应拦截器
request.interceptors.response.use(
  (response) => {
    const { data } = response
    
    // 根据后端接口规范处理响应
    if (data.code === 200 || data.success) {
      return data
    } else {
      ElMessage.error(data.msg || data.message || '請求失敗')
      return Promise.reject(new Error(data.msg || data.message || '請求失敗'))
    }
  },
  (error) => {
    console.error('回應錯誤:', error)
    
    // 处理HTTP错误状态码
    if (error.response) {
      const { status } = error.response
      switch (status) {
        case 401:
        case 403:
          ElMessage.error('未授權，請重新登入')
          clearToken()
          // Bounce back to the login screen using the hash router. Use a
          // direct location.hash assignment rather than importing the
          // router, which would create a circular dependency.
          if (typeof window !== 'undefined' &&
              window.location && !window.location.hash.includes('#/login')) {
            window.location.hash = '#/login'
          }
          break
        case 404:
          ElMessage.error('找不到請求位址')
          break
        case 500:
          ElMessage.error('伺服器內部錯誤')
          break
        default:
          ElMessage.error('網路錯誤')
      }
    } else {
      ElMessage.error('網路連線失敗')
    }
    
    return Promise.reject(error)
  }
)

// 封装常用的请求方法
export const http = {
  get(url, params) {
    return request.get(url, { params })
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
