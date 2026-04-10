<template>
  <div class="profile-management">
    <div class="page-header">
      <h1>Profile 設定</h1>
    </div>

    <div class="profile-list-container">
      <div class="profile-toolbar">
        <el-input
          v-model="searchKeyword"
          placeholder="輸入 Profile 名稱搜尋"
          prefix-icon="Search"
          clearable
        />
        <div class="action-buttons">
          <el-button type="primary" @click="openCreateDialog">新增 Profile</el-button>
          <el-button type="info" @click="fetchProfiles" :loading="isRefreshing">
            <el-icon :class="{ 'is-loading': isRefreshing }"><Refresh /></el-icon>
            <span>{{ isRefreshing ? '重新整理中' : '重新整理' }}</span>
          </el-button>
        </div>
      </div>

      <div v-if="filteredProfiles.length > 0" class="profile-list">
        <el-table :data="filteredProfiles" style="width: 100%">
          <el-table-column prop="name" label="Profile" min-width="180" />
          <el-table-column label="綁定帳號" min-width="220">
            <template #default="scope">
              <div class="account-tags">
                <el-tag
                  v-for="accountId in scope.row.accountIds"
                  :key="accountId"
                  class="account-tag"
                >
                  {{ getAccountName(accountId) }}
                </el-tag>
                <span v-if="scope.row.accountIds.length === 0" class="muted-text">尚未綁定</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="OneDrive / 儲存" min-width="220">
            <template #default="scope">
              <div class="cell-lines">
                <div>{{ scope.row.settings?.storage?.remoteName || '-' }}</div>
                <div class="muted-text">{{ scope.row.settings?.storage?.remotePath || '-' }}</div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="Google 試算表" min-width="220">
            <template #default="scope">
              <div class="cell-lines">
                <div>{{ scope.row.settings?.googleSheet?.spreadsheetId || '-' }}</div>
                <div class="muted-text">匯出時自動建立：日期-Profile 名稱</div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="320">
            <template #default="scope">
              <el-button size="small" @click="openEditDialog(scope.row)">編輯</el-button>
              <el-button size="small" type="success" @click="openGenerateDialog(scope.row)">批次產生</el-button>
              <el-button size="small" type="danger" @click="handleDelete(scope.row)">刪除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div v-else class="empty-data">
        <el-empty description="目前沒有 Profile 資料" />
      </div>
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="dialogType === 'create' ? '新增 Profile' : '編輯 Profile'"
      width="900px"
      class="profile-dialog"
    >
      <el-form :model="profileForm" label-width="140px">
        <el-form-item label="Profile 名稱">
          <el-input v-model="profileForm.name" placeholder="例如：運動品牌-主帳號群組" />
        </el-form-item>

        <el-form-item label="預設帳號群組">
          <el-select
            v-model="profileForm.accountIds"
            multiple
            filterable
            placeholder="選擇這個 Profile 預設要用的帳號"
            style="width: 100%"
          >
            <el-option
              v-for="account in accountStore.accounts"
              :key="account.id"
              :label="`${account.name}（${account.platform}）`"
              :value="account.id"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="系統提示詞">
          <el-input
            v-model="profileForm.systemPrompt"
            type="textarea"
            :rows="4"
            placeholder="定義這個 Profile 的寫作風格、語氣、禁忌、目標受眾與輸出格式"
          />
        </el-form-item>

        <el-form-item label="聯絡資訊">
          <el-input
            v-model="profileForm.contactDetails"
            type="textarea"
            :rows="2"
            placeholder="例如：Telegram、Email、Website"
          />
        </el-form-item>

        <el-form-item label="CTA / 行動呼籲">
          <el-input
            v-model="profileForm.cta"
            type="textarea"
            :rows="2"
            placeholder="例如：追蹤、加入 Patreon、私訊合作"
          />
        </el-form-item>

        <el-divider>LLM 與轉錄</el-divider>

        <el-form-item label="API Base URL">
          <el-input v-model="profileForm.settings.llm.apiBaseUrl" placeholder="https://llmapi.iamwillywang.com/" />
        </el-form-item>

        <el-form-item label="轉錄模型">
          <el-input v-model="profileForm.settings.llm.transcriptionModel" placeholder="Audio-Speech-Group" />
        </el-form-item>

        <el-form-item label="文案生成模型">
          <el-input v-model="profileForm.settings.llm.generationModel" placeholder="reasoning / Multimodal-Generation-Groups" />
        </el-form-item>

        <el-divider>素材同步與 OneDrive</el-divider>

        <el-form-item label="Rclone Remote">
          <el-input v-model="profileForm.settings.storage.remoteName" placeholder="Onedrive-Yahooforsub-Tao" />
        </el-form-item>

        <el-form-item label="Remote Path">
          <el-input v-model="profileForm.settings.storage.remotePath" placeholder="Scripts-ssh-ssl-keys/SocialUpload" />
        </el-form-item>

        <el-form-item label="公開網址範本">
          <el-input
            v-model="profileForm.settings.storage.publicUrlTemplate"
            placeholder="選填，例如：https://cdn.example.com/{relative_path}"
          />
        </el-form-item>

        <el-divider>浮水印</el-divider>

        <el-form-item label="啟用浮水印">
          <el-switch v-model="profileForm.settings.watermark.enabled" />
        </el-form-item>

        <el-form-item label="浮水印類型">
          <el-radio-group v-model="profileForm.settings.watermark.type">
            <el-radio label="text">文字</el-radio>
            <el-radio label="image">圖片</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item label="浮水印模式">
          <el-radio-group v-model="profileForm.settings.watermark.mode">
            <el-radio label="static">固定</el-radio>
            <el-radio label="dynamic">隨機</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item label="模式說明">
          <div class="muted-text">
            圖片使用隨機位置；影片會以 1 至 5 秒區段隨機切換浮水印位置。
          </div>
        </el-form-item>

        <el-form-item label="浮水印文字" v-if="profileForm.settings.watermark.type === 'text'">
          <el-input v-model="profileForm.settings.watermark.text" placeholder="例如：@brandname" />
        </el-form-item>

        <el-form-item label="浮水印圖片路徑" v-else>
          <el-input v-model="profileForm.settings.watermark.imagePath" placeholder="本機可存取路徑，例如：C:/logo.png" />
        </el-form-item>

        <el-form-item label="預設位置">
          <el-select v-model="profileForm.settings.watermark.position" style="width: 100%">
            <el-option label="右下角" value="bottom-right" />
            <el-option label="左下角" value="bottom-left" />
            <el-option label="右上角" value="top-right" />
            <el-option label="左上角" value="top-left" />
            <el-option label="居中" value="center" />
          </el-select>
        </el-form-item>

        <el-form-item label="透明度">
          <el-slider v-model="profileForm.settings.watermark.opacity" :min="0.1" :max="1" :step="0.05" show-input />
        </el-form-item>

        <el-divider>Google 試算表</el-divider>

        <el-form-item label="Spreadsheet ID">
          <el-input v-model="profileForm.settings.googleSheet.spreadsheetId" placeholder="Google Sheet ID" />
        </el-form-item>

        <el-form-item label="工作表命名規則">
          <el-alert
            title="匯出時會自動建立「YYYY-MM-DD-Profile 名稱」工作表"
            type="info"
            :closable="false"
            show-icon
          />
        </el-form-item>

        <el-divider>CSV / Import 預設值</el-divider>

        <el-form-item label="預設連結">
          <el-input v-model="profileForm.settings.socialImport.defaultLink" placeholder="https://example.com" />
        </el-form-item>

        <el-form-item label="Category">
          <el-input v-model="profileForm.settings.socialImport.category" placeholder="選填" />
        </el-form-item>

        <el-form-item label="Watermark 名稱">
          <el-input v-model="profileForm.settings.socialImport.watermarkName" placeholder="例如：Default" />
        </el-form-item>

        <el-form-item label="Hashtag Group">
          <el-input v-model="profileForm.settings.socialImport.hashtagGroup" placeholder="排程工具中既有的 Hashtag Group 名稱" />
        </el-form-item>

        <el-form-item label="CTA Group">
          <el-input v-model="profileForm.settings.socialImport.ctaGroup" placeholder="排程工具中既有的 CTA Group 名稱" />
        </el-form-item>

        <el-form-item label="首則留言">
          <el-input v-model="profileForm.settings.socialImport.firstComment" placeholder="Facebook / Instagram / LinkedIn / Bluesky / Threads 首則留言" />
        </el-form-item>

        <el-form-item label="Story">
          <el-switch v-model="profileForm.settings.socialImport.story" />
        </el-form-item>

        <el-form-item label="Pinterest Board">
          <el-input v-model="profileForm.settings.socialImport.pinterestBoard" placeholder="選填" />
        </el-form-item>

        <el-form-item label="Alt Text">
          <el-input v-model="profileForm.settings.socialImport.altText" type="textarea" :rows="2" />
        </el-form-item>

        <el-divider>Post Preset</el-divider>

        <el-form-item label="X / Twitter Preset">
          <el-input v-model="profileForm.settings.postPresets.twitter" />
        </el-form-item>

        <el-form-item label="Threads Preset">
          <el-input v-model="profileForm.settings.postPresets.threads" />
        </el-form-item>

        <el-form-item label="Instagram Preset">
          <el-input v-model="profileForm.settings.postPresets.instagram" />
        </el-form-item>

        <el-form-item label="Facebook Preset">
          <el-input v-model="profileForm.settings.postPresets.facebook" />
        </el-form-item>

        <el-form-item label="YouTube Preset">
          <el-input v-model="profileForm.settings.postPresets.youtube" />
        </el-form-item>

        <el-form-item label="TikTok Preset">
          <el-input v-model="profileForm.settings.postPresets.tiktok" />
        </el-form-item>
      </el-form>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="dialogVisible = false">取消</el-button>
          <el-button type="primary" @click="submitProfile" :loading="isSubmitting">
            {{ isSubmitting ? '儲存中' : '儲存' }}
          </el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog
      v-model="generateDialogVisible"
      title="批次產生文案並匯出 Google 試算表"
      width="900px"
      class="generate-dialog"
    >
      <el-form :model="generateForm" label-width="140px">
        <el-form-item label="Profile">
          <el-input :model-value="currentProfile?.name || ''" disabled />
        </el-form-item>

        <el-form-item label="素材">
          <el-select
            v-model="generateForm.materialIds"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            placeholder="可一次選擇多個圖片或影片素材"
            style="width: 100%"
          >
            <el-option
              v-for="material in materials"
              :key="material.id"
              :label="material.filename"
              :value="material.id"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="本次發送帳號">
          <el-checkbox-group v-model="generateForm.selectedAccountIds" class="generate-account-list">
            <el-checkbox
              v-for="account in currentProfileAccounts"
              :key="account.id"
              :label="account.id"
            >
              {{ account.name }}（{{ account.platform }}）
            </el-checkbox>
          </el-checkbox-group>
          <div v-if="currentProfileAccounts.length === 0" class="muted-text">
            這個 Profile 目前沒有綁定任何帳號。
          </div>
        </el-form-item>

        <el-form-item label="導流連結">
          <el-input v-model="generateForm.link" placeholder="選填，可覆蓋 Profile 預設連結" />
        </el-form-item>

        <el-form-item label="排程時間">
          <el-date-picker
            v-model="generateForm.scheduleAt"
            type="datetime"
            value-format="YYYY-MM-DDTHH:mm:ss"
            placeholder="選填"
            style="width: 100%"
          />
        </el-form-item>

        <el-form-item label="寫入 Google 試算表">
          <el-switch v-model="generateForm.writeToSheet" />
        </el-form-item>
      </el-form>

      <div v-if="generationBatchResult" class="generation-result">
        <el-alert
          :title="buildBatchSummaryTitle()"
          type="success"
          :closable="false"
          show-icon
        />

        <div class="result-block">
          <h3>本次帳號</h3>
          <div class="account-tags">
            <el-tag
              v-for="account in selectedGenerationAccounts"
              :key="account.id"
              class="account-tag"
            >
              {{ account.name }}（{{ account.platform }}）
            </el-tag>
            <span v-if="selectedGenerationAccounts.length === 0" class="muted-text">未挑選帳號</span>
          </div>
        </div>

        <div class="batch-result-list">
          <div
            v-for="item in generationBatchResult.results"
            :key="item.material.id"
            class="batch-result-card"
          >
            <div class="result-block">
              <h3>{{ item.material.filename }}</h3>
              <el-link :href="item.storage?.publicUrl" target="_blank" type="primary">
                {{ item.storage?.publicUrl }}
              </el-link>
            </div>

            <div class="result-block">
              <h3>轉錄內容</h3>
              <el-input :model-value="item.transcript" type="textarea" :rows="6" readonly />
            </div>

            <div class="post-grid">
              <div v-for="(label, key) in postLabels" :key="key" class="post-card">
                <h4>{{ label }}</h4>
                <el-input :model-value="item.posts?.[key] || ''" type="textarea" :rows="6" readonly />
              </div>
            </div>
          </div>
        </div>
      </div>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="generateDialogVisible = false">關閉</el-button>
          <el-button type="primary" @click="submitGeneration" :loading="isGenerating">
            {{ isGenerating ? '產生中' : '開始批次產生' }}
          </el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { profileApi } from '@/api/profile'
import { accountApi } from '@/api/account'
import { materialApi } from '@/api/material'
import { useAccountStore } from '@/stores/account'
import { useAppStore } from '@/stores/app'

const accountStore = useAccountStore()
const appStore = useAppStore()

const searchKeyword = ref('')
const isRefreshing = ref(false)
const isSubmitting = ref(false)
const isGenerating = ref(false)

const profiles = ref([])
const dialogVisible = ref(false)
const dialogType = ref('create')
const generateDialogVisible = ref(false)
const currentProfile = ref(null)
const generationBatchResult = ref(null)

const postLabels = {
  twitter: 'X / Twitter 貼文',
  threads: 'Threads',
  instagram: 'Instagram 長文',
  facebook: 'Facebook 長文',
  youtube: 'YouTube 貼文與說明',
  tiktok: 'TikTok 貼文與說明',
  telegram: 'Telegram',
  patreon: 'Patreon'
}

const makeDefaultProfile = () => ({
  id: null,
  name: '',
  systemPrompt: '',
  contactDetails: '',
  cta: '',
  accountIds: [],
  settings: {
    llm: {
      apiBaseUrl: 'https://llmapi.iamwillywang.com/',
      transcriptionModel: '',
      generationModel: ''
    },
    storage: {
      remoteName: '',
      remotePath: 'Scripts-ssh-ssl-keys/SocialUpload',
      publicUrlTemplate: ''
    },
    watermark: {
      enabled: false,
      type: 'text',
      mode: 'static',
      text: '',
      imagePath: '',
      position: 'bottom-right',
      opacity: 0.45
    },
    googleSheet: {
      spreadsheetId: '',
      worksheetName: 'Sheet1'
    },
    socialImport: {
      defaultLink: '',
      category: '',
      watermarkName: '',
      hashtagGroup: '',
      videoThumbnailUrl: '',
      ctaGroup: '',
      firstComment: '',
      story: false,
      pinterestBoard: '',
      altText: '',
      pinTitle: ''
    },
    postPresets: {
      twitter: '',
      threads: '',
      instagram: '',
      facebook: '',
      youtube: '',
      tiktok: ''
    }
  }
})

const profileForm = ref(makeDefaultProfile())
const generateForm = ref({
  materialIds: [],
  selectedAccountIds: [],
  link: '',
  scheduleAt: '',
  writeToSheet: true
})

const filteredProfiles = computed(() => {
  if (!searchKeyword.value.trim()) {
    return profiles.value
  }

  const keyword = searchKeyword.value.trim().toLowerCase()
  return profiles.value.filter(profile => profile.name.toLowerCase().includes(keyword))
})

const materials = computed(() => appStore.materials)
const currentProfileAccounts = computed(() => {
  if (!currentProfile.value) {
    return []
  }
  const selectedIds = new Set(currentProfile.value.accountIds || [])
  return accountStore.accounts.filter(item => selectedIds.has(item.id))
})
const selectedGenerationAccounts = computed(() => {
  const selectedIds = new Set(generationBatchResult.value?.selectedAccountIds || [])
  return accountStore.accounts.filter(item => selectedIds.has(item.id))
})

const getAccountName = (accountId) => {
  const account = accountStore.accounts.find(item => item.id === accountId)
  return account ? `${account.name}（${account.platform}）` : accountId
}

const fetchProfiles = async () => {
  isRefreshing.value = true
  try {
    const response = await profileApi.getProfiles()
    profiles.value = response.data || []
  } catch (error) {
    ElMessage.error('取得 Profile 清單失敗')
  } finally {
    isRefreshing.value = false
  }
}

const ensureAccounts = async () => {
  if (accountStore.accounts.length > 0) {
    return
  }

  try {
    const response = await accountApi.getAccounts()
    if (response.code === 200 && response.data) {
      accountStore.setAccounts(response.data)
    }
  } catch (error) {
    ElMessage.error('取得帳號清單失敗')
  }
}

const ensureMaterials = async () => {
  if (appStore.materials.length > 0) {
    return
  }

  try {
    const response = await materialApi.getAllMaterials()
    if (response.code === 200) {
      appStore.setMaterials(response.data || [])
    }
  } catch (error) {
    ElMessage.error('取得素材清單失敗')
  }
}

const openCreateDialog = () => {
  dialogType.value = 'create'
  profileForm.value = makeDefaultProfile()
  dialogVisible.value = true
}

const openEditDialog = (profile) => {
  dialogType.value = 'edit'
  profileForm.value = JSON.parse(JSON.stringify(profile))
  dialogVisible.value = true
}

const submitProfile = async () => {
  if (!profileForm.value.name.trim()) {
    ElMessage.warning('請輸入 Profile 名稱')
    return
  }

  isSubmitting.value = true
  try {
    const response = await profileApi.saveProfile(profileForm.value)
    const savedProfile = response.data
    const index = profiles.value.findIndex(item => item.id === savedProfile.id)

    if (index > -1) {
      profiles.value[index] = savedProfile
    } else {
      profiles.value.unshift(savedProfile)
    }

    dialogVisible.value = false
    ElMessage.success('Profile 儲存成功')
  } catch (error) {
    ElMessage.error(error.message || 'Profile 儲存失敗')
  } finally {
    isSubmitting.value = false
  }
}

const handleDelete = (profile) => {
  ElMessageBox.confirm(
    `確定要刪除 Profile「${profile.name}」嗎？`,
    '提醒',
    {
      confirmButtonText: '確定',
      cancelButtonText: '取消',
      type: 'warning'
    }
  ).then(async () => {
    try {
      await profileApi.deleteProfile(profile.id)
      profiles.value = profiles.value.filter(item => item.id !== profile.id)
      ElMessage.success('刪除成功')
    } catch (error) {
      ElMessage.error('刪除失敗')
    }
  }).catch(() => {})
}

const openGenerateDialog = async (profile) => {
  currentProfile.value = profile
  generationBatchResult.value = null
  generateForm.value = {
    materialIds: [],
    selectedAccountIds: [...(profile.accountIds || [])],
    link: profile.settings?.socialImport?.defaultLink || '',
    scheduleAt: '',
    writeToSheet: true
  }
  await ensureMaterials()
  generateDialogVisible.value = true
}

const submitGeneration = async () => {
  if (!currentProfile.value) {
    return
  }

  if (generateForm.value.materialIds.length === 0) {
    ElMessage.warning('請至少選擇一份素材')
    return
  }

  isGenerating.value = true
  try {
    const response = await profileApi.generateBatchContent({
      profileId: currentProfile.value.id,
      materialIds: generateForm.value.materialIds,
      selectedAccountIds: generateForm.value.selectedAccountIds,
      link: generateForm.value.link,
      scheduleAt: generateForm.value.scheduleAt,
      writeToSheet: generateForm.value.writeToSheet
    })
    generationBatchResult.value = response.data
    ElMessage.success('批次文案產生完成')
  } catch (error) {
    ElMessage.error(error.message || '批次產生失敗')
  } finally {
    isGenerating.value = false
  }
}

const buildBatchSummaryTitle = () => {
  if (!generationBatchResult.value) {
    return ''
  }
  const summary = generationBatchResult.value.summary || {}
  const rowCount = summary.sheetRows || 0
  const materialCount = summary.materials || 0
  const worksheetText = (summary.worksheets || []).join('、')
  if (generateForm.value.writeToSheet) {
    return `已完成 ${materialCount} 份素材，匯出 ${rowCount} 列到 ${worksheetText || 'Google 試算表'}`
  }
  return `已完成 ${materialCount} 份素材，僅產生文案，尚未寫入 Google 試算表`
}

onMounted(async () => {
  await Promise.all([
    fetchProfiles(),
    ensureAccounts(),
    ensureMaterials()
  ])
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

.profile-management {
  .page-header {
    margin-bottom: 20px;

    h1 {
      font-size: 24px;
      color: $text-primary;
      margin: 0;
    }
  }

  .profile-list-container {
    background-color: #fff;
    border-radius: 4px;
    box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.1);
    padding: 20px;
  }

  .profile-toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 20px;

    .el-input {
      width: 320px;
    }

    .action-buttons {
      display: flex;
      gap: 10px;
    }

    .is-loading {
      animation: rotate 1s linear infinite;
    }
  }

  .empty-data {
    padding: 40px 0;
  }

  .account-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .account-tag {
    margin-right: 0;
  }

  .muted-text {
    color: #909399;
    font-size: 13px;
  }

  .cell-lines {
    line-height: 1.6;
  }

  .generate-account-list {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 16px;
  }

  .generation-result {
    margin-top: 20px;
  }

  .result-block {
    margin-top: 20px;

    h3 {
      margin-bottom: 10px;
      font-size: 16px;
      color: $text-primary;
    }
  }

  .batch-result-list {
    display: flex;
    flex-direction: column;
    gap: 20px;
    margin-top: 20px;
  }

  .batch-result-card {
    background-color: #f8fafc;
    border: 1px solid #e5eaf3;
    border-radius: 10px;
    padding: 20px;
  }

  .post-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 16px;
    margin-top: 20px;
  }

  .post-card {
    background-color: #f5f7fa;
    border-radius: 6px;
    padding: 16px;

    h4 {
      margin: 0 0 10px 0;
      font-size: 14px;
      color: $text-primary;
    }
  }
}

.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}
</style>
