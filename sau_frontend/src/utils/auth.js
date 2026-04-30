// Auth-token plumbing for the frontend.
//
// The backend accepts a bearer token via the `Authorization` header. The
// token is persisted in localStorage so a page refresh stays logged in.
// In open mode (no SAU_API_TOKENS configured server-side) any token —
// including the empty string — passes the gate; the LoginView still uses
// /whoami to confirm the backend is reachable.

const TOKEN_KEY = 'sau-auth-token'

export function getToken() {
  try {
    return window.localStorage.getItem(TOKEN_KEY) || ''
  } catch {
    // localStorage can be unavailable in private browsing modes.
    return ''
  }
}

export function setToken(token) {
  try {
    window.localStorage.setItem(TOKEN_KEY, token || '')
  } catch {
    // ignore
  }
}

export function clearToken() {
  try {
    window.localStorage.removeItem(TOKEN_KEY)
  } catch {
    // ignore
  }
}

export function appendAuthQuery(url) {
  // EventSource cannot send custom headers, so the SSE endpoint reads the
  // token from a query parameter instead. We URL-encode and append it so
  // we never break an existing query string.
  const token = getToken()
  if (!token) return url
  const sep = url.includes('?') ? '&' : '?'
  return `${url}${sep}auth=${encodeURIComponent(token)}`
}
