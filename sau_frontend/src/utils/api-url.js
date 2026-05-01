const trimTrailingSlash = (value = '') => value.replace(/\/+$/, '')

const ensureLeadingSlash = (value = '') => {
  if (!value) {
    return ''
  }

  return value.startsWith('/') ? value : `/${value}`
}

const apiBaseUrl = trimTrailingSlash((import.meta.env.VITE_API_BASE_URL ?? '').trim())

export const getApiBaseUrl = () => apiBaseUrl

export const buildApiUrl = (path = '') => {
  const normalizedPath = ensureLeadingSlash(path)

  if (!apiBaseUrl) {
    return normalizedPath
  }

  return `${apiBaseUrl}${normalizedPath}`
}
