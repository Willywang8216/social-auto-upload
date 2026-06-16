<template>
  <div class="tiktok-publish">
    <el-card>
      <template #header>
        <div class="card-header">
          <h2>Post to TikTok</h2>
          <el-tag v-if="creatorInfo" type="success">
            Connected as: {{ creatorInfo.creator_nickname }}
          </el-tag>
        </div>
      </template>

      <!-- Account Selector -->
      <el-form-item label="Select TikTok Account" required>
        <el-select
          v-model="selectedAccountId"
          placeholder="Choose a TikTok account"
          style="width: 100%"
          @change="onAccountChange"
        >
          <el-option
            v-for="acc in tiktokAccounts"
            :key="acc.id"
            :label="acc.account_name + ' (' + acc.display_name + ')'"
            :value="acc.id"
          />
        </el-select>
      </el-form-item>

      <el-alert v-if="tiktokAccounts.length === 0 && !loadingAccounts" type="warning" :closable="false">
        No TikTok accounts found. Please add a TikTok account in Account Management first.
      </el-alert>

      <!-- Creator Info Display -->
      <el-alert v-if="!creatorInfo && !loading" type="warning" :closable="false">
        Loading creator info...
      </el-alert>

      <el-alert v-if="creatorInfo && !creatorInfo.can_post" type="error" :closable="false">
        You cannot make more posts at this moment. Please try again later.
      </el-alert>

      <div v-if="creatorInfo" class="creator-info">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="Account">
            {{ creatorInfo.creator_nickname }}
          </el-descriptions-item>
          <el-descriptions-item label="Can Post">
            <el-tag :type="creatorInfo.can_post ? 'success' : 'danger'">
              {{ creatorInfo.can_post ? 'Yes' : 'No' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="Max Video Duration">
            {{ creatorInfo.max_video_post_duration_sec }}s
          </el-descriptions-item>
          <el-descriptions-item label="Privacy Options">
            {{ (creatorInfo.privacy_level_options || []).join(', ') }}
          </el-descriptions-item>
        </el-descriptions>
      </div>

      <!-- Content Preview -->
      <div v-if="videoUrl || imageUrl" class="preview-section">
        <h3>Content Preview</h3>
        <div class="preview-container">
          <video v-if="videoUrl" :src="videoUrl" controls class="preview-video" />
          <img v-else-if="imageUrl" :src="imageUrl" class="preview-image" />
        </div>
        <p class="preview-note">
          This is a preview. Content may take a few minutes to process and appear on your TikTok profile after publishing.
        </p>
      </div>

      <!-- Title -->
      <el-form label-position="top" class="publish-form">
        <el-form-item label="Title" required>
          <el-input
            v-model="form.title"
            placeholder="Enter a title for your post"
            maxlength="150"
            show-word-limit
          />
        </el-form-item>

        <!-- Privacy Status -->
        <el-form-item label="Privacy Status" required>
          <el-select
            v-model="form.privacy_level"
            placeholder="Select privacy status"
            style="width: 100%"
          >
            <el-option
              v-for="option in privacyOptions"
              :key="option"
              :label="formatPrivacyLabel(option)"
              :value="option"
              :disabled="option === 'PRIVATE_TO_SELF' && brandedContentSelected"
            />
          </el-select>
          <div v-if="brandedContentSelected && form.privacy_level === 'PRIVATE_TO_SELF'" class="form-hint error">
            Branded content visibility cannot be set to private.
          </div>
        </el-form-item>

        <!-- Interaction Abilities -->
        <el-form-item label="Interaction Settings">
          <div class="interaction-toggles">
            <el-checkbox
              v-model="form.allow_comment"
              :disabled="!creatorInfo?.comment_disabled === false"
            >
              Allow Comment
              <el-tag v-if="creatorInfo?.comment_disabled" type="info" size="small">Disabled in settings</el-tag>
            </el-checkbox>
            <el-checkbox
              v-if="!isPhotoPost"
              v-model="form.allow_duet"
              :disabled="!creatorInfo?.duet_disabled === false"
            >
              Allow Duet
              <el-tag v-if="creatorInfo?.duet_disabled" type="info" size="small">Disabled in settings</el-tag>
            </el-checkbox>
            <el-checkbox
              v-if="!isPhotoPost"
              v-model="form.allow_stitch"
              :disabled="!creatorInfo?.stitch_disabled === false"
            >
              Allow Stitch
              <el-tag v-if="creatorInfo?.stitch_disabled" type="info" size="small">Disabled in settings</el-tag>
            </el-checkbox>
          </div>
          <p class="form-hint">All interactions are off by default. Toggle on to enable.</p>
        </el-form-item>

        <!-- Commercial Content Disclosure -->
        <el-form-item label="Commercial Content Disclosure">
          <el-switch v-model="form.brand_content_toggle" />
          <span class="toggle-label">Indicate if this content promotes yourself, a brand, product or service</span>

          <div v-if="form.brand_content_toggle" class="brand-options">
            <el-checkbox v-model="form.brand_organics">
              <strong>Your Brand</strong> — Promoting yourself or your own business
              <div v-if="form.brand_organics" class="brand-prompt">
                Your {{ isPhotoPost ? 'photo' : 'video' }} will be labeled as "Promotional content"
              </div>
            </el-checkbox>
            <el-checkbox v-model="form.branded_content">
              <strong>Branded Content</strong> — Promoting another brand or third party
              <div v-if="form.branded_content" class="brand-prompt">
                Your {{ isPhotoPost ? 'photo' : 'video' }} will be labeled as "Paid partnership"
              </div>
            </el-checkbox>
            <div v-if="form.brand_content_toggle && !form.brand_organics && !form.branded_content" class="brand-warning">
              You need to indicate if your content promotes yourself, a third party, or both.
            </div>
          </div>
        </el-form-item>

        <!-- Consent Declaration -->
        <el-form-item>
          <el-checkbox v-model="consentGiven" class="consent-checkbox">
            {{ consentText }}
          </el-checkbox>
        </el-form-item>

        <!-- Publish Button -->
        <el-form-item>
          <el-button
            type="primary"
            size="large"
            :disabled="!canPublish"
            :loading="publishing"
            @click="handlePublish"
          >
            {{ publishing ? 'Publishing...' : 'Publish to TikTok' }}
          </el-button>
          <el-button v-if="!canPublish" type="info" plain disabled>
            Please fill all required fields
          </el-button>
        </el-form-item>
      </el-form>

      <!-- Publish Status -->
      <div v-if="publishStatus" class="publish-status">
        <el-alert
          :type="statusType"
          :title="statusTitle"
          :description="statusDescription"
          :closable="false"
          show-icon
        />
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { ElMessage } from 'element-plus'

const props = defineProps({
  videoUrl: { type: String, default: '' },
  imageUrl: { type: String, default: '' },
  isPhotoPost: { type: Boolean, default: false },
  accountId: { type: Number, default: null },
})

const emit = defineEmits(['published', 'error'])

const creatorInfo = ref(null)
const loading = ref(true)
const loadingAccounts = ref(true)
const publishing = ref(false)
const consentGiven = ref(false)
const publishStatus = ref(null)
const tiktokAccounts = ref([])
const selectedAccountId = ref(null)

const form = ref({
  title: '',
  privacy_level: '',  // No default - user must select
  allow_comment: false,
  allow_duet: false,
  allow_stitch: false,
  brand_content_toggle: false,
  brand_organics: false,
  branded_content: false,
})

const privacyOptions = computed(() => {
  return creatorInfo.value?.privacy_level_options || ['PUBLIC_TO_EVERYONE', 'MUTUAL_FOLLOW_FRIENDS', 'SELF_ONLY']
})

const brandedContentSelected = computed(() => {
  return form.value.brand_content_toggle && form.value.branded_content
})

const consentText = computed(() => {
  if (form.value.brand_content_toggle && form.value.branded_content) {
    return 'By posting, you agree to TikTok\'s Branded Content Policy and Music Usage Confirmation.'
  }
  return 'By posting, you agree to TikTok\'s Music Usage Confirmation.'
})

const canPublish = computed(() => {
  if (!creatorInfo.value?.can_post) return false
  if (!form.value.title.trim()) return false
  if (!form.value.privacy_level) return false
  if (!consentGiven.value) return false
  if (form.value.brand_content_toggle && !form.value.brand_organics && !form.value.branded_content) return false
  return true
})

const statusType = computed(() => {
  if (!publishStatus.value) return 'info'
  const s = publishStatus.value.status
  if (s === 'SUCCESS') return 'success'
  if (s === 'FAILED') return 'error'
  return 'info'
})

const statusTitle = computed(() => {
  if (!publishStatus.value) return ''
  const s = publishStatus.value.status
  if (s === 'SUCCESS') return 'Published Successfully!'
  if (s === 'FAILED') return 'Publishing Failed'
  return 'Processing...'
})

const statusDescription = computed(() => {
  if (!publishStatus.value) return ''
  return publishStatus.value.status === 'SUCCESS'
    ? 'Your content has been published to TikTok. It may take a few minutes to appear on your profile.'
    : publishStatus.value.fail_reason || 'Your content is being processed...'
})

function formatPrivacyLabel(option) {
  const labels = {
    'PUBLIC_TO_EVERYONE': 'Public',
    'MUTUAL_FOLLOW_FRIENDS': 'Friends',
    'SELF_ONLY': 'Only Me',
  }
  return labels[option] || option
}

// Watch for branded content + private privacy conflict
watch(() => form.value.branded_content, (val) => {
  if (val && form.value.privacy_level === 'PRIVATE_TO_SELF') {
    form.value.privacy_level = ''
    ElMessage.warning('Branded content visibility cannot be set to private. Please select a different privacy option.')
  }
})

onMounted(async () => {
  await loadTikTokAccounts()
})

async function loadTikTokAccounts() {
  loadingAccounts.value = true
  try {
    const { http } = await import('@/utils/request')
    const res = await http.get('/getAccounts')
    const accounts = res.data || res
    tiktokAccounts.value = (Array.isArray(accounts) ? accounts : []).filter(
      a => a.platform === 'tiktok'
    )
    // Auto-select if only one account
    if (tiktokAccounts.value.length === 1) {
      selectedAccountId.value = tiktokAccounts.value[0].id
      await fetchCreatorInfo()
    }
  } catch (e) {
    console.error('Failed to load accounts:', e)
  } finally {
    loadingAccounts.value = false
  }
}

async function onAccountChange(accountId) {
  if (accountId) {
    await fetchCreatorInfo()
  } else {
    creatorInfo.value = null
  }
}

async function fetchCreatorInfo() {
  if (!selectedAccountId.value) return
  loading.value = true
  try {
    const { http } = await import('@/utils/request')
    const res = await http.get(`/tiktok/creator-info/${selectedAccountId.value}`)
    const data = res.data || res
    creatorInfo.value = data

    // Check if creator can post
    if (!data.can_post) {
      ElMessage.error('You cannot make more posts at this moment. Please try again later.')
    }
  } catch (e) {
    ElMessage.error('Failed to load creator info: ' + (e.message || e))
    creatorInfo.value = null
  } finally {
    loading.value = false
  }
}

async function handlePublish() {
  if (!canPublish.value) return

  // Validate video duration if applicable
  if (props.videoUrl && creatorInfo.value?.max_video_post_duration_sec) {
    // Duration check would happen server-side
  }

  publishing.value = true
  publishStatus.value = { status: 'PROCESSING' }

  try {
    const { http } = await import('@/utils/request')
    const payload = {
      account_id: selectedAccountId.value,
      title: form.value.title,
      privacy_level: form.value.privacy_level,
      allow_comment: form.value.allow_comment,
      allow_duet: form.value.allow_duet,
      allow_stitch: form.value.allow_stitch,
      brand_content_toggle: form.value.brand_content_toggle,
      brand_organics: form.value.brand_organics,
      branded_content: form.value.branded_content,
      video_url: props.videoUrl,
      image_url: props.imageUrl,
    }

    const res = await http.post('/tiktok/publish', payload)
    const data = res.data || res

    publishStatus.value = data
    emit('published', data)

    // Poll for status if needed
    if (data.publish_id) {
      pollPublishStatus(data.publish_id)
    }
  } catch (e) {
    publishStatus.value = { status: 'FAILED', fail_reason: e.message || 'Unknown error' }
    emit('error', e)
  } finally {
    publishing.value = false
  }
}

async function pollPublishStatus(publishId) {
  const maxAttempts = 30
  const interval = 5000  // 5 seconds

  for (let i = 0; i < maxAttempts; i++) {
    await new Promise(resolve => setTimeout(resolve, interval))

    try {
      const { http } = await import('@/utils/request')
      const res = await http.get(`/tiktok/publish-status/${publishId}`)
      const data = res.data || res

      publishStatus.value = data

      if (data.status === 'SUCCESS' || data.status === 'FAILED') {
        return
      }
    } catch (e) {
      console.error('Status poll error:', e)
    }
  }
}
</script>

<style scoped>
.tiktok-publish {
  max-width: 800px;
  margin: 0 auto;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.creator-info {
  margin-bottom: 24px;
}
.preview-section {
  margin-bottom: 24px;
}
.preview-container {
  max-width: 400px;
  margin: 12px 0;
}
.preview-video, .preview-image {
  width: 100%;
  border-radius: 8px;
}
.preview-note {
  font-size: 13px;
  color: #909399;
  margin-top: 8px;
}
.publish-form {
  margin-top: 20px;
}
.interaction-toggles {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.form-hint {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
}
.form-hint.error {
  color: #f56c6c;
}
.toggle-label {
  margin-left: 8px;
  font-size: 14px;
}
.brand-options {
  margin-top: 12px;
  padding: 16px;
  background: #f5f7fa;
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.brand-prompt {
  font-size: 12px;
  color: #409eff;
  margin-top: 4px;
}
.brand-warning {
  font-size: 12px;
  color: #e6a23c;
  padding: 8px;
  background: #fdf6ec;
  border-radius: 4px;
}
.consent-checkbox {
  font-size: 14px;
}
.publish-status {
  margin-top: 20px;
}
</style>
