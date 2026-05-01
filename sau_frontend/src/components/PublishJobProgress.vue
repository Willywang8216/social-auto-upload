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
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  job: {
    type: Object,
    default: null
  }
})

defineEmits(['cancel'])

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
</script>

<style lang="scss" scoped>
.publish-job-progress {
  margin: 16px 0;
  padding: 16px;
  background-color: #f5f7fa;
  border: 1px solid #e4e7ed;
  border-radius: 6px;

  .progress-header {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 12px;

    .job-id {
      font-weight: 500;
      color: #303133;
    }

    .counters {
      color: #606266;
      font-size: 13px;
      flex: 1;
    }
  }

  .progress-bar {
    margin-bottom: 12px;
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
      background-color: #fff;
      border-radius: 4px;
      font-size: 13px;
      color: #606266;

      .target-account,
      .target-file {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .target-attempts {
        color: #e6a23c;
        font-size: 12px;
      }

      .target-error {
        color: #f56c6c;
        font-size: 12px;
        max-width: 280px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      &.target-failed {
        background-color: #fef0f0;
      }

      &.target-succeeded {
        background-color: #f0f9eb;
      }
    }
  }
}
</style>
