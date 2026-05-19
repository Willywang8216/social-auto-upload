<template>
  <el-card class="profile-card" shadow="hover">
    <template #header>
      <div class="card-header">
        <div class="header-info">
          <h3 class="profile-name">{{ profile.name }}</h3>
          <p v-if="profile.description" class="profile-desc">{{ profile.description }}</p>
        </div>
        <div class="header-actions">
          <el-button size="small" type="primary" text @click="$emit('edit', profile)">
            <el-icon><Edit /></el-icon>
          </el-button>
          <el-button size="small" type="danger" text @click="$emit('delete', profile)">
            <el-icon><Delete /></el-icon>
          </el-button>
        </div>
      </div>
    </template>

    <el-collapse v-model="activeSections">
      <!-- Settings Section -->
      <el-collapse-item title="基本設定" name="settings">
        <div class="section-content">
          <div v-if="settings.systemPrompt" class="setting-row">
            <label>System Prompt</label>
            <pre class="setting-pre">{{ settings.systemPrompt }}</pre>
          </div>
          <div v-if="settings.contactDetails" class="setting-row">
            <label>聯絡資訊</label>
            <span>{{ settings.contactDetails }}</span>
          </div>
          <div v-if="settings.ctaText" class="setting-row">
            <label>CTA</label>
            <span>{{ settings.ctaText }}</span>
          </div>
          <el-empty v-if="!settings.systemPrompt && !settings.contactDetails && !settings.ctaText" description="無設定" :image-size="40" />
        </div>
      </el-collapse-item>

      <!-- Watermark Section -->
      <el-collapse-item title="浮水印設定" name="watermark">
        <div class="section-content watermark-section">
          <div class="setting-row">
            <label>文字</label>
            <span>{{ watermark.text || '—' }}</span>
          </div>
          <div class="setting-row">
            <label>樣式</label>
            <el-tag size="small">{{ watermarkStyleLabel }}</el-tag>
          </div>
          <div v-if="watermark.style === 'static'" class="setting-row">
            <label>位置</label>
            <span>{{ watermark.position || 'random' }}</span>
          </div>
          <div v-if="watermark.style === 'slanted'" class="setting-row">
            <label>角度</label>
            <span>{{ watermark.angle ?? -30 }}°</span>
          </div>
          <div class="setting-row">
            <label>透明度</label>
            <span>{{ Math.round((watermark.opacity ?? 0.5) * 100) }}%</span>
          </div>
          <div class="setting-row">
            <label>顏色</label>
            <span class="color-preview" :style="{ backgroundColor: watermark.color || 'white' }"></span>
            <span>{{ watermark.color || 'white' }}</span>
          </div>
          <el-empty v-if="!watermark.text" description="未設定浮水印" :image-size="40" />
        </div>
      </el-collapse-item>

      <!-- AI Services Section -->
      <el-collapse-item title="AI 服務" name="aiServices">
        <div class="section-content">
          <div v-if="aiServices.length > 0">
            <div v-for="(svc, idx) in aiServices" :key="idx" class="ai-service-row">
              <div class="ai-service-info">
                <strong>{{ svc.name || '服務 ' + (idx + 1) }}</strong>
                <span class="ai-service-detail">{{ svc.apiBaseUrl }}</span>
                <el-tag size="small" type="info">{{ svc.model || 'gpt-4.1-mini' }}</el-tag>
              </div>
            </div>
          </div>
          <el-empty v-else description="使用全域設定 (SAU_LLM_API_BASE_URL)" :image-size="40" />
        </div>
      </el-collapse-item>

      <!-- Intro/Outro Section -->
      <el-collapse-item title="片頭 / 片尾" name="intros">
        <div class="section-content">
          <div class="intros-outros-grid">
            <div class="intro-outro-block">
              <label>片頭 (Intro)</label>
              <div class="media-chips">
                <el-tag
                  v-for="item in introItems"
                  :key="item.id"
                  closable
                  size="small"
                  @close="$emit('remove-intro', item.id)"
                >
                  {{ item.filename }}
                </el-tag>
                <el-tag v-if="introItems.length === 0" type="info" size="small">無</el-tag>
              </div>
            </div>
            <div class="intro-outro-block">
              <label>片尾 (Outro)</label>
              <div class="media-chips">
                <el-tag
                  v-for="item in outroItems"
                  :key="item.id"
                  closable
                  size="small"
                  @close="$emit('remove-outro', item.id)"
                >
                  {{ item.filename }}
                </el-tag>
                <el-tag v-if="outroItems.length === 0" type="info" size="small">無</el-tag>
              </div>
            </div>
          </div>
          <el-button size="small" type="primary" text @click="$emit('edit-intros')">
            <el-icon><Plus /></el-icon> 編輯片頭/片尾
          </el-button>
        </div>
      </el-collapse-item>

      <!-- Accounts Section -->
      <el-collapse-item title="帳號列表" name="accounts">
        <div class="section-content">
          <div class="accounts-toolbar">
            <el-input
              v-model="searchQuery"
              placeholder="搜尋帳號..."
              size="small"
              clearable
              prefix-icon="Search"
            />
            <div class="platform-filters">
              <el-tag
                v-for="p in availablePlatforms"
                :key="p"
                size="small"
                :type="activePlatformFilters.has(p) ? '' : 'info'"
                :effect="activePlatformFilters.has(p) ? 'dark' : 'plain'"
                class="filter-chip"
                @click="togglePlatformFilter(p)"
              >
                {{ p }}
              </el-tag>
            </div>
          </div>

          <div
            class="accounts-list"
            @dragover.prevent="onDragOver"
            @dragleave="onDragLeave"
            @drop="onDrop"
            :class="{ 'drag-over': isDragOver }"
          >
            <div
              v-for="account in filteredAccounts"
              :key="account.id"
              class="account-row"
              draggable="true"
              @dragstart="onDragStart($event, account)"
              @click="emit('account-click', account)"
            >
              <span class="account-platform">
                <el-tag size="small" :type="platformTagType(account.platformSlug)">
                  {{ account.platform }}
                </el-tag>
              </span>
              <span class="account-name">{{ account.accountName || account.name }}</span>
              <span class="account-detail">{{ account.connectionDetail || '' }}</span>
              <el-tag
                size="small"
                :type="account.connectionTagType === 'success' ? 'success' : account.connectionTagType === 'warning' ? 'warning' : 'danger'"
              >
                {{ account.connectionLabel || account.status }}
              </el-tag>
              <el-button
                size="small"
                type="danger"
                text
                class="account-delete-btn"
                @click.stop="emit('account-delete', account)"
              >
                <el-icon><Delete /></el-icon>
              </el-button>
            </div>
            <el-empty v-if="filteredAccounts.length === 0" description="無符合條件的帳號" :image-size="40" />
          </div>
          <el-button size="small" type="primary" text @click="emit('add-account', profile.id)" style="margin-top: 8px;">
            <el-icon><Plus /></el-icon> 新增帳號
          </el-button>
        </div>
      </el-collapse-item>
    </el-collapse>
  </el-card>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { Edit, Delete, Plus } from '@element-plus/icons-vue'

const props = defineProps({
  profile: { type: Object, required: true },
  accounts: { type: Array, default: () => [] },
  materials: { type: Array, default: () => [] }
})

const emit = defineEmits(['edit', 'delete', 'account-drop', 'account-click', 'account-delete', 'add-account', 'edit-intros', 'remove-intro', 'remove-outro'])

const activeSections = ref(['accounts'])
const searchQuery = ref('')
const activePlatformFilters = ref(new Set())
const isDragOver = ref(false)
let dragCounter = 0

const settings = computed(() => props.profile.settings || {})
const watermark = computed(() => {
  const wm = settings.value.watermark
  if (typeof wm === 'string') return { text: wm, style: 'static' }
  return wm || {}
})

const watermarkStyleLabel = computed(() => {
  const map = { static: '靜態', moving: '動態移動', slanted: '傾斜', repeated: '重複鋪滿' }
  return map[watermark.value.style] || '靜態'
})

const aiServices = computed(() => settings.value.aiServices || [])

const introIds = computed(() => settings.value.intros || [])
const outroIds = computed(() => settings.value.outros || [])

const introItems = computed(() =>
  introIds.value.map(id => props.materials.find(m => m.id === id)).filter(Boolean)
)
const outroItems = computed(() =>
  outroIds.value.map(id => props.materials.find(m => m.id === id)).filter(Boolean)
)

const availablePlatforms = computed(() => {
  const set = new Set()
  for (const acc of props.accounts) {
    if (acc.platform) set.add(acc.platform)
  }
  return [...set].sort()
})

const filteredAccounts = computed(() => {
  const q = searchQuery.value.toLowerCase().trim()
  return props.accounts.filter(acc => {
    if (activePlatformFilters.value.size > 0 && !activePlatformFilters.value.has(acc.platform)) return false
    if (!q) return true
    const name = (acc.accountName || acc.name || '').toLowerCase()
    const detail = (acc.connectionDetail || '').toLowerCase()
    const platform = (acc.platform || '').toLowerCase()
    return name.includes(q) || detail.includes(q) || platform.includes(q)
  })
})

function togglePlatformFilter(platform) {
  const s = new Set(activePlatformFilters.value)
  if (s.has(platform)) s.delete(platform)
  else s.add(platform)
  activePlatformFilters.value = s
}

function platformTagType(slug) {
  const map = {
    facebook: '', instagram: 'danger', threads: 'info', twitter: '',
    reddit: 'warning', youtube: 'danger', tiktok: '', telegram: '',
    discord: 'info', douyin: '', kuaishou: '', tencent: '',
    xiaohongshu: 'warning', bilibili: '', baijiahao: '', medium: '', substack: ''
  }
  return map[slug] || ''
}

function onDragStart(event, account) {
  event.dataTransfer.setData('application/json', JSON.stringify({
    accountId: account.id,
    sourceProfileId: props.profile.id
  }))
  event.dataTransfer.effectAllowed = 'move'
}

function onDragOver(event) {
  event.dataTransfer.dropEffect = 'move'
  dragCounter++
  isDragOver.value = true
}

function onDragLeave() {
  dragCounter--
  if (dragCounter <= 0) {
    isDragOver.value = false
    dragCounter = 0
  }
}

function onDrop(event) {
  isDragOver.value = false
  dragCounter = 0
  try {
    const data = JSON.parse(event.dataTransfer.getData('application/json'))
    if (data.accountId && data.sourceProfileId !== props.profile.id) {
      emit('account-drop', data.accountId, data.sourceProfileId, props.profile.id)
    }
  } catch {}
}
</script>

<style scoped>
.profile-card {
  margin-bottom: 16px;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
}

.header-info {
  flex: 1;
  min-width: 0;
}

.profile-name {
  margin: 0 0 4px 0;
  font-size: 16px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.profile-desc {
  margin: 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.header-actions {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}

.section-content {
  padding: 4px 0;
}

.setting-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 8px;
}

.setting-row label {
  font-weight: 500;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  flex-shrink: 0;
  min-width: 80px;
}

.setting-pre {
  margin: 0;
  padding: 8px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
  font-size: 12px;
  max-height: 120px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-all;
  flex: 1;
}

.color-preview {
  display: inline-block;
  width: 16px;
  height: 16px;
  border-radius: 3px;
  border: 1px solid var(--el-border-color);
  vertical-align: middle;
}

.intros-outros-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-bottom: 8px;
}

.intro-outro-block label {
  display: block;
  font-weight: 500;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin-bottom: 4px;
}

.media-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.accounts-toolbar {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 8px;
}

.platform-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.filter-chip {
  cursor: pointer;
  user-select: none;
}

.accounts-list {
  border: 2px dashed transparent;
  border-radius: 6px;
  transition: border-color 0.2s;
  min-height: 60px;
}

.accounts-list.drag-over {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}

.account-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  cursor: grab;
}

.account-row:last-child {
  border-bottom: none;
}

.account-row:active {
  cursor: grabbing;
}

.account-name {
  flex: 1;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.account-detail {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 120px;
}

.account-delete-btn {
  opacity: 0;
  transition: opacity 0.2s;
}

.account-row:hover .account-delete-btn {
  opacity: 1;
}

.ai-service-row {
  padding: 8px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.ai-service-row:last-child {
  border-bottom: none;
}

.ai-service-info {
  display: flex;
  align-items: center;
  gap: 8px;
}

.ai-service-detail {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 200px;
}
</style>
