<template>
  <div class="login-view">
    <el-card class="login-card">
      <template #header>
        <div class="card-header">
          <h2>自媒体自动化运营系统</h2>
          <p class="subtitle">请输入访问令牌以登录</p>
        </div>
      </template>

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        @submit.prevent="onSubmit"
      >
        <el-form-item label="访问令牌（API Token）" prop="token">
          <el-input
            v-model="form.token"
            type="password"
            show-password
            placeholder="留空表示后端处于开放模式"
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
            登录
          </el-button>
        </el-form-item>

        <div v-if="errorMessage" class="login-error">{{ errorMessage }}</div>
        <div v-else-if="openModeNotice" class="login-hint">
          后端当前在开放模式，可直接进入。生产部署请配置 <code>SAU_API_TOKENS</code>。
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
        errorMessage.value = '令牌无效。请检查后端 SAU_API_TOKENS 配置。'
      } else {
        errorMessage.value = error?.message || '无法连接后端，请稍后再试。'
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
      ElMessage.success(openModeNotice.value ? '已进入（开放模式）' : '登录成功')
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
