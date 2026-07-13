// Google-session auth store.
//
// Sits alongside the legacy bearer-token flow (utils/auth.js) rather than
// replacing it: a workspace is authenticated when EITHER a Google session
// cookie is live OR a legacy token is present. The backend contract:
//
//   GET  /auth/google/start   -> 302 to Google (full-page redirect)
//   GET  /api/v1/session       -> { authenticated, user, workspace,
//                                    permissions, csrfToken }
//                                 (404 when Google login is disabled)
//   POST /api/v1/logout        -> revoke session (needs X-CSRF-Token)
//
// The session endpoint returns a raw body without the {code:200} envelope the
// shared request interceptor expects, so this store uses its own thin axios
// client that bypasses that interceptor.

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import axios from 'axios'

import { getApiBaseUrl } from '@/utils/api-url'
import {
  clearCsrfToken,
  clearToken,
  getToken,
  setCsrfToken,
} from '@/utils/auth'

const authClient = axios.create({
  baseURL: getApiBaseUrl(),
  timeout: 15000,
  withCredentials: true, // send/receive the session cookie cross-origin in dev
})

export const useAuthStore = defineStore('auth', () => {
  const session = ref(null) // { user, workspace, permissions } | null
  const googleLoginEnabled = ref(false)
  const bootstrapped = ref(false)

  const isSessionAuthenticated = computed(() => session.value !== null)
  const isAuthenticated = computed(
    () => isSessionAuthenticated.value || Boolean(getToken()),
  )
  const currentUser = computed(() => session.value?.user ?? null)
  const currentWorkspace = computed(() => session.value?.workspace ?? null)
  const permissions = computed(() => session.value?.permissions ?? [])

  function applySession(data) {
    if (data && data.authenticated) {
      session.value = {
        user: data.user ?? null,
        workspace: data.workspace ?? null,
        permissions: data.permissions ?? [],
      }
      setCsrfToken(data.csrfToken || '')
    } else {
      session.value = null
      clearCsrfToken()
    }
  }

  // Resolve the current session on app start. A 404 means the Google-login
  // blueprint isn't registered (feature disabled); any error leaves the app in
  // the legacy token-only mode.
  async function bootstrap() {
    try {
      const { data } = await authClient.get('/api/v1/session')
      googleLoginEnabled.value = true
      applySession(data)
    } catch {
      googleLoginEnabled.value = false
      session.value = null
      clearCsrfToken()
    } finally {
      bootstrapped.value = true
    }
    return session.value
  }

  function loginWithGoogle() {
    // Full-page navigation so the browser follows the 302 to Google and, on
    // return, receives the Set-Cookie from the callback.
    window.location.href = `${getApiBaseUrl()}/auth/google/start`
  }

  async function logout() {
    try {
      const csrf =
        typeof window !== 'undefined' && window.sessionStorage
          ? window.sessionStorage.getItem('sau-csrf-token') || ''
          : ''
      await authClient.post('/api/v1/logout', {}, { headers: { 'X-CSRF-Token': csrf } })
    } catch {
      // Even if the revoke call fails, drop local auth state below.
    }
    session.value = null
    clearCsrfToken()
    clearToken()
  }

  // Called by the request interceptor when the backend rejects a request so a
  // stale session doesn't keep the UI in an authenticated state.
  function clearSession() {
    session.value = null
    clearCsrfToken()
  }

  return {
    session,
    googleLoginEnabled,
    bootstrapped,
    isSessionAuthenticated,
    isAuthenticated,
    currentUser,
    currentWorkspace,
    permissions,
    bootstrap,
    loginWithGoogle,
    logout,
    clearSession,
  }
})
