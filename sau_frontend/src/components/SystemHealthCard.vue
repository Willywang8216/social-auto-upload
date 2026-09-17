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
  </el-card>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { http } from '@/utils/request'

const loading = ref(false)
const offload = ref({})
const inbox = ref({})
const digest = ref({})
const publish = ref({})

const publishCounts = computed(() => publish.value.targetsByStatus || {})

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
  loading.value = true
  try {
    const res = await http.get('/api/system/health')
    const data = res?.data || {}
    offload.value = data.offload || {}
    inbox.value = data.inbox || {}
    digest.value = data.digest || {}
    publish.value = data.publish || {}
  } catch {
    // health panel is informational — never block the inbox view
  } finally {
    loading.value = false
  }
}

onMounted(load)
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
