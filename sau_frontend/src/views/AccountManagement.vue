<template>
  <div class="account-management">
    <div class="page-header">
      <h1>帳號管理</h1>
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
            @refresh="fetchAccounts"
            @relogin="handleReLogin"
            @search="onSearchChange"
          />
        </el-tab-pane>
        <el-tab-pane
          v-for="platform in accountPlatformTabs"
          :key="platform.publishSlug"
          :label="platform.label"
          :name="platform.publishSlug"
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
            @refresh="fetchAccounts"
            @relogin="handleReLogin"
            @search="onSearchChange"
          />
        </el-tab-pane>
      </el-tabs>
    </div>
    
    <!-- 添加/編輯帳號对话框 -->
    <el-dialog
      v-model="dialogVisible"
      :title="dialogType === 'add' ? '新增帳號' : '編輯帳號'"
      width="500px"
      :close-on-click-modal="false"
      :close-on-press-escape="!sseConnecting"
      :show-close="!sseConnecting"
    >
      <el-form :model="accountForm" label-width="80px" :rules="rules" ref="accountFormRef">
        <el-form-item label="平台" prop="platform">
          <el-select 
            v-model="accountForm.platform" 
            placeholder="請選擇平台" 
            style="width: 100%"
            :disabled="dialogType === 'edit' || sseConnecting"
          >
            <el-option
              v-for="platform in accountPlatformTabs"
              :key="platform.publishSlug"
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
        
        <!-- 二维码显示区域 -->
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
// Only the icons still rendered by THIS file remain; per-row action icons
// (Download, Upload, Loading, Refresh) live inside AccountTabPane now.
import { CircleCheckFilled, CircleCloseFilled, Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { accountApi } from '@/api/account'
import { useAccountStore } from '@/stores/account'
import { useAppStore } from '@/stores/app'
import { buildApiUrl } from '@/utils/api-url'
import { http } from '@/utils/request'
import { appendAuthQuery, getToken } from '@/utils/auth'
import {
  ACCOUNT_PLATFORM_OPTIONS,
  LEGACY_ACCOUNT_PLATFORM_ORDER,
  getLegacyPlatformType
} from '@/utils/platforms'
import AccountTabPane from '@/components/AccountTabPane.vue'

// 获取账号状态管理
const accountStore = useAccountStore()
// 获取应用状态管理
const appStore = useAppStore()

// 当前激活的标签页
const activeTab = ref('all')

// 搜索关键词
const searchKeyword = ref('')

const accountPlatformTabs = LEGACY_ACCOUNT_PLATFORM_ORDER
  .map((publishSlug) =>
    ACCOUNT_PLATFORM_OPTIONS.find((platform) => platform.publishSlug === publishSlug)
  )
  .filter(Boolean)

// 获取账号数据（快速，不验证）
const fetchAccountsQuick = async () => {
  try {
    const res = await accountApi.getAccounts()
    if (res.code === 200 && res.data) {
      // 将所有账号的状态暂时设为"验证中"
      const accountsWithPendingStatus = res.data.map(account => {
        const updatedAccount = [...account];
        updatedAccount[4] = -1; // -1 表示验证中的临时状态
        return updatedAccount;
      });
      accountStore.setAccounts(accountsWithPendingStatus);
    }
  } catch (error) {
    console.error('快速取得帳號資料失敗:', error)
  }
}

// 获取账号数据（带验证）
const fetchAccounts = async () => {
  if (appStore.isAccountRefreshing) return

  appStore.setAccountRefreshing(true)

  try {
    const res = await accountApi.getValidAccounts()
    if (res.code === 200 && res.data) {
      accountStore.setAccounts(res.data)
      ElMessage.success('帳號資料取得成功')
      // 标记为已访问
      if (appStore.isFirstTimeAccountManagement) {
        appStore.setAccountManagementVisited()
      }
    } else {
      ElMessage.error('取得帳號資料失敗')
    }
  } catch (error) {
    console.error('取得帳號資料失敗:', error)
    ElMessage.error('取得帳號資料失敗')
  } finally {
    appStore.setAccountRefreshing(false)
  }
}

// 后台验证所有账号（优化版本，使用setTimeout避免阻塞UI）
const validateAllAccountsInBackground = async () => {
  // 使用setTimeout将验证过程放在下一个事件循环，避免阻塞UI
  setTimeout(async () => {
    try {
      const res = await accountApi.getValidAccounts()
      if (res.code === 200 && res.data) {
        accountStore.setAccounts(res.data)
      }
    } catch (error) {
      console.error('后台验证账号失败:', error)
    }
  }, 0)
}

// 页面加载时获取账号数据
onMounted(() => {
  // 快速获取账号列表（不验证），立即显示
  fetchAccountsQuick()

  // 在后台验证所有账号
  setTimeout(() => {
    validateAllAccountsInBackground()
  }, 100) // 稍微延迟一下，让用户看到快速加载的效果
})

// 过滤后的账号列表
const filteredAccounts = computed(() => {
  if (!searchKeyword.value) return accountStore.accounts
  return accountStore.accounts.filter(account =>
    account.name.includes(searchKeyword.value)
  )
})

const getFilteredAccountsByPlatform = (platformLabel) =>
  filteredAccounts.value.filter(account => account.platform === platformLabel)

// 搜索处理。AccountTabPane 把输入值通过 @search 传回这里。
const onSearchChange = (value) => {
  searchKeyword.value = value
}

// 对话框相关
const dialogVisible = ref(false)
const dialogType = ref('add') // 'add' 或 'edit'
const accountFormRef = ref(null)

// 账号表单
const accountForm = reactive({
  id: null,
  name: '',
  platform: '',
  status: '正常'
})

// 表单验证规则
const rules = {
  platform: [{ required: true, message: '請選擇平台', trigger: 'change' }],
  name: [{ required: true, message: '請輸入帳號名稱', trigger: 'blur' }]
}

// SSE连接状态
const sseConnecting = ref(false)
const qrCodeData = ref('')
const loginStatus = ref('')

// 新增帳號
const handleAddAccount = () => {
  dialogType.value = 'add'
  Object.assign(accountForm, {
    id: null,
    name: '',
    platform: '',
    status: '正常'
  })
  // 重置SSE状态
  sseConnecting.value = false
  qrCodeData.value = ''
  loginStatus.value = ''
  dialogVisible.value = true
}

// 編輯帳號
const handleEdit = (row) => {
  dialogType.value = 'edit'
  Object.assign(accountForm, {
    id: row.id,
    name: row.name,
    platform: row.platform,
    status: row.status
  })
  dialogVisible.value = true
}

// 删除账号
const handleDelete = (row) => {
  ElMessageBox.confirm(
    `確定要刪除帳號 ${row.name} 嗎？`,
    '警告',
    {
      confirmButtonText: '確定',
      cancelButtonText: '取消',
      type: 'warning',
    }
  )
    .then(async () => {
      try {
        // 调用API删除账号
        const response = await accountApi.deleteAccount(row.id)

        if (response.code === 200) {
          // 从状态管理中删除账号
          accountStore.deleteAccount(row.id)
          ElMessage({
            type: 'success',
            message: '刪除成功',
          })
        } else {
          ElMessage.error(response.msg || '刪除失敗')
        }
      } catch (error) {
        console.error('刪除帳號失敗:', error)
        ElMessage.error('刪除帳號失敗')
      }
    })
    .catch(() => {
      // 取消删除
    })
}

// Cookie file download. We fetch via axios so the Authorization header is
// attached, then turn the response into a Blob and trigger a synthetic
// download. Using a plain <a download> would skip the auth header and 401.
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

// 上传Cookie文件
const handleUploadCookie = (row) => {
  // 创建一个隐藏的文件输入框
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.json'
  input.style.display = 'none'
  document.body.appendChild(input)

  input.onchange = async (event) => {
    const file = event.target.files[0]
    if (!file) return

    // 检查文件类型
    if (!file.name.endsWith('.json')) {
      ElMessage.error('請選擇 JSON 格式的 Cookie 檔案')
      document.body.removeChild(input)
      return
    }

    try {
      // 创建FormData对象
      const formData = new FormData()
      formData.append('file', file)
      formData.append('id', row.id)
      formData.append('platform', row.platform)

      // 使用统一的http封装发送上传请求
      const result = await http.upload('/uploadCookie', formData)

      ElMessage.success('Cookie 檔案上傳成功')
      // 刷新账号列表以显示更新
      fetchAccounts()
    } catch (error) {
      ElMessage.error('Cookie 檔案上傳失敗')
    } finally {
      document.body.removeChild(input)
    }
  }

  input.click()
}

// 重新登录账号
const handleReLogin = (row) => {
  // 设置表单信息
  dialogType.value = 'edit'
  Object.assign(accountForm, {
    id: row.id,
    name: row.name,
    platform: row.platform,
    status: row.status
  })

  // 重置SSE状态
  sseConnecting.value = false
  qrCodeData.value = ''
  loginStatus.value = ''

  // 显示对话框
  dialogVisible.value = true

  // 立即开始登录流程
  setTimeout(() => {
    connectSSE(row.platform, row.name)
  }, 300)
}

// SSE事件源对象
let eventSource = null

// 关闭SSE连接
const closeSSEConnection = () => {
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }
}

// 建立SSE连接
const connectSSE = (platform, name) => {
  // 关闭可能存在的连接
  closeSSEConnection()

  // 设置连接状态
  sseConnecting.value = true
  qrCodeData.value = ''
  loginStatus.value = ''

  const type = String(getLegacyPlatformType(platform) || 1)

  // EventSource cannot attach an Authorization header, so we tunnel the
  // auth token through a query parameter that the backend specifically
  // honours for /login. In open mode this is a no-op.
  const url = appendAuthQuery(
    buildApiUrl(`/login?type=${type}&id=${encodeURIComponent(name)}`)
  )

  eventSource = new EventSource(url)

  // 监听消息
  eventSource.onmessage = (event) => {
    const data = event.data

    // 如果还没有二维码数据，且数据长度较长，认为是二维码
    if (!qrCodeData.value && data.length > 100) {
      try {
        if (data.startsWith('data:image')) {
          qrCodeData.value = data
        } else {
          qrCodeData.value = `data:image/png;base64,${data}`
        }
      } catch (error) {
        // 处理二维码数据出错
      }
    }
    // 如果收到状态码
    else if (data === '200' || data === '500') {
      loginStatus.value = data

      // 如果登录成功
      if (data === '200') {
        setTimeout(() => {
          // 关闭连接
          closeSSEConnection()

          // 1秒后关闭对话框并开始刷新
          setTimeout(() => {
            dialogVisible.value = false
            sseConnecting.value = false

            // 根据是否是重新登录显示不同提示
            ElMessage.success(dialogType.value === 'edit' ? '重新登入成功' : '帳號新增成功')

            // 显示更新账号信息提示
            ElMessage({
              type: 'info',
              message: '正在同步帳號資訊...',
              duration: 0
            })

            // 触发刷新操作
            fetchAccounts().then(() => {
              // 刷新完成后关闭提示
              ElMessage.closeAll()
              ElMessage.success('帳號資訊已更新')
            })
          }, 1000)
        }, 1000)
      } else {
        // 登录失败，关闭连接
        closeSSEConnection()

        // 2秒后重置状态，允许重试
        setTimeout(() => {
          sseConnecting.value = false
          qrCodeData.value = ''
          loginStatus.value = ''
        }, 2000)
      }
    }
  }

  // 监听错误
  eventSource.onerror = (error) => {
    console.error('SSE 連線錯誤:', error)
    ElMessage.error('連線伺服器失敗，請稍後再試')
    closeSSEConnection()
    sseConnecting.value = false
  }
}

// 提交账号表单
const submitAccountForm = () => {
  accountFormRef.value.validate(async (valid) => {
    if (valid) {
      if (dialogType.value === 'add') {
        // 建立SSE连接
        connectSSE(accountForm.platform, accountForm.name)
      } else {
        // 編輯帳號逻辑
        try {
          const type = getLegacyPlatformType(accountForm.platform) || 1

          const res = await accountApi.updateAccount({
            id: accountForm.id,
            type,
            userName: accountForm.name
          })
          if (res.code === 200) {
            // 更新状态管理中的账号
            const updatedAccount = {
              id: accountForm.id,
              name: accountForm.name,
              platform: accountForm.platform,
              status: accountForm.status // Keep the existing status
            };
            accountStore.updateAccount(accountForm.id, updatedAccount)
            ElMessage.success('更新成功')
            dialogVisible.value = false
            // 刷新账号列表
            fetchAccounts()
          } else {
            ElMessage.error(res.msg || '更新帳號失敗')
          }
        } catch (error) {
          console.error('更新帳號失敗:', error)
          ElMessage.error('更新帳號失敗')
        }
      }
    } else {
      return false
    }
  })
}

// 组件卸载前关闭SSE连接
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
    
    h1 {
      font-size: 24px;
      color: $text-primary;
      margin: 0;
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
  
  // 列表/搜索/状态样式现在跟随 AccountTabPane.vue。

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
    
    .loading-wrapper, .success-wrapper, .error-wrapper {
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
