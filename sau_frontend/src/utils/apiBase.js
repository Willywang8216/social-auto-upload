const rawBaseUrl = (import.meta.env.VITE_API_BASE_URL || '').trim()

export const apiBaseUrl = rawBaseUrl ? rawBaseUrl.replace(/\/$/, '') : window.location.origin

export const apiRequestBase = rawBaseUrl ? rawBaseUrl.replace(/\/$/, '') : ''

export const buildApiUrl = (path) => {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`
  return `${apiBaseUrl}${normalizedPath}`
}
