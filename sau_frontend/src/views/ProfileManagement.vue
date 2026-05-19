<template>
  <div class="profile-management">
    <div class="page-header">
      <h2>Profile 管理</h2>
      <el-button type="primary" @click="openCreateDialog">
        <el-icon><Plus /></el-icon> 新增 Profile
      </el-button>
    </div>

    <div v-if="loading" class="loading-state">
      <el-skeleton :rows="3" animated />
    </div>

    <div v-else-if="profiles.length === 0" class="empty-state">
      <el-empty description="尚無 Profile，點擊上方按鈕建立">
        <el-button type="primary" @click="openCreateDialog">新增 Profile</el-button>
      </el-empty>
    </div>

    <div v-else class="profiles-grid">
      <ProfileCard
        v-for="profile in profiles"
        :key="profile.id"
        :profile="profile"
        :accounts="getAccountsForProfile(profile.id)"
        :materials="materials"
        @edit="openEditDialog"
        @delete="handleDelete"
        @account-drop="handleAccountDrop"
        @account-click="handleAccountClick"
        @account-delete="handleAccountDelete"
        @add-account="handleAddAccount"
        @edit-intros="openIntroOutroPicker(profile)"
        @remove-intro="(id) => removeIntroOutro(profile, 'intros', id)"
        @remove-outro="(id) => removeIntroOutro(profile, 'outros', id)"
      />
    </div>

    <!-- Create/Edit Profile Dialog -->
    <el-dialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? '新增 Profile' : '編輯 Profile'"
      width="600px"
      destroy-on-close
    >
      <el-form :model="form" label-position="top">
        <el-form-item label="名稱" required>
          <el-input v-model="form.name" placeholder="Profile 名稱" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="2" placeholder="選填描述" />
        </el-form-item>
        <el-form-item label="System Prompt">
          <el-input v-model="form.systemPrompt" type="textarea" :rows="4" placeholder="AI 系統提示詞" />
        </el-form-item>
        <el-divider content-position="left">浮水印設定</el-divider>
        <el-form-item label="浮水印文字">
          <el-input v-model="form.watermarkText" placeholder="浮水印文字" />
        </el-form-item>
        <el-row :gutter="16">
          <el-col :span="8">
            <el-form-item label="樣式">
              <el-select v-model="form.watermarkStyle" style="width: 100%">
                <el-option label="靜態" value="static" />
                <el-option label="動態移動" value="moving" />
                <el-option label="傾斜" value="slanted" />
                <el-option label="重複鋪滿" value="repeated" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item v-if="form.watermarkStyle === 'static'" label="位置">
              <el-select v-model="form.watermarkPosition" style="width: 100%">
                <el-option label="隨機" value="random" />
                <el-option label="置中" value="center" />
                <el-option label="左上" value="top-left" />
                <el-option label="右上" value="top-right" />
                <el-option label="左下" value="bottom-left" />
                <el-option label="右下" value="bottom-right" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="form.watermarkStyle === 'slanted'" label="角度">
              <el-input-number v-model="form.watermarkAngle" :min="-90" :max="90" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="透明度">
              <el-slider v-model="form.watermarkOpacity" :min="10" :max="100" :step="5" show-input size="small" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="16">
          <el-col :span="8">
            <el-form-item label="字體大小">
              <el-input-number v-model="form.watermarkFontSize" :min="12" :max="120" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="顏色">
              <el-select v-model="form.watermarkColor" style="width: 100%">
                <el-option label="白色" value="white" />
                <el-option label="黑色" value="black" />
                <el-option label="紅色" value="red" />
                <el-option label="綠色" value="green" />
                <el-option label="藍色" value="blue" />
                <el-option label="黃色" value="yellow" />
                <el-option label="灰色" value="gray" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-divider content-position="left">AI 服務</el-divider>
        <p class="form-hint">可指定多組 AI 服務，發佈時會優先使用此設定而非全域環境變數</p>
        <div v-for="(svc, idx) in form.aiServices" :key="idx" class="ai-service-form-row">
          <el-row :gutter="12">
            <el-col :span="6">
              <el-form-item label="名稱" size="small">
                <el-input v-model="svc.name" placeholder="OpenAI" />
              </el-form-item>
            </el-col>
            <el-col :span="9">
              <el-form-item label="API Base URL" size="small">
                <el-input v-model="svc.apiBaseUrl" placeholder="https://api.openai.com/v1" />
              </el-form-item>
            </el-col>
            <el-col :span="5">
              <el-form-item label="Model" size="small">
                <el-input v-model="svc.model" placeholder="gpt-4.1-mini" />
              </el-form-item>
            </el-col>
            <el-col :span="4">
              <el-form-item label=" " size="small">
                <el-button type="danger" text @click="form.aiServices.splice(idx, 1)">
                  <el-icon><Delete /></el-icon>
                </el-button>
              </el-form-item>
            </el-col>
          </el-row>
          <el-form-item label="API Key" size="small">
            <el-input v-model="svc.apiKey" type="password" show-password placeholder="sk-..." />
          </el-form-item>
        </div>
        <el-button size="small" type="primary" text @click="form.aiServices.push({ name: '', apiBaseUrl: '', apiKey: '', model: '' })">
          <el-icon><Plus /></el-icon> 新增 AI 服務
        </el-button>
        <el-divider content-position="left">其他設定</el-divider>
        <el-form-item label="聯絡資訊">
          <el-input v-model="form.contactDetails" placeholder="Email、社群連結等" />
        </el-form-item>
        <el-form-item label="CTA 文字">
          <el-input v-model="form.ctaText" placeholder="行動呼籲文字" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :disabled="!form.name.trim()" @click="submitForm">
          {{ dialogMode === 'create' ? '建立' : '更新' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- Intro/Outro Picker Dialog -->
    <el-dialog
      v-model="introOutroDialogVisible"
      title="編輯片頭 / 片尾"
      width="700px"
      destroy-on-close
    >
      <div class="intro-outro-picker">
        <div class="picker-section">
          <h4>片頭 (Intro)</h4>
          <p class="hint">選擇要在影片開頭加入的素材</p>
          <el-transfer
            v-model="selectedIntroIds"
            :data="videoMaterials"
            :titles="['可用素材', '已選片頭']"
            :props="{ key: 'id', label: 'filename' }"
            filterable
            filter-placeholder="搜尋素材"
          />
        </div>
        <el-divider />
        <div class="picker-section">
          <h4>片尾 (Outro)</h4>
          <p class="hint">選擇要在影片結尾加入的素材</p>
          <el-transfer
            v-model="selectedOutroIds"
            :data="videoMaterials"
            :titles="['可用素材', '已選片尾']"
            :props="{ key: 'id', label: 'filename' }"
            filterable
            filter-placeholder="搜尋素材"
          />
        </div>
      </div>
      <template #footer>
        <el-button @click="introOutroDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveIntroOutro">儲存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Delete } from '@element-plus/icons-vue'
import { profilesApi } from '@/api/profiles'
import { accountApi } from '@/api/account'
import { materialApi } from '@/api/material'
import { useProfilesStore } from '@/stores/profiles'
import ProfileCard from '@/components/ProfileCard.vue'

const router = useRouter()
const profilesStore = useProfilesStore()

const loading = ref(true)
const profiles = computed(() => profilesStore.profiles)
const materials = ref([])
const dialogVisible = ref(false)
const dialogMode = ref('create')
const editingProfileId = ref(null)
const introOutroDialogVisible = ref(false)
const editingProfileForIntros = ref(null)
const selectedIntroIds = ref([])
const selectedOutroIds = ref([])

const form = reactive({
  name: '',
  description: '',
  systemPrompt: '',
  watermarkText: '',
  watermarkStyle: 'static',
  watermarkPosition: 'random',
  watermarkAngle: -30,
  watermarkOpacity: 50,
  watermarkFontSize: 24,
  watermarkColor: 'white',
  aiServices: [],
  contactDetails: '',
  ctaText: ''
})

const videoMaterials = computed(() =>
  materials.value.filter(m => {
    const ext = (m.filename || '').toLowerCase()
    return ext.endsWith('.mp4') || ext.endsWith('.mov') || ext.endsWith('.avi') || ext.endsWith('.mkv') || ext.endsWith('.webm')
  })
)

function getAccountsForProfile(profileId) {
  return profilesStore.accountsByProfile[profileId] || []
}

async function loadMaterials() {
  try {
    const res = await materialApi.getAllMaterials()
    materials.value = res?.data || []
  } catch (e) {
    console.error('Failed to load materials:', e)
  }
}

async function loadProfiles() {
  loading.value = true
  try {
    await profilesStore.refreshProfiles()
    for (const p of profiles.value) {
      await profilesStore.fetchAccountsForProfile(p.id)
    }
  } catch (e) {
    ElMessage.error('載入 Profile 失敗')
    console.error(e)
  } finally {
    loading.value = false
  }
}

function openCreateDialog() {
  dialogMode.value = 'create'
  editingProfileId.value = null
  Object.assign(form, {
    name: '', description: '', systemPrompt: '',
    watermarkText: '', watermarkStyle: 'static', watermarkPosition: 'random',
    watermarkAngle: -30, watermarkOpacity: 50, watermarkFontSize: 24, watermarkColor: 'white',
    aiServices: [],
    contactDetails: '', ctaText: ''
  })
  dialogVisible.value = true
}

function openEditDialog(profile) {
  dialogMode.value = 'edit'
  editingProfileId.value = profile.id
  const settings = profile.settings || {}
  const wm = typeof settings.watermark === 'string' ? { text: settings.watermark } : (settings.watermark || {})
  Object.assign(form, {
    name: profile.name || '',
    description: profile.description || '',
    systemPrompt: settings.systemPrompt || '',
    watermarkText: wm.text || '',
    watermarkStyle: wm.style || 'static',
    watermarkPosition: wm.position || 'random',
    watermarkAngle: wm.angle ?? -30,
    watermarkOpacity: Math.round((wm.opacity ?? 0.5) * 100),
    watermarkFontSize: wm.fontSize || 24,
    watermarkColor: wm.color || 'white',
    aiServices: (settings.aiServices || []).map(s => ({ ...s })),
    contactDetails: settings.contactDetails || '',
    ctaText: settings.ctaText || ''
  })
  dialogVisible.value = true
}

async function submitForm() {
  if (!form.name.trim()) {
    ElMessage.error('請輸入 Profile 名稱')
    return
  }

  const payload = {
    name: form.name,
    description: form.description,
    settings: {
      systemPrompt: form.systemPrompt,
      watermark: {
        text: form.watermarkText,
        style: form.watermarkStyle,
        position: form.watermarkPosition,
        angle: form.watermarkAngle,
        opacity: form.watermarkOpacity / 100,
        fontSize: form.watermarkFontSize,
        color: form.watermarkColor
      },
      aiServices: form.aiServices.filter(s => s.apiBaseUrl),
      contactDetails: form.contactDetails,
      ctaText: form.ctaText
    }
  }

  try {
    if (dialogMode.value === 'edit' && editingProfileId.value) {
      // Preserve existing intros/outros
      const existing = profiles.value.find(p => p.id === editingProfileId.value)
      const existingSettings = existing?.settings || {}
      payload.settings.intros = existingSettings.intros || []
      payload.settings.outros = existingSettings.outros || []
      await profilesApi.update(editingProfileId.value, payload)
      ElMessage.success('Profile 更新成功')
    } else {
      await profilesApi.create(payload)
      ElMessage.success('Profile 建立成功')
    }
    dialogVisible.value = false
    await loadProfiles()
  } catch (e) {
    ElMessage.error(e?.message || '操作失敗')
  }
}

async function handleDelete(profile) {
  try {
    await ElMessageBox.confirm(
      `確定要刪除 Profile「${profile.name}」嗎？此操作會一併移除所有關聯帳號的 Profile 指派。`,
      '確認刪除',
      { type: 'warning', confirmButtonText: '刪除', cancelButtonText: '取消' }
    )
    await profilesStore.deleteProfile(profile.id)
    ElMessage.success('Profile 已刪除')
  } catch (e) {
    if (e !== 'cancel') {
      ElMessage.error(e?.message || '刪除失敗')
    }
  }
}

async function handleAccountDrop(accountId, sourceProfileId, targetProfileId) {
  try {
    await profilesApi.updateAccount(accountId, { profileId: targetProfileId })
    ElMessage.success('帳號已移動')
    await profilesStore.fetchAccountsForProfile(sourceProfileId)
    await profilesStore.fetchAccountsForProfile(targetProfileId)
  } catch (e) {
    ElMessage.error(e?.message || '移動帳號失敗')
  }
}

function handleAccountClick(account) {
  router.push({ path: '/account-management', query: { editAccountId: account.id } })
}

function handleAddAccount(profileId) {
  router.push({ path: '/account-management', query: { addAccountToProfile: profileId } })
}

async function handleAccountDelete(account) {
  try {
    await ElMessageBox.confirm(
      `確定要刪除帳號「${account.accountName || account.name}」嗎？`,
      '確認刪除',
      { type: 'warning', confirmButtonText: '刪除', cancelButtonText: '取消' }
    )
    await accountApi.deleteAccount(account.id)
    ElMessage.success('帳號已刪除')
    // Refresh accounts for all profiles
    for (const p of profiles.value) {
      await profilesStore.fetchAccountsForProfile(p.id)
    }
  } catch (e) {
    if (e !== 'cancel') {
      ElMessage.error(e?.message || '刪除失敗')
    }
  }
}

function openIntroOutroPicker(profile) {
  editingProfileForIntros.value = profile
  const settings = profile.settings || {}
  selectedIntroIds.value = [...(settings.intros || [])]
  selectedOutroIds.value = [...(settings.outros || [])]
  introOutroDialogVisible.value = true
}

async function saveIntroOutro() {
  const profile = editingProfileForIntros.value
  if (!profile) return

  const settings = { ...(profile.settings || {}) }
  settings.intros = [...selectedIntroIds.value]
  settings.outros = [...selectedOutroIds.value]

  try {
    await profilesApi.update(profile.id, { settings })
    ElMessage.success('片頭/片尾已更新')
    introOutroDialogVisible.value = false
    await loadProfiles()
  } catch (e) {
    ElMessage.error(e?.message || '更新失敗')
  }
}

async function removeIntroOutro(profile, field, itemId) {
  const settings = { ...(profile.settings || {}) }
  settings[field] = (settings[field] || []).filter(id => id !== itemId)
  try {
    await profilesApi.update(profile.id, { settings })
    ElMessage.success('已移除')
    await loadProfiles()
  } catch (e) {
    ElMessage.error(e?.message || '移除失敗')
  }
}

onMounted(async () => {
  await Promise.all([loadProfiles(), loadMaterials()])
})
</script>

<style scoped>
.profile-management {
  max-width: 1400px;
  margin: 0 auto;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}

.page-header h2 {
  margin: 0;
  font-size: 20px;
}

.profiles-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(520px, 1fr));
  gap: 16px;
}

.loading-state {
  padding: 40px;
}

.empty-state {
  padding: 80px 0;
}

.intro-outro-picker {
  max-height: 60vh;
  overflow-y: auto;
}

.picker-section h4 {
  margin: 0 0 4px 0;
}

.picker-section .hint {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin: 0 0 12px 0;
}

:deep(.el-transfer) {
  display: flex;
  justify-content: center;
}

:deep(.el-transfer-panel) {
  width: 260px;
}

.ai-service-form-row {
  padding: 12px;
  margin-bottom: 8px;
  background: var(--el-fill-color-lighter);
  border-radius: 6px;
}

.form-hint {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin: 0 0 12px 0;
}
</style>
