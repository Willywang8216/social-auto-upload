<template>
  <div class="publish-job-progress" v-if="job">
    <div class="progress-header">
      <span class="job-id">任務 #{{ job.id }}</span>
      <el-tag :type="statusTagType" size="small" effect="plain">
        {{ statusLabel }}
      </el-tag>
      <span class="counters">
        {{ job.completedTargets }} / {{ job.totalTargets }} 完成
        <template v-if="job.failedTargets > 0">
          · {{ job.failedTargets }} 失敗
        </template>
      </span>
      <el-button
        v-if="!isTerminal"
        size="small"
        type="warning"
        plain
        @click="$emit('cancel')"
      >
        取消任務
      </el-button>
    </div>
    <p v-if="publishActionHint(job.status)" class="polling-status">{{ publishActionHint(job.status) }}</p>
    <div v-if="polling" class="polling-status" role="status">
      {{ pollingStatusText(polling) }}
      <el-button v-if="['error', 'paused', 'stopped'].includes(polling.status)" size="small" text @click="$emit('refresh')">更新狀態（不重送）</el-button>
    </div>

    <el-progress
      :percentage="percentage"
      :status="progressStatus"
      :stroke-width="10"
      class="progress-bar"
    />

    <div v-if="job.targets && job.targets.length" class="targets">
      <div
        v-for="target in normalizedTargets"
        :key="target.id"
        :class="['target-row', `target-${target.status}`]"
      >
        <el-tag :type="targetTagType(target.status)" size="small" effect="plain">
          {{ targetStatusLabel(target.status) }}
        </el-tag>
        <span class="target-account" :title="target.accountRef">
          {{ shortRef(target.accountRef) }}
        </span>
        <span class="target-file" :title="target.fileRef">
          {{ shortRef(target.fileRef) }}
        </span>
        <span v-if="target.attempts > 1" class="target-attempts">
          第 {{ target.attempts }} 次嘗試
        </span>
        <span v-if="target.lastError" class="target-error" :title="target.lastError">
          {{ target.lastError }}
        </span>
        <span v-if="targetReliabilityDetails(target)" class="target-reliability">{{ targetReliabilityDetails(target) }}</span>
      </div>
    </div>

    <div v-if="tiktokStatuses.length" class="tiktok-statuses">
      <div class="tiktok-statuses-title">TikTok 發佈狀態</div>
      <div
        v-for="ts in tiktokStatuses"
        :key="ts.publish_id"
        :class="['tiktok-status-row', `tiktok-${ts.status}`]"
      >
        <el-tag :type="tiktokStatusTag(ts.status)" size="small" effect="plain">
          {{ tiktokStatusLabel(ts.status) }}
        </el-tag>
        <span v-if="ts.post_id" class="tiktok-post-id">Post: {{ ts.post_id }}</span>
        <a
          v-if="ts.platform_url"
          rel="noopener noreferrer"
          :href="ts.platform_url"
          target="_blank"
          class="tiktok-link"
        >
          查看貼文
        </a>
        <span v-if="ts.fail_reason" class="tiktok-fail">{{ ts.fail_reason }}</span>
      </div>
    </div>
    <p v-if="['backoff', 'error'].includes(providerPolling?.status)" class="polling-status" role="status">TikTok：{{ pollingStatusText(providerPolling) }}</p>
  </div>
</template>

<script setup>
import { computed, ref, watch, onUnmounted } from 'vue'
import { tiktokApi } from '@/api/tiktok'
import { createPoller, invalidResponse, pollingStatusText } from '@/utils/polling'
import { isTerminalJobStatus, needsPublishAction, normalizeTarget, publishActionHint, publishStatusLabel, publishStatusType, targetReliabilityDetails } from '@/utils/publish-status'

const props = defineProps({
  job: {
    type: Object,
    default: null
  },
  polling: { type: Object, default: null }
})

defineEmits(['cancel', 'refresh'])

const normalizedTargets = computed(() => (props.job?.targets || []).map(normalizeTarget))
const tiktokStatuses = ref([])
const providerPolling = ref(null)
let providerPoller = null

function clearPoll() {
  providerPoller?.stop()
  providerPoller = null
}

// Watch identity, not the entire snapshot: each job refresh must not spawn a
// second provider read. The same bounded/cancellable read loop is used here.
watch([() => props.job?.id, () => props.job?.platform], ([jobId, platform]) => {
  clearPoll()
  tiktokStatuses.value = []
  providerPolling.value = null
  if (!jobId || platform !== 'tiktok' || needsPublishAction(props.job?.status)) return
  providerPoller = createPoller(
    (signal) => tiktokApi.getPublishStatus(jobId, { signal, silentError: true }),
    {
      interval: 10000,
      onState: (state) => { providerPolling.value = state },
      onResult: (response) => {
        const statuses = response?.data?.data || response?.data
        if (!Array.isArray(statuses)) throw invalidResponse()
        tiktokStatuses.value = statuses
        if (needsPublishAction(props.job?.status)) return 'paused'
        if ((statuses.length && statuses.every((item) => ['publish_complete', 'failed', 'needs_review', 'blocked'].includes(item.status)))
          || (!statuses.length && isTerminalJobStatus(props.job?.status))) return 'completed'
      },
    }
  )
  providerPoller.start()
}, { immediate: true })

watch(() => props.job?.status, (status) => { if (needsPublishAction(status)) clearPoll() })
onUnmounted(clearPoll)

const isTerminal = computed(() => isTerminalJobStatus(props.job?.status))

const percentage = computed(() => {
  if (!props.job || !props.job.totalTargets) return 0
  const settled = (props.job.completedTargets || 0) + (props.job.failedTargets || 0)
  return Math.min(100, Math.round((settled / props.job.totalTargets) * 100))
})

const progressStatus = computed(() => {
  if (!props.job) return ''
  if (props.job.status === 'succeeded') return 'success'
  if (props.job.status === 'failed') return 'exception'
  if (props.job.status === 'cancelled') return 'warning'
  return ''
})

const statusLabel = computed(() => publishStatusLabel(props.job?.status))
const statusTagType = computed(() => publishStatusType(props.job?.status))
const targetStatusLabel = publishStatusLabel
const targetTagType = publishStatusType

function shortRef(value) {
  if (!value) return ''
  if (value.length <= 32) return value
  return `${value.slice(0, 12)}…${value.slice(-12)}`
}

const tiktokStatusLabel = publishStatusLabel
const tiktokStatusTag = publishStatusType
</script>

<style lang="scss" scoped>
.publish-job-progress {
  margin: var(--space-5) 0;
  padding: var(--space-5);
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: var(--r-lg);

  .progress-header {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: var(--space-4);

    .job-id {
      font-weight: 500;
      color: var(--text);
    }

    .counters {
      color: var(--text-2);
      font-size: 13px;
      flex: 1;
    }
  }

  .progress-bar {
    margin-bottom: var(--space-4);
  }

  .polling-status {
    margin: 8px 0;
    color: var(--text-2);
    font-size: 13px;
  }
  .targets {
    display: flex;
    flex-direction: column;
    gap: 6px;

    .target-row {
      display: grid;
      grid-template-columns: max-content minmax(0, 1fr) minmax(0, 1fr) auto auto;
      gap: 12px;
      align-items: center;
      padding: 6px 10px;
      background: var(--panel);
      border-radius: var(--r-md);
      font-size: 13px;
      color: var(--text-2);

      .target-reliability {
        grid-column: 1 / -1;
        color: var(--text-2);
        overflow-wrap: anywhere;
      }
      .target-account,
      .target-file {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .target-attempts {
        color: var(--color-warning);
        font-size: 12px;
      }

      .target-error {
        color: var(--color-danger);
        font-size: 12px;
        max-width: 280px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      &.target-failed {
        background: var(--color-danger-light);
      }

      &.target-succeeded {
        background: var(--color-success-light);
      }
    }
  }

  .tiktok-statuses {
    margin-top: var(--space-4);
    padding-top: var(--space-4);
    border-top: 1px solid var(--line);

    .tiktok-statuses-title {
      font-size: 13px;
      font-weight: 500;
      color: var(--text);
      margin-bottom: 8px;
    }

    .tiktok-status-row {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 6px 10px;
      background: var(--panel);
      border-radius: var(--r-md);
      font-size: 13px;
      color: var(--text-2);
      margin-bottom: 4px;

      &.tiktok-publish_complete {
        background: var(--color-success-light);
      }

      &.tiktok-failed {
        background: var(--color-danger-light);
      }

      .tiktok-link {
        color: var(--accent);
        text-decoration: none;
        font-size: 12px;
        &:hover { text-decoration: underline; }
      }

      .tiktok-fail {
        color: var(--color-danger);
        font-size: 12px;
      }
    }
  }
}
</style>
