<template>
  <div class="tiktok-post-settings">
    <!-- Creator info header -->
    <div v-if="creatorInfo" class="tks-creator">
      <img
        v-if="accountAvatar"
        :src="accountAvatar"
        class="tks-creator-avatar"
        alt=""
        @error="(e) => e.target.style.display = 'none'"
      />
      <el-tag type="success" size="small">TikTok</el-tag>
      <span v-if="creatorNickname" class="tks-creator-name">{{ creatorNickname }}</span>
      <el-tag v-if="remainingPostCount !== null" :type="postLimitReached ? 'danger' : 'success'" size="small">
        剩餘發佈次數：{{ remainingPostCount }}
      </el-tag>
      <el-tag v-if="postLimitReached" type="danger" size="small">
        已達發佈上限
      </el-tag>
    </div>

    <el-alert
      v-if="postLimitReached"
      title="此帳號目前無法發佈更多貼文，請稍後再試"
      type="warning"
      :closable="false"
      show-icon
      style="margin: 8px 0;"
    />

    <el-alert
      v-if="videoDurationError"
      :title="videoDurationError"
      type="error"
      :closable="false"
      show-icon
      style="margin: 8px 0;"
    />

    <div v-if="!postLimitReached" class="tks-fields">
      <!-- Privacy level -->
      <el-form-item label="隱私設定" required>
        <el-select
          :model-value="modelValue.privacyLevel"
          placeholder="請選擇隱私設定"
          style="width: 100%;"
          @update:model-value="updateField('privacyLevel', $event)"
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
        <div class="tks-interactions">
          <el-tooltip
            :content="commentDisabledReason"
            :disabled="!commentDisabledByApp"
            placement="top"
          >
            <el-checkbox
              :model-value="modelValue.allowComment"
              :disabled="commentDisabledByApp"
              @update:model-value="updateField('allowComment', $event)"
            >
              允許留言
            </el-checkbox>
          </el-tooltip>
          <el-tooltip
            :content="duetDisabledReason"
            :disabled="!duetDisabledByApp"
            placement="top"
          >
            <el-checkbox
              v-if="!isPhotoPost"
              :model-value="modelValue.allowDuet"
              :disabled="duetDisabledByApp"
              @update:model-value="updateField('allowDuet', $event)"
            >
              允許 Duet
            </el-checkbox>
          </el-tooltip>
          <el-tooltip
            :content="stitchDisabledReason"
            :disabled="!stitchDisabledByApp"
            placement="top"
          >
            <el-checkbox
              v-if="!isPhotoPost"
              :model-value="modelValue.allowStitch"
              :disabled="stitchDisabledByApp"
              @update:model-value="updateField('allowStitch', $event)"
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
          <el-tooltip
            content="您需要說明您的內容是推廣自己、第三方，或兩者皆是。"
            placement="top"
          >
            <el-icon style="margin-left: 4px; vertical-align: middle; color: #909399;"><InfoFilled /></el-icon>
          </el-tooltip>
        </template>
        <el-switch
          :model-value="modelValue.contentDisclosureEnabled"
          @update:model-value="onDisclosureToggle"
        />
      </el-form-item>

      <div v-if="modelValue.contentDisclosureEnabled" class="tks-disclosure-options">
        <el-checkbox
          :model-value="modelValue.yourBrand"
          @update:model-value="updateField('yourBrand', $event)"
        >
          你的品牌
          <span class="tks-disclosure-hint">— 你的影片/照片將被標記為「推廣內容」</span>
        </el-checkbox>
        <el-tooltip
          :content="brandedContentDisabledReason"
          :disabled="!brandedContentDisabled"
          placement="top"
        >
          <el-checkbox
            :model-value="modelValue.brandedContent"
            :disabled="brandedContentDisabled"
            @update:model-value="updateField('brandedContent', $event)"
          >
            品牌合作內容
            <span class="tks-disclosure-hint">— 你的影片/照片將被標記為「付費合作」</span>
          </el-checkbox>
        </el-tooltip>
        <el-alert
          v-if="modelValue.contentDisclosureEnabled && !modelValue.yourBrand && !modelValue.brandedContent"
          title="您需要說明您的內容是推廣自己、第三方，或兩者皆是。"
          type="warning"
          :closable="false"
          show-icon
          style="margin-top: 8px;"
        />
      </div>

      <!-- Combined prompt when both options selected -->
      <el-alert
        v-if="modelValue.contentDisclosureEnabled && modelValue.yourBrand && modelValue.brandedContent"
        title="你的影片/照片將被標記為「付費合作」"
        type="info"
        :closable="false"
        show-icon
        style="margin-top: 8px;"
      />

      <!-- Compliance declaration with consent checkbox -->
      <div class="tks-declaration">
        <el-checkbox
          :model-value="modelValue.consentChecked"
          @update:model-value="updateField('consentChecked', $event)"
        >
          <el-text type="info" size="small">{{ declarationText }}</el-text>
        </el-checkbox>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, watch, ref } from 'vue'
import { InfoFilled } from '@element-plus/icons-vue'
import { tiktokApi } from '@/api/tiktok'

const props = defineProps({
  modelValue: {
    type: Object,
    required: true,
  },
  creatorInfo: {
    type: Object,
    default: null,
  },
  accountAvatar: {
    type: String,
    default: '',
  },
  isPhotoPost: {
    type: Boolean,
    default: false,
  },
  mediaFiles: {
    type: Array,
    default: () => [],
  },
})

const emit = defineEmits(['update:modelValue', 'validity-change'])

// --- Video file check ---

function isVideoFile(path) {
  return /\.(mp4|mov|avi|mkv|webm|m4v)$/i.test(path || '')
}

// --- Video duration fetching ---

const videoDurations = ref({}) // { filePath: durationSec }

watch(() => props.mediaFiles, async (files) => {
  if (!files || files.length === 0) {
    videoDurations.value = {}
    return
  }
  const next = {}
  for (const f of files) {
    // Prefer f.path (uploaded server path like uuid_filename.ext) over
    // f.name (original client filename) — the video-info endpoint needs
    // the server-side path under videoFile/.
    const path = f.path || f.filePath || f.file_path || f.name
    if (!path || videoDurations.value[path] !== undefined) {
      next[path] = videoDurations.value[path]
      continue
    }
    if (!isVideoFile(path)) {
      next[path] = null
      continue
    }
    try {
      const res = await tiktokApi.getVideoInfo(path)
      const data = res?.data?.data || res?.data || {}
      next[path] = data.duration_sec ?? null
    } catch {
      next[path] = null
    }
  }
  videoDurations.value = next
}, { immediate: true })

// --- Creator info derived data ---

const creatorNickname = computed(() => {
  const info = props.creatorInfo
  if (!info) return ''
  // TikTok API v2 returns data nested under "data" or directly
  return info?.creator_nickname || info?.data?.creator_nickname || ''
})

const remainingPostCount = computed(() => {
  const info = props.creatorInfo
  if (!info) return null
  const remaining = info?.remaining_post_count ?? info?.data?.remaining_post_count
  if (remaining === undefined || remaining === null) return null
  return remaining
})

const maxVideoDurationSec = computed(() => {
  const info = props.creatorInfo
  if (!info) return null
  return info?.max_video_post_duration_sec || info?.data?.max_video_post_duration_sec || null
})

const privacyLevelOptions = computed(() => {
  const info = props.creatorInfo
  if (!info) return []
  return info?.privacy_level_options || info?.data?.privacy_level_options || []
})

const postLimitReached = computed(() => {
  const info = props.creatorInfo
  if (!info) return false
  // TikTok may return remaining_post_count or a boolean flag
  const remaining = info?.remaining_post_count ?? info?.data?.remaining_post_count
  if (remaining !== undefined && remaining !== null) {
    return remaining <= 0
  }
  return false
})

// --- Interaction capabilities from creator_info ---

const commentDisabledByApp = computed(() => {
  const info = props.creatorInfo
  if (!info) return false
  // TikTok returns "0" for disabled, "1" for enabled, or boolean
  const val = info?.comment ?? info?.data?.comment
  if (val === undefined || val === null) return false
  return val === '0' || val === 0 || val === false
})

const duetDisabledByApp = computed(() => {
  const info = props.creatorInfo
  if (!info) return false
  const val = info?.duet ?? info?.data?.duet
  if (val === undefined || val === null) return false
  return val === '0' || val === 0 || val === false
})

const stitchDisabledByApp = computed(() => {
  const info = props.creatorInfo
  if (!info) return false
  const val = info?.stitch ?? info?.data?.stitch
  if (val === undefined || val === null) return false
  return val === '0' || val === 0 || val === false
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
  if (!options || options.length === 0) {
    // No fallback — creator_info must provide the options.
    // Return empty list so the dropdown shows "no options available".
    return []
  }
  return options.map(val => {
    const isSelfOnly = val === 'SELF_ONLY'
    const brandedChecked = props.modelValue.brandedContent
    return {
      value: val,
      label: PRIVACY_LABELS[val] || val,
      disabled: isSelfOnly && brandedChecked,
      disabledReason: isSelfOnly && brandedChecked ? '品牌合作內容的隱私設定不能設為僅自己可見' : '',
    }
  })
})

// --- Commercial content + privacy cross-validation ---

const brandedContentDisabled = computed(() => {
  return props.modelValue.privacyLevel === 'SELF_ONLY'
})

const brandedContentDisabledReason = '品牌合作內容的隱私設定不能設為僅自己可見'

// --- Video duration validation ---

const videoDurationError = computed(() => {
  if (!maxVideoDurationSec.value || !props.mediaFiles?.length) return ''
  const maxSec = maxVideoDurationSec.value
  for (const f of props.mediaFiles) {
    const path = f.filePath || f.file_path || f.name
    const dur = videoDurations.value[path]
    if (dur !== undefined && dur !== null && dur > maxSec) {
      return `影片時長 ${Math.round(dur)} 秒，超過 TikTok 限制的 ${maxSec} 秒`
    }
  }
  return ''
})

// --- Declaration text ---

const declarationText = computed(() => {
  const { contentDisclosureEnabled, yourBrand, brandedContent } = props.modelValue
  if (contentDisclosureEnabled && brandedContent) {
    // Branded Content (with or without Your Brand)
    return '發佈即表示您同意 TikTok 的品牌合作內容政策和音樂使用確認。'
  }
  // No disclosure or only Your Brand
  return '發佈即表示您同意 TikTok 的音樂使用確認。'
})

// --- Validity ---

const isValid = computed(() => {
  if (postLimitReached.value) return false
  if (!props.modelValue.privacyLevel) return false
  if (props.modelValue.contentDisclosureEnabled && !props.modelValue.yourBrand && !props.modelValue.brandedContent) return false
  if (videoDurationError.value) return false
  if (!props.modelValue.consentChecked) return false
  return true
})

// Emit validity changes
watch(isValid, (val) => emit('validity-change', val), { immediate: true })

// --- Helpers ---

function updateField(key, value) {
  emit('update:modelValue', { ...props.modelValue, [key]: value })
}

function onDisclosureToggle(value) {
  const next = { ...props.modelValue, contentDisclosureEnabled: value }
  if (!value) {
    // Reset sub-options when toggling off
    next.yourBrand = false
    next.brandedContent = false
  }
  emit('update:modelValue', next)
}

</script>

<style scoped>
.tiktok-post-settings {
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  padding: 12px;
  margin-top: 8px;
  background: #fafafa;
}

.tks-creator {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.tks-creator-name {
  font-weight: 600;
  font-size: 14px;
}

.tks-creator-avatar {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  object-fit: cover;
  flex-shrink: 0;
}

.tks-fields {
  margin-top: 8px;
}

.tks-fields .el-form-item {
  margin-bottom: 12px;
}

.tks-interactions {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
}

.tks-disclosure-options {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px 0 8px 24px;
}

.tks-disclosure-hint {
  color: #909399;
  font-size: 12px;
  margin-left: 4px;
}

.tks-declaration {
  margin-top: 12px;
  padding: 8px 12px;
  background: #f0f9eb;
  border-radius: 4px;
  border-left: 3px solid #67c23a;
}
</style>
