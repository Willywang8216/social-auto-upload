<template>
  <div class="jobs-view">
    <div class="page-header">
      <h1>任務中心</h1>
      <div class="page-actions">
        <el-select v-model="statusFilter" placeholder="全部狀態" clearable @change="loadJobs">
          <el-option label="佇列中" value="pending" />
          <el-option label="發佈中" value="running" />
          <el-option label="已完成" value="succeeded" />
          <el-option label="部分失敗" value="failed" />
          <el-option label="已取消" value="cancelled" />
        </el-select>
        <el-select v-model="platformFilter" placeholder="全部平台" clearable @change="loadJobs">
          <el-option
            v-for="platform in platformOptions"
            :key="platform.value"
            :label="platform.label"
            :value="platform.value"
          />
        </el-select>
        <el-button type="primary" @click="loadJobs" :loading="loading">
          <el-icon><Refresh /></el-icon>
          重新整理
        </el-button>
        <el-button type="warning" plain @click="drainNow" :loading="draining">
          立即排空佇列
        </el-button>
      </div>
    </div>

    <el-empty v-if="!loading && jobs.length === 0" description="目前沒有任務" />

    <div v-else class="jobs-list">
      <el-table :data="jobs" style="width: 100%">
        <el-table-column prop="id" label="ID" width="80" />
        <el-table-column prop="platform" label="平台" width="110">
          <template #default="scope">
            <el-tag :type="platformTagType(scope.row.platform)" effect="plain">
              {{ platformLabel(scope.row.platform) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="標題">
          <template #default="scope">
            {{ scope.row.payload?.title || '—' }}
          </template>
        </el-table-column>
        <el-table-column label="進度" width="200">
          <template #default="scope">
            <div class="job-progress-cell">
              <el-progress
                :percentage="percentage(scope.row)"
                :status="progressStatus(scope.row.status)"
                :stroke-width="6"
              />
              <span class="counters">
                {{ scope.row.completedTargets }}/{{ scope.row.totalTargets }}
                <template v-if="scope.row.failedTargets > 0">
                  · {{ scope.row.failedTargets }} 失敗
                </template>
              </span>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="狀態" width="110">
          <template #default="scope">
            <el-tag :type="statusTagType(scope.row.status)" effect="plain">
              {{ statusLabel(scope.row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="建立時間" width="180">
          <template #default="scope">
            {{ formatTime(scope.row.createdAt) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="180">
          <template #default="scope">
            <el-button size="small" @click="openDetail(scope.row)">詳情</el-button>
            <el-button
              v-if="!isTerminal(scope.row.status)"
              size="small"
              type="warning"
              @click="cancel(scope.row)"
            >取消</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-drawer
      v-model="drawerVisible"
      title="任務詳情"
      direction="rtl"
      size="640px"
    >
      <PublishJobProgress
        v-if="selectedJob"
        :job="selectedJob"
        @cancel="cancelSelected"
      />
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import PublishJobProgress from '@/components/PublishJobProgress.vue'
import { jobsApi } from '@/api/jobs'
import { useJobsStore, JOB_STATUS } from '@/stores/jobs'
import { getPlatformLabel, getPlatformTagType, PUBLISH_PLATFORM_OPTIONS } from '@/utils/platforms'

const jobsStore = useJobsStore()
const loading = ref(false)
const draining = ref(false)
const statusFilter = ref('')
const platformFilter = ref('')
const drawerVisible = ref(false)
const selectedJobId = ref(null)
let refreshTimer = null

const jobs = computed(() => jobsStore.jobs)
const selectedJob = computed(() =>
  selectedJobId.value != null ? jobsStore.jobsById[selectedJobId.value] : null
)

const TERMINAL = new Set([
  JOB_STATUS.SUCCEEDED,
  JOB_STATUS.FAILED,
  JOB_STATUS.CANCELLED
])

function isTerminal(status) {
  return TERMINAL.has(status)
}

async function loadJobs() {
  loading.value = true
  try {
    await jobsStore.refreshList({
      status: statusFilter.value || undefined,
      platform: platformFilter.value || undefined
    })
  } catch (error) {
    console.error('載入任務清單失敗:', error)
  } finally {
    loading.value = false
  }
}

async function drainNow() {
  draining.value = true
  try {
    await jobsApi.runDrain()
    ElMessage.success('佇列已排空')
    await loadJobs()
  } catch (error) {
    ElMessage.error(error?.message || '排空佇列失敗')
  } finally {
    draining.value = false
  }
}

async function openDetail(job) {
  selectedJobId.value = job.id
  drawerVisible.value = true
  try {
    await jobsStore.fetchJob(job.id)
    if (!isTerminal(job.status)) {
      jobsStore.startPolling(job.id, { interval: 1500 })
    }
  } catch (error) {
    ElMessage.error('載入任務詳情失敗')
  }
}

async function cancel(job) {
  try {
    await ElMessageBox.confirm(`確定取消任務 #${job.id} 嗎？`, '提示', {
      type: 'warning'
    })
  } catch {
    return
  }
  try {
    await jobsStore.cancelJob(job.id)
    ElMessage.success('任務已取消')
  } catch (error) {
    ElMessage.error(error?.message || '取消失敗')
  }
}

async function cancelSelected() {
  if (selectedJob.value) {
    await cancel(selectedJob.value)
  }
}

const platformOptions = PUBLISH_PLATFORM_OPTIONS

function platformTagType(platform) {
  return getPlatformTagType(platform)
}

function platformLabel(platform) {
  return getPlatformLabel(platform)
}

const STATUS_LABELS = {
  pending: '佇列中',
  running: '發佈中',
  succeeded: '已完成',
  failed: '部分失敗',
  cancelled: '已取消'
}

function statusLabel(status) {
  return STATUS_LABELS[status] || status
}

function statusTagType(status) {
  switch (status) {
    case 'succeeded': return 'success'
    case 'failed':    return 'danger'
    case 'cancelled': return 'info'
    case 'running':   return 'warning'
    default:          return 'info'
  }
}

function progressStatus(status) {
  switch (status) {
    case 'succeeded': return 'success'
    case 'failed':    return 'exception'
    case 'cancelled': return 'warning'
    default:          return ''
  }
}

function percentage(job) {
  if (!job?.totalTargets) return 0
  const settled = (job.completedTargets || 0) + (job.failedTargets || 0)
  return Math.min(100, Math.round((settled / job.totalTargets) * 100))
}

function formatTime(iso) {
  if (!iso) return '—'
  return iso.replace('T', ' ').replace(/\..*$/, '')
}

onMounted(() => {
  loadJobs()
  // Refresh the list every 5 seconds while the page is mounted so newly
  // enqueued jobs from other tabs show up without a manual refresh.
  refreshTimer = window.setInterval(loadJobs, 5000)
})

onBeforeUnmount(() => {
  if (refreshTimer) {
    window.clearInterval(refreshTimer)
    refreshTimer = null
  }
  jobsStore.stopAllPolling()
})
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

.jobs-view {
  .page-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 20px;

    h1 {
      font-size: 24px;
      color: $text-primary;
      margin: 0;
    }

    .page-actions {
      display: flex;
      gap: 10px;
    }
  }

  .jobs-list {
    background-color: #fff;
    border-radius: 4px;
    box-shadow: $box-shadow-light;
    padding: 20px;
  }

  .job-progress-cell {
    display: flex;
    flex-direction: column;
    gap: 4px;

    .counters {
      font-size: 12px;
      color: $text-secondary;
    }
  }
}
</style>
