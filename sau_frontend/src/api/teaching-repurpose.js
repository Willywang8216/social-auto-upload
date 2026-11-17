import request, { http } from '@/utils/request'

const BASE = '/api/publish/teaching-repurpose'
const read = (config = {}) => ({ silentError: true, ...config })

// Preparation only. Approval, blog publication and social release stay with
// the existing Notion/website owner; this UI has no release/approve endpoint.
export const teachingRepurposeApi = {
  status: (config) => http.get(`${BASE}/status`, undefined, read(config)),
  kits: (config) => http.get(`${BASE}/kits`, undefined, read(config)),
  tasks: (config) => http.get(`${BASE}/tasks`, undefined, read(config)),
  task: (id, config) => http.get(`${BASE}/tasks/${id}`, undefined, read(config)),
  createKit: (theme, config) => http.post(`${BASE}/kits`, { theme }, read(config)),
  regenerate: (id, payload, config) => http.post(`${BASE}/tasks/${id}/regenerate`, payload, read(config)),
  cancel: (id, config) => request.delete(`${BASE}/tasks/${id}`, read(config)),
  asset: (id, name, config) => {
    if (!Number.isSafeInteger(id) || id < 1 || !/^[a-z0-9][a-z0-9.-]{0,199}$/.test(name) || name.includes('..')) {
      return Promise.reject(new Error('invalid_asset_reference'))
    }
    return http.get(`${BASE}/tasks/${id}/assets/${encodeURIComponent(name)}`, undefined, {
      ...read(config), responseType: 'blob', sauBinary: true,
    })
  },
}
