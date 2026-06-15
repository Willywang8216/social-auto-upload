import { http } from '@/utils/request'

export const sheetExportApi = {
  // List sheet exports
  listExports: (params = {}) => {
    return http.get('/api/sheet-exports', { params })
  },
}
