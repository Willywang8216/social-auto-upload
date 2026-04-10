<template>
  <div class="account-management">
    <div class="page-header">
      <h1>帳號管理</h1>
    </div>

    <div class="account-list-container">
      <div class="account-toolbar">
        <div class="toolbar-filters">
          <el-input
            v-model="searchKeyword"
            placeholder="輸入帳號名稱搜尋"
            clearable
          />
          <el-select v-model="platformFilter" clearable placeholder="全部平台" style="width: 200px">
            <el-option
              v-for="option in platformOptions"
              :key="option.key"
              :label="option.label"
              :value="option.key"
            />
          </el-select>
        </div>

        <div class="action-buttons">
          <el-button type="primary" @click="openCreateDialog">新增帳號</el-button>
          <el-button @click="fetchAccounts" :loading="isRefreshing">重新整理</el-button>
          <el-button type="success" plain @click="refreshValidAccounts" :loading="isValidating">
            重新驗證狀態
          </el-button>
        </div>
      </div>

      <div v-if="filteredAccounts.length > 0">
        <el-table :data="filteredAccounts" style="width: 100%">
          <el-table-column prop="name" label="帳號名稱" min-width="200" />
          <el-table-column prop="platform" label="平台" width="160" />
          <el-table-column label="登入模式" width="140">
            <template #default="scope">
              {{ getAuthModeLabel(scope.row.authMode) }}
            </template>
          </el-table-column>
          <el-table-column label="狀態" width="120">
            <template #default="scope">
              <el-tag :type="getStatusTagType(scope.row.status)">
                {{ scope.row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="Metadata" min-width="260">
            <template #default="scope">
              <div class="cell-lines">
                <div>{{ getMetadataSummary(scope.row.metadata) }}</div>
                <div v-if="scope.row.filePath" class="muted-text">Cookie 檔：{{ scope.row.filePath }}</div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="操作" min-width="300">
            <template #default="scope">
              <div class="table-actions">
                <el-button size="small" @click="openEditDialog(scope.row)">編輯</el-button>
                <el-button
                  v-if="scope.row.supportsCookieUpload"
                  size="small"
                  type="warning"
                  plain
                  @click="openCookiePicker(scope.row)"
                >
                  上傳 Cookie
                </el-button>
                <el-button
                  v-if="scope.row.supportsCookieUpload && scope.row.filePath"
                  size="small"
                  type="info"
                  plain
                  @click="downloadCookie(scope.row)"
                >
                  下載 Cookie
                </el-button>
                <el-button size="small" type="danger" @click="handleDelete(scope.row)">刪除</el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div v-else class="empty-data">
        <el-empty description="目前沒有帳號資料" />
      </div>
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="dialogType === 'create' ? '新增帳號' : '編輯帳號'"
      width="760px"
      @closed="handleDialogClosed"
    >
      <el-form :model="accountForm" label-width="140px">
        <el-form-item label="帳號名稱">
          <el-input v-model="accountForm.name" placeholder="例如：光光 X 主帳" />
        </el-form-item>

        <el-form-item label="平台">
          <el-select v-model="accountForm.platformKey" style="width: 100%" @change="handlePlatformChange">
            <el-option
              v-for="option in platformOptions"
              :key="option.key"
              :label="option.label"
              :value="option.key"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="登入模式">
          <el-select v-model="accountForm.authMode" style="width: 100%">
            <el-option
              v-for="option in authModeOptions"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="Metadata JSON">
          <el-input
            v-model="accountForm.metadataJson"
            type="textarea"
            :rows="8"
            placeholder='例如：{"handle":"@creator","pageId":"12345"}'
          />
          <div class="muted-text">
            {{ selectedPlatformMetadataHint }}
          </div>
        </el-form-item>

        <el-form-item v-if="selectedPlatformConfig?.supportsQrLogin" label="國內平台登入">
          <el-alert
            title="國內平台可直接使用 QR 登入建立帳號。若先手動建立，也可之後再上傳 Cookie。"
            type="info"
            :closable="false"
            show-icon
          />
        </el-form-item>

        <div v-if="sseConnecting || qrCodeData || loginStatus" class="qr-login-panel">
          <h3>QR 登入</h3>
          <div v-if="qrCodeData" class="qr-image-wrapper">
            <img :src="qrCodeData" alt="QR Code" class="qr-image">
          </div>
          <el-alert
            v-if="loginStatus"
            :title="loginStatus === '200' ? '登入成功，正在同步帳號資料' : '登入失敗，請重試'"
            :type="loginStatus === '200' ? 'success' : 'error'"
            :closable="false"
            show-icon
          />
          <div v-else class="muted-text">
            {{ sseConnecting ? '等待掃碼與登入完成…' : '準備中…' }}
          </div>
        </div>
      </el-form>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="dialogVisible = false">取消</el-button>
          <el-button
            v-if="dialogType === 'create' && selectedPlatformConfig?.supportsQrLogin"
            type="success"
            plain
            @click="startQrLogin"
            :loading="sseConnecting"
          >
            QR 登入建立
          </el-button>
          <el-button type="primary" @click="submitAccountForm" :loading="isSubmitting">
            {{ isSubmitting ? '儲存中' : '儲存' }}
          </el-button>
        </div>
      </template>
    </el-dialog>

    <input
      ref="cookieInputRef"
      type="file"
      accept=".json,application/json"
      class="hidden-input"
      @change="handleCookieSelected"
    >
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { accountApi } from '@/api/account'
import { buildApiUrl } from '@/utils/apiBase'
import { useAccountStore } from '@/stores/account'

const accountStore = useAccountStore()

const platformOptions = [
  { key: 'xiaohongshu', label: '小紅書', legacyType: 1, supportsQrLogin: true, supportsCookieUpload: true },
  { key: 'channels', label: '影片號', legacyType: 2, supportsQrLogin: true, supportsCookieUpload: true },
  { key: 'douyin', label: '抖音', legacyType: 3, supportsQrLogin: true, supportsCookieUpload: true },
  { key: 'kuaishou', label: '快手', legacyType: 4, supportsQrLogin: true, supportsCookieUpload: true },
  { key: 'twitter', label: 'X / Twitter', legacyType: 0, supportsQrLogin: false, supportsCookieUpload: false },
  { key: 'threads', label: 'Threads', legacyType: 0, supportsQrLogin: false, supportsCookieUpload: false },
  { key: 'facebook', label: 'Facebook', legacyType: 0, supportsQrLogin: false, supportsCookieUpload: false },
  { key: 'reddit', label: 'Reddit', legacyType: 0, supportsQrLogin: false, supportsCookieUpload: false },
  { key: 'tiktok', label: 'TikTok', legacyType: 0, supportsQrLogin: false, supportsCookieUpload: false },
  { key: 'youtube', label: 'YouTube', legacyType: 0, supportsQrLogin: false, supportsCookieUpload: false }
]

const authModeOptionGroups = {
  domestic: [
    { value: 'qr_cookie', label: 'QR / Cookie' },
    { value: 'manual', label: '手動建立' }
  ],
  international: [
    { value: 'manual', label: '手動建立' },
    { value: 'oauth_token', label: 'OAuth / Token' }
  ]
}

const metadataHints = {
  twitter: '建議欄位：handle、appId、clientId、notes。',
  threads: '建議欄位：handle、userId、notes。',
  facebook: '建議欄位：pageId、pageName、notes。',
  reddit: '建議欄位：subreddit、username、notes。',
  tiktok: '建議欄位：username、channelId、notes。',
  youtube: '建議欄位：channelId、channelTitle、notes。',
  xiaohongshu: '通常不需要額外 metadata，可留空或填 notes。',
  channels: '通常不需要額外 metadata，可留空或填 notes。',
  douyin: '通常不需要額外 metadata，可留空或填 notes。',
  kuaishou: '通常不需要額外 metadata，可留空或填 notes。'
}

const makeDefaultForm = () => ({
  id: null,
  name: '',
  platformKey: 'twitter',
  authMode: 'manual',
  metadataJson: '{}',
  filePath: ''
})

const searchKeyword = ref('')
const platformFilter = ref('')
const isRefreshing = ref(false)
const isValidating = ref(false)
const isSubmitting = ref(false)
const dialogVisible = ref(false)
const dialogType = ref('create')
const accountForm = ref(makeDefaultForm())
const cookieInputRef = ref(null)
const cookieTargetAccount = ref(null)
const sseConnecting = ref(false)
const qrCodeData = ref('')
const loginStatus = ref('')
let eventSource = null

const filteredAccounts = computed(() => {
  const keyword = searchKeyword.value.trim().toLowerCase()
  return accountStore.accounts.filter((account) => {
    const matchesKeyword = !keyword || account.name.toLowerCase().includes(keyword)
    const matchesPlatform = !platformFilter.value || account.platformKey === platformFilter.value
    return matchesKeyword && matchesPlatform
  })
})

const selectedPlatformConfig = computed(() => (
  platformOptions.find(option => option.key === accountForm.value.platformKey) || null
))

const authModeOptions = computed(() => (
  selectedPlatformConfig.value?.supportsQrLogin
    ? authModeOptionGroups.domestic
    : authModeOptionGroups.international
))

const selectedPlatformMetadataHint = computed(() => (
  metadataHints[accountForm.value.platformKey] || '可留空，或填入此平台後續發佈所需的 metadata。'
))

const getAuthModeLabel = (authMode) => {
  const option = [...authModeOptionGroups.domestic, ...authModeOptionGroups.international]
    .find(item => item.value === authMode)
  return option?.label || authMode || '未設定'
}

const getStatusTagType = (status) => {
  if (status === '正常') {
    return 'success'
  }
  if (status === '驗證中') {
    return 'warning'
  }
  return 'danger'
}

const getMetadataSummary = (metadata) => {
  const entries = Object.entries(metadata || {}).filter(([, value]) => value !== '' && value !== null && value !== undefined)
  if (!entries.length) {
    return '無額外 metadata'
  }
  return entries.slice(0, 3).map(([key, value]) => `${key}: ${value}`).join('｜')
}

const parseMetadataJson = () => {
  if (!accountForm.value.metadataJson.trim()) {
    return {}
  }

  try {
    const parsed = JSON.parse(accountForm.value.metadataJson)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('Metadata 必須是 JSON 物件')
    }
    return parsed
  } catch (error) {
    throw new Error('Metadata JSON 格式錯誤')
  }
}

const closeSseConnection = () => {
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }
  sseConnecting.value = false
}

const resetQrState = () => {
  qrCodeData.value = ''
  loginStatus.value = ''
  closeSseConnection()
}

const fetchAccounts = async () => {
  isRefreshing.value = true
  try {
    const response = await accountApi.getAccounts()
    if (response.code === 200) {
      accountStore.setAccounts(response.data || [])
    }
  } catch (error) {
    ElMessage.error(error.message || '取得帳號清單失敗')
  } finally {
    isRefreshing.value = false
  }
}

const refreshValidAccounts = async () => {
  isValidating.value = true
  try {
    const response = await accountApi.getValidAccounts()
    if (response.code === 200) {
      accountStore.setAccounts(response.data || [])
      ElMessage.success('帳號狀態已更新')
    }
  } catch (error) {
    ElMessage.error(error.message || '驗證帳號狀態失敗')
  } finally {
    isValidating.value = false
  }
}

const handlePlatformChange = () => {
  accountForm.value.authMode = selectedPlatformConfig.value?.supportsQrLogin ? 'qr_cookie' : 'manual'
}

const openCreateDialog = () => {
  dialogType.value = 'create'
  accountForm.value = makeDefaultForm()
  dialogVisible.value = true
}

const openEditDialog = (account) => {
  dialogType.value = 'edit'
  accountForm.value = {
    id: account.id,
    name: account.name,
    platformKey: account.platformKey,
    authMode: account.authMode || (account.supportsQrLogin ? 'qr_cookie' : 'manual'),
    metadataJson: JSON.stringify(account.metadata || {}, null, 2),
    filePath: account.filePath || ''
  }
  dialogVisible.value = true
}

const connectSseLogin = (platformConfig, accountName) => {
  resetQrState()
  sseConnecting.value = true
  const url = buildApiUrl(`/login?type=${platformConfig.legacyType}&id=${encodeURIComponent(accountName)}`)
  eventSource = new EventSource(url)

  eventSource.onmessage = async (event) => {
    const data = event.data
    if (!qrCodeData.value && data.length > 100) {
      qrCodeData.value = data.startsWith('data:image') ? data : `data:image/png;base64,${data}`
      return
    }

    if (data === '200' || data === '500') {
      loginStatus.value = data
      closeSseConnection()

      if (data === '200') {
        ElMessage.success('QR 登入成功，正在同步帳號清單')
        await fetchAccounts()
        dialogVisible.value = false
      } else {
        ElMessage.error('QR 登入失敗，請重試')
      }
    }
  }

  eventSource.onerror = () => {
    ElMessage.error('QR 登入連線失敗')
    closeSseConnection()
  }
}

const startQrLogin = () => {
  if (!accountForm.value.name.trim()) {
    ElMessage.warning('請先輸入帳號名稱')
    return
  }
  if (!selectedPlatformConfig.value?.supportsQrLogin) {
    ElMessage.warning('目前選擇的平台不支援 QR 登入')
    return
  }
  connectSseLogin(selectedPlatformConfig.value, accountForm.value.name.trim())
}

const submitAccountForm = async () => {
  if (!accountForm.value.name.trim()) {
    ElMessage.warning('請輸入帳號名稱')
    return
  }

  let metadata = {}
  try {
    metadata = parseMetadataJson()
  } catch (error) {
    ElMessage.error(error.message)
    return
  }

  isSubmitting.value = true
  try {
    const payload = {
      id: accountForm.value.id,
      userName: accountForm.value.name.trim(),
      name: accountForm.value.name.trim(),
      platformKey: accountForm.value.platformKey,
      authMode: accountForm.value.authMode,
      metadata,
      status: 0
    }

    const response = dialogType.value === 'create'
      ? await accountApi.addAccount(payload)
      : await accountApi.updateAccount(payload)

    if (response.code === 200) {
      if (dialogType.value === 'create') {
        accountStore.addAccount(response.data)
      } else {
        accountStore.updateAccount(response.data.id, response.data)
      }
      dialogVisible.value = false
      ElMessage.success(dialogType.value === 'create' ? '帳號建立成功' : '帳號更新成功')
    }
  } catch (error) {
    ElMessage.error(error.message || '帳號儲存失敗')
  } finally {
    isSubmitting.value = false
  }
}

const openCookiePicker = (account) => {
  cookieTargetAccount.value = account
  if (cookieInputRef.value) {
    cookieInputRef.value.value = ''
    cookieInputRef.value.click()
  }
}

const handleCookieSelected = async (event) => {
  const file = event.target.files?.[0]
  if (!file || !cookieTargetAccount.value) {
    return
  }

  try {
    const response = await accountApi.uploadCookie({
      id: cookieTargetAccount.value.id,
      platform: cookieTargetAccount.value.platformKey,
      file
    })
    if (response.code === 200) {
      ElMessage.success('Cookie 檔案上傳成功')
      await fetchAccounts()
    }
  } catch (error) {
    ElMessage.error(error.message || 'Cookie 檔案上傳失敗')
  } finally {
    cookieTargetAccount.value = null
    if (cookieInputRef.value) {
      cookieInputRef.value.value = ''
    }
  }
}

const downloadCookie = (account) => {
  if (!account.filePath) {
    ElMessage.warning('這個帳號沒有 Cookie 檔案')
    return
  }
  window.open(accountApi.getDownloadCookieUrl(account.filePath), '_blank')
}

const handleDelete = (account) => {
  ElMessageBox.confirm(
    `確定要刪除帳號「${account.name}」嗎？`,
    '提醒',
    {
      confirmButtonText: '確定',
      cancelButtonText: '取消',
      type: 'warning'
    }
  ).then(async () => {
    try {
      await accountApi.deleteAccount(account.id)
      accountStore.deleteAccount(account.id)
      ElMessage.success('刪除成功')
    } catch (error) {
      ElMessage.error(error.message || '刪除失敗')
    }
  }).catch(() => {})
}

const handleDialogClosed = () => {
  resetQrState()
}

onMounted(() => {
  fetchAccounts()
})
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

.account-management {
  .page-header {
    margin-bottom: 20px;

    h1 {
      font-size: 24px;
      color: $text-primary;
      margin: 0;
    }
  }

  .account-list-container {
    background-color: #fff;
    border-radius: 4px;
    box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.1);
    padding: 20px;
  }

  .account-toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
    margin-bottom: 20px;
  }

  .toolbar-filters {
    display: flex;
    gap: 12px;
    flex: 1;
  }

  .action-buttons {
    display: flex;
    gap: 10px;
  }

  .table-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .empty-data {
    padding: 40px 0;
  }

  .muted-text {
    color: #909399;
    font-size: 13px;
    margin-top: 8px;
  }

  .cell-lines {
    line-height: 1.6;
  }

  .qr-login-panel {
    margin-top: 20px;
    padding: 16px;
    border: 1px solid #e5eaf3;
    border-radius: 10px;
    background-color: #f8fafc;

    h3 {
      margin: 0 0 12px 0;
      font-size: 16px;
      color: $text-primary;
    }
  }

  .qr-image-wrapper {
    display: flex;
    justify-content: center;
    margin-bottom: 16px;
  }

  .qr-image {
    width: 220px;
    height: 220px;
    object-fit: contain;
    border-radius: 8px;
    background: #fff;
    padding: 8px;
  }

  .hidden-input {
    display: none;
  }
}

.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}
</style>
