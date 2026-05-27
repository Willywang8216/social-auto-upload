<template>
  <div class="publish-center">
    <!-- Header: template controls -->
    <el-card class="pc-card pc-card--header" shadow="never">
      <div class="pc-header-row">
        <div class="pc-header-left">
          <el-select
            v-model="selectedTemplateId"
            placeholder="載入範本"
            clearable
            filterable
            style="width: 280px"
            @change="onTemplateSelected"
          >
            <el-option
              v-for="template in templatesStore.templates"
              :key="template.id"
              :label="template.name"
              :value="template.id"
            />
          </el-select>
          <el-button type="primary" plain @click="showSaveTemplateDialog">
            另存為範本
          </el-button>
          <el-button @click="resetForm">重置</el-button>
          <router-link to="/template-management" class="pc-link">範本管理</router-link>
        </div>
        <div class="pc-header-right">
          <el-tag v-if="loadedTemplateName" type="info">已套用：{{ loadedTemplateName }}</el-tag>
        </div>
      </div>
    </el-card>

    <!-- 1. Media -->
    <el-card class="pc-card" shadow="never">
      <template #header>
        <div class="pc-section-header">
          <h3>1. 媒體素材</h3>
          <span class="pc-subtle">可上傳一個或多個圖片 / 影片</span>
        </div>
      </template>
      <el-upload
        drag
        multiple
        :auto-upload="true"
        :action="uploadAction"
        :headers="authHeaders"
        :show-file-list="false"
        :on-success="onUploadSuccess"
        :on-error="onUploadError"
        accept="image/*,video/*"
      >
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">拖曳檔案到此或<em>點擊上傳</em></div>
        <template #tip>
          <div class="el-upload__tip">支援多檔上傳。單媒體平台 (Tencent / TikTok / YouTube …) 若上傳多個檔案，會自動拆成多則貼文、每 {{ STAGGER_MINUTES }} 分鐘間隔發佈。</div>
        </template>
      </el-upload>
      <el-button class="pc-mb-8" link @click="openMaterialLibrary">從素材庫挑選</el-button>
      <div v-if="mediaFiles.length > 0" class="pc-media-list">
        <div v-for="(file, idx) in mediaFiles" :key="file.path + idx" class="pc-media-item">
          <el-tag :type="isVideo(file.path) ? 'success' : 'primary'">{{ isVideo(file.path) ? '影片' : '圖片' }}</el-tag>
          <span class="pc-media-name">{{ file.name }}</span>
          <el-button text type="danger" @click="removeMedia(idx)">移除</el-button>
        </div>
      </div>
    </el-card>

    <!-- 2. Profiles -->
    <el-card class="pc-card" shadow="never">
      <template #header>
        <div class="pc-section-header">
          <h3>2. 目標 Profile</h3>
          <span class="pc-subtle">可複選；每個 profile 會用各自的 AI 設定與系統提示</span>
        </div>
      </template>
      <el-checkbox-group v-model="selectedProfileIds" @change="onProfileSelectionChanged">
        <el-checkbox
          v-for="profile in profilesStore.profiles"
          :key="profile.id"
          :label="profile.id"
        >
          {{ profile.name }}
        </el-checkbox>
      </el-checkbox-group>
      <div v-if="selectedProfileIds.length === 0" class="pc-help">尚未選擇任何 profile</div>
      <div v-for="profileId in selectedProfileIds" :key="`accts-${profileId}`" class="pc-profile-accounts">
        <div class="pc-profile-title">{{ findProfile(profileId)?.name }} 的帳號：</div>
        <el-checkbox-group v-model="selectedAccountIds">
          <el-checkbox
            v-for="account in accountsForProfile(profileId)"
            :key="account.id"
            :label="account.id"
          >
            {{ account.account_name }} ({{ account.platform }})
          </el-checkbox>
        </el-checkbox-group>
        <span v-if="accountsForProfile(profileId).length === 0" class="pc-help">尚未取得此 profile 的帳號</span>
      </div>
    </el-card>

    <!-- 3. Processing options -->
    <el-card class="pc-card" shadow="never">
      <template #header>
        <div class="pc-section-header">
          <h3>3. 處理選項</h3>
        </div>
      </template>
      <div class="pc-options-grid">
        <el-checkbox v-model="options.watermark">套用浮水印（圖片與影片）</el-checkbox>
        <el-checkbox v-model="options.intro">加入片頭（影片）</el-checkbox>
        <el-checkbox v-model="options.outro">加入片尾（影片）</el-checkbox>
        <el-checkbox v-model="options.linkInFirstComment">連結放第一則留言（支援的平台）</el-checkbox>
        <el-checkbox v-if="hasTiktokSelected && !isTiktokSandbox" v-model="options.tiktokDirectPost">
          直接發佈到 TikTok（跳過草稿）
        </el-checkbox>
        <el-alert
          v-if="isTiktokSandbox"
          title="TikTok 開發模式：影片將直接發佈到個人檔案（草稿功能需通過 App 審核後才能使用）"
          type="info"
          show-icon
          :closable="false"
          style="margin-top: 8px;"
        />
        <el-alert
          v-if="hasTiktokSelected && options.watermark"
          type="warning"
          show-icon
          :closable="false"
          style="margin-top: 8px;"
        >
          TikTok 不允許促銷浮水印，發佈到 TikTok 的內容將不會套用浮水印。
          <a href="https://developers.tiktok.com/doc/content-sharing-guidelines" target="_blank" rel="noopener" style="color: #409eff; margin-left: 4px;">
            查看規範
          </a>
        </el-alert>
      </div>
      <el-divider />
      <div class="pc-screenshots">
        <el-checkbox v-model="options.screenshots.enabled">從影片擷取截圖</el-checkbox>
        <div v-if="options.screenshots.enabled" class="pc-screenshots-row">
          <span>張數：</span>
          <el-input-number v-model="options.screenshots.count" :min="1" :max="20" />
          <span class="pc-subtle">指定時間（逗號分隔 HH:MM:SS，留空為隨機）：</span>
          <el-input
            v-model="options.screenshots.timestampsRaw"
            placeholder="如 00:05, 00:30, 01:15"
            style="width: 280px"
          />
        </div>
        <div class="pc-subtle pc-mt-4">
          檔名格式：<code>原始檔名_YYYYMMDD-HHMMSS_screenshotN.jpg</code>
        </div>
      </div>
    </el-card>

    <!-- 4. Brief -->
    <el-card class="pc-card" shadow="never">
      <template #header>
        <div class="pc-section-header">
          <h3>4. 貼文簡介</h3>
          <span class="pc-subtle">告訴 AI 這篇貼文要傳達什麼</span>
        </div>
      </template>
      <el-input
        v-model="brief"
        type="textarea"
        :rows="4"
        placeholder="例：宣傳新品發佈活動，強調限定折扣與時間，鼓勵留言互動"
      />
      <div class="pc-actions">
        <el-button
          type="primary"
          :loading="generating"
          :disabled="!canGeneratePreviews"
          @click="generatePreviews"
        >
          生成各帳號草稿
        </el-button>
        <span v-if="!canGeneratePreviews" class="pc-help">請先選擇至少一個 profile 與帳號</span>
      </div>
    </el-card>

    <!-- 5. Per-profile drafts -->
    <el-card v-if="previews.length > 0" class="pc-card" shadow="never">
      <template #header>
        <div class="pc-section-header">
          <h3>5. 各帳號草稿</h3>
          <span class="pc-subtle">可逐一編輯，或點選「重新生成」要求 AI 改寫</span>
        </div>
      </template>
      <el-collapse v-model="expandedProfiles">
        <el-collapse-item
          v-for="profile in previews"
          :key="profile.profileId"
          :name="profile.profileId"
        >
          <template #title>
            <strong>{{ profile.profileName }}</strong>
            <span class="pc-subtle pc-ml-8">{{ profile.accounts.length }} 個帳號</span>
          </template>
          <div v-for="account in profile.accounts" :key="account.accountId" class="pc-account-draft">
            <div class="pc-account-head">
              <el-tag :type="account.supportsMultiMedia ? 'success' : 'warning'">{{ account.platform }}</el-tag>
              <span class="pc-account-name">{{ account.accountName }}</span>
              <span class="pc-subtle">
                {{ countChars(account.draft.message) }}
                <template v-if="account.maxChars">/ {{ account.maxChars }}</template>
              </span>
              <el-button
                size="small"
                :loading="regeneratingKey === draftKey(profile.profileId, account.accountId)"
                @click="regenerateAccount(profile, account)"
              >
                重新生成
              </el-button>
              <el-tag v-if="!account.supportsMultiMedia && mediaFiles.length > 1" type="info" size="small">
                {{ mediaFiles.length }} 則貼文，每 {{ STAGGER_MINUTES }} 分鐘
              </el-tag>
            </div>
            <el-input
              v-if="isBlogPlatform(account.platform)"
              v-model="account.draft.title"
              placeholder="Post title"
              style="margin-bottom: 8px;"
            />
            <el-input
              v-model="account.draft.message"
              type="textarea"
              :rows="4"
              :maxlength="account.maxChars || undefined"
            />
            <div v-if="account.supportsFirstComment && options.linkInFirstComment" class="pc-first-comment">
              <span class="pc-subtle">第一則留言：</span>
              <el-input v-model="account.draft.firstComment" placeholder="貼文連結等資訊" />
            </div>
            <!-- TikTok per-post settings (audit compliance) -->
            <TikTokPostSettings
              v-if="account.platform === 'tiktok'"
              :model-value="tiktokPostSettings[account.accountId] || {}"
              :creator-info="tiktokCreatorInfo[account.accountId] || null"
              :is-photo-post="!mediaFiles.some(f => isVideo(f.path))"
              :media-files="mediaFiles"
              @update:model-value="tiktokPostSettings[account.accountId] = $event"
              @validity-change="tiktokSettingsValidity[account.accountId] = $event"
            />
          </div>
        </el-collapse-item>
      </el-collapse>
    </el-card>

    <!-- 6. Schedule -->
    <el-card class="pc-card" shadow="never">
      <template #header>
        <div class="pc-section-header">
          <h3>6. 排程</h3>
        </div>
      </template>
      <el-radio-group v-model="schedule.mode">
        <el-radio label="now">立即發佈</el-radio>
        <el-radio label="schedule">排程發佈</el-radio>
      </el-radio-group>
      <div v-if="schedule.mode === 'schedule'" class="pc-mt-8">
        <el-date-picker
          v-model="schedule.startAt"
          type="datetime"
          placeholder="選擇首則貼文時間"
          format="YYYY-MM-DD HH:mm"
          value-format="YYYY-MM-DDTHH:mm:00"
          style="width: 280px"
        />
        <span class="pc-subtle pc-ml-8">
          後續單媒體拆分貼文會自動 +{{ STAGGER_MINUTES }} 分鐘
        </span>
      </div>
    </el-card>

    <!-- 7. Publish -->
    <el-card class="pc-card pc-card--footer" shadow="never">
      <div class="pc-footer-row">
        <el-button :loading="submitting" type="success" size="large" :disabled="!canSubmit" @click="submit">
          {{ schedule.mode === 'now' ? '立即發佈' : '送出排程' }}
        </el-button>
        <el-button size="large" @click="resetForm">取消</el-button>
      </div>
      <div v-if="submitResult" class="pc-submit-result">
        <el-alert
          :title="submitResult.message"
          :type="submitResult.type"
          :closable="false"
          show-icon
        />
        <el-alert
          v-if="hasTiktokSelected && submitResult.jobs?.length"
          title="TikTok 內容可能需要幾分鐘的處理時間才會在平台上顯示。"
          type="info"
          show-icon
          :closable="false"
          style="margin-top: 8px;"
        />
        <div v-if="submitResult.jobs?.length" class="pc-jobs-list">
          <div v-for="job in submitResult.jobs" :key="job.id">
            <router-link :to="`/jobs?jobId=${job.id}`" class="pc-link">Job #{{ job.id }}</router-link>
            <span class="pc-subtle pc-ml-8">{{ job.platform }} — {{ job.totalTargets }} target(s)</span>
          </div>
        </div>
      </div>
    </el-card>

    <!-- Save template dialog -->
    <el-dialog v-model="saveTemplateDialogVisible" title="另存為範本" width="520px">
      <el-form label-position="top">
        <el-form-item label="名稱" required>
          <el-input v-model="saveTemplateForm.name" />
        </el-form-item>
        <el-form-item label="說明">
          <el-input v-model="saveTemplateForm.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="儲存哪些設定">
          <el-checkbox-group v-model="saveTemplateForm.includedSettings">
            <el-checkbox label="profileIds">所選 Profiles</el-checkbox>
            <el-checkbox label="accountIds">所選帳號</el-checkbox>
            <el-checkbox label="watermark">浮水印開關</el-checkbox>
            <el-checkbox label="intro">片頭開關</el-checkbox>
            <el-checkbox label="outro">片尾開關</el-checkbox>
            <el-checkbox label="linkInFirstComment">連結放留言</el-checkbox>
            <el-checkbox label="screenshots">截圖設定</el-checkbox>
            <el-checkbox label="schedule">排程設定</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="saveTemplateDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveTemplate">儲存</el-button>
      </template>
    </el-dialog>

    <!-- Material library dialog -->
    <el-dialog v-model="materialLibraryVisible" title="素材庫" width="640px">
      <el-table :data="materialsList" max-height="420" @selection-change="onMaterialSelectionChange">
        <el-table-column type="selection" width="48" />
        <el-table-column prop="filename" label="檔名" />
        <el-table-column prop="filesize" label="大小 (MB)" width="120" />
      </el-table>
      <template #footer>
        <el-button @click="materialLibraryVisible = false">取消</el-button>
        <el-button type="primary" @click="addMaterialsFromLibrary">加入</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled } from '@element-plus/icons-vue'

import { useProfilesStore } from '@/stores/profiles'
import { usePublishTemplatesStore } from '@/stores/publish-templates'
import { getToken } from '@/utils/auth'
import { buildApiUrl } from '@/utils/api-url'
import { publishCenterApi } from '@/api/publish-center'
import { materialApi } from '@/api/material'
import { tiktokApi } from '@/api/tiktok'
import TikTokPostSettings from '@/components/TikTokPostSettings.vue'

const STAGGER_MINUTES = 5

const profilesStore = useProfilesStore()
const templatesStore = usePublishTemplatesStore()

const uploadAction = buildApiUrl('/upload')
const authHeaders = computed(() => {
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
})

const mediaFiles = ref([]) // [{path, name, size}]
const selectedProfileIds = ref([])
const selectedAccountIds = ref([])
const profileAccountCache = reactive({}) // profileId -> [account]

const options = reactive({
  watermark: true,
  intro: true,
  outro: true,
  linkInFirstComment: false,
  tiktokDirectPost: false,
  screenshots: {
    enabled: false,
    count: 3,
    timestampsRaw: '',
  },
})

// True when any selected account is on TikTok. Drives the visibility of
// the "Direct post (skip draft)" toggle in section 3, which TikTok's app
// review specifically wants demonstrated alongside an explicit user
// confirmation modal.
const hasTiktokSelected = computed(() => {
  for (const profileId of selectedProfileIds.value) {
    const accounts = profileAccountCache[profileId] || []
    for (const account of accounts) {
      if (account.platform === 'tiktok' && selectedAccountIds.value.includes(account.id)) {
        return true
      }
    }
  }
  return false
})

const isTiktokSandbox = computed(() => {
  for (const profileId of selectedProfileIds.value) {
    const accounts = profileAccountCache[profileId] || []
    for (const account of accounts) {
      if (account.platform === 'tiktok' && selectedAccountIds.value.includes(account.id) && account.isSandbox) {
        return true
      }
    }
  }
  return false
})

const brief = ref('')
const previews = ref([]) // [{profileId, profileName, accounts: [...]}]
const expandedProfiles = ref([])
const generating = ref(false)
const regeneratingKey = ref(null)

const schedule = reactive({ mode: 'now', startAt: null })

const submitting = ref(false)
const submitResult = ref(null)

const saveTemplateDialogVisible = ref(false)
const saveTemplateForm = reactive({
  name: '',
  description: '',
  includedSettings: ['profileIds', 'accountIds', 'watermark', 'intro', 'outro', 'linkInFirstComment', 'screenshots', 'schedule'],
})
const saving = ref(false)
const selectedTemplateId = ref(null)
const loadedTemplateName = ref('')

const materialLibraryVisible = ref(false)
const materialsList = ref([])
const selectedMaterials = ref([])

// TikTok per-post settings and creator info
const tiktokCreatorInfo = reactive({}) // accountId -> creator info response
const tiktokPostSettings = reactive({}) // accountId -> per-post settings
const tiktokSettingsValidity = reactive({}) // accountId -> boolean

function ensureTiktokSettings(accountId) {
  if (!tiktokPostSettings[accountId]) {
    tiktokPostSettings[accountId] = {
      privacyLevel: null,
      allowComment: false,
      allowDuet: false,
      allowStitch: false,
      contentDisclosureEnabled: false,
      yourBrand: false,
      brandedContent: false,
    }
  }
}

onMounted(async () => {
  try {
    await profilesStore.refreshProfiles()
    await templatesStore.refresh()
  } catch (err) {
    // Errors handled by axios interceptor
  }
})

function isVideo(path) {
  return /\.(mp4|mov|avi|mkv|webm|m4v)$/i.test(path || '')
}

function countChars(text) {
  return (text || '').length
}

function isBlogPlatform(platform) {
  return ['teaching_blog', 'nw_sw_blog'].includes(platform)
}

function findProfile(profileId) {
  return profilesStore.profiles.find((p) => p.id === profileId) || null
}

function accountsForProfile(profileId) {
  return profileAccountCache[profileId] || []
}

async function onProfileSelectionChanged() {
  for (const profileId of selectedProfileIds.value) {
    if (!profileAccountCache[profileId]) {
      try {
        const accounts = await profilesStore.fetchAccountsForProfile(profileId, { enabled: true })
        profileAccountCache[profileId] = accounts || []
        // default-select all enabled accounts
        for (const account of accounts || []) {
          if (!selectedAccountIds.value.includes(account.id)) {
            selectedAccountIds.value.push(account.id)
          }
        }
      } catch (err) {
        profileAccountCache[profileId] = []
      }
    }
  }
  // Fetch TikTok creator info for newly selected accounts
  await fetchTiktokCreatorInfo()
}

async function fetchTiktokCreatorInfo() {
  for (const profileId of selectedProfileIds.value) {
    const accounts = profileAccountCache[profileId] || []
    for (const account of accounts) {
      if (account.platform === 'tiktok' && selectedAccountIds.value.includes(account.id)) {
        const accountId = account.id
        ensureTiktokSettings(accountId)
        if (!tiktokCreatorInfo[accountId]) {
          try {
            const resp = await tiktokApi.getCreatorInfo(accountId)
            tiktokCreatorInfo[accountId] = resp?.data || resp || {}
          } catch (err) {
            tiktokCreatorInfo[accountId] = { _error: err?.message || 'Failed to fetch creator info' }
          }
        }
      }
    }
  }
}

function removeMedia(index) {
  mediaFiles.value.splice(index, 1)
}

function onUploadSuccess(response, file) {
  // /upload returns { code: 200, msg, data: '<uuid>_<filename>' } — the
  // axios body's `data` field is the *string* filename relative to
  // videoFile/. el-upload hands us that raw body, not the file object,
  // so we cannot reuse file.name (which would drop the uuid prefix).
  let path = null
  if (typeof response?.data === 'string') {
    path = response.data
  } else if (typeof response === 'string') {
    path = response
  } else if (response?.data?.filepath) {
    path = response.data.filepath
  }
  if (!path) {
    ElMessage.error('檔案上傳成功但無法解析路徑')
    return
  }
  mediaFiles.value.push({ path, name: file.name, size: file.size })
}

function onUploadError() {
  ElMessage.error('檔案上傳失敗')
}

async function openMaterialLibrary() {
  materialLibraryVisible.value = true
  try {
    const response = await materialApi.getAllMaterials()
    materialsList.value = response?.data || []
  } catch (err) {
    materialsList.value = []
  }
}

function onMaterialSelectionChange(rows) {
  selectedMaterials.value = rows
}

function addMaterialsFromLibrary() {
  for (const material of selectedMaterials.value) {
    mediaFiles.value.push({
      path: material.filepath || material.file_path || material.filename,
      name: material.filename,
      size: material.filesize ? material.filesize * 1024 * 1024 : 0,
    })
  }
  selectedMaterials.value = []
  materialLibraryVisible.value = false
}

const canGeneratePreviews = computed(
  () => selectedProfileIds.value.length > 0 && selectedAccountIds.value.length > 0
)

// Check if all TikTok accounts have valid settings (privacy selected, disclosure valid)
const tiktokSettingsAllValid = computed(() => {
  for (const profileId of selectedProfileIds.value) {
    const accounts = profileAccountCache[profileId] || []
    for (const account of accounts) {
      if (account.platform === 'tiktok' && selectedAccountIds.value.includes(account.id)) {
        const valid = tiktokSettingsValidity[account.id]
        if (valid === false) return false
      }
    }
  }
  return true
})

const canSubmit = computed(
  () => canGeneratePreviews.value && mediaFiles.value.length > 0 && previews.value.length > 0 && tiktokSettingsAllValid.value
)

// Re-fetch creator info when account selection changes
watch(selectedAccountIds, async (newIds, oldIds) => {
  // Initialize settings for newly selected TikTok accounts
  for (const profileId of selectedProfileIds.value) {
    const accounts = profileAccountCache[profileId] || []
    for (const account of accounts) {
      if (account.platform === 'tiktok' && newIds.includes(account.id)) {
        ensureTiktokSettings(account.id)
      }
    }
  }
  await fetchTiktokCreatorInfo()
}, { deep: true })

function buildOptionsPayload() {
  const screenshots = options.screenshots
  const raw = (screenshots.timestampsRaw || '').trim()
  let timestamps = null
  if (raw) {
    timestamps = raw.split(',').map((s) => s.trim()).filter(Boolean)
  }
  return {
    watermark: options.watermark,
    intro: options.intro,
    outro: options.outro,
    linkInFirstComment: options.linkInFirstComment,
    tiktokDirectPost: options.tiktokDirectPost,
    screenshots: {
      enabled: screenshots.enabled,
      count: screenshots.count,
      timestamps,
    },
  }
}

async function generatePreviews() {
  generating.value = true
  previews.value = []
  // Ensure TikTok settings are initialized for all selected TikTok accounts
  for (const profileId of selectedProfileIds.value) {
    const accounts = profileAccountCache[profileId] || []
    for (const account of accounts) {
      if (account.platform === 'tiktok' && selectedAccountIds.value.includes(account.id)) {
        ensureTiktokSettings(account.id)
      }
    }
  }
  // Fetch creator info if not already loaded
  await fetchTiktokCreatorInfo()
  try {
    const response = await publishCenterApi.preview({
      profileIds: selectedProfileIds.value,
      selectedAccountIds: selectedAccountIds.value,
      brief: brief.value,
      options: buildOptionsPayload(),
    })
    previews.value = response?.data?.profiles || []
    expandedProfiles.value = previews.value.map((p) => p.profileId)
  } finally {
    generating.value = false
  }
}

function draftKey(profileId, accountId) {
  return `${profileId}:${accountId}`
}

async function regenerateAccount(profile, account) {
  const key = draftKey(profile.profileId, account.accountId)
  regeneratingKey.value = key
  try {
    const response = await publishCenterApi.regenerate({
      profileId: profile.profileId,
      accountId: account.accountId,
      brief: brief.value,
      options: buildOptionsPayload(),
    })
    const draft = response?.data?.draft
    if (draft) {
      account.draft = { ...account.draft, ...draft }
    }
  } finally {
    regeneratingKey.value = null
  }
}

function buildSchedulePayload() {
  if (schedule.mode === 'now') return { publishNow: true }
  return { publishNow: false, startAt: schedule.startAt }
}

function buildAccountDraftsPayload() {
  const out = {}
  for (const profile of previews.value) {
    for (const account of profile.accounts) {
      out[account.accountId] = account.draft
    }
  }
  return out
}

function buildTiktokPostSettingsPayload() {
  const out = {}
  for (const profileId of selectedProfileIds.value) {
    const accounts = profileAccountCache[profileId] || []
    for (const account of accounts) {
      if (account.platform === 'tiktok' && selectedAccountIds.value.includes(account.id)) {
        const settings = tiktokPostSettings[account.id]
        if (settings) {
          out[account.id] = {
            privacyLevel: settings.privacyLevel,
            disableComment: !settings.allowComment,
            disableDuet: !settings.allowDuet,
            disableStitch: !settings.allowStitch,
            contentDisclosure: settings.contentDisclosureEnabled ? {
              enabled: true,
              yourBrand: settings.yourBrand,
              brandedContent: settings.brandedContent,
            } : null,
          }
        }
      }
    }
  }
  return out
}

async function submit() {
  // TikTok's app review requires explicit user confirmation before
  // video.publish (direct-post) fires. The toggle only flips the
  // behaviour for TikTok accounts in this batch — gate it behind a
  // modal so the consent moment is visible in the demo recording.
  if (hasTiktokSelected.value) {
    // Build compliance declaration based on commercial content settings
    let hasBrandedContent = false
    for (const profileId of selectedProfileIds.value) {
      const accounts = profileAccountCache[profileId] || []
      for (const account of accounts) {
        if (account.platform === 'tiktok' && selectedAccountIds.value.includes(account.id)) {
          const settings = tiktokPostSettings[account.id]
          if (settings?.contentDisclosureEnabled && settings?.brandedContent) {
            hasBrandedContent = true
          }
        }
      }
    }
    const declaration = hasBrandedContent
      ? '發佈即表示您同意 TikTok 的品牌合作內容政策和音樂使用確認。'
      : '發佈即表示您同意 TikTok 的音樂使用確認。'

    const confirmBody = options.tiktokDirectPost
      ? `即將直接發佈到 TikTok 個人檔案（跳過草稿，無法在 TikTok App 內二次編輯）。\n\n${declaration}\n\n確認要繼續嗎？`
      : `${declaration}\n\n確認要繼續嗎？`

    try {
      await ElMessageBox.confirm(
        confirmBody,
        '確認發佈到 TikTok',
        {
          type: 'warning',
          confirmButtonText: '是，發佈',
          cancelButtonText: '取消',
        },
      )
    } catch (cancelled) {
      return
    }
  }
  submitting.value = true
  submitResult.value = null
  try {
    const response = await publishCenterApi.submit({
      profileIds: selectedProfileIds.value,
      selectedAccountIds: selectedAccountIds.value,
      mediaFilePaths: mediaFiles.value.map((file) => file.path),
      brief: brief.value,
      options: buildOptionsPayload(),
      schedule: buildSchedulePayload(),
      accountDrafts: buildAccountDraftsPayload(),
      tiktokPostSettings: buildTiktokPostSettingsPayload(),
    })
    const data = response?.data || {}
    submitResult.value = {
      type: data.jobs?.length ? 'success' : 'warning',
      message: data.jobs?.length
        ? `已排入 ${data.jobs.length} 個發佈工作`
        : '送出成功，但未產生任何工作',
      jobs: data.jobs,
      skipped: data.skipped,
    }
  } catch (err) {
    submitResult.value = { type: 'error', message: err?.message || '送出失敗' }
  } finally {
    submitting.value = false
  }
}

function showSaveTemplateDialog() {
  saveTemplateForm.name = ''
  saveTemplateForm.description = ''
  saveTemplateDialogVisible.value = true
}

async function saveTemplate() {
  if (!saveTemplateForm.name.trim()) {
    ElMessage.warning('請輸入名稱')
    return
  }
  saving.value = true
  try {
    await templatesStore.create({
      name: saveTemplateForm.name,
      description: saveTemplateForm.description,
      includedSettings: saveTemplateForm.includedSettings,
      config: {
        profileIds: selectedProfileIds.value,
        accountIds: selectedAccountIds.value,
        watermark: options.watermark,
        intro: options.intro,
        outro: options.outro,
        linkInFirstComment: options.linkInFirstComment,
        screenshots: { ...options.screenshots },
        schedule: { ...schedule },
      },
    })
    saveTemplateDialogVisible.value = false
    ElMessage.success('已儲存範本')
  } finally {
    saving.value = false
  }
}

async function onTemplateSelected(templateId) {
  if (!templateId) {
    loadedTemplateName.value = ''
    return
  }
  const template = templatesStore.templates.find((t) => t.id === templateId)
  if (!template) return
  const include = new Set(template.includedSettings || [])
  const config = template.config || {}
  if (include.has('profileIds') && Array.isArray(config.profileIds)) {
    selectedProfileIds.value = [...config.profileIds]
    await onProfileSelectionChanged()
  }
  if (include.has('accountIds') && Array.isArray(config.accountIds)) {
    selectedAccountIds.value = [...config.accountIds]
  }
  if (include.has('watermark')) options.watermark = !!config.watermark
  if (include.has('intro')) options.intro = !!config.intro
  if (include.has('outro')) options.outro = !!config.outro
  if (include.has('linkInFirstComment')) options.linkInFirstComment = !!config.linkInFirstComment
  if (include.has('screenshots') && config.screenshots) {
    Object.assign(options.screenshots, config.screenshots)
  }
  if (include.has('schedule') && config.schedule) {
    Object.assign(schedule, config.schedule)
  }
  loadedTemplateName.value = template.name
}

function resetForm() {
  mediaFiles.value = []
  selectedProfileIds.value = []
  selectedAccountIds.value = []
  options.watermark = true
  options.intro = true
  options.outro = true
  options.linkInFirstComment = false
  options.screenshots.enabled = false
  options.screenshots.count = 3
  options.screenshots.timestampsRaw = ''
  brief.value = ''
  previews.value = []
  expandedProfiles.value = []
  schedule.mode = 'now'
  schedule.startAt = null
  submitResult.value = null
  selectedTemplateId.value = null
  loadedTemplateName.value = ''
  // Clear TikTok state
  Object.keys(tiktokCreatorInfo).forEach(k => delete tiktokCreatorInfo[k])
  Object.keys(tiktokPostSettings).forEach(k => delete tiktokPostSettings[k])
  Object.keys(tiktokSettingsValidity).forEach(k => delete tiktokSettingsValidity[k])
}
</script>

<style scoped>
.publish-center {
  padding: 16px;
  max-width: 1100px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.pc-card {
  border-radius: 8px;
}
.pc-card--header :deep(.el-card__body),
.pc-card--footer :deep(.el-card__body) {
  padding: 14px 18px;
}
.pc-header-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}
.pc-header-left {
  display: flex;
  align-items: center;
  gap: 10px;
}
.pc-section-header {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.pc-section-header h3 {
  margin: 0;
  font-size: 16px;
}
.pc-subtle {
  color: #888;
  font-size: 12px;
}
.pc-help {
  color: #b0b0b0;
  font-size: 12px;
  margin-top: 4px;
}
.pc-mb-8 {
  margin-bottom: 8px;
}
.pc-mt-4 {
  margin-top: 4px;
}
.pc-mt-8 {
  margin-top: 8px;
}
.pc-ml-8 {
  margin-left: 8px;
}
.pc-media-list {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.pc-media-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  background: #f7f7f7;
  border-radius: 6px;
}
.pc-media-name {
  flex: 1;
}
.pc-profile-accounts {
  margin-top: 12px;
  padding: 8px 10px;
  background: #fafafa;
  border-radius: 6px;
}
.pc-profile-title {
  font-weight: 500;
  margin-bottom: 4px;
}
.pc-options-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 6px 14px;
}
.pc-screenshots {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.pc-screenshots-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.pc-actions {
  margin-top: 10px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.pc-account-draft {
  border-top: 1px solid #f0f0f0;
  padding: 10px 0;
}
.pc-account-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
  flex-wrap: wrap;
}
.pc-account-name {
  font-weight: 500;
}
.pc-first-comment {
  margin-top: 6px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.pc-footer-row {
  display: flex;
  gap: 10px;
}
.pc-submit-result {
  margin-top: 12px;
}
.pc-jobs-list {
  margin-top: 8px;
}
.pc-link {
  color: var(--el-color-primary);
  text-decoration: none;
}
</style>
