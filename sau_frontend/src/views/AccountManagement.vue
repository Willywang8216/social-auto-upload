<template>
  <div class="account-management">
    <div class="page-header">
      <h1>帳號管理</h1>
    </div>
    
    <div class="account-tabs">
      <el-tabs v-model="activeTab" class="account-tabs-nav">
        <el-tab-pane label="全部" name="all">
          <div class="account-list-container">
            <div class="account-search">
              <el-input
                v-model="searchKeyword"
                placeholder="輸入名稱或帳號搜尋"
                prefix-icon="Search"
                clearable
                @clear="handleSearch"
                @input="handleSearch"
              />
              <div class="action-buttons">
                <el-button type="primary" @click="handleAddAccount">新增帳號</el-button>
                <el-button type="info" @click="fetchAccounts" :loading="false">
                  <el-icon :class="{ 'is-loading': appStore.isAccountRefreshing }"><Refresh /></el-icon>
                  <span v-if="appStore.isAccountRefreshing">重新整理中</span>
                </el-button>
              </div>
            </div>
            
            <div v-if="filteredAccounts.length > 0" class="account-list">
              <el-table :data="filteredAccounts" style="width: 100%">
                <el-table-column label="頭像" width="80">
                  <template #default="scope">
                    <el-avatar :src="getDefaultAvatar(scope.row.name)" :size="40" />
                  </template>
                </el-table-column>
                <el-table-column prop="name" label="名稱" width="180" />
                <el-table-column prop="platform" label="平台">
                  <template #default="scope">
                    <el-tag
                      :type="getPlatformTagType(scope.row.platform)"
                      effect="plain"
                    >
                      {{ scope.row.platform }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="status" label="狀態">
                  <template #default="scope">
                    <el-tag
                      :type="getStatusTagType(scope.row.status)"
                      effect="plain"
                      :class="{'clickable-status': isStatusClickable(scope.row.status)}"
                      @click="handleStatusClick(scope.row)"
                    >
                      <el-icon :class="scope.row.status === '驗證中' ? 'is-loading' : ''" v-if="scope.row.status === '驗證中'">
                        <Loading />
                      </el-icon>
                      {{ scope.row.status }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="操作">
                  <template #default="scope">
                    <el-button size="small" @click="handleEdit(scope.row)">編輯</el-button>
                    <el-button size="small" type="primary" :icon="Download" @click="handleDownloadCookie(scope.row)">下載 Cookie</el-button>
                    <el-button size="small" type="info" :icon="Upload" @click="handleUploadCookie(scope.row)">上傳 Cookie</el-button>
                    <el-button size="small" type="danger" @click="handleDelete(scope.row)">刪除</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>
            
            <div v-else class="empty-data">
              <el-empty description="目前沒有帳號資料" />
            </div>
          </div>
        </el-tab-pane>
        
        <el-tab-pane label="快手" name="kuaishou">
          <div class="account-list-container">
            <div class="account-search">
              <el-input
                v-model="searchKeyword"
                placeholder="輸入名稱或帳號搜尋"
                prefix-icon="Search"
                clearable
                @clear="handleSearch"
                @input="handleSearch"
              />
              <div class="action-buttons">
                <el-button type="primary" @click="handleAddAccount">新增帳號</el-button>
                <el-button type="info" @click="fetchAccounts" :loading="false">
                  <el-icon :class="{ 'is-loading': appStore.isAccountRefreshing }"><Refresh /></el-icon>
                  <span v-if="appStore.isAccountRefreshing">重新整理中</span>
                </el-button>
              </div>
            </div>
            
            <div v-if="filteredKuaishouAccounts.length > 0" class="account-list">
              <el-table :data="filteredKuaishouAccounts" style="width: 100%">
                <el-table-column label="頭像" width="80">
                  <template #default="scope">
                    <el-avatar :src="getDefaultAvatar(scope.row.name)" :size="40" />
                  </template>
                </el-table-column>
                <el-table-column prop="name" label="名稱" width="180" />
                <el-table-column prop="platform" label="平台">
                  <template #default="scope">
                    <el-tag
                      :type="getPlatformTagType(scope.row.platform)"
                      effect="plain"
                    >
                      {{ scope.row.platform }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="status" label="狀態">
                  <template #default="scope">
                    <el-tag
                      :type="getStatusTagType(scope.row.status)"
                      effect="plain"
                      :class="{'clickable-status': isStatusClickable(scope.row.status)}"
                      @click="handleStatusClick(scope.row)"
                    >
                      <el-icon :class="scope.row.status === '驗證中' ? 'is-loading' : ''" v-if="scope.row.status === '驗證中'">
                        <Loading />
                      </el-icon>
                      {{ scope.row.status }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="操作">
                  <template #default="scope">
                    <el-button size="small" @click="handleEdit(scope.row)">編輯</el-button>
                    <el-button size="small" type="primary" :icon="Download" @click="handleDownloadCookie(scope.row)">下載 Cookie</el-button>
                    <el-button size="small" type="info" :icon="Upload" @click="handleUploadCookie(scope.row)">上傳 Cookie</el-button>
                    <el-button size="small" type="danger" @click="handleDelete(scope.row)">刪除</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>
            
            <div v-else class="empty-data">
              <el-empty description="目前沒有快手帳號資料" />
            </div>
          </div>
        </el-tab-pane>
        
        <el-tab-pane label="抖音" name="douyin">
          <div class="account-list-container">
            <div class="account-search">
              <el-input
                v-model="searchKeyword"
                placeholder="輸入名稱或帳號搜尋"
                prefix-icon="Search"
                clearable
                @clear="handleSearch"
                @input="handleSearch"
              />
              <div class="action-buttons">
                <el-button type="primary" @click="handleAddAccount">新增帳號</el-button>
                <el-button type="info" @click="fetchAccounts" :loading="false">
                  <el-icon :class="{ 'is-loading': appStore.isAccountRefreshing }"><Refresh /></el-icon>
                  <span v-if="appStore.isAccountRefreshing">重新整理中</span>
                </el-button>
              </div>
            </div>
            
            <div v-if="filteredDouyinAccounts.length > 0" class="account-list">
              <el-table :data="filteredDouyinAccounts" style="width: 100%">
                <el-table-column label="頭像" width="80">
                  <template #default="scope">
                    <el-avatar :src="getDefaultAvatar(scope.row.name)" :size="40" />
                  </template>
                </el-table-column>
                <el-table-column prop="name" label="名稱" width="180" />
                <el-table-column prop="platform" label="平台">
                  <template #default="scope">
                    <el-tag
                      :type="getPlatformTagType(scope.row.platform)"
                      effect="plain"
                    >
                      {{ scope.row.platform }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="status" label="狀態">
                  <template #default="scope">
                    <el-tag
                      :type="getStatusTagType(scope.row.status)"
                      effect="plain"
                      :class="{'clickable-status': isStatusClickable(scope.row.status)}"
                      @click="handleStatusClick(scope.row)"
                    >
                      <el-icon :class="scope.row.status === '驗證中' ? 'is-loading' : ''" v-if="scope.row.status === '驗證中'">
                        <Loading />
                      </el-icon>
                      {{ scope.row.status }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="操作">
                  <template #default="scope">
                    <el-button size="small" @click="handleEdit(scope.row)">編輯</el-button>
                    <el-button size="small" type="primary" :icon="Download" @click="handleDownloadCookie(scope.row)">下載 Cookie</el-button>
                    <el-button size="small" type="info" :icon="Upload" @click="handleUploadCookie(scope.row)">上傳 Cookie</el-button>
                    <el-button size="small" type="danger" @click="handleDelete(scope.row)">刪除</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>
            
            <div v-else class="empty-data">
              <el-empty description="目前沒有抖音帳號資料" />
            </div>
          </div>
        </el-tab-pane>
        
        <el-tab-pane label="影片號" name="channels">
          <div class="account-list-container">
            <div class="account-search">
              <el-input
                v-model="searchKeyword"
                placeholder="輸入名稱或帳號搜尋"
                prefix-icon="Search"
                clearable
                @clear="handleSearch"
                @input="handleSearch"
              />
              <div class="action-buttons">
                <el-button type="primary" @click="handleAddAccount">新增帳號</el-button>
                <el-button type="info" @click="fetchAccounts" :loading="false">
                  <el-icon :class="{ 'is-loading': appStore.isAccountRefreshing }"><Refresh /></el-icon>
                  <span v-if="appStore.isAccountRefreshing">重新整理中</span>
                </el-button>
              </div>
            </div>
            
            <div v-if="filteredChannelsAccounts.length > 0" class="account-list">
              <el-table :data="filteredChannelsAccounts" style="width: 100%">
                <el-table-column label="頭像" width="80">
                  <template #default="scope">
                    <el-avatar :src="getDefaultAvatar(scope.row.name)" :size="40" />
                  </template>
                </el-table-column>
                <el-table-column prop="name" label="名稱" width="180" />
                <el-table-column prop="platform" label="平台">
                  <template #default="scope">
                    <el-tag
                      :type="getPlatformTagType(scope.row.platform)"
                      effect="plain"
                    >
                      {{ scope.row.platform }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="status" label="狀態">
                  <template #default="scope">
                    <el-tag
                      :type="getStatusTagType(scope.row.status)"
                      effect="plain"
                      :class="{'clickable-status': isStatusClickable(scope.row.status)}"
                      @click="handleStatusClick(scope.row)"
                    >
                      <el-icon :class="scope.row.status === '驗證中' ? 'is-loading' : ''" v-if="scope.row.status === '驗證中'">
                        <Loading />
                      </el-icon>
                      {{ scope.row.status }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="操作">
                  <template #default="scope">
                    <el-button size="small" @click="handleEdit(scope.row)">編輯</el-button>
                    <el-button size="small" type="primary" :icon="Download" @click="handleDownloadCookie(scope.row)">下載 Cookie</el-button>
                    <el-button size="small" type="info" :icon="Upload" @click="handleUploadCookie(scope.row)">上傳 Cookie</el-button>
                    <el-button size="small" type="danger" @click="handleDelete(scope.row)">刪除</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>
            
            <div v-else class="empty-data">
              <el-empty description="目前沒有影片號帳號資料" />
            </div>
          </div>
        </el-tab-pane>
        
        <el-tab-pane label="小紅書" name="xiaohongshu">
          <div class="account-list-container">
            <div class="account-search">
              <el-input
                v-model="searchKeyword"
                placeholder="輸入名稱或帳號搜尋"
                prefix-icon="Search"
                clearable
                @clear="handleSearch"
                @input="handleSearch"
              />
              <div class="action-buttons">
                <el-button type="primary" @click="handleAddAccount">新增帳號</el-button>
                <el-button type="info" @click="fetchAccounts" :loading="false">
                  <el-icon :class="{ 'is-loading': appStore.isAccountRefreshing }"><Refresh /></el-icon>
                  <span v-if="appStore.isAccountRefreshing">重新整理中</span>
                </el-button>
              </div>
            </div>
            
            <div v-if="filteredXiaohongshuAccounts.length > 0" class="account-list">
              <el-table :data="filteredXiaohongshuAccounts" style="width: 100%">
                <el-table-column label="頭像" width="80">
                  <template #default="scope">
                    <el-avatar :src="getDefaultAvatar(scope.row.name)" :size="40" />
                  </template>
                </el-table-column>
                <el-table-column prop="name" label="名稱" width="180" />
                <el-table-column prop="platform" label="平台">
                  <template #default="scope">
                    <el-tag
                      :type="getPlatformTagType(scope.row.platform)"
                      effect="plain"
                    >
                      {{ scope.row.platform }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="status" label="狀態">
                  <template #default="scope">
                    <el-tag
                      :type="getStatusTagType(scope.row.status)"
                      effect="plain"
                      :class="{'clickable-status': isStatusClickable(scope.row.status)}"
                      @click="handleStatusClick(scope.row)"
                    >
                      <el-icon :class="scope.row.status === '驗證中' ? 'is-loading' : ''" v-if="scope.row.status === '驗證中'">
                        <Loading />
                      </el-icon>
                      {{ scope.row.status }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="操作">
                  <template #default="scope">
                    <el-button size="small" @click="handleEdit(scope.row)">編輯</el-button>
                    <el-button size="small" type="primary" :icon="Download" @click="handleDownloadCookie(scope.row)">下載 Cookie</el-button>
                    <el-button size="small" type="info" :icon="Upload" @click="handleUploadCookie(scope.row)">上傳 Cookie</el-button>
                    <el-button size="small" type="danger" @click="handleDelete(scope.row)">刪除</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>
            
            <div v-else class="empty-data">
              <el-empty description="目前沒有小紅書帳號資料" />
            </div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </div>
    
    <!-- 新增/編輯帳號对话框 -->
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
            <el-option label="快手" value="快手" />
            <el-option label="抖音" value="抖音" />
            <el-option label="影片號" value="影片號" />
            <el-option label="小紅書" value="小紅書" />
          </el-select>
        </el-form-item>
        <el-form-item label="名稱" prop="name">
          <el-input 
            v-model="accountForm.name" 
            placeholder="請輸入帳號名稱" 
            :disabled="sseConnecting"
          />
        </el-form-item>
        
        <!-- QR Code 顯示區域 -->
        <div v-if="sseConnecting" class="qrcode-container">
          <div v-if="qrCodeData && !loginStatus" class="qrcode-wrapper">
            <p class="qrcode-tip">請使用對應平台 App 掃描 QR Code 登入</p>
            <img :src="qrCodeData" alt="登入 QR Code" class="qrcode-image" />
          </div>
          <div v-else-if="!qrCodeData && !loginStatus" class="loading-wrapper">
            <el-icon class="is-loading"><Refresh /></el-icon>
            <span>請求中...</span>
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
            {{ sseConnecting ? '请求中' : '确认' }}
          </el-button>
        </span>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'
import { Refresh, CircleCheckFilled, CircleCloseFilled, Download, Upload, Loading } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { accountApi } from '@/api/account'
import { useAccountStore } from '@/stores/account'
import { useAppStore } from '@/stores/app'
import { http } from '@/utils/request'
import { buildApiUrl } from '@/utils/apiBase'

// 获取帳號状态管理
const accountStore = useAccountStore()
// 取得應用狀態管理
const appStore = useAppStore()

// 目前啟用的分頁
const activeTab = ref('all')

// 搜尋關鍵字
const searchKeyword = ref('')

// 取得帳號資料（快速，不驗證）
const fetchAccountsQuick = async () => {
  try {
    const res = await accountApi.getAccounts()
    if (res.code === 200 && res.data) {
      // 將所有帳號的狀態暫時設為「驗證中」
      const accountsWithPendingStatus = res.data.map(account => {
        const updatedAccount = [...account];
        updatedAccount[4] = -1; // -1 表示驗證中的暫時狀態
        return updatedAccount;
      });
      accountStore.setAccounts(accountsWithPendingStatus);
    }
  } catch (error) {
    console.error('快速取得帳號資料失敗:', error)
  }
}

// 取得帳號資料（含驗證）
const fetchAccounts = async () => {
  if (appStore.isAccountRefreshing) return

  appStore.setAccountRefreshing(true)

  try {
    const res = await accountApi.getValidAccounts()
    if (res.code === 200 && res.data) {
      accountStore.setAccounts(res.data)
      ElMessage.success('帳號資料取得成功')
      // 標記為已造訪
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

// 背景驗證所有帳號（優化版本，使用 setTimeout 避免阻塞 UI）
const validateAllAccountsInBackground = async () => {
  // 使用 setTimeout 將驗證流程放到下一個事件迴圈，避免阻塞 UI
  setTimeout(async () => {
    try {
      const res = await accountApi.getValidAccounts()
      if (res.code === 200 && res.data) {
        accountStore.setAccounts(res.data)
      }
    } catch (error) {
      console.error('背景驗證帳號失敗:', error)
    }
  }, 0)
}

// 頁面載入時取得帳號資料
onMounted(() => {
  // 快速取得帳號列表（不驗證），立即顯示
  fetchAccountsQuick()

  // 在背景驗證所有帳號
  setTimeout(() => {
    validateAllAccountsInBackground()
  }, 100) // 稍微延遲一下，讓使用者看到快速載入效果
})

// 获取平台标签类型
const getPlatformTagType = (platform) => {
  const typeMap = {
    '快手': 'success',
    '抖音': 'danger',
    '影片號': 'warning',
    '小紅書': 'info'
  }
  return typeMap[platform] || 'info'
}

// 判斷狀態是否可點擊（異常狀態可點擊）
const isStatusClickable = (status) => {
  return status === '異常'; // 只有異常狀態可點擊，驗證中不可點擊
}

// 取得狀態標籤類型
const getStatusTagType = (status) => {
  if (status === '驗證中') {
    return 'info'; // 驗證中使用灰色
  } else if (status === '正常') {
    return 'success'; // 正常使用綠色
  } else {
    return 'danger'; // 無效使用紅色
  }
}

// 處理狀態點擊事件
const handleStatusClick = (row) => {
  if (isStatusClickable(row.status)) {
    // 觸發重新登入流程
    handleReLogin(row)
  }
}

// 篩選後的帳號列表
const filteredAccounts = computed(() => {
  if (!searchKeyword.value) return accountStore.accounts
  return accountStore.accounts.filter(account =>
    account.name.includes(searchKeyword.value)
  )
})

// 依平台篩選的帳號列表
const filteredKuaishouAccounts = computed(() => {
  return filteredAccounts.value.filter(account => account.platform === '快手')
})

const filteredDouyinAccounts = computed(() => {
  return filteredAccounts.value.filter(account => account.platform === '抖音')
})

const filteredChannelsAccounts = computed(() => {
  return filteredAccounts.value.filter(account => account.platform === '影片號')
})

const filteredXiaohongshuAccounts = computed(() => {
  return filteredAccounts.value.filter(account => account.platform === '小紅書')
})

// 搜尋處理
const handleSearch = () => {
  // 搜尋邏輯已透過計算屬性實作
}

// 對話框相關
const dialogVisible = ref(false)
const dialogType = ref('add') // 'add' 或 'edit'
const accountFormRef = ref(null)

// 帳號表單
const accountForm = reactive({
  id: null,
  name: '',
  platform: '',
  status: '正常'
})

// 表單驗證規則
const rules = {
  platform: [{ required: true, message: '請選擇平台', trigger: 'change' }],
  name: [{ required: true, message: '請輸入帳號名稱', trigger: 'blur' }]
}

// SSE 連線狀態
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

// 刪除帳號
const handleDelete = (row) => {
  ElMessageBox.confirm(
    `确定要刪除帳號 ${row.name} 吗？`,
    '警告',
    {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning',
    }
  )
    .then(async () => {
      try {
        // 调用API刪除帳號
        const response = await accountApi.deleteAccount(row.id)

        if (response.code === 200) {
          // 从状态管理中刪除帳號
          accountStore.deleteAccount(row.id)
          ElMessage({
            type: 'success',
            message: '刪除成功',
          })
        } else {
          ElMessage.error(response.msg || '刪除失败')
        }
      } catch (error) {
        console.error('刪除帳號失败:', error)
        ElMessage.error('刪除帳號失败')
      }
    })
    .catch(() => {
      // 取消刪除
    })
}

// 下載 Cookie文件
const handleDownloadCookie = (row) => {
  // 从后端获取Cookie文件
  const downloadUrl = buildApiUrl(`/downloadCookie?filePath=${encodeURIComponent(row.filePath)}`)

  // 创建一个隐藏的链接来触发下载
  const link = document.createElement('a')
  link.href = downloadUrl
  link.download = `${row.name}_cookie.json`
  link.target = '_blank'
  link.style.display = 'none'
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
}

// 上傳 Cookie文件
const handleUploadCookie = (row) => {
  // 创建一个隐藏的文件輸入框
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
      ElMessage.error('请选择JSON格式的Cookie文件')
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

      ElMessage.success('Cookie文件上传成功')
      // 刷新帳號列表以显示更新
      fetchAccounts()
    } catch (error) {
      ElMessage.error('Cookie 檔案上傳失敗')
    } finally {
      document.body.removeChild(input)
    }
  }

  input.click()
}

// 重新登入帳號
const handleReLogin = (row) => {
  // 設定表單資訊
  dialogType.value = 'edit'
  Object.assign(accountForm, {
    id: row.id,
    name: row.name,
    platform: row.platform,
    status: row.status
  })

  // 重置 SSE 狀態
  sseConnecting.value = false
  qrCodeData.value = ''
  loginStatus.value = ''

  // 顯示對話框
  dialogVisible.value = true

  // 立即開始登入流程
  setTimeout(() => {
    connectSSE(row.platform, row.name)
  }, 300)
}

// 取得預設頭像
const getDefaultAvatar = (name) => {
  // 使用簡單的預設頭像，可依使用者名稱產生不同顏色
  return `https://ui-avatars.com/api/?name=${encodeURIComponent(name)}&background=random`
}

// SSE 事件來源物件
let eventSource = null

// 關閉 SSE 連線
const closeSSEConnection = () => {
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }
}

// 建立 SSE 連線
const connectSSE = (platform, name) => {
  // 關閉可能存在的連線
  closeSSEConnection()

  // 設定連線狀態
  sseConnecting.value = true
  qrCodeData.value = ''
  loginStatus.value = ''

  // 取得平台類型編號
  const platformTypeMap = {
    '小紅書': '1',
    '影片號': '2',
    '抖音': '3',
    '快手': '4'
  }

  const type = platformTypeMap[platform] || '1'

  // 建立 SSE 連線
  const url = buildApiUrl(`/login?type=${type}&id=${encodeURIComponent(name)}`)

  eventSource = new EventSource(url)

  // 監聽訊息
  eventSource.onmessage = (event) => {
    const data = event.data

    // 如果還沒有 QR Code 資料，且資料長度較長，視為 QR Code
    if (!qrCodeData.value && data.length > 100) {
      try {
        if (data.startsWith('data:image')) {
          qrCodeData.value = data
        } else {
          qrCodeData.value = `data:image/png;base64,${data}`
        }
      } catch (error) {
        // 處理 QR Code 資料出錯
      }
    }
    // 如果收到狀態碼
    else if (data === '200' || data === '500') {
      loginStatus.value = data

      // 如果登入成功
      if (data === '200') {
        setTimeout(() => {
          // 關閉連線
          closeSSEConnection()

          // 1 秒後關閉對話框並開始刷新
          setTimeout(() => {
            dialogVisible.value = false
            sseConnecting.value = false

            // 依是否為重新登入顯示不同提示
            ElMessage.success(dialogType.value === 'edit' ? '重新登入成功' : '帳號新增成功')

            // 顯示更新帳號資訊提示
            ElMessage({
              type: 'info',
              message: '正在同步帳號資訊...',
              duration: 0
            })

            // 觸發刷新動作
            fetchAccounts().then(() => {
              // 刷新完成後關閉提示
              ElMessage.closeAll()
              ElMessage.success('帳號資訊已更新')
            })
          }, 1000)
        }, 1000)
      } else {
        // 登入失敗，關閉連線
        closeSSEConnection()

        // 2 秒後重置狀態，允許重試
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
    console.error('SSE连接错误:', error)
    ElMessage.error('连接服务器失败，请稍后再试')
    closeSSEConnection()
    sseConnecting.value = false
  }
}

// 提交帳號表单
const submitAccountForm = () => {
  accountFormRef.value.validate(async (valid) => {
    if (valid) {
      if (dialogType.value === 'add') {
        // 建立SSE连接
        connectSSE(accountForm.platform, accountForm.name)
      } else {
        // 編輯帳號逻辑
        try {
          // 将平台名称转换为类型数字
          const platformTypeMap = {
            '小紅書': 1,
            '影片號': 2,
            '抖音': 3,
            '快手': 4
          };
          const type = platformTypeMap[accountForm.platform] || 1;

          const res = await accountApi.updateAccount({
            id: accountForm.id,
            type: type,
            userName: accountForm.name
          })
          if (res.code === 200) {
            // 更新状态管理中的帳號
            const updatedAccount = {
              id: accountForm.id,
              name: accountForm.name,
              platform: accountForm.platform,
              status: accountForm.status // Keep the existing status
            };
            accountStore.updateAccount(accountForm.id, updatedAccount)
            ElMessage.success('更新成功')
            dialogVisible.value = false
            // 刷新帳號列表
            fetchAccounts()
          } else {
            ElMessage.error(res.msg || '更新帳號失败')
          }
        } catch (error) {
          console.error('更新帳號失败:', error)
          ElMessage.error('更新帳號失败')
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
  
  .account-list-container {
    .account-search {
      display: flex;
      justify-content: space-between;
      margin-bottom: 20px;
      
      .el-input {
        width: 300px;
      }
      
      .action-buttons {
        display: flex;
        gap: 10px;
        
        .el-icon.is-loading {
          animation: rotate 1s linear infinite;
        }
      }
    }
    
    .account-list {
      margin-bottom: 20px;
    }
    
    .empty-data {
      padding: 40px 0;
    }
  }
  
  // 二维码容器样式
  .clickable-status {
    cursor: pointer;
    transition: all 0.3s;

    &:hover {
      transform: scale(1.05);
      box-shadow: 0 0 8px rgba(0, 0, 0, 0.15);
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
