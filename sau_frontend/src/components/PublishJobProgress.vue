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

    <el-progress
      :percentage="percentage"
      :status="progressStatus"
      :stroke-width="10"
      class="progress-bar"
    />

    <div v-if="job.targets && job.targets.length" class="targets">
      <div
        v-for="target in job.targets"
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
          :href="ts.platform_url"
          target="_blank"
          class="tiktok-link"
        >
          查看貼文
        </a>
        <span v-if="ts.fail_reason" class="tiktok-fail">{{ ts.fail_reason }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch, onUnmounted } from 'vue'
import { tiktokApi } from '@/api/tiktok'

const props = defineProps({
  job: {
    type: Object,
    default: null
  }
})

defineEmits(['cancel'])

const tiktokStatuses = ref([])
let pollTimer = null

function clearPoll() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function fetchTiktokStatus(jobId) {
  try {
    const res = await tiktokApi.getPublishStatus(jobId)
    tiktokStatuses.value = res?.data?.data || res?.data || []
  } catch {
    tiktokStatuses.value = []
  }
}

function isTiktokProcessing() {
  return tiktokStatuses.value.some(s => s.status === 'processing')
}

watch(() => props.job, async (job) => {
  clearPoll()
  if (!job || job.platform !== 'tiktok' || !job.id) {
    tiktokStatuses.value = []
    return
  }
  await fetchTiktokStatus(job.id)
  // Poll every 10 seconds while any TikTok status is still processing
  if (isTiktokProcessing()) {
    pollTimer = setInterval(async () => {
      await fetchTiktokStatus(job.id)
      if (!isTiktokProcessing()) {
        clearPoll()
      }
    }, 10000)
  }
}, { immediate: true })

onUnmounted(clearPoll)

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled'])

const isTerminal = computed(() => props.job && TERMINAL.has(props.job.status))

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

const STATUS_LABELS = {
  pending: '佇列中',
  running: '發佈中',
  succeeded: '已完成',
  failed: '部分失敗',
  cancelled: '已取消'
}

const statusLabel = computed(() => STATUS_LABELS[props.job?.status] || props.job?.status || '')

const statusTagType = computed(() => {
  switch (props.job?.status) {
    case 'succeeded': return 'success'
    case 'failed':    return 'danger'
    case 'cancelled': return 'info'
    case 'running':   return 'warning'
    default:          return 'info'
  }
})

function targetTagType(status) {
  switch (status) {
    case 'succeeded': return 'success'
    case 'failed':    return 'danger'
    case 'cancelled': return 'info'
    case 'running':   return 'warning'
    case 'retrying':  return 'warning'
    default:          return 'info'
  }
}

const TARGET_LABELS = {
  pending: '佇列中',
  running: '執行中',
  retrying: '重試中',
  succeeded: '成功',
  failed: '失敗',
  cancelled: '已取消'
}

function targetStatusLabel(status) {
  return TARGET_LABELS[status] || status
}

function shortRef(value) {
  if (!value) return ''
  if (value.length <= 32) return value
  return `${value.slice(0, 12)}…${value.slice(-12)}`
}

function tiktokStatusTag(status) {
  switch (status) {
    case 'publish_complete': return 'success'
    case 'failed': return 'danger'
    default: return 'warning'
  }
}

function tiktokStatusLabel(status) {
  switch (status) {
    case 'processing': return '處理中'
    case 'publish_complete': return '已發佈'
    case 'failed': return '發佈失敗'
    default: return status
  }
}
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

  .targets {
    display: flex;
    flex-direction: column;
    gap: 6px;

    .target-row {
      display: grid;
      grid-template-columns: 80px 1fr 1fr auto auto;
      gap: 12px;
      align-items: center;
      padding: 6px 10px;
      background: var(--panel);
      border-radius: var(--r-md);
      font-size: 13px;
      color: var(--text-2);

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
