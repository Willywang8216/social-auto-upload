<template>
  <div class="profile-management">
    <div class="page-header">
      <h1>Profile 管理</h1>
    </div>

    <div class="profile-list-container">
      <div class="profile-toolbar">
        <el-input
          v-model="searchKeyword"
          placeholder="输入 Profile 名称搜索"
          prefix-icon="Search"
          clearable
        />
        <div class="action-buttons">
          <el-button type="primary" @click="openCreateDialog">新增 Profile</el-button>
          <el-button type="info" @click="fetchProfiles" :loading="isRefreshing">
            <el-icon :class="{ 'is-loading': isRefreshing }"><Refresh /></el-icon>
            <span v-if="isRefreshing">刷新中</span>
          </el-button>
        </div>
      </div>

      <div v-if="filteredProfiles.length > 0" class="profile-list">
        <el-table :data="filteredProfiles" style="width: 100%">
          <el-table-column prop="name" label="Profile" min-width="180" />
          <el-table-column label="绑定账号" min-width="220">
            <template #default="scope">
              <div class="account-tags">
                <el-tag
                  v-for="accountId in scope.row.accountIds"
                  :key="accountId"
                  class="account-tag"
                >
                  {{ getAccountName(accountId) }}
                </el-tag>
                <span v-if="scope.row.accountIds.length === 0" class="muted-text">未绑定</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="存储" min-width="220">
            <template #default="scope">
              <div class="cell-lines">
                <div>{{ scope.row.settings?.storage?.remoteName || '-' }}</div>
                <div class="muted-text">{{ scope.row.settings?.storage?.remotePath || '-' }}</div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="Google Sheet" min-width="220">
            <template #default="scope">
              <div class="cell-lines">
                <div>{{ scope.row.settings?.googleSheet?.spreadsheetId || '-' }}</div>
                <div class="muted-text">{{ scope.row.settings?.googleSheet?.worksheetName || 'Sheet1' }}</div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="320">
            <template #default="scope">
              <el-button size="small" @click="openEditDialog(scope.row)">编辑</el-button>
              <el-button size="small" type="success" @click="openGenerateDialog(scope.row)">生成内容</el-button>
              <el-button size="small" type="danger" @click="handleDelete(scope.row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div v-else class="empty-data">
        <el-empty description="暂无 Profile 数据" />
      </div>
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="dialogType === 'create' ? '新增 Profile' : '编辑 Profile'"
      width="900px"
      class="profile-dialog"
    >
      <el-form :model="profileForm" label-width="140px">
        <el-form-item label="Profile 名称">
          <el-input v-model="profileForm.name" placeholder="例如：sports-creator" />
        </el-form-item>

        <el-form-item label="绑定账号">
          <el-select
            v-model="profileForm.accountIds"
            multiple
            filterable
            placeholder="选择对应账号"
            style="width: 100%"
          >
            <el-option
              v-for="account in accountStore.accounts"
              :key="account.id"
              :label="`${account.name} (${account.platform})`"
              :value="account.id"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="System Prompt">
          <el-input
            v-model="profileForm.systemPrompt"
            type="textarea"
            :rows="4"
            placeholder="定义此 Profile 的内容风格、语气、禁忌和目标受众"
          />
        </el-form-item>

        <el-form-item label="Contact Details">
          <el-input
            v-model="profileForm.contactDetails"
            type="textarea"
            :rows="2"
            placeholder="例如：Telegram、Email、Website"
          />
        </el-form-item>

        <el-form-item label="CTA">
          <el-input
            v-model="profileForm.cta"
            type="textarea"
            :rows="2"
            placeholder="例如：Follow、Join Patreon、DM for collab"
          />
        </el-form-item>

        <el-divider>LLM 与转录</el-divider>

        <el-form-item label="API Base URL">
          <el-input v-model="profileForm.settings.llm.apiBaseUrl" placeholder="https://llmapi.iamwillywang.com/" />
        </el-form-item>

        <el-form-item label="转录模型">
          <el-input v-model="profileForm.settings.llm.transcriptionModel" placeholder="Audio-Speech-Group" />
        </el-form-item>

        <el-form-item label="生成模型">
          <el-input v-model="profileForm.settings.llm.generationModel" placeholder="reasoning / Multimodal-Generation-Groups" />
        </el-form-item>

        <el-divider>媒体存储</el-divider>

        <el-form-item label="Rclone Remote">
          <el-input v-model="profileForm.settings.storage.remoteName" placeholder="Onedrive-Yahooforsub-Tao" />
        </el-form-item>

        <el-form-item label="Remote Path">
          <el-input v-model="profileForm.settings.storage.remotePath" placeholder="Scripts-ssh-ssl-keys/SocialUpload" />
        </el-form-item>

        <el-form-item label="Public URL Template">
          <el-input
            v-model="profileForm.settings.storage.publicUrlTemplate"
            placeholder="选填，例如：https://cdn.example.com/{relative_path}"
          />
        </el-form-item>

        <el-divider>浮水印</el-divider>

        <el-form-item label="启用浮水印">
          <el-switch v-model="profileForm.settings.watermark.enabled" />
        </el-form-item>

        <el-form-item label="浮水印类型">
          <el-radio-group v-model="profileForm.settings.watermark.type">
            <el-radio label="text">文字</el-radio>
            <el-radio label="image">图片</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item label="浮水印模式">
          <el-radio-group v-model="profileForm.settings.watermark.mode">
            <el-radio label="static">静态</el-radio>
            <el-radio label="dynamic">动态</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item label="浮水印文字" v-if="profileForm.settings.watermark.type === 'text'">
          <el-input v-model="profileForm.settings.watermark.text" placeholder="例如：@brandname" />
        </el-form-item>

        <el-form-item label="浮水印图片路径" v-else>
          <el-input v-model="profileForm.settings.watermark.imagePath" placeholder="本机可访问路径，例如：C:/logo.png" />
        </el-form-item>

        <el-form-item label="位置">
          <el-select v-model="profileForm.settings.watermark.position" style="width: 100%">
            <el-option label="右下" value="bottom-right" />
            <el-option label="左下" value="bottom-left" />
            <el-option label="右上" value="top-right" />
            <el-option label="左上" value="top-left" />
            <el-option label="居中" value="center" />
          </el-select>
        </el-form-item>

        <el-form-item label="透明度">
          <el-slider v-model="profileForm.settings.watermark.opacity" :min="0.1" :max="1" :step="0.05" show-input />
        </el-form-item>

        <el-divider>Google Sheet</el-divider>

        <el-form-item label="Spreadsheet ID">
          <el-input v-model="profileForm.settings.googleSheet.spreadsheetId" placeholder="Google Sheet ID" />
        </el-form-item>

        <el-form-item label="Worksheet Name">
          <el-input v-model="profileForm.settings.googleSheet.worksheetName" placeholder="Sheet1" />
        </el-form-item>

        <el-divider>CSV / Import 默认值</el-divider>

        <el-form-item label="默认链接">
          <el-input v-model="profileForm.settings.socialImport.defaultLink" placeholder="https://example.com" />
        </el-form-item>

        <el-form-item label="Category">
          <el-input v-model="profileForm.settings.socialImport.category" placeholder="可选" />
        </el-form-item>

        <el-form-item label="Watermark 名称">
          <el-input v-model="profileForm.settings.socialImport.watermarkName" placeholder="例如：Default" />
        </el-form-item>

        <el-form-item label="Hashtag Group">
          <el-input v-model="profileForm.settings.socialImport.hashtagGroup" placeholder="调度器内已存在的 Hashtag Group 名称" />
        </el-form-item>

        <el-form-item label="CTA Group">
          <el-input v-model="profileForm.settings.socialImport.ctaGroup" placeholder="调度器内已存在的 CTA Group 名称" />
        </el-form-item>

        <el-form-item label="First Comment">
          <el-input v-model="profileForm.settings.socialImport.firstComment" placeholder="Facebook / Instagram / LinkedIn / Bluesky / Threads 首评" />
        </el-form-item>

        <el-form-item label="Story">
          <el-switch v-model="profileForm.settings.socialImport.story" />
        </el-form-item>

        <el-form-item label="Pinterest Board">
          <el-input v-model="profileForm.settings.socialImport.pinterestBoard" placeholder="可选" />
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
            {{ isSubmitting ? '保存中' : '保存' }}
          </el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog
      v-model="generateDialogVisible"
      title="生成内容并写入 Google Sheet"
      width="900px"
      class="generate-dialog"
    >
      <el-form :model="generateForm" label-width="140px">
        <el-form-item label="Profile">
          <el-input :model-value="currentProfile?.name || ''" disabled />
        </el-form-item>

        <el-form-item label="素材">
          <el-select v-model="generateForm.materialId" filterable placeholder="选择素材" style="width: 100%">
            <el-option
              v-for="material in materials"
              :key="material.id"
              :label="material.filename"
              :value="material.id"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="导流链接">
          <el-input v-model="generateForm.link" placeholder="选填，覆盖 profile 默认链接" />
        </el-form-item>

        <el-form-item label="排程时间">
          <el-date-picker
            v-model="generateForm.scheduleAt"
            type="datetime"
            value-format="YYYY-MM-DDTHH:mm:ss"
            placeholder="选填"
            style="width: 100%"
          />
        </el-form-item>

        <el-form-item label="写入 Google Sheet">
          <el-switch v-model="generateForm.writeToSheet" />
        </el-form-item>
      </el-form>

      <div v-if="generationResult" class="generation-result">
        <el-alert
          :title="generationResult.sheetResult ? `已写入 ${generationResult.sheetResult.appended} 行` : '仅生成，未写入 Sheet'"
          type="success"
          :closable="false"
          show-icon
        />

        <div class="result-block">
          <h3>媒体 URL</h3>
          <el-link :href="generationResult.storage?.publicUrl" target="_blank" type="primary">
            {{ generationResult.storage?.publicUrl }}
          </el-link>
        </div>

        <div class="result-block">
          <h3>Transcript</h3>
          <el-input :model-value="generationResult.transcript" type="textarea" :rows="8" readonly />
        </div>

        <div class="post-grid">
          <div v-for="(label, key) in postLabels" :key="key" class="post-card">
            <h4>{{ label }}</h4>
            <el-input :model-value="generationResult.posts?.[key] || ''" type="textarea" :rows="6" readonly />
          </div>
        </div>
      </div>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="generateDialogVisible = false">关闭</el-button>
          <el-button type="primary" @click="submitGeneration" :loading="isGenerating">
            {{ isGenerating ? '生成中' : '开始生成' }}
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
const generationResult = ref(null)

const postLabels = {
  twitter: 'X / Twitter',
  threads: 'Threads',
  instagram: 'Instagram',
  facebook: 'Facebook',
  youtube: 'YouTube',
  tiktok: 'TikTok',
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
  materialId: null,
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

const getAccountName = (accountId) => {
  const account = accountStore.accounts.find(item => item.id === accountId)
  return account ? `${account.name} (${account.platform})` : accountId
}

const fetchProfiles = async () => {
  isRefreshing.value = true
  try {
    const response = await profileApi.getProfiles()
    profiles.value = response.data || []
  } catch (error) {
    ElMessage.error('获取 Profile 列表失败')
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
    ElMessage.error('获取账号列表失败')
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
    ElMessage.error('获取素材列表失败')
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
    ElMessage.warning('请输入 Profile 名称')
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
    ElMessage.success('Profile 保存成功')
  } catch (error) {
    ElMessage.error(error.message || 'Profile 保存失败')
  } finally {
    isSubmitting.value = false
  }
}

const handleDelete = (profile) => {
  ElMessageBox.confirm(
    `确定要删除 Profile ${profile.name} 吗？`,
    '警告',
    {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning'
    }
  ).then(async () => {
    try {
      await profileApi.deleteProfile(profile.id)
      profiles.value = profiles.value.filter(item => item.id !== profile.id)
      ElMessage.success('删除成功')
    } catch (error) {
      ElMessage.error('删除失败')
    }
  }).catch(() => {})
}

const openGenerateDialog = async (profile) => {
  currentProfile.value = profile
  generationResult.value = null
  generateForm.value = {
    materialId: null,
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

  if (!generateForm.value.materialId) {
    ElMessage.warning('请选择素材')
    return
  }

  isGenerating.value = true
  try {
    const response = await profileApi.generateContent({
      profileId: currentProfile.value.id,
      materialId: generateForm.value.materialId,
      link: generateForm.value.link,
      scheduleAt: generateForm.value.scheduleAt,
      writeToSheet: generateForm.value.writeToSheet
    })
    generationResult.value = response.data
    ElMessage.success('内容生成完成')
  } catch (error) {
    ElMessage.error(error.message || '生成失败')
  } finally {
    isGenerating.value = false
  }
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
