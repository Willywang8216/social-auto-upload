<template>
  <div class="login-page">
    <!-- Background pattern -->
    <div class="login-bg"></div>

    <!-- Login card -->
    <div class="login-wrapper">
      <div class="login-card">
        <!-- Brand -->
        <div class="login-brand">
          <img src="/socialupload-app-icon.png" alt="Socialupload" class="login-logo" />
          <h1>Socialupload</h1>
          <p>Enter your access token to sign in</p>
        </div>

        <!-- Google sign-in (shown when the backend has Google login enabled) -->
        <div v-if="authStore.googleLoginEnabled" class="google-signin">
          <el-button
            size="large"
            class="google-btn"
            @click="onGoogleLogin"
          >
            <svg class="google-logo" viewBox="0 0 48 48" width="18" height="18" aria-hidden="true">
              <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/>
              <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/>
              <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/>
              <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/>
            </svg>
            Sign in with Google
          </el-button>
          <div class="login-divider"><span>or use an access token</span></div>
        </div>

        <!-- Form -->
        <el-form
          ref="formRef"
          :model="form"
          :rules="rules"
          label-position="top"
          @submit.prevent="onSubmit"
          class="login-form"
        >
          <el-form-item label="Access Token" prop="token">
            <el-input
              v-model="form.token"
              type="password"
              show-password
              placeholder="Leave empty for open mode"
              autocomplete="current-password"
              size="large"
              @keyup.enter="onSubmit"
            />
          </el-form-item>

          <el-form-item>
            <el-button
              type="primary"
              :loading="loading"
              size="large"
              class="login-btn"
              @click="onSubmit"
            >
              {{ loading ? 'Signing in...' : 'Sign In' }}
            </el-button>
          </el-form-item>

          <!-- Error message -->
          <div v-if="errorMessage" class="login-alert error">
            <el-icon><WarningFilled /></el-icon>
            <span>{{ errorMessage }}</span>
          </div>

          <!-- Open mode notice -->
          <div v-else-if="openModeNotice" class="login-alert info">
            <el-icon><InfoFilled /></el-icon>
            <span>Backend is in open mode. Set <code>SAU_API_TOKENS</code> for production use.</span>
          </div>
        </el-form>

        <!-- Footer links -->
        <div class="login-footer">
          <router-link to="/">Home</router-link>
          <span class="dot">·</span>
          <router-link to="/privacy">Privacy</router-link>
          <span class="dot">·</span>
          <router-link to="/terms">Terms</router-link>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { WarningFilled, InfoFilled } from '@element-plus/icons-vue'

import { http } from '@/utils/request'
import { clearToken, getToken, setToken } from '@/utils/auth'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const formRef = ref(null)
const form = ref({ token: getToken() })
const rules = {}

const loading = ref(false)
const errorMessage = ref('')
const openModeNotice = ref(false)

onMounted(async () => {
  // Surface a failed Google callback (redirected here as ?error=...).
  if (route.query.error) {
    errorMessage.value = googleErrorMessage(String(route.query.error))
    return
  }
  // Already signed in via a Google session cookie.
  if (authStore.isSessionAuthenticated) {
    router.replace(route.query.redirect || '/dashboard')
    return
  }
  const ok = await probeToken(getToken() || '', { silent: true })
  if (ok) {
    if (openModeNotice.value || getToken()) {
      router.replace(route.query.redirect || '/dashboard')
    }
  }
})

function onGoogleLogin() {
  authStore.loginWithGoogle()
}

function googleErrorMessage(code) {
  switch (code) {
    case 'oauth':
      return 'Google sign-in was cancelled or denied. Please try again.'
    case 'state':
      return 'Your sign-in link expired. Please try signing in again.'
    case 'verify':
      return 'We could not verify your Google account. Please try again.'
    default:
      return 'Google sign-in failed. Please try again.'
  }
}

async function probeToken(token, { silent = false } = {}) {
  setToken(token)
  try {
    const response = await http.get('/whoami')
    openModeNotice.value = !!response?.data?.openMode
    return true
  } catch (error) {
    if (!silent) {
      const status = error?.response?.status
      if (status === 401) {
        errorMessage.value = 'Invalid token. Check your SAU_API_TOKENS configuration.'
      } else {
        errorMessage.value = error?.message || 'Cannot connect to backend. Please try again.'
      }
      clearToken()
    }
    return false
  }
}

async function onSubmit() {
  errorMessage.value = ''
  loading.value = true
  try {
    const ok = await probeToken(form.value.token || '')
    if (ok) {
      ElMessage.success(openModeNotice.value ? 'Entered (open mode)' : 'Signed in successfully')
      router.replace(route.query.redirect || '/dashboard')
    }
  } finally {
    loading.value = false
  }
}
</script>

<style lang="scss" scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  overflow: hidden;
}

.login-bg {
  position: absolute;
  inset: 0;
  background-image:
    radial-gradient(circle at 25% 25%, rgba(255,255,255,0.1) 0%, transparent 50%),
    radial-gradient(circle at 75% 75%, rgba(255,255,255,0.08) 0%, transparent 50%);
}

.login-wrapper {
  position: relative;
  z-index: 1;
  width: 100%;
  max-width: 420px;
  padding: 24px;
}

.login-card {
  background: var(--color-bg);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-xl);
  padding: 40px 36px 32px;
  animation: slideUp 0.4s cubic-bezier(0.16, 1, 0.3, 1);
}

@keyframes slideUp {
  from {
    opacity: 0;
    transform: translateY(20px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.login-brand {
  text-align: center;
  margin-bottom: 32px;

  .login-logo {
    width: 48px;
    height: 48px;
    border-radius: 12px;
    margin-bottom: 16px;
  }

  h1 {
    font-size: 22px;
    font-weight: 700;
    color: var(--color-text);
    margin: 0 0 8px;
    letter-spacing: -0.025em;
  }

  p {
    font-size: 14px;
    color: var(--color-text-secondary);
    margin: 0;
  }
}

.google-signin {
  margin-bottom: 24px;

  .google-btn {
    width: 100%;
    height: 44px;
    font-weight: 600;
    font-size: 15px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    color: var(--color-text);

    &:hover {
      background: var(--color-bg-hover, rgba(0, 0, 0, 0.02));
      border-color: var(--color-primary);
    }
  }

  .google-logo {
    flex-shrink: 0;
  }
}

.login-divider {
  display: flex;
  align-items: center;
  text-align: center;
  margin: 20px 0 4px;
  color: var(--color-text-placeholder);
  font-size: 12px;

  &::before,
  &::after {
    content: '';
    flex: 1;
    border-bottom: 1px solid var(--color-border);
  }

  span {
    padding: 0 12px;
  }
}

.login-form {
  .el-form-item {
    margin-bottom: 20px;
  }

  .login-btn {
    width: 100%;
    font-weight: 600;
    height: 44px;
    font-size: 15px;
  }
}

.login-alert {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 16px;
  border-radius: var(--radius-md);
  font-size: 13px;
  line-height: 1.5;
  margin-bottom: 16px;

  .el-icon {
    flex-shrink: 0;
    margin-top: 1px;
  }

  &.error {
    background: var(--color-danger-light);
    color: var(--color-danger);
  }

  &.info {
    background: var(--color-info-light);
    color: var(--color-info);
  }

  code {
    background: rgba(0, 0, 0, 0.06);
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 12px;
  }
}

.login-footer {
  text-align: center;
  margin-top: 24px;
  padding-top: 20px;
  border-top: 1px solid var(--color-border);

  a {
    font-size: 13px;
    color: var(--color-text-muted);
    text-decoration: none;
    font-weight: 500;
    transition: color var(--transition-fast);

    &:hover {
      color: var(--color-primary);
    }
  }

  .dot {
    margin: 0 10px;
    color: var(--color-text-placeholder);
  }
}
</style>
