<template>
  <div class="login-view">
    <el-card class="login-card">
      <template #header>
        <div class="card-header">
          <h2>自媒體自動化營運系統</h2>
          <p class="subtitle">請輸入存取權杖以登入</p>
        </div>
      </template>

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        @submit.prevent="onSubmit"
      >
        <el-form-item label="存取權杖（API Token）" prop="token">
          <el-input
            v-model="form.token"
            type="password"
            show-password
            placeholder="留空表示後端處於開放模式"
            autocomplete="current-password"
            @keyup.enter="onSubmit"
          />
        </el-form-item>

        <el-form-item>
          <el-button
            type="primary"
            :loading="loading"
            class="submit-btn"
            @click="onSubmit"
          >
            登入
          </el-button>
        </el-form-item>

        <div v-if="errorMessage" class="login-error">{{ errorMessage }}</div>
        <div v-else-if="openModeNotice" class="login-hint">
          後端目前為開放模式，可直接進入。正式部署請設定 <code>SAU_API_TOKENS</code>。
        </div>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { http } from '@/utils/request'
import { clearToken, getToken, setToken } from '@/utils/auth'

const router = useRouter()
const route = useRoute()
const formRef = ref(null)
const form = ref({ token: getToken() })
const rules = {
  // 不强制非空，因为后端可能在开放模式。
}

const loading = ref(false)
const errorMessage = ref('')
const openModeNotice = ref(false)

onMounted(async () => {
  // Try the existing token first; if that works, skip the login screen.
  // If there is no token AND the backend is in open mode, /whoami will
  // still return 200 with openMode=true and we redirect through.
  const ok = await probeToken(getToken() || '', { silent: true })
  if (ok) {
    if (openModeNotice.value || getToken()) {
      router.replace(route.query.redirect || '/')
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
        errorMessage.value = '權杖無效。請檢查後端 SAU_API_TOKENS 設定。'
      } else {
        errorMessage.value = error?.message || '無法連線後端，請稍後再試。'
      }
    }
    clearToken()
    return false
  }
}

async function onSubmit() {
  errorMessage.value = ''
  loading.value = true
  try {
    const ok = await probeToken(form.value.token || '')
    if (ok) {
      ElMessage.success(openModeNotice.value ? '已進入（開放模式）' : '登入成功')
      router.replace(route.query.redirect || '/')
    }
  } finally {
    loading.value = false
  }
}
</script>

<style lang="scss" scoped>
.login-view {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background-color: #f2f3f5;
  padding: 20px;

  .login-card {
    width: 100%;
    max-width: 420px;

    .card-header {
      text-align: center;

      h2 {
        margin: 0;
        font-size: 20px;
        color: #303133;
      }

      .subtitle {
        margin: 8px 0 0;
        color: #909399;
        font-size: 13px;
      }
    }

    .submit-btn {
      width: 100%;
    }

    .login-error {
      margin-top: 8px;
      color: #f56c6c;
      font-size: 13px;
    }

    .login-hint {
      margin-top: 8px;
      color: #909399;
      font-size: 12px;

      code {
        background: #f4f4f5;
        padding: 0 4px;
        border-radius: 2px;
      }
    }
  }
}
</style>
