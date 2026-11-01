<template>
  <el-card class="system-health" shadow="never">
    <div class="sh-head">
      <span class="sh-title">系統健康</span>
      <div class="sh-tags">
        <el-tag
          :type="offloadTagType"
          effect="plain"
          size="small"
        >
          Offload {{ offloadLabel }}
        </el-tag>
        <el-tag type="info" effect="plain" size="small">
          排程 {{ publishCounts.pending ?? '—' }} 待發
        </el-tag>
        <el-tag type="info" effect="plain" size="small">
          成功 {{ publishCounts.succeeded ?? '—' }}
        </el-tag>
        <el-tag
          v-if="Number(publishCounts.failed) > 0"
          type="warning"
          effect="plain"
          size="small"
        >
          失敗 {{ publishCounts.failed }}
        </el-tag>
        <el-tag v-for="status in ['blocked', 'awaiting_delivery', 'needs_review'].filter(s => Number(publishCounts[s]) > 0)" :key="status" :type="publishStatusType(status)" effect="plain" size="small">
          {{ publishStatusLabel(status) }} {{ publishCounts[status] }}
        </el-tag>
      </div>
      <div class="spacer" />
      <span class="sh-time">{{ offload.lastRun || '—' }}</span>
      <el-button size="small" text @click="load" :loading="loading">更新</el-button>
    </div>

    <div class="sh-detail">
      <span>Offload：rc={{ offload.exitCode ?? '—' }} · 本機檔案 {{ offload.localFiles ?? '—' }}</span>
      <span class="dot">·</span>
      <span>傳入佇列：待發 {{ inbox.ready ?? 0 }} / 待確認 {{ inbox.pending ?? 0 }} / 隔離 {{ inbox.quarantined ?? 0 }}</span>
      <span class="dot">·</span>
      <span>每日摘要：{{ digest.lastSent || '—' }}</span>
    </div>
    <div v-if="publishHealth" class="sh-detail">
      <span>發佈成功率：{{ successRate }}</span>
      <span>停滯任務：{{ publishHealth.staleTargets?.length ?? 0 }}</span>
      <span>需重新連線帳號：{{ publishHealth.accountsNeedingReconnect?.length ?? 0 }}</span>
      <span>近期錯誤群組：{{ publishHealth.recentFailures?.length ?? 0 }}</span>
      <span>{{ publishHealth.timestamp || '' }}</span>
    </div>
    <div v-else-if="publishHealthUnavailable" class="sh-detail" role="status">發佈健康摘要暫不可用；不影響既有功能。</div>
  </el-card>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { http } from '@/utils/request'
import { publishStatusLabel, publishStatusType } from '@/utils/publish-status'

const loading = ref(false)
const offload = ref({})
const inbox = ref({})
const digest = ref({})
const publish = ref({})

const publishHealth = ref(null)
const publishHealthUnavailable = ref(false)
let controller = null
const publishCounts = computed(() => publishHealth.value?.totals || publish.value.targetsByStatus || {})
const successRate = computed(() => {
  const rate = publishHealth.value?.totals?.overallSuccessRate
  return typeof rate === 'number' && Number.isFinite(rate) ? `${Math.round(rate * 100)}%` : '—'
})

const offloadLabel = computed(() => {
  if (offload.value.healthy === true) return '正常'
  if (offload.value.healthy === false) return '失敗'
  return '未知'
})

const offloadTagType = computed(() => {
  if (offload.value.healthy === true) return 'success'
  if (offload.value.healthy === false) return 'danger'
  return 'info'
})

async function load() {
  if (loading.value) return
  loading.value = true
  controller = new AbortController()
  const signal = controller.signal
  const config = { silentError: true, signal, timeout: 10000 }
  const [systemResult, publishResult] = await Promise.allSettled([
    http.get('/api/system/health', undefined, config),
    http.get('/api/publish/health', undefined, config),
  ])
  if (signal.aborted) return
  if (systemResult.status === 'fulfilled') {
    const data = systemResult.value?.data || {}
    offload.value = data.offload || {}
    inbox.value = data.inbox || {}
    digest.value = data.digest || {}
    publish.value = data.publish || {}
  }
  const snapshot = publishResult.status === 'fulfilled' ? publishResult.value?.data : null
  publishHealth.value = snapshot?.totals && typeof snapshot.totals === 'object' && !Array.isArray(snapshot.totals) ? snapshot : null
  publishHealthUnavailable.value = !publishHealth.value
  loading.value = false
}

onMounted(load)
onBeforeUnmount(() => controller?.abort())
</script>

<style scoped>
.system-health {
  margin-bottom: var(--space-4, 16px);
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--r-lg, 12px);
}
.sh-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.sh-title {
  font-weight: 600;
  font-size: 14px;
}
.sh-tags {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.spacer { flex: 1; }
.sh-time {
  font-size: 12px;
  color: var(--text-2);
  font-variant-numeric: tabular-nums;
}
.sh-detail {
  margin-top: 10px;
  font-size: 12px;
  color: var(--text-2);
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.dot { opacity: 0.5; }
</style>
