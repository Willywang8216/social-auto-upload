<template>
  <div class="account-management">
    <div class="page-header">
      <h1>帳號管理</h1>
      <div class="profile-toolbar">
        <el-select v-model="selectedProfileFilter" style="width: 260px" placeholder="篩選 Profile">
          <el-option label="全部帳號" value="all" />
          <el-option label="Legacy 帳號" value="legacy" />
          <el-option
            v-for="profile in profileOptions"
            :key="profile.id"
            :label="profile.name"
            :value="String(profile.id)"
          />
        </el-select>
        <el-button type="primary" plain @click="openProfileDialog">新增 Profile</el-button>
      </div>
    </div>

    <div class="account-tabs">
      <el-tabs v-model="activeTab" class="account-tabs-nav">
        <el-tab-pane label="全部" name="all">
          <AccountTabPane
            :accounts="filteredAccounts"
            :search-keyword="searchKeyword"
            :refreshing="appStore.isAccountRefreshing"
            empty-text="目前沒有帳號資料"
            @add="handleAddAccount"
            @edit="handleEdit"
            @delete="handleDelete"
            @download-cookie="handleDownloadCookie"
            @upload-cookie="handleUploadCookie"
            @refresh="refreshAccounts"
            @relogin="handleReLogin"
            @search="onSearchChange"
          />
        </el-tab-pane>
        <el-tab-pane
          v-for="platform in accountPlatformTabs"
          :key="platform.value"
          :label="platform.label"
          :name="platform.value"
        >
          <AccountTabPane
            :accounts="getFilteredAccountsByPlatform(platform.label)"
            :search-keyword="searchKeyword"
            :refreshing="appStore.isAccountRefreshing"
            :empty-text="`目前沒有${platform.label}帳號資料`"
            @add="handleAddAccount"
            @edit="handleEdit"
            @delete="handleDelete"
            @download-cookie="handleDownloadCookie"
            @upload-cookie="handleUploadCookie"
            @refresh="refreshAccounts"
            @relogin="handleReLogin"
            @search="onSearchChange"
          />
        </el-tab-pane>
      </el-tabs>
    </div>

    <el-dialog
      v-model="profileDialogVisible"
      title="新增 Profile"
      width="560px"
      :close-on-click-modal="false"
    >
      <el-form :model="profileForm" label-width="100px" ref="profileFormRef">
        <el-form-item label="名稱" required>
          <el-input v-model="profileForm.name" placeholder="例如：Brand A" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="profileForm.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="System Prompt">
          <el-input v-model="profileForm.systemPrompt" type="textarea" :rows="4" />
        </el-form-item>
        <el-form-item label="浮水印">
          <el-input v-model="profileForm.watermark" placeholder="文字浮水印" />
        </el-form-item>
        <el-form-item label="聯絡資訊">
          <el-input v-model="profileForm.contactDetails" />
        </el-form-item>
        <el-form-item label="CTA">
          <el-input v-model="profileForm.ctaText" />
        </el-form-item>
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="profileDialogVisible = false">取消</el-button>
          <el-button type="primary" @click="submitProfileForm">建立 Profile</el-button>
        </span>
      </template>
    </el-dialog>

    <el-dialog
      v-model="dialogVisible"
      :title="dialogType === 'add' ? '新增帳號' : '編輯帳號'"
      width="700px"
      :close-on-click-modal="false"
      :close-on-press-escape="!sseConnecting"
      :show-close="!sseConnecting"
    >
      <el-form :model="accountForm" label-width="100px" :rules="rules" ref="accountFormRef">
        <el-form-item label="Profile">
          <el-select
            v-model="accountForm.profileId"
            clearable
            filterable
            placeholder="留空代表 Legacy 帳號"
            style="width: 100%"
            :disabled="sseConnecting"
          >
            <el-option
              v-for="profile in profileOptions"
              :key="profile.id"
              :label="profile.name"
              :value="profile.id"
            />
          </el-select>
          <div class="field-hint">選擇 Profile 後會使用新的 profile/account registry；留空則維持舊版 QR login 帳號。</div>
        </el-form-item>

        <el-form-item label="平台" prop="platform">
          <el-select
            v-model="accountForm.platform"
            placeholder="請選擇平台"
            style="width: 100%"
            :disabled="sseConnecting"
            @change="handlePlatformChange"
          >
            <el-option
              v-for="platform in accountPlatformTabs"
              :key="platform.value"
              :label="platform.label"
              :value="platform.value"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="名稱" prop="name">
          <el-input
            v-model="accountForm.name"
            placeholder="請輸入帳號名稱"
            :disabled="sseConnecting"
          />
        </el-form-item>

        <template v-if="isStructuredAccountForm">
          <el-form-item label="登入方式">
            <el-select v-model="accountForm.authType" style="width: 100%">
              <el-option label="cookie" value="cookie" />
              <el-option label="oauth" value="oauth" />
              <el-option label="manual" value="manual" />
            </el-select>
          </el-form-item>
          <el-form-item label="啟用狀態">
            <el-switch v-model="accountForm.enabled" active-text="啟用" inactive-text="停用" />
          </el-form-item>
          <el-form-item v-if="accountForm.authType === 'cookie'" label="Cookie 路徑">
            <el-input
              v-model="accountForm.cookiePath"
              placeholder="可留空，後端會依 Profile/平台自動產生"
            />
          </el-form-item>
          <el-form-item v-if="selectedPlatformGuide" label="JSON 範例">
            <div class="json-guide">
              <div class="json-guide__header">
                <div>
                  <div class="json-guide__title">{{ selectedPlatformGuide.title }}</div>
                  <div class="json-guide__description">{{ selectedPlatformGuide.description }}</div>
                </div>
                <div class="json-guide__actions">
                  <el-button size="small" @click="applyPlatformExample">帶入範例</el-button>
                  <el-button size="small" @click="formatConfigJson" :disabled="!accountForm.configText.trim()">格式化 JSON</el-button>
                </div>
              </div>
              <ul class="json-guide__keys">
                <li v-for="entry in selectedPlatformGuide.keys" :key="entry.key">
                  <strong>{{ entry.key }}</strong> — {{ entry.description }}
                </li>
              </ul>
              <pre class="json-guide__example">{{ selectedPlatformGuide.exampleText }}</pre>
            </div>
          </el-form-item>
          <el-form-item label="平台設定 JSON">
            <el-input
              v-model="accountForm.configText"
              type="textarea"
              :rows="9"
              :placeholder="jsonPlaceholder"
            />
            <div class="field-hint">此 JSON 會原樣存入 account config，供 campaign 與 publisher runtime 使用。</div>
          </el-form-item>
        </template>

        <div v-else class="legacy-login-hint">
          Legacy 帳號使用現有 QR Login / Cookie 流程。非舊版平台請先建立 Profile，再新增該平台帳號。
        </div>

        <div v-if="sseConnecting" class="qrcode-container">
          <div v-if="qrCodeData && !loginStatus" class="qrcode-wrapper">
            <p class="qrcode-tip">請使用對應平台 App 掃描 QR Code 登入</p>
            <img :src="qrCodeData" alt="登入 QR Code" class="qrcode-image" />
          </div>
          <div v-else-if="!qrCodeData && !loginStatus" class="loading-wrapper">
            <el-icon class="is-loading"><Refresh /></el-icon>
            <span>載入中...</span>
          </div>
          <div v-else-if="loginStatus === '200'" class="success-wrapper">
            <el-icon><CircleCheckFilled /></el-icon>
            <span>新增成功</span>
          </div>
          <div v-else-if="loginStatus === '500'" class="error-wrapper">
            <el-icon><CircleCloseFilled /></el-icon>
            <span>新增失敗，請稍後再試</span>
          </div>
        </div>
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="dialogVisible = false">取消</el-button>
          <el-button
            type="primary"
            @click="submitAccountForm"
            :loading="sseConnecting"
            :disabled="sseConnecting"
          >
            {{ sseConnecting ? '處理中' : '確認' }}
          </el-button>
        </span>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'
import { CircleCheckFilled, CircleCloseFilled, Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { accountApi } from '@/api/account'
import { profilesApi } from '@/api/profiles'
import AccountTabPane from '@/components/AccountTabPane.vue'
import { useAccountStore } from '@/stores/account'
import { useAppStore } from '@/stores/app'
import { useProfilesStore } from '@/stores/profiles'
import { buildApiUrl } from '@/utils/api-url'
import { appendAuthQuery, getToken } from '@/utils/auth'
import { http } from '@/utils/request'
import { PROFILE_PLATFORM_OPTIONS, getLegacyPlatformType, getPlatformLabel } from '@/utils/platforms'

const PLATFORM_JSON_GUIDES = {
  twitter: {
    title: 'X / Twitter JSON 設定',
    description: '適合放與匯出、預設連結或帳號識別相關的設定。',
    defaultAuthType: 'oauth',
    example: {
      username: 'brand_main',
      sheetPostPreset: 'X Brand',
      defaultLink: 'https://example.com'
    },
    keys: [
      { key: 'username', description: 'X 帳號識別名稱' },
      { key: 'sheetPostPreset', description: 'Google Sheet / 匯出 preset 名稱' },
      { key: 'defaultLink', description: '此帳號預設附加的連結' }
    ]
  },
  telegram: {
    title: 'Telegram JSON 設定',
    description: '至少需要 chatId，才能在之後的 publisher runtime 中知道發送目標。',
    defaultAuthType: 'oauth',
    example: {
      chatId: '@brandchannel',
      disableWebPreview: false,
      silent: false
    },
    keys: [
      { key: 'chatId', description: '頻道或群組，例如 @brandchannel' },
      { key: 'disableWebPreview', description: '是否關閉連結預覽' },
      { key: 'silent', description: '是否靜音發送' }
    ]
  },
  reddit: {
    title: 'Reddit JSON 設定',
    description: '請提供至少一個 subreddit，供 campaign 準備與未來 publisher 使用。',
    defaultAuthType: 'oauth',
    example: {
      subreddits: ['subreddit_a', 'subreddit_b'],
      sheetPostPreset: 'Reddit Brand'
    },
    keys: [
      { key: 'subreddits', description: '要投遞的 subreddit 名稱陣列' },
      { key: 'sheetPostPreset', description: 'Google Sheet / 匯出 preset 名稱' }
    ]
  },
  youtube: {
    title: 'YouTube JSON 設定',
    description: '請提供 channelId；privacyStatus 與 playlistId 可選。',
    defaultAuthType: 'oauth',
    example: {
      channelId: 'UCxxxxxxxxxxxxxxxx',
      privacyStatus: 'public',
      playlistId: ''
    },
    keys: [
      { key: 'channelId', description: 'YouTube channel ID' },
      { key: 'privacyStatus', description: 'public / unlisted / private' },
      { key: 'playlistId', description: '可選，預設播放清單 ID' }
    ]
  }
}

Object.values(PLATFORM_JSON_GUIDES).forEach((guide) => {
  guide.exampleText = JSON.stringify(guide.example, null, 2)
})

const accountStore = useAccountStore()
const appStore = useAppStore()
const profilesStore = useProfilesStore()

const activeTab = ref('all')
const searchKeyword = ref('')
const selectedProfileFilter = ref('all')

const accountPlatformTabs = PROFILE_PLATFORM_OPTIONS
const profileOptions = computed(() => profilesStore.profiles)

const dialogVisible = ref(false)
const dialogType = ref('add')
const accountFormRef = ref(null)
const profileDialogVisible = ref(false)
const profileFormRef = ref(null)

const accountForm = reactive({
  id: null,
  profileId: null,
  name: '',
  platform: '',
  authType: 'cookie',
  enabled: true,
  cookiePath: '',
  configText: '',
  status: '正常'
})

const profileForm = reactive({
  name: '',
  description: '',
  systemPrompt: '',
  watermark: '',
  contactDetails: '',
  ctaText: ''
})

const rules = {
  platform: [{ required: true, message: '請選擇平台', trigger: 'change' }],
  name: [{ required: true, message: '請輸入帳號名稱', trigger: 'blur' }]
}

const isStructuredAccountForm = computed(() => Boolean(accountForm.profileId))
const selectedPlatformGuide = computed(() => PLATFORM_JSON_GUIDES[accountForm.platform] || null)
const jsonPlaceholder = computed(() => selectedPlatformGuide.value?.exampleText || '{\n  "key": "value"\n}')

const sseConnecting = ref(false)
const qrCodeData = ref('')
const loginStatus = ref('')
let eventSource = null

const filteredAccounts = computed(() => {
  const keyword = searchKeyword.value.trim().toLowerCase()
  return accountStore.accounts.filter((account) => {
    if (selectedProfileFilter.value === 'legacy' && account.profileId != null) return false
    if (selectedProfileFilter.value !== 'all' && selectedProfileFilter.value !== 'legacy') {
      if (String(account.profileId) !== selectedProfileFilter.value) return false
    }

    if (!keyword) return true
    return [account.name, account.platform, account.profileName]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(keyword))
  })
})

const getFilteredAccountsByPlatform = (platformLabel) =>
  filteredAccounts.value.filter((account) => account.platform === platformLabel)

const onSearchChange = (value) => {
  searchKeyword.value = value
}

const resetAccountForm = () => {
  Object.assign(accountForm, {
    id: null,
    profileId: selectedProfileFilter.value !== 'all' && selectedProfileFilter.value !== 'legacy'
      ? Number(selectedProfileFilter.value)
      : null,
    name: '',
    platform: activeTab.value !== 'all' ? activeTab.value : '',
    authType: 'cookie',
    enabled: true,
    cookiePath: '',
    configText: '',
    status: '正常'
  })
}

const openProfileDialog = () => {
  Object.assign(profileForm, {
    name: '',
    description: '',
    systemPrompt: '',
    watermark: '',
    contactDetails: '',
    ctaText: ''
  })
  profileDialogVisible.value = true
}

const submitProfileForm = async () => {
  if (!profileForm.name.trim()) {
    ElMessage.error('請輸入 Profile 名稱')
    return
  }

  try {
    const created = await profilesApi.create({
      name: profileForm.name,
      description: profileForm.description,
      settings: {
        systemPrompt: profileForm.systemPrompt,
        watermark: profileForm.watermark,
        contactDetails: profileForm.contactDetails,
        ctaText: profileForm.ctaText
      }
    })
    await profilesStore.refreshProfiles()
    profileDialogVisible.value = false
    selectedProfileFilter.value = String(created.data.id)
    ElMessage.success('Profile 建立成功')
  } catch (error) {
    console.error('建立 Profile 失敗:', error)
    ElMessage.error(error?.message || '建立 Profile 失敗')
  }
}

const fetchAccounts = async (validateLegacy = true) => {
  if (appStore.isAccountRefreshing) return
  appStore.setAccountRefreshing(true)

  try {
    const profiles = await profilesStore.refreshProfiles()
    const legacyResponse = validateLegacy
      ? await accountApi.getValidAccounts()
      : await accountApi.getAccounts()
    const legacyAccounts = legacyResponse?.data || []

    const structuredGroups = await Promise.all(
      profiles.map(async (profile) => {
        const items = await profilesStore.fetchAccountsForProfile(profile.id)
        return items.map((item) => ({ ...item, profileName: profile.name }))
      })
    )

    accountStore.setAccounts([...legacyAccounts, ...structuredGroups.flat()])
    if (validateLegacy) {
      ElMessage.success('帳號資料取得成功')
      if (appStore.isFirstTimeAccountManagement) {
        appStore.setAccountManagementVisited()
      }
    }
  } catch (error) {
    console.error('取得帳號資料失敗:', error)
    if (validateLegacy) {
      ElMessage.error('取得帳號資料失敗')
    }
  } finally {
    appStore.setAccountRefreshing(false)
  }
}

const refreshAccounts = () => fetchAccounts(true)

onMounted(() => {
  fetchAccounts(false)
  setTimeout(() => {
    fetchAccounts(true)
  }, 100)
})

const handlePlatformChange = (platform) => {
  if (!isStructuredAccountForm.value) return
  const guide = PLATFORM_JSON_GUIDES[platform]
  if (!guide) return
  if (dialogType.value === 'add' && !accountForm.configText.trim()) {
    accountForm.configText = guide.exampleText
  }
  if (dialogType.value === 'add') {
    accountForm.authType = guide.defaultAuthType
  }
}

const applyPlatformExample = () => {
  if (selectedPlatformGuide.value) {
    accountForm.configText = selectedPlatformGuide.value.exampleText
    accountForm.authType = selectedPlatformGuide.value.defaultAuthType
  }
}

const formatConfigJson = () => {
  if (!accountForm.configText.trim()) return
  try {
    accountForm.configText = JSON.stringify(JSON.parse(accountForm.configText), null, 2)
  } catch (error) {
    ElMessage.error('目前 JSON 格式無法格式化，請先修正內容')
  }
}

const handleAddAccount = () => {
  dialogType.value = 'add'
  resetAccountForm()
  sseConnecting.value = false
  qrCodeData.value = ''
  loginStatus.value = ''
  dialogVisible.value = true
  if (accountForm.platform) {
    handlePlatformChange(accountForm.platform)
  }
}

const handleEdit = (row) => {
  dialogType.value = 'edit'
  Object.assign(accountForm, {
    id: row.id,
    profileId: row.profileId,
    name: row.name,
    platform: row.platformSlug || row.platform,
    authType: row.authType || 'cookie',
    enabled: row.enabled !== false,
    cookiePath: row.filePath || '',
    configText: row.config && Object.keys(row.config).length > 0
      ? JSON.stringify(row.config, null, 2)
      : '',
    status: row.status
  })
  sseConnecting.value = false
  qrCodeData.value = ''
  loginStatus.value = ''
  dialogVisible.value = true
}

const handleDelete = (row) => {
  ElMessageBox.confirm(`確定要刪除帳號 ${row.name} 嗎？`, '警告', {
    confirmButtonText: '確定',
    cancelButtonText: '取消',
    type: 'warning'
  })
    .then(async () => {
      try {
        const response = await accountApi.deleteAccount(row.id)
        if (response.code === 200) {
          accountStore.deleteAccount(row.id)
          ElMessage.success('刪除成功')
        }
      } catch (error) {
        console.error('刪除帳號失敗:', error)
        ElMessage.error('刪除帳號失敗')
      }
    })
    .catch(() => {})
}

const handleDownloadCookie = async (row) => {
  try {
    const response = await fetch(
      buildApiUrl(`/downloadCookie?filePath=${encodeURIComponent(row.filePath)}`),
      {
        headers: getToken() ? { Authorization: `Bearer ${getToken()}` } : {}
      }
    )
    if (!response.ok) {
      ElMessage.error(response.status === 401 ? '未授權，請重新登入' : '下載失敗')
      return
    }
    const blob = await response.blob()
    const objectUrl = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = objectUrl
    link.download = `${row.name}_cookie.json`
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    URL.revokeObjectURL(objectUrl)
  } catch (error) {
    console.error('下載 Cookie 失敗:', error)
    ElMessage.error('下載 Cookie 失敗')
  }
}

const handleUploadCookie = (row) => {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.json'
  input.style.display = 'none'
  document.body.appendChild(input)

  input.onchange = async (event) => {
    const file = event.target.files[0]
    if (!file) return
    if (!file.name.endsWith('.json')) {
      ElMessage.error('請選擇 JSON 格式的 Cookie 檔案')
      document.body.removeChild(input)
      return
    }

    try {
      const formData = new FormData()
      formData.append('file', file)
      formData.append('id', row.id)
      formData.append('platform', row.platformSlug || row.platform)
      await http.upload('/uploadCookie', formData)
      ElMessage.success('Cookie 檔案上傳成功')
      await refreshAccounts()
    } catch (error) {
      ElMessage.error('Cookie 檔案上傳失敗')
    } finally {
      document.body.removeChild(input)
    }
  }

  input.click()
}

const handleReLogin = (row) => {
  if (!row.supportsRelogin) {
    ElMessage.warning('此帳號不支援舊版 QR 重新登入，請改用 Cookie / OAuth 更新')
    return
  }

  dialogType.value = 'edit'
  Object.assign(accountForm, {
    id: row.id,
    profileId: null,
    name: row.name,
    platform: row.platformSlug || row.platform,
    authType: 'cookie',
    enabled: true,
    cookiePath: row.filePath || '',
    configText: '',
    status: row.status
  })

  sseConnecting.value = false
  qrCodeData.value = ''
  loginStatus.value = ''
  dialogVisible.value = true

  setTimeout(() => {
    connectSSE(accountForm.platform, accountForm.name)
  }, 300)
}

const closeSSEConnection = () => {
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }
}

const connectSSE = (platform, name) => {
  closeSSEConnection()
  sseConnecting.value = true
  qrCodeData.value = ''
  loginStatus.value = ''

  const type = String(getLegacyPlatformType(platform) || 1)
  const url = appendAuthQuery(buildApiUrl(`/login?type=${type}&id=${encodeURIComponent(name)}`))
  eventSource = new EventSource(url)

  eventSource.onmessage = (event) => {
    const data = event.data
    if (!qrCodeData.value && data.length > 100) {
      qrCodeData.value = data.startsWith('data:image') ? data : `data:image/png;base64,${data}`
    } else if (data === '200' || data === '500') {
      loginStatus.value = data
      if (data === '200') {
        setTimeout(() => {
          closeSSEConnection()
          setTimeout(() => {
            dialogVisible.value = false
            sseConnecting.value = false
            ElMessage.success(dialogType.value === 'edit' ? '重新登入成功' : '帳號新增成功')
            ElMessage({ type: 'info', message: '正在同步帳號資訊...', duration: 0 })
            refreshAccounts().then(() => {
              ElMessage.closeAll()
              ElMessage.success('帳號資訊已更新')
            })
          }, 1000)
        }, 1000)
      } else {
        closeSSEConnection()
        setTimeout(() => {
          sseConnecting.value = false
          qrCodeData.value = ''
          loginStatus.value = ''
        }, 2000)
      }
    }
  }

  eventSource.onerror = (error) => {
    console.error('SSE 連線錯誤:', error)
    ElMessage.error('連線伺服器失敗，請稍後再試')
    closeSSEConnection()
    sseConnecting.value = false
  }
}

const validateStructuredConfig = (platform, config) => {
  if (platform === 'telegram' && !config.chatId) {
    return 'Telegram 設定 JSON 需要 chatId'
  }
  if (platform === 'reddit' && (!Array.isArray(config.subreddits) || config.subreddits.length === 0)) {
    return 'Reddit 設定 JSON 需要至少一個 subreddit'
  }
  if (platform === 'youtube' && !config.channelId) {
    return 'YouTube 設定 JSON 需要 channelId'
  }
  return null
}

const submitStructuredAccount = async () => {
  let config = {}
  if (accountForm.configText.trim()) {
    try {
      config = JSON.parse(accountForm.configText)
    } catch (error) {
      throw new Error('平台設定 JSON 格式錯誤')
    }
  }

  const validationError = validateStructuredConfig(accountForm.platform, config)
  if (validationError) {
    throw new Error(validationError)
  }

  const payload = {
    platform: accountForm.platform,
    accountName: accountForm.name,
    authType: accountForm.authType,
    enabled: accountForm.enabled,
    config
  }
  if (accountForm.authType === 'cookie' && accountForm.cookiePath.trim()) {
    payload.cookiePath = accountForm.cookiePath.trim()
  }

  if (dialogType.value === 'add') {
    await profilesApi.createAccount(accountForm.profileId, payload)
  } else {
    await profilesApi.updateAccount(accountForm.id, payload)
  }
}

const submitLegacyAccount = async () => {
  const legacyType = getLegacyPlatformType(accountForm.platform)
  if (legacyType == null) {
    throw new Error(`${getPlatformLabel(accountForm.platform)} 帳號必須先指定 Profile`) 
  }

  if (dialogType.value === 'add') {
    connectSSE(accountForm.platform, accountForm.name)
    return
  }

  await accountApi.updateAccount({
    id: accountForm.id,
    type: legacyType,
    userName: accountForm.name
  })
}

const submitAccountForm = () => {
  accountFormRef.value.validate(async (valid) => {
    if (!valid) return false

    try {
      if (isStructuredAccountForm.value) {
        await submitStructuredAccount()
        dialogVisible.value = false
        ElMessage.success(dialogType.value === 'add' ? '帳號新增成功' : '帳號更新成功')
        await refreshAccounts()
      } else {
        await submitLegacyAccount()
        if (dialogType.value === 'edit') {
          dialogVisible.value = false
          ElMessage.success('更新成功')
          await refreshAccounts()
        }
      }
    } catch (error) {
      console.error('提交帳號失敗:', error)
      ElMessage.error(error?.message || '提交帳號失敗')
    }
  })
}

onBeforeUnmount(() => {
  closeSSEConnection()
})
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

@keyframes rotate {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

.account-management {
  .page-header {
    margin-bottom: 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;

    h1 {
      font-size: 24px;
      color: $text-primary;
      margin: 0;
    }

    .profile-toolbar {
      display: flex;
      align-items: center;
      gap: 12px;
    }
  }

  .account-tabs {
    background-color: #fff;
    border-radius: 4px;
    box-shadow: $box-shadow-light;

    .account-tabs-nav {
      padding: 20px;
    }
  }

  .field-hint,
  .legacy-login-hint {
    margin-top: 6px;
    color: #909399;
    font-size: 13px;
    line-height: 1.6;
  }

  .legacy-login-hint {
    margin-bottom: 12px;
    padding: 10px 12px;
    background: #f5f7fa;
    border-radius: 4px;
  }

  .json-guide {
    width: 100%;
    padding: 12px;
    background: #f5f7fa;
    border-radius: 6px;
    border: 1px solid #ebeef5;

    .json-guide__header {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      margin-bottom: 10px;
    }

    .json-guide__title {
      font-size: 14px;
      font-weight: 600;
      color: #303133;
      margin-bottom: 4px;
    }

    .json-guide__description {
      font-size: 13px;
      color: #606266;
      line-height: 1.6;
    }

    .json-guide__actions {
      display: flex;
      gap: 8px;
      flex-shrink: 0;
    }

    .json-guide__keys {
      margin: 0 0 10px 18px;
      padding: 0;
      color: #606266;
      font-size: 13px;
      line-height: 1.7;
    }

    .json-guide__example {
      margin: 0;
      padding: 10px;
      background: #fff;
      border-radius: 4px;
      border: 1px solid #e4e7ed;
      font-size: 12px;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-word;
    }
  }

  .qrcode-container {
    margin-top: 20px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 250px;

    .qrcode-wrapper {
      text-align: center;

      .qrcode-tip {
        margin-bottom: 15px;
        color: #606266;
      }

      .qrcode-image {
        max-width: 200px;
        max-height: 200px;
        border: 1px solid #ebeef5;
        background-color: black;
      }
    }

    .loading-wrapper,
    .success-wrapper,
    .error-wrapper {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 10px;

      .el-icon {
        font-size: 48px;

        &.is-loading {
          animation: rotate 1s linear infinite;
        }
      }

      span {
        font-size: 16px;
      }
    }

    .success-wrapper .el-icon {
      color: #67c23a;
    }

    .error-wrapper .el-icon {
      color: #f56c6c;
    }
  }
}
</style>
