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

const router = useRouter()
const route = useRoute()
const formRef = ref(null)
const form = ref({ token: getToken() })
const rules = {}

const loading = ref(false)
const errorMessage = ref('')
const openModeNotice = ref(false)

onMounted(async () => {
  const ok = await probeToken(getToken() || '', { silent: true })
  if (ok) {
    if (openModeNotice.value || getToken()) {
      router.replace(route.query.redirect || '/dashboard')
    }
  }
})

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
    color: #b91c1c;
  }

  &.info {
    background: var(--color-info-light);
    color: #4338ca;
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
