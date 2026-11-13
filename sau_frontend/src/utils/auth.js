import { ref } from 'vue'

// Auth-token plumbing for the frontend.
//
// The backend accepts a bearer token via the `Authorization` header. The
// token is persisted in localStorage so a page refresh stays logged in.
// In open mode (no SAU_API_TOKENS configured server-side) any token —
// including the empty string — passes the gate; the LoginView still uses
// /whoami to confirm the backend is reachable.

const TOKEN_KEY = 'sau-auth-token'
// CSRF secret for the Google session. Handed out by GET /api/v1/session and
// echoed on mutating requests via the X-CSRF-Token header. Kept in
// sessionStorage (cleared when the tab closes) and re-fetched on every
// bootstrap, so it never needs to outlive a session.
const CSRF_KEY = 'sau-csrf-token'

const authRevision = ref(0)

export function getAuthRevision() {
  return authRevision.value
}

if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
  window.addEventListener('storage', (event) => {
    if (event.key === TOKEN_KEY || event.key === CSRF_KEY || event.key === null) {
      authRevision.value += 1
    }
  })
}

export function getToken() {
  // Track localStorage changes in computed auth state without copying secrets
  // into a second store. Storage remains the source of truth.
  getAuthRevision()
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
  } finally {
    authRevision.value += 1
  }
}

export function clearToken() {
  try {
    window.localStorage.removeItem(TOKEN_KEY)
  } catch {
    // ignore
  } finally {
    authRevision.value += 1
  }
}

export function getCsrfToken() {
  try {
    return window.sessionStorage.getItem(CSRF_KEY) || ''
  } catch {
    return ''
  }
}

export function setCsrfToken(token) {
  try {
    if (token) {
      window.sessionStorage.setItem(CSRF_KEY, token)
    } else {
      window.sessionStorage.removeItem(CSRF_KEY)
    }
  } catch {
    // ignore
  } finally {
    authRevision.value += 1
  }
}

export function clearCsrfToken() {
  try {
    window.sessionStorage.removeItem(CSRF_KEY)
  } catch {
    // ignore
  } finally {
    authRevision.value += 1
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
