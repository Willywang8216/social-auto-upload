import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

// The auth store builds its own axios client at import time; mock axios so the
// session endpoint can be driven from the test. vi.hoisted keeps the fns
// defined before the hoisted vi.mock factory runs.
const { mockGet, mockPost } = vi.hoisted(() => ({ mockGet: vi.fn(), mockPost: vi.fn() }))
vi.mock('axios', () => ({
  default: { create: () => ({ get: mockGet, post: mockPost }) },
}))

import { useAuthStore } from '@/stores/auth'

describe('auth store (Google session)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockGet.mockReset()
    mockPost.mockReset()
  })

  it('marks the session authenticated when /api/v1/session confirms it', async () => {
    mockGet.mockResolvedValue({
      data: {
        authenticated: true,
        user: { email: 'a@x.com', displayName: 'A' },
        workspace: { id: 'w1', name: 'Workspace A', role: 'owner' },
        permissions: ['profiles:read'],
        csrfToken: 'csrf-123',
      },
    })
    const store = useAuthStore()
    await store.bootstrap()

    expect(store.googleLoginEnabled).toBe(true)
    expect(store.isSessionAuthenticated).toBe(true)
    expect(store.isAuthenticated).toBe(true)
    expect(store.currentUser.email).toBe('a@x.com')
    expect(store.currentWorkspace.name).toBe('Workspace A')
    expect(store.permissions).toContain('profiles:read')
  })

  it('stays unauthenticated but enables the button when not signed in', async () => {
    mockGet.mockResolvedValue({ data: { authenticated: false } })
    const store = useAuthStore()
    await store.bootstrap()

    expect(store.googleLoginEnabled).toBe(true)
    expect(store.isSessionAuthenticated).toBe(false)
    expect(store.currentUser).toBeNull()
  })

  it('disables Google login when the session endpoint 404s', async () => {
    mockGet.mockRejectedValue({ response: { status: 404 } })
    const store = useAuthStore()
    await store.bootstrap()

    expect(store.googleLoginEnabled).toBe(false)
    expect(store.isSessionAuthenticated).toBe(false)
    expect(store.bootstrapped).toBe(true)
  })

  it('clearSession drops the session so the guard stops trusting it', async () => {
    mockGet.mockResolvedValue({
      data: { authenticated: true, user: {}, workspace: {}, permissions: [], csrfToken: 'c' },
    })
    const store = useAuthStore()
    await store.bootstrap()
    expect(store.isSessionAuthenticated).toBe(true)

    store.clearSession()
    expect(store.isSessionAuthenticated).toBe(false)
    expect(store.isAuthenticated).toBe(false)
  })
})
