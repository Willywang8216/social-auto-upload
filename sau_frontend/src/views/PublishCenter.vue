<template>
  <div class="publish-center">
    <div class="page-header">
      <div>
        <h2>發佈工作台</h2>
        <p>以 Profile 為中心批次選擇素材、帳號與內容帳號，先審稿再決定立即執行、排程或固定頻率入列。</p>
      </div>
      <div class="header-actions">
        <el-button @click="loadBaseData" :loading="isLoadingBaseData">重新整理資料</el-button>
        <el-button type="primary" @click="generateDraftRows" :loading="isGenerating">產生審稿草稿</el-button>
      </div>
    </div>

    <el-alert
      v-if="legacyImportCount > 0"
      type="info"
      :closable="false"
      show-icon
      class="legacy-alert"
      :title="`已從舊版發佈 handoff 匯入 ${legacyImportCount} 筆草稿。這些草稿可直接編修後儲存或排程；沒有 materialId 的舊草稿暫不支援 LLM 重寫。`"
    />

    <div class="selection-grid">
      <el-card class="selection-card">
        <template #header>
          <div class="card-header">
            <span>1. 選擇 Profiles</span>
            <el-tag type="info">{{ selectedProfileIds.length }} 個</el-tag>
          </div>
        </template>
        <el-empty v-if="profiles.length === 0" description="尚未建立任何 Profile" />
        <el-checkbox-group v-else v-model="selectedProfileIds" class="checkbox-stack">
          <el-checkbox
            v-for="profile in profiles"
            :key="profile.id"
            :label="profile.id"
          >
            <div class="selection-item">
              <div class="selection-title">{{ profile.name }}</div>
              <div class="selection-meta">
                綁定帳號 {{ (profile.accountIds || []).length }} 個，
                內容帳號 {{ (profile.settings?.contentAccounts || []).length }} 個
              </div>
            </div>
          </el-checkbox>
        </el-checkbox-group>
      </el-card>

      <el-card class="selection-card">
        <template #header>
          <div class="card-header">
            <span>2. 選擇素材</span>
            <el-tag type="info">{{ selectionForm.materialIds.length }} 份</el-tag>
          </div>
        </template>
        <el-empty v-if="materials.length === 0" description="素材庫目前沒有資料" />
        <el-checkbox-group v-else v-model="selectionForm.materialIds" class="checkbox-stack">
          <el-checkbox
            v-for="material in materials"
            :key="material.id"
            :label="material.id"
          >
            <div class="selection-item">
              <div class="selection-title">{{ material.filename }}</div>
              <div class="selection-meta">
                {{ material.filesize }} MB · {{ material.upload_time }}
              </div>
            </div>
          </el-checkbox>
        </el-checkbox-group>
      </el-card>
    </div>

    <div v-if="selectedProfiles.length > 0" class="profile-configs">
      <el-card
        v-for="profile in selectedProfiles"
        :key="profile.id"
        class="profile-config-card"
      >
        <template #header>
          <div class="card-header">
            <span>{{ profile.name }}</span>
            <el-tag type="success">Profile 設定</el-tag>
          </div>
        </template>

        <div class="profile-config-body">
          <div class="config-block">
            <div class="config-title">受管帳號</div>
            <el-checkbox-group v-model="profileSelectionState[profile.id].selectedAccountIds" class="checkbox-stack">
              <el-checkbox
                v-for="account in getManagedAccounts(profile)"
                :key="account.id"
                :label="account.id"
                :disabled="!getManagedAccountSelectionState(account).selectable"
              >
                <div class="selection-item">
                  <div class="selection-title selection-title-inline">
                    <span>{{ account.name }} · {{ account.platform }}</span>
                    <el-tag size="small" :type="getValidationStatusTagType(account)">
                      {{ getManagedAccountSelectionState(account).label }}
                    </el-tag>
                  </div>
                  <div class="selection-meta">{{ getManagedAccountSelectionState(account).reason }}</div>
                </div>
              </el-checkbox>
            </el-checkbox-group>
            <el-empty
              v-if="getManagedAccounts(profile).length === 0"
              description="這個 Profile 目前沒有綁定受管帳號"
            />
          </div>

          <div class="config-block">
            <div class="config-title">內容帳號</div>
            <el-checkbox-group v-model="profileSelectionState[profile.id].selectedContentAccountIds" class="checkbox-stack">
              <el-checkbox
                v-for="account in getContentAccounts(profile)"
                :key="account.id"
                :label="account.id"
                :disabled="!getContentAccountSelectionState(profile, account).selectable"
              >
                <div class="selection-item">
                  <div class="selection-title">{{ account.name || account.platform }} · {{ account.platform }}</div>
                  <div class="selection-meta">
                    <span v-if="account.publishingAccountId">
                      綁定發佈帳號：{{ getPublishingAccountLabel(profile, account.publishingAccountId) }}
                    </span>
                    <span v-else-if="account.publisherTargetId">
                      Direct Target：{{ account.publisherTargetId }}
                    </span>
                    <span v-else>
                      尚未綁定發佈帳號
                    </span>
                  </div>
                  <div class="muted-text">{{ getContentAccountSelectionState(profile, account).reason }}</div>
                </div>
              </el-checkbox>
            </el-checkbox-group>
            <el-empty
              v-if="getContentAccounts(profile).length === 0"
              description="這個 Profile 目前沒有設定內容帳號"
            />
          </div>

          <div class="config-block">
            <div class="config-title">附加連結</div>
            <el-input
              v-model="profileSelectionState[profile.id].link"
              placeholder="可選：覆寫這個 Profile 的預設連結"
              clearable
            />
          </div>
        </div>
      </el-card>
    </div>

    <el-card v-if="reviewRows.length > 0" class="review-card">
      <template #header>
        <div class="card-header">
          <span>3. 審稿與調整</span>
          <div class="header-inline-actions">
            <el-tag type="info">{{ reviewRows.length }} 筆</el-tag>
            <el-button text @click="clearReviewRows">清空</el-button>
          </div>
        </div>
      </template>

      <div v-if="generationSummary" class="summary-banner">
        已為 {{ generationSummary.profiles || 0 }} 個 Profile 產生 {{ generationSummary.items || 0 }} 筆草稿。
      </div>

      <div class="review-list">
        <el-card
          v-for="row in reviewRows"
          :key="getRowKey(row)"
          class="review-item"
          shadow="never"
        >
          <div class="review-item-header">
            <div class="review-item-title">
              <span>{{ row.targetName || row.platformKey }}</span>
              <div class="review-tags">
                <el-tag size="small">{{ row.profileName || '未命名 Profile' }}</el-tag>
                <el-tag size="small" type="success">{{ row.platformKey }}</el-tag>
                <el-tag size="small" type="warning">{{ formatDeliveryMode(row.deliveryMode) }}</el-tag>
                <el-tag v-if="row.materialName" size="small" type="info">{{ row.materialName }}</el-tag>
                <el-tag v-if="getDriverPersonaLabel(row)" size="small" type="primary">
                  Persona：{{ getDriverPersonaLabel(row) }}
                </el-tag>
                <el-tag v-if="getDriverPublishingAccountLabel(row)" size="small" type="danger">
                  發佈帳號：{{ getDriverPublishingAccountLabel(row) }}
                </el-tag>
                <el-tag size="small" type="info">
                  {{ getDriverSourceLabel(row) }}
                </el-tag>
              </div>
            </div>
            <el-button text type="danger" @click="removeReviewRow(row)">移除</el-button>
          </div>

          <div class="review-item-body">
            <div class="media-block">
              <div class="media-label">媒體</div>
              <template v-if="row.mediaPublicUrl">
                <el-link :href="row.mediaPublicUrl" target="_blank" type="primary">
                  開啟已上傳媒體
                </el-link>
              </template>
              <template v-else-if="row.mediaPath">
                <span class="muted-text">{{ row.mediaPath }}</span>
              </template>
              <span v-else class="muted-text">未附帶媒體資訊</span>
            </div>

            <el-form label-position="top">
              <el-form-item label="標題">
                <el-input v-model="row.title" maxlength="120" show-word-limit />
              </el-form-item>

              <el-form-item label="文案">
                <el-input
                  v-model="row.message"
                  type="textarea"
                  :rows="5"
                  placeholder="在這裡直接調整生成內容"
                />
              </el-form-item>

              <el-form-item label="給 LLM 的補充指示">
                <div class="instruction-row">
                  <el-input
                    v-model="row.instructionText"
                    type="textarea"
                    :rows="2"
                    placeholder="例如：語氣更強烈、保留 CTA、改寫成更口語"
                  />
                  <el-button
                    type="primary"
                    plain
                    :loading="regeneratingRowKeys.includes(getRowKey(row))"
                    :disabled="!canRegenerateRow(row)"
                    @click="regenerateRow(row)"
                  >
                    重新生成
                  </el-button>
                </div>
                <div v-if="!canRegenerateRow(row)" class="helper-text">
                  這筆草稿缺少 Profile 或素材識別資訊，暫時只能手動編修。
                </div>
              </el-form-item>
            </el-form>
          </div>
        </el-card>
      </div>

      <el-divider />

      <div class="delivery-panel">
        <div class="delivery-panel-header">
          <div>
            <div class="panel-title">4. 決定交付方式</div>
            <div class="panel-description">直接執行、指定時間排程，或以固定頻率展開成一組 queue jobs。</div>
          </div>
          <el-radio-group v-model="deliveryMode">
            <el-radio-button label="now">立即執行</el-radio-button>
            <el-radio-button label="schedule">指定時間</el-radio-button>
            <el-radio-button label="queue">固定頻率 Queue</el-radio-button>
            <el-radio-button label="draft">儲存草稿</el-radio-button>
          </el-radio-group>
        </div>

        <div v-if="deliveryMode === 'schedule'" class="delivery-fields">
          <el-date-picker
            v-model="deliveryForm.scheduledAt"
            type="datetime"
            value-format="YYYY-MM-DDTHH:mm:ss"
            placeholder="選擇排程時間"
          />
        </div>

        <div v-else-if="deliveryMode === 'queue'" class="delivery-fields queue-fields">
          <el-date-picker
            v-model="deliveryForm.startAt"
            type="datetime"
            value-format="YYYY-MM-DDTHH:mm:ss"
            placeholder="第一筆開始時間"
          />
          <el-input-number v-model="deliveryForm.frequencyValue" :min="1" />
          <el-select v-model="deliveryForm.frequencyUnit" class="unit-select">
            <el-option label="分鐘" value="minutes" />
            <el-option label="小時" value="hours" />
            <el-option label="天" value="days" />
          </el-select>
          <el-input-number v-model="deliveryForm.repeatCount" :min="1" />
        </div>

        <div class="delivery-actions">
          <el-button
            type="primary"
            :loading="isSaving"
            @click="persistReviewRows"
          >
            {{ actionButtonLabel }}
          </el-button>
        </div>
      </div>
    </el-card>

    <el-card class="saved-jobs-card">
      <template #header>
        <div class="card-header">
          <span>最近儲存的任務</span>
          <div class="header-inline-actions">
            <el-tag v-if="currentBatchId" type="success">批次 {{ currentBatchId.slice(0, 8) }}</el-tag>
            <el-button text @click="refreshSavedJobs" :loading="isRefreshingSavedJobs">重新整理</el-button>
          </div>
        </div>
      </template>

      <el-empty v-if="savedJobs.length === 0" description="尚未建立任何 publish jobs" />
      <el-table v-else :data="savedJobs" size="small">
        <el-table-column prop="profileName" label="Profile" min-width="140" />
        <el-table-column prop="targetName" label="目標帳號" min-width="160" />
        <el-table-column prop="platformKey" label="平台" width="110" />
        <el-table-column prop="deliveryMode" label="交付方式" width="120">
          <template #default="{ row }">
            {{ formatDeliveryMode(row.deliveryMode) }}
          </template>
        </el-table-column>
        <el-table-column prop="status" label="狀態" width="120" />
        <el-table-column prop="scheduledAt" label="排程時間" min-width="180" />
        <el-table-column label="操作" min-width="220" fixed="right">
          <template #default="{ row }">
            <div class="saved-job-actions">
              <el-button link type="primary" @click="runSavedJobNow(row)" v-if="row.status !== 'published' && row.status !== 'exported'">
                立即執行
              </el-button>
              <el-button link type="danger" @click="cancelSavedJob(row)" v-if="row.status !== 'cancelled' && row.status !== 'manual_done'">
                取消
              </el-button>
              <el-button
                link
                type="success"
                @click="completeManualSavedJob(row)"
                v-if="row.deliveryMode === 'manual_only' && row.status !== 'manual_done'"
              >
                標記完成
              </el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { accountApi } from '@/api/account'
import { materialApi } from '@/api/material'
import { profileApi } from '@/api/profile'
import { publishApi } from '@/api/publish'
import { useAccountStore } from '@/stores/account'
import { useAppStore } from '@/stores/app'

const PUBLISH_HANDOFF_STORAGE_KEY = 'sau-publish-handoff-drafts'

const legacyPlatformMap = {
  1: { platformKey: 'xiaohongshu', deliveryMode: 'direct_upload' },
  2: { platformKey: 'channels', deliveryMode: 'direct_upload' },
  3: { platformKey: 'douyin', deliveryMode: 'direct_upload' },
  4: { platformKey: 'kuaishou', deliveryMode: 'direct_upload' }
}

const accountStore = useAccountStore()
const appStore = useAppStore()

const profiles = ref([])
const selectedProfileIds = ref([])
const selectionForm = reactive({
  materialIds: []
})
const profileSelectionState = reactive({})

const reviewRows = ref([])
const savedJobs = ref([])
const generationSummary = ref(null)
const currentBatchId = ref('')
const legacyImportCount = ref(0)

const deliveryMode = ref('now')
const deliveryForm = reactive({
  scheduledAt: '',
  startAt: '',
  frequencyValue: 6,
  frequencyUnit: 'hours',
  repeatCount: 3
})

const isLoadingBaseData = ref(false)
const isGenerating = ref(false)
const isSaving = ref(false)
const isRefreshingSavedJobs = ref(false)
const regeneratingRowKeys = ref([])

const materials = computed(() => appStore.materials || [])
const selectedProfiles = computed(() => (
  profiles.value.filter(profile => selectedProfileIds.value.includes(profile.id))
))
const managedDirectPublishPlatforms = new Set(['xiaohongshu', 'channels', 'douyin', 'kuaishou', 'twitter', 'reddit'])
const contentDirectTargetPlatforms = new Set(['telegram', 'discord'])
const contentManagedBindingPlatforms = new Set(['twitter', 'reddit'])

const actionButtonLabel = computed(() => {
  if (deliveryMode.value === 'schedule') {
    return '建立排程任務'
  }
  if (deliveryMode.value === 'queue') {
    return '建立固定頻率 Queue'
  }
  if (deliveryMode.value === 'draft') {
    return '儲存內部草稿'
  }
  return '立即建立並執行任務'
})

watch(selectedProfileIds, () => {
  syncProfileSelectionState()
}, { deep: true })

const loadBaseData = async () => {
  isLoadingBaseData.value = true
  try {
    const [profileResponse, accountResponse, materialResponse] = await Promise.all([
      profileApi.getProfiles(),
      accountApi.getAccounts(),
      materialApi.getAllMaterials()
    ])
    profiles.value = profileResponse.data || []
    accountStore.setAccounts(accountResponse.data || [])
    appStore.setMaterials(materialResponse.data || [])
    syncProfileSelectionState()
  } catch (error) {
    ElMessage.error(error.message || '載入資料失敗')
  } finally {
    isLoadingBaseData.value = false
  }
}

const syncProfileSelectionState = () => {
  const activeIds = new Set(selectedProfileIds.value)
  Object.keys(profileSelectionState).forEach((key) => {
    if (!activeIds.has(Number(key))) {
      delete profileSelectionState[key]
    }
  })

  selectedProfiles.value.forEach((profile) => {
    const selectableAccountIds = getSelectableManagedAccountIds(profile)
    const selectableContentAccountIds = getSelectableContentAccountIds(profile)
    if (!profileSelectionState[profile.id]) {
      profileSelectionState[profile.id] = {
        profileId: profile.id,
        selectedAccountIds: selectableAccountIds,
        selectedContentAccountIds: selectableContentAccountIds,
        link: profile.settings?.socialImport?.defaultLink || ''
      }
      return
    }

    profileSelectionState[profile.id].selectedAccountIds = (
      profileSelectionState[profile.id].selectedAccountIds || []
    ).filter(id => selectableAccountIds.includes(id))
    profileSelectionState[profile.id].selectedContentAccountIds = (
      profileSelectionState[profile.id].selectedContentAccountIds || []
    ).filter(id => selectableContentAccountIds.includes(id))
  })
}

const getManagedAccounts = (profile) => {
  const accountIds = new Set(profile.accountIds || [])
  return accountStore.accounts.filter(account => accountIds.has(account.id))
}

const getContentAccounts = (profile) => profile.settings?.contentAccounts || []

const getProfileById = (profileId) => profiles.value.find(item => item.id === Number(profileId))

const getContentAccountById = (profileId, contentAccountId) => {
  const profile = getProfileById(profileId)
  return getContentAccounts(profile || {}).find(item => item.id === contentAccountId)
}

const isAccountValidated = (account) => {
  if (!account?.supportsValidation) {
    return true
  }
  return account.status === '正常'
}

const getValidationStatusTagType = (account) => {
  if (account.status === '正常') {
    return 'success'
  }
  if (account.status === '驗證中') {
    return 'warning'
  }
  return 'danger'
}

const formatValidationTime = (value) => {
  const text = String(value || '').trim()
  if (!text) {
    return ''
  }
  return text.replace('T', ' ')
}

const getManagedAccountSelectionState = (account) => {
  if (!account) {
    return { selectable: false, label: '不可用', reason: '找不到帳號資料。' }
  }
  if (!managedDirectPublishPlatforms.has(account.platformKey)) {
    return { selectable: true, label: '可使用', reason: '這個平台可用於產稿、匯出或手動交付流程。' }
  }
  if (isAccountValidated(account)) {
    return {
      selectable: true,
      label: account.validationMessage || '已驗證',
      reason: account.lastValidatedAt ? `最後驗證：${formatValidationTime(account.lastValidatedAt)}` : '已通過驗證，可直接投遞。'
    }
  }
  return {
    selectable: false,
    label: account.validationMessage || '未驗證',
    reason: account.lastError || '這個 publishing account 尚未驗證通過，暫時不能在發佈工作台選取。'
  }
}

const getPublishingAccount = (profile, accountId) => (
  getManagedAccounts(profile).find(item => item.id === Number(accountId))
)

const getPublishingAccountLabel = (profile, accountId) => {
  const account = getPublishingAccount(profile, accountId)
  return account ? `${account.name}（${account.platform}）` : `帳號 ${accountId}`
}

const getContentAccountSelectionState = (profile, contentAccount) => {
  const platformKey = String(contentAccount?.platform || '').trim().toLowerCase()
  const publishingAccountId = Number(contentAccount?.publishingAccountId || 0)
  if (publishingAccountId) {
    const publishingAccount = getPublishingAccount(profile, publishingAccountId)
    if (!publishingAccount) {
      return { selectable: false, reason: '綁定的發佈帳號不在這個 Profile 內。' }
    }
    if (!isAccountValidated(publishingAccount)) {
      return { selectable: false, reason: `綁定帳號尚未驗證通過：${publishingAccount.name}` }
    }
    return { selectable: true, reason: `會沿用 ${publishingAccount.name} 的憑證做實際投遞。` }
  }

  if (contentDirectTargetPlatforms.has(platformKey)) {
    if (contentAccount?.publisherTargetId) {
      return { selectable: true, reason: '將透過 Direct Target 執行實際投遞。' }
    }
    return { selectable: false, reason: '這個平台需要先設定 Direct Target。' }
  }

  if (contentManagedBindingPlatforms.has(platformKey)) {
    if (contentAccount?.publisherTargetId) {
      return { selectable: true, reason: '將透過 Direct Target 執行實際投遞。' }
    }
    return { selectable: false, reason: '請先綁定已驗證的 publishing account，或指定 Direct Target。' }
  }

  return { selectable: true, reason: '未綁定發佈帳號時，將走 Google Sheet 或手動流程。' }
}

const getSelectableManagedAccountIds = (profile) => (
  getManagedAccounts(profile)
    .filter(account => getManagedAccountSelectionState(account).selectable)
    .map(account => account.id)
)

const getSelectableContentAccountIds = (profile) => (
  getContentAccounts(profile)
    .filter(account => getContentAccountSelectionState(profile, account).selectable)
    .map(account => account.id)
)

const getDriverPersonaLabel = (row) => {
  const metadata = row.metadata || {}
  const contentAccountId = row.contentAccountId || metadata.contentAccountId || ''
  if (!contentAccountId || !row.profileId) {
    return ''
  }
  const account = getContentAccountById(row.profileId, contentAccountId)
  return account?.name || account?.platform || contentAccountId
}

const getDriverPublishingAccountLabel = (row) => {
  const metadata = row.metadata || {}
  const publishingAccountId = metadata.publishingAccountId || row.accountId || ''
  if (!publishingAccountId || !row.profileId) {
    return ''
  }
  const profile = getProfileById(row.profileId)
  if (!profile) {
    return String(publishingAccountId)
  }
  return getPublishingAccountLabel(profile, publishingAccountId)
}

const getDriverSourceLabel = (row) => {
  const source = String((row.metadata || {}).source || '').trim()
  if (source === 'linked_content_account_generation') {
    return '綁定 Persona 驅動'
  }
  if (source === 'content_account_generation') {
    return 'Persona 直出'
  }
  if (source === 'managed_account_generation') {
    return '平台預設文案'
  }
  return '草稿來源未標記'
}

const getRowKey = (row) => row.id || row.tempKey

const canRegenerateRow = (row) => Boolean(row.profileId && row.materialId)

const formatDeliveryMode = (value) => {
  if (value === 'direct_upload') {
    return '直接上傳'
  }
  if (value === 'sheet_export') {
    return 'Google Sheet'
  }
  if (value === 'manual_only') {
    return '手動完成'
  }
  return value || '未設定'
}

const normalizeReviewRows = (items) => (
  (items || []).map((item, index) => ({
    ...item,
    tempKey: item.tempKey || `${Date.now()}-${index}-${Math.random().toString(36).slice(2, 8)}`,
    instructionText: item.instructionText || ''
  }))
)

const buildProfileSelectionsPayload = () => (
  selectedProfiles.value.map((profile) => ({
    profileId: profile.id,
    materialIds: [...selectionForm.materialIds],
    selectedAccountIds: [...(profileSelectionState[profile.id]?.selectedAccountIds || [])]
      .filter(id => getSelectableManagedAccountIds(profile).includes(id)),
    selectedContentAccountIds: [...(profileSelectionState[profile.id]?.selectedContentAccountIds || [])]
      .filter(id => getSelectableContentAccountIds(profile).includes(id)),
    link: profileSelectionState[profile.id]?.link || ''
  }))
)

const validateGenerationInput = () => {
  if (selectedProfileIds.value.length === 0) {
    ElMessage.warning('請至少選擇一個 Profile')
    return false
  }

  if (selectionForm.materialIds.length === 0) {
    ElMessage.warning('請至少選擇一份素材')
    return false
  }

  const hasTarget = buildProfileSelectionsPayload().some(item => (
    item.selectedAccountIds.length > 0 || item.selectedContentAccountIds.length > 0
  ))
  if (!hasTarget) {
    ElMessage.warning('請至少選擇一個受管帳號或內容帳號')
    return false
  }

  return true
}

const generateDraftRows = async () => {
  if (!validateGenerationInput()) {
    return
  }

  isGenerating.value = true
  try {
    const response = await publishApi.generateBatchDrafts({
      profileSelections: buildProfileSelectionsPayload()
    })
    reviewRows.value = normalizeReviewRows(response.data?.items || [])
    generationSummary.value = response.data?.summary || null
    currentBatchId.value = response.data?.batchId || ''
    if (reviewRows.value.length === 0) {
      ElMessage.warning('這次沒有產生任何可審稿的內容')
      return
    }
    ElMessage.success(`已產生 ${reviewRows.value.length} 筆審稿草稿`)
  } catch (error) {
    ElMessage.error(error.message || '產生審稿草稿失敗')
  } finally {
    isGenerating.value = false
  }
}

const serializeReviewRow = (row) => ({
  profileId: row.profileId || null,
  profileName: row.profileName || '',
  targetKind: row.targetKind || 'content_account',
  accountId: row.accountId || null,
  contentAccountId: row.contentAccountId || '',
  platformKey: row.platformKey || '',
  targetName: row.targetName || '',
  deliveryMode: row.deliveryMode || 'manual_only',
  materialId: row.materialId || null,
  materialName: row.materialName || '',
  mediaPath: row.mediaPath || '',
  mediaPublicUrl: row.mediaPublicUrl || '',
  title: row.title || '',
  message: row.message || '',
  hashtags: row.hashtags || [],
  metadata: row.metadata || {}
})

const regenerateRow = async (row) => {
  if (!canRegenerateRow(row)) {
    return
  }

  const rowKey = getRowKey(row)
  regeneratingRowKeys.value.push(rowKey)
  try {
    const response = await publishApi.regenerateJob(
      row.id
        ? {
            jobId: row.id,
            instructionText: row.instructionText
          }
        : {
            instructionText: row.instructionText,
            draft: serializeReviewRow(row)
          }
    )
    Object.assign(row, {
      ...response.data,
      tempKey: row.tempKey,
      instructionText: row.instructionText
    })
    ElMessage.success('文案已重新生成')
  } catch (error) {
    ElMessage.error(error.message || '重新生成失敗')
  } finally {
    regeneratingRowKeys.value = regeneratingRowKeys.value.filter(item => item !== rowKey)
  }
}

const removeReviewRow = (row) => {
  reviewRows.value = reviewRows.value.filter(item => getRowKey(item) !== getRowKey(row))
}

const clearReviewRows = () => {
  reviewRows.value = []
  generationSummary.value = null
}

const buildSavePayload = () => {
  const payload = {
    batchId: currentBatchId.value || undefined,
    mode: deliveryMode.value,
    items: reviewRows.value.map(serializeReviewRow)
  }

  if (deliveryMode.value === 'schedule') {
    payload.scheduledAt = deliveryForm.scheduledAt
  } else if (deliveryMode.value === 'queue') {
    payload.startAt = deliveryForm.startAt
    payload.repeatCount = deliveryForm.repeatCount
    payload.frequencyValue = deliveryForm.frequencyValue
    payload.frequencyUnit = deliveryForm.frequencyUnit
  }

  return payload
}

const validateDeliveryForm = () => {
  if (reviewRows.value.length === 0) {
    ElMessage.warning('請先產生或匯入至少一筆草稿')
    return false
  }
  if (deliveryMode.value === 'schedule' && !deliveryForm.scheduledAt) {
    ElMessage.warning('請選擇排程時間')
    return false
  }
  if (deliveryMode.value === 'queue' && !deliveryForm.startAt) {
    ElMessage.warning('請選擇第一筆開始時間')
    return false
  }
  return true
}

const persistReviewRows = async () => {
  if (!validateDeliveryForm()) {
    return
  }

  isSaving.value = true
  try {
    const response = await publishApi.saveJobs(buildSavePayload())
    currentBatchId.value = response.data?.batchId || currentBatchId.value

    if (deliveryMode.value === 'now') {
      for (const jobId of response.data?.jobIds || []) {
        await publishApi.runJobNow(jobId)
      }
    }

    await refreshSavedJobs()
    ElMessage.success(`已建立 ${response.data?.count || 0} 筆任務`)

    if (deliveryMode.value !== 'draft') {
      clearReviewRows()
    }
  } catch (error) {
    ElMessage.error(error.message || '儲存任務失敗')
  } finally {
    isSaving.value = false
  }
}

const refreshSavedJobs = async () => {
  isRefreshingSavedJobs.value = true
  try {
    const response = await publishApi.getJobs(currentBatchId.value ? { batchId: currentBatchId.value } : {})
    const items = response.data || []
    savedJobs.value = currentBatchId.value ? items : items.slice(0, 30)
  } catch (error) {
    ElMessage.error(error.message || '取得任務列表失敗')
  } finally {
    isRefreshingSavedJobs.value = false
  }
}

const runSavedJobNow = async (row) => {
  try {
    await publishApi.runJobNow(row.id)
    await refreshSavedJobs()
    ElMessage.success('任務已送出執行')
  } catch (error) {
    ElMessage.error(error.message || '立即執行失敗')
  }
}

const cancelSavedJob = async (row) => {
  try {
    await ElMessageBox.confirm('確定要取消這筆任務嗎？', '取消任務', {
      confirmButtonText: '確定',
      cancelButtonText: '取消',
      type: 'warning'
    })
    await publishApi.cancelJob(row.id)
    await refreshSavedJobs()
    ElMessage.success('任務已取消')
  } catch (error) {
    if (error !== 'cancel') {
      ElMessage.error(error.message || '取消任務失敗')
    }
  }
}

const completeManualSavedJob = async (row) => {
  try {
    await publishApi.completeManualJob(row.id)
    await refreshSavedJobs()
    ElMessage.success('已標記為手動完成')
  } catch (error) {
    ElMessage.error(error.message || '更新任務狀態失敗')
  }
}

const importLegacyHandoffDrafts = () => {
  const raw = localStorage.getItem(PUBLISH_HANDOFF_STORAGE_KEY)
  if (!raw) {
    return
  }

  let drafts = []
  try {
    drafts = JSON.parse(raw)
  } catch (error) {
    localStorage.removeItem(PUBLISH_HANDOFF_STORAGE_KEY)
    return
  }

  if (!Array.isArray(drafts) || drafts.length === 0) {
    localStorage.removeItem(PUBLISH_HANDOFF_STORAGE_KEY)
    return
  }

  const importedRows = []
  drafts.forEach((draft) => {
    const platform = legacyPlatformMap[draft.selectedPlatform] || { platformKey: 'manual', deliveryMode: 'manual_only' }
    const files = Array.isArray(draft.fileList) && draft.fileList.length > 0 ? draft.fileList : [{}]
    const accounts = Array.isArray(draft.selectedAccounts) && draft.selectedAccounts.length > 0 ? draft.selectedAccounts : [null]
    files.forEach((file, fileIndex) => {
      accounts.forEach((accountId, accountIndex) => {
        const account = accountStore.accounts.find(item => item.id === accountId)
        importedRows.push({
          tempKey: `legacy-${Date.now()}-${fileIndex}-${accountIndex}-${Math.random().toString(36).slice(2, 8)}`,
          profileId: null,
          profileName: '舊版 handoff',
          targetKind: 'managed_account',
          accountId,
          contentAccountId: '',
          platformKey: platform.platformKey,
          targetName: account?.name || `帳號 ${accountId || '未指定'}`,
          deliveryMode: platform.deliveryMode,
          materialId: null,
          materialName: file.name || draft.label || '未命名素材',
          mediaPath: file.path || '',
          mediaPublicUrl: file.url || '',
          title: draft.title || '',
          message: draft.description || '',
          hashtags: draft.selectedTopics || [],
          metadata: {
            mediaKind: String(file.type || '').includes('image') ? 'image' : 'video',
            legacyDraft: true
          },
          instructionText: ''
        })
      })
    })
  })

  if (importedRows.length > 0) {
    reviewRows.value = normalizeReviewRows(importedRows)
    legacyImportCount.value = importedRows.length
  }

  localStorage.removeItem(PUBLISH_HANDOFF_STORAGE_KEY)
}

onMounted(async () => {
  await loadBaseData()
  importLegacyHandoffDrafts()
  await refreshSavedJobs()
})
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

.publish-center {
  display: flex;
  flex-direction: column;
  gap: 20px;

  .page-header {
    display: flex;
    justify-content: space-between;
    gap: 20px;
    padding: 24px;
    background: #fff;
    border-radius: 8px;
    box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.08);

    h2 {
      margin: 0 0 8px;
      color: $text-primary;
    }

    p {
      margin: 0;
      color: $text-secondary;
      line-height: 1.6;
      max-width: 760px;
    }

    .header-actions {
      display: flex;
      align-items: flex-start;
      gap: 12px;
      flex-shrink: 0;
    }
  }

  .legacy-alert {
    margin-top: -4px;
  }

  .selection-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 20px;
  }

  .selection-card,
  .review-card,
  .saved-jobs-card,
  .profile-config-card {
    border-radius: 8px;
    box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.08);
  }

  .profile-configs {
    display: grid;
    gap: 20px;
  }

  .profile-config-body {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 20px;
  }

  .config-block {
    min-width: 0;
  }

  .config-title,
  .panel-title {
    font-size: 14px;
    font-weight: 600;
    color: $text-primary;
    margin-bottom: 12px;
  }

  .panel-description,
  .helper-text,
  .muted-text,
  .selection-meta {
    color: $text-secondary;
    font-size: 13px;
    line-height: 1.5;
  }

  .checkbox-stack {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .selection-item {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }

  .selection-title {
    font-size: 14px;
    color: $text-primary;
    word-break: break-word;
  }

  .selection-title-inline {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .card-header,
  .review-item-header,
  .delivery-panel-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
  }

  .header-inline-actions {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .summary-banner {
    padding: 12px 14px;
    background: #f5f7fa;
    border-radius: 6px;
    color: $text-regular;
    margin-bottom: 16px;
  }

  .review-list {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .review-item {
    border: 1px solid #ebeef5;
  }

  .review-item-title {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .review-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .review-item-body {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .media-block {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .media-label {
    font-size: 13px;
    font-weight: 600;
    color: $text-primary;
  }

  .instruction-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 12px;
    align-items: start;
  }

  .delivery-panel {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .delivery-fields {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    align-items: center;
  }

  .queue-fields {
    .unit-select {
      width: 140px;
    }
  }

  .delivery-actions {
    display: flex;
    justify-content: flex-end;
  }

  .saved-job-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 10px;
  }

  @media (max-width: 1100px) {
    .selection-grid,
    .profile-config-body {
      grid-template-columns: 1fr;
    }

    .page-header,
    .delivery-panel-header {
      flex-direction: column;
    }
  }

  @media (max-width: 768px) {
    .instruction-row {
      grid-template-columns: 1fr;
    }
  }
}
</style>
