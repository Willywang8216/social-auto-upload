<template>
  <el-dialog
    :model-value="visible"
    title="確認發佈到 TikTok"
    width="680px"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    @update:model-value="$emit('update:visible', $event)"
  >
    <!-- Creator info loading -->
    <div v-if="creatorInfoLoading" class="trm-loading">
      <el-icon class="is-loading"><Loading /></el-icon>
      <span>正在載入 TikTok 創作者資訊...</span>
    </div>

    <!-- Creator info error -->
    <el-alert
      v-else-if="creatorInfo?._error"
      :title="`無法載入創作者資訊：${creatorInfo._error}`"
      type="error"
      :closable="false"
      show-icon
      style="margin-bottom: 16px;"
    />

    <!-- Creator info panel -->
    <div v-else-if="creatorInfo" class="trm-creator">
      <img
        v-if="creatorAvatarUrl || accountAvatar"
        :src="creatorAvatarUrl || accountAvatar"
        class="trm-creator-avatar"
        alt=""
        @error="(e) => e.target.style.display = 'none'"
      />
      <div class="trm-creator-info">
        <div class="trm-creator-name">{{ creatorNickname || 'TikTok Creator' }}</div>
        <div v-if="creatorUsername" class="trm-creator-handle">@{{ creatorUsername }}</div>
      </div>
      <el-tag v-if="remainingPostCount !== null" :type="postLimitReached ? 'danger' : remainingPostCount <= 2 ? 'warning' : 'success'" size="small">
        剩餘發佈次數：{{ remainingPostCount }}
      </el-tag>
    </div>

    <!-- No creator info -->
    <el-alert
      v-else
      title="無法載入 TikTok 創作者資訊，請稍後再試。"
      type="error"
      :closable="false"
      show-icon
      style="margin-bottom: 16px;"
    />

    <!-- Post limit warning -->
    <el-alert
      v-if="postLimitReached"
      title="TikTok 說此帳號目前無法發佈更多貼文，請稍後再試。"
      type="error"
      :closable="false"
      show-icon
      style="margin-bottom: 16px;"
    />

    <!-- Video preview -->
    <div v-if="mediaPreviewUrl" class="trm-preview">
      <video
        v-if="isVideo"
        :src="mediaPreviewUrl"
        controls
        preload="metadata"
        class="trm-preview-media"
      />
      <img
        v-else
        :src="mediaPreviewUrl"
        class="trm-preview-media"
        alt=""
      />
    </div>

    <!-- Title -->
    <el-form-item label="標題" required>
      <el-input
        :model-value="title"
        placeholder="輸入標題"
        :maxlength="150"
        show-word-limit
        @update:model-value="$emit('update:title', $event)"
      />
    </el-form-item>

    <!-- Privacy level -->
    <el-form-item label="隱私設定" required>
      <el-select
        :model-value="privacyLevel"
        placeholder="請選擇隱私設定"
        style="width: 100%;"
        @update:model-value="$emit('update:privacyLevel', $event)"
      >
        <el-option
          v-for="opt in privacyOptions"
          :key="opt.value"
          :label="opt.label"
          :value="opt.value"
          :disabled="opt.disabled"
        >
          <span>{{ opt.label }}</span>
          <el-tooltip v-if="opt.disabledReason" :content="opt.disabledReason" placement="right">
            <el-icon style="margin-left: 4px; color: #909399;"><InfoFilled /></el-icon>
          </el-tooltip>
        </el-option>
      </el-select>
    </el-form-item>

    <!-- Interaction settings -->
    <el-form-item label="互動設定">
      <div class="trm-interactions">
        <el-tooltip :content="commentDisabledReason" :disabled="!commentDisabledByApp" placement="top">
          <el-checkbox
            :model-value="allowComment"
            :disabled="commentDisabledByApp"
            @update:model-value="$emit('update:allowComment', $event)"
          >
            允許留言
          </el-checkbox>
        </el-tooltip>
        <el-tooltip :content="duetDisabledReason" :disabled="!duetDisabledByApp" placement="top">
          <el-checkbox
            v-if="isVideo"
            :model-value="allowDuet"
            :disabled="duetDisabledByApp"
            @update:model-value="$emit('update:allowDuet', $event)"
          >
            允許 Duet
          </el-checkbox>
        </el-tooltip>
        <el-tooltip :content="stitchDisabledReason" :disabled="!stitchDisabledByApp" placement="top">
          <el-checkbox
            v-if="isVideo"
            :model-value="allowStitch"
            :disabled="stitchDisabledByApp"
            @update:model-value="$emit('update:allowStitch', $event)"
          >
            允許 Stitch
          </el-checkbox>
        </el-tooltip>
      </div>
    </el-form-item>

    <!-- Commercial content disclosure -->
    <el-form-item>
      <template #label>
        <span>商業內容揭露</span>
        <el-tooltip content="您需要說明您的內容是推廣自己、第三方，或兩者皆是。" placement="top">
          <el-icon style="margin-left: 4px; vertical-align: middle; color: #909399;"><InfoFilled /></el-icon>
        </el-tooltip>
      </template>
      <el-switch
        :model-value="contentDisclosureEnabled"
        @update:model-value="onDisclosureToggle"
      />
    </el-form-item>

    <div v-if="contentDisclosureEnabled" class="trm-disclosure-options">
      <el-checkbox
        :model-value="yourBrand"
        @update:model-value="$emit('update:yourBrand', $event)"
      >
        你的品牌
        <span class="trm-disclosure-hint">— 你的影片/照片將被標記為「推廣內容」</span>
      </el-checkbox>
      <el-tooltip :content="brandedContentDisabledReason" :disabled="!brandedContentDisabled" placement="top">
        <el-checkbox
          :model-value="brandedContent"
          :disabled="brandedContentDisabled"
          @update:model-value="$emit('update:brandedContent', $event)"
        >
          品牌合作內容
          <span class="trm-disclosure-hint">— 你的影片/照片將被標記為「付費合作」</span>
        </el-checkbox>
      </el-tooltip>
      <el-alert
        v-if="contentDisclosureEnabled && !yourBrand && !brandedContent"
        title="您需要說明您的內容是推廣自己、第三方，或兩者皆是。"
        type="warning"
        :closable="false"
        show-icon
        style="margin-top: 8px;"
      />
    </div>

    <el-alert
      v-if="contentDisclosureEnabled && yourBrand && brandedContent"
      title="你的影片/照片將被標記為「付費合作」"
      type="info"
      :closable="false"
      show-icon
      style="margin-top: 8px;"
    />

    <!-- Branded content + private visibility warning -->
    <el-alert
      v-if="brandedContent && privacyLevel === 'SELF_ONLY'"
      title="品牌合作內容的隱私設定不能設為僅自己可見，已自動切換為公開。"
      type="warning"
      :closable="false"
      show-icon
      style="margin-top: 8px;"
    />

    <!-- Declaration/consent -->
    <div class="trm-declaration">
      <el-checkbox
        :model-value="consentChecked"
        @update:model-value="$emit('update:consentChecked', $event)"
      >
        <el-text type="info" size="small">{{ declarationText }}</el-text>
      </el-checkbox>
    </div>

    <template #footer>
      <el-button @click="$emit('update:visible', false)">取消</el-button>
      <el-button
        type="primary"
        :disabled="!canPublish"
        :loading="publishing"
        @click="$emit('publish')"
      >
        發佈
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed } from 'vue'
import { InfoFilled, Loading } from '@element-plus/icons-vue'

const props = defineProps({
  visible: { type: Boolean, default: false },
  creatorInfo: { type: Object, default: null },
  creatorInfoLoading: { type: Boolean, default: false },
  accountAvatar: { type: String, default: '' },
  mediaPreviewUrl: { type: String, default: '' },
  isVideo: { type: Boolean, default: true },
  title: { type: String, default: '' },
  privacyLevel: { type: String, default: '' },
  allowComment: { type: Boolean, default: false },
  allowDuet: { type: Boolean, default: false },
  allowStitch: { type: Boolean, default: false },
  contentDisclosureEnabled: { type: Boolean, default: false },
  yourBrand: { type: Boolean, default: false },
  brandedContent: { type: Boolean, default: false },
  consentChecked: { type: Boolean, default: false },
  publishing: { type: Boolean, default: false },
})

const emit = defineEmits([
  'update:visible', 'update:title', 'update:privacyLevel',
  'update:allowComment', 'update:allowDuet', 'update:allowStitch',
  'update:contentDisclosureEnabled', 'update:yourBrand', 'update:brandedContent',
  'update:consentChecked', 'publish',
])

// --- Creator info derived ---

const creatorAvatarUrl = computed(() => {
  const info = props.creatorInfo
  return info?.creator_avatar_url || info?.data?.creator_avatar_url || ''
})

const creatorNickname = computed(() => {
  const info = props.creatorInfo
  return info?.creator_nickname || info?.data?.creator_nickname || ''
})

const creatorUsername = computed(() => {
  const info = props.creatorInfo
  return info?.creator_username || info?.data?.creator_username || ''
})

const remainingPostCount = computed(() => {
  const info = props.creatorInfo
  return info?.remaining_post_count ?? info?.data?.remaining_post_count ?? null
})

const postLimitReached = computed(() => {
  return remainingPostCount.value !== null && remainingPostCount.value <= 0
})

const privacyLevelOptions = computed(() => {
  const info = props.creatorInfo
  return info?.privacy_level_options || info?.data?.privacy_level_options || []
})

// --- Interaction capabilities (official TikTok field names) ---

const commentDisabledByApp = computed(() => {
  const info = props.creatorInfo
  // Official field: comment_disabled (boolean)
  const val = info?.comment_disabled ?? info?.data?.comment_disabled
  if (val !== undefined && val !== null) return val === true || val === 1
  // Legacy fallback
  const legacy = info?.comment ?? info?.data?.comment
  return legacy === '0' || legacy === 0 || legacy === false
})

const duetDisabledByApp = computed(() => {
  const info = props.creatorInfo
  // Official field: duet_disabled (boolean)
  const val = info?.duet_disabled ?? info?.data?.duet_disabled
  if (val !== undefined && val !== null) return val === true || val === 1
  // Legacy fallback
  const legacy = info?.duet ?? info?.data?.duet
  return legacy === '0' || legacy === 0 || legacy === false
})

const stitchDisabledByApp = computed(() => {
  const info = props.creatorInfo
  // Official field: stitch_disabled (boolean)
  const val = info?.stitch_disabled ?? info?.data?.stitch_disabled
  if (val !== undefined && val !== null) return val === true || val === 1
  // Legacy fallback
  const legacy = info?.stitch ?? info?.data?.stitch
  return legacy === '0' || legacy === 0 || legacy === false
})

const commentDisabledReason = '此帳號在 TikTok 設定中已停用留言功能'
const duetDisabledReason = '此帳號在 TikTok 設定中已停用 Duet 功能'
const stitchDisabledReason = '此帳號在 TikTok 設定中已停用 Stitch 功能'

// --- Privacy options ---

const PRIVACY_LABELS = {
  PUBLIC_TO_EVERYONE: '公開',
  FOLLOWER_OF_CREATOR: '追蹤者',
  MUTUAL_FOLLOW_FRIENDS: '朋友',
  SELF_ONLY: '僅自己',
}

const privacyOptions = computed(() => {
  const options = privacyLevelOptions.value
  if (!options || options.length === 0) return []
  return options.map(val => {
    const isSelfOnly = val === 'SELF_ONLY'
    const brandedChecked = props.brandedContent
    return {
      value: val,
      label: PRIVACY_LABELS[val] || val,
      disabled: isSelfOnly && brandedChecked,
      disabledReason: isSelfOnly && brandedChecked ? '品牌合作內容的隱私設定不能設為僅自己可見' : '',
    }
  })
})

// --- Commercial content ---

const brandedContentDisabled = computed(() => props.privacyLevel === 'SELF_ONLY')
const brandedContentDisabledReason = '品牌合作內容的隱私設定不能設為僅自己可見'

function onDisclosureToggle(value) {
  emit('update:contentDisclosureEnabled', value)
  if (!value) {
    emit('update:yourBrand', false)
    emit('update:brandedContent', false)
  }
}

// --- Declaration text (English required for TikTok review) ---

const declarationText = computed(() => {
  if (props.contentDisclosureEnabled && props.brandedContent) {
    return 'By posting, you agree to TikTok\'s Branded Content Policy and Music Usage Confirmation.'
  }
  return 'By posting, you agree to TikTok\'s Music Usage Confirmation.'
})

// --- Validity ---

const canPublish = computed(() => {
  if (props.creatorInfoLoading) return false
  if (props.creatorInfo?._error) return false
  if (!props.creatorInfo) return false
  if (postLimitReached.value) return false
  if (!props.title?.trim()) return false
  if (!props.privacyLevel) return false
  if (props.contentDisclosureEnabled && !props.yourBrand && !props.brandedContent) return false
  if (!props.consentChecked) return false
  return true
})
</script>

<style scoped>
.trm-loading {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 16px;
  background: #f5f7fa;
  border-radius: 8px;
  margin-bottom: 16px;
  color: #909399;
}

.trm-creator {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
  padding: 12px;
  background: #f5f7fa;
  border-radius: 8px;
}

.trm-creator-avatar {
  width: 40px;
  height: 40px;
  border-radius: 50%;
  object-fit: cover;
}

.trm-creator-info {
  flex: 1;
}

.trm-creator-name {
  font-weight: 600;
  font-size: 15px;
}

.trm-creator-handle {
  font-size: 13px;
  color: #909399;
}

.trm-preview {
  margin-bottom: 16px;
  border-radius: 8px;
  overflow: hidden;
  background: #000;
}

.trm-preview-media {
  width: 100%;
  max-height: 300px;
  object-fit: contain;
}

.trm-interactions {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
}

.trm-disclosure-options {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px 0 8px 24px;
}

.trm-disclosure-hint {
  color: #909399;
  font-size: 12px;
  margin-left: 4px;
}

.trm-declaration {
  margin-top: 16px;
  padding: 12px;
  background: #f0f9eb;
  border-radius: 4px;
  border-left: 3px solid #67c23a;
}
</style>
