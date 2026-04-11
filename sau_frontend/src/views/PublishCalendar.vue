<template>
  <div class="publish-calendar">
    <div class="page-header">
      <div>
        <h1>發佈日曆</h1>
        <p>檢視內部 publish jobs 的排程、Queue 展開結果與手動任務狀態。</p>
      </div>
      <div class="header-actions">
        <el-button @click="shiftMonth(-1)">上個月</el-button>
        <el-button @click="jumpToToday">今天</el-button>
        <el-button @click="shiftMonth(1)">下個月</el-button>
      </div>
    </div>

    <el-card class="filter-card">
      <div class="legend-row">
        <div class="legend-item">
          <span class="legend-swatch lifecycle-container-processing"></span>
          <span>Container processing</span>
        </div>
        <div class="legend-item">
          <span class="legend-swatch lifecycle-publish-triggered"></span>
          <span>Publish triggered</span>
        </div>
        <div class="legend-item">
          <span class="legend-swatch lifecycle-published"></span>
          <span>Published</span>
        </div>
        <div class="legend-item">
          <span class="legend-swatch lifecycle-failed"></span>
          <span>Failed</span>
        </div>
      </div>

      <div class="filter-row">
        <el-select v-model="filters.profileId" clearable placeholder="全部 Profiles">
          <el-option
            v-for="profile in profiles"
            :key="profile.id"
            :label="profile.name"
            :value="profile.id"
          />
        </el-select>
        <el-select v-model="filters.status" clearable placeholder="全部狀態">
          <el-option label="草稿" value="draft" />
          <el-option label="已排程" value="scheduled" />
          <el-option label="佇列中" value="queued" />
          <el-option label="處理中" value="processing" />
          <el-option label="已發布" value="published" />
          <el-option label="已匯出" value="exported" />
          <el-option label="手動完成" value="manual_done" />
          <el-option label="失敗" value="failed" />
          <el-option label="已取消" value="cancelled" />
        </el-select>
        <el-select v-model="filters.deliveryMode" clearable placeholder="全部交付方式">
          <el-option label="直接上傳" value="direct_upload" />
          <el-option label="Google Sheet" value="sheet_export" />
          <el-option label="手動完成" value="manual_only" />
        </el-select>
        <el-select v-model="filters.platformKey" clearable placeholder="全部平台">
          <el-option
            v-for="platform in platformOptions"
            :key="platform"
            :label="platform"
            :value="platform"
          />
        </el-select>
        <el-button type="primary" plain @click="refreshCalendar" :loading="isRefreshing">重新整理</el-button>
      </div>
    </el-card>

    <el-card class="calendar-card" v-loading="isRefreshing">
      <template #header>
        <div class="card-header">
          <span>{{ monthTitle }}</span>
          <el-tag type="info">{{ totalJobsThisMonth }} 筆任務</el-tag>
        </div>
      </template>

      <el-calendar v-model="calendarDate">
        <template #date-cell="{ data }">
          <div
            class="calendar-cell"
            :class="`lifecycle-${getDayLifecycleState(entriesMap[data.day] || [])}`"
            @click="openDayDrawer(data.day)"
          >
            <div class="cell-date">{{ data.day.split('-')[2] }}</div>
            <div v-if="entriesMap[data.day]?.length" class="cell-events">
              <el-tag size="small" :type="getLifecycleTagType(getDayLifecycleState(entriesMap[data.day] || []))">
                {{ entriesMap[data.day].length }} 筆
              </el-tag>
              <div
                v-for="job in entriesMap[data.day].slice(0, 2)"
                :key="job.id"
                class="cell-job"
                :class="`lifecycle-${getJobLifecycleState(job)}`"
              >
                {{ job.targetName || job.platformKey }}
              </div>
            </div>
          </div>
        </template>
      </el-calendar>
    </el-card>

    <el-drawer
      v-model="drawerVisible"
      :title="selectedDate ? `${selectedDate} 任務` : '任務詳情'"
      size="520px"
    >
      <el-empty v-if="selectedDateJobs.length === 0" description="這一天沒有任務" />
      <div v-else class="drawer-job-list">
        <el-card
          v-for="job in selectedDateJobs"
          :key="job.id"
          class="drawer-job-card"
          :class="`lifecycle-${getJobLifecycleState(job)}`"
          shadow="never"
        >
          <div class="drawer-job-header">
            <div>
              <div class="drawer-job-title">{{ job.targetName || job.platformKey }}</div>
              <div class="drawer-job-meta">
                {{ job.profileName || '未命名 Profile' }} · {{ job.platformKey }} · {{ formatDeliveryMode(job.deliveryMode) }}
              </div>
            </div>
            <el-tag :type="getLifecycleTagType(getJobLifecycleState(job))">{{ job.status }}</el-tag>
          </div>

          <div class="drawer-job-body">
            <div class="drawer-line"><strong>排程：</strong>{{ job.scheduledAt || '立即 / 無指定' }}</div>
            <div class="drawer-line"><strong>素材：</strong>{{ job.materialName || '-' }}</div>
            <div v-if="getLifecycleTags(job).length" class="drawer-tag-list">
              <el-tag
                v-for="tag in getLifecycleTags(job)"
                :key="tag.key"
                size="small"
                :type="tag.type"
              >
                {{ tag.label }}
              </el-tag>
            </div>
            <div
              v-for="(line, index) in getLifecycleLines(job)"
              :key="`${job.id}-lifecycle-${index}`"
              class="drawer-line"
            >
              <strong>生命週期：</strong>{{ line }}
            </div>
            <div v-if="job.metadata?.publishedUrl" class="drawer-line">
              <strong>連結：</strong>
              <el-link :href="job.metadata.publishedUrl" target="_blank" type="primary">
                開啟已發布內容
              </el-link>
            </div>
            <div class="drawer-message">{{ job.message || '這筆任務目前沒有文案內容' }}</div>
          </div>

          <div class="drawer-job-actions">
            <el-button
              size="small"
              type="primary"
              plain
              @click="runJobNow(job)"
              v-if="canRunNow(job)"
            >
              立即執行
            </el-button>
            <el-button
              size="small"
              @click="openRescheduleDialog(job)"
              v-if="canReschedule(job)"
            >
              重排時間
            </el-button>
            <el-button
              size="small"
              type="danger"
              plain
              @click="cancelJob(job)"
              v-if="canCancel(job)"
            >
              取消
            </el-button>
            <el-button
              size="small"
              type="success"
              plain
              @click="completeManualJob(job)"
              v-if="job.deliveryMode === 'manual_only' && job.status !== 'manual_done'"
            >
              標記完成
            </el-button>
          </div>
        </el-card>
      </div>
    </el-drawer>

    <el-dialog v-model="rescheduleDialogVisible" title="重排任務時間" width="420px">
      <el-date-picker
        v-model="rescheduleAt"
        type="datetime"
        value-format="YYYY-MM-DDTHH:mm:ss"
        placeholder="選擇新的排程時間"
        style="width: 100%"
      />
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="rescheduleDialogVisible = false">取消</el-button>
          <el-button type="primary" @click="submitReschedule" :loading="isRescheduling">儲存</el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { profileApi } from '@/api/profile'
import { publishApi } from '@/api/publish'
import { getLifecycleLines, getLifecycleTags, getLifecycleVisualState } from '@/utils/publishLifecycle'

const profiles = ref([])
const calendarDate = ref(new Date())
const calendarItems = ref([])
const drawerVisible = ref(false)
const selectedDate = ref('')
const rescheduleDialogVisible = ref(false)
const rescheduleTargetJob = ref(null)
const rescheduleAt = ref('')
const isRefreshing = ref(false)
const isRescheduling = ref(false)

const filters = reactive({
  profileId: '',
  status: '',
  deliveryMode: '',
  platformKey: ''
})

const platformOptions = [
  'xiaohongshu',
  'channels',
  'douyin',
  'kuaishou',
  'twitter',
  'threads',
  'instagram',
  'facebook',
  'youtube',
  'tiktok',
  'telegram',
  'patreon',
  'reddit'
]

const entriesMap = computed(() => {
  const map = {}
  ;(calendarItems.value || []).forEach((item) => {
    map[item.date] = item.jobs || []
  })
  return map
})

const selectedDateJobs = computed(() => entriesMap.value[selectedDate.value] || [])
const totalJobsThisMonth = computed(() => (
  (calendarItems.value || []).reduce((sum, item) => sum + (item.count || 0), 0)
))
const monthTitle = computed(() => {
  const value = calendarDate.value instanceof Date ? calendarDate.value : new Date(calendarDate.value)
  return `${value.getFullYear()} 年 ${value.getMonth() + 1} 月`
})

watch(
  [
    calendarDate,
    () => filters.profileId,
    () => filters.status,
    () => filters.deliveryMode,
    () => filters.platformKey
  ],
  () => {
    refreshCalendar()
  }
)

const buildMonthRange = (dateValue) => {
  const value = dateValue instanceof Date ? dateValue : new Date(dateValue)
  const start = new Date(value.getFullYear(), value.getMonth(), 1, 0, 0, 0)
  const end = new Date(value.getFullYear(), value.getMonth() + 1, 0, 23, 59, 59)
  const format = (date) => {
    const pad = (valueToPad) => String(valueToPad).padStart(2, '0')
    return [
      `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`,
      `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
    ].join('T')
  }
  return {
    startDate: format(start),
    endDate: format(end)
  }
}

const loadProfiles = async () => {
  const response = await profileApi.getProfiles()
  profiles.value = response.data || []
}

const refreshCalendar = async () => {
  isRefreshing.value = true
  try {
    const range = buildMonthRange(calendarDate.value)
    const response = await publishApi.getCalendarEntries({
      ...range,
      profileId: filters.profileId || undefined,
      status: filters.status || undefined,
      deliveryMode: filters.deliveryMode || undefined,
      platformKey: filters.platformKey || undefined
    })
    calendarItems.value = response.data?.items || []
  } catch (error) {
    ElMessage.error(error.message || '載入發佈日曆失敗')
  } finally {
    isRefreshing.value = false
  }
}

const shiftMonth = (offset) => {
  const value = calendarDate.value instanceof Date ? calendarDate.value : new Date(calendarDate.value)
  calendarDate.value = new Date(value.getFullYear(), value.getMonth() + offset, 1)
}

const jumpToToday = () => {
  calendarDate.value = new Date()
}

const formatDeliveryMode = (value) => {
  if (value === 'direct_upload') {
    return '直接上傳'
  }
  if (value === 'sheet_export') {
    return 'Google Sheet'
  }
  if (value === 'manual_only') {
    return '手動完成'
  }
  return value || '未設定'
}

const lifecyclePriority = {
  failed: 4,
  'publish-triggered': 3,
  'container-processing': 2,
  published: 1,
  default: 0
}

const getJobLifecycleState = (job) => getLifecycleVisualState(job)

const getDayLifecycleState = (jobs = []) => {
  let selectedState = 'default'
  let selectedPriority = -1

  ;(jobs || []).forEach((job) => {
    const state = getJobLifecycleState(job)
    const priority = lifecyclePriority[state] ?? 0
    if (priority > selectedPriority) {
      selectedState = state
      selectedPriority = priority
    }
  })

  return selectedState
}

const getLifecycleTagType = (state) => {
  if (state === 'failed') {
    return 'danger'
  }
  if (state === 'published') {
    return 'success'
  }
  if (state === 'publish-triggered') {
    return 'primary'
  }
  if (state === 'container-processing') {
    return 'warning'
  }
  return 'info'
}

const openDayDrawer = (date) => {
  selectedDate.value = date
  drawerVisible.value = true
}

const canRunNow = (job) => !['published', 'exported', 'manual_done', 'cancelled'].includes(job.status)
const canCancel = (job) => !['published', 'exported', 'manual_done', 'cancelled'].includes(job.status)
const canReschedule = (job) => !['published', 'exported', 'manual_done', 'cancelled'].includes(job.status)

const runJobNow = async (job) => {
  try {
    await publishApi.runJobNow(job.id)
    await refreshCalendar()
    ElMessage.success('任務已送出執行')
  } catch (error) {
    ElMessage.error(error.message || '立即執行失敗')
  }
}

const cancelJob = async (job) => {
  try {
    await ElMessageBox.confirm('確定要取消這筆任務嗎？', '取消任務', {
      confirmButtonText: '確定',
      cancelButtonText: '取消',
      type: 'warning'
    })
    await publishApi.cancelJob(job.id)
    await refreshCalendar()
    ElMessage.success('任務已取消')
  } catch (error) {
    if (error !== 'cancel') {
      ElMessage.error(error.message || '取消任務失敗')
    }
  }
}

const completeManualJob = async (job) => {
  try {
    await publishApi.completeManualJob(job.id)
    await refreshCalendar()
    ElMessage.success('已標記為手動完成')
  } catch (error) {
    ElMessage.error(error.message || '更新任務狀態失敗')
  }
}

const openRescheduleDialog = (job) => {
  rescheduleTargetJob.value = job
  rescheduleAt.value = job.scheduledAt || ''
  rescheduleDialogVisible.value = true
}

const submitReschedule = async () => {
  if (!rescheduleTargetJob.value || !rescheduleAt.value) {
    ElMessage.warning('請先選擇新的排程時間')
    return
  }

  isRescheduling.value = true
  try {
    await publishApi.updateJob({
      jobId: rescheduleTargetJob.value.id,
      scheduledAt: rescheduleAt.value,
      status: 'scheduled'
    })
    rescheduleDialogVisible.value = false
    await refreshCalendar()
    ElMessage.success('任務排程時間已更新')
  } catch (error) {
    ElMessage.error(error.message || '更新排程時間失敗')
  } finally {
    isRescheduling.value = false
  }
}

onMounted(async () => {
  await Promise.all([
    loadProfiles(),
    refreshCalendar()
  ])
})
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

.publish-calendar {
  display: flex;
  flex-direction: column;
  gap: 20px;

  .page-header,
  .filter-card,
  .calendar-card {
    background: #fff;
    border-radius: 8px;
    box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.08);
  }

  .page-header {
    display: flex;
    justify-content: space-between;
    gap: 20px;
    padding: 24px;

    h1 {
      margin: 0 0 8px;
      color: $text-primary;
    }

    p {
      margin: 0;
      color: $text-secondary;
    }
  }

  .header-actions,
  .filter-row,
  .card-header,
  .dialog-footer {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .header-actions,
  .card-header {
    justify-content: flex-end;
  }

  .filter-row {
    flex-wrap: wrap;
  }

  .legend-row {
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    margin-bottom: 16px;
    color: $text-secondary;
    font-size: 13px;
  }

  .legend-item {
    display: inline-flex;
    align-items: center;
    gap: 8px;
  }

  .legend-swatch {
    display: inline-flex;
    width: 12px;
    height: 12px;
    border-radius: 999px;
    border: 1px solid transparent;
    flex-shrink: 0;
  }

  .legend-swatch.lifecycle-container-processing {
    background: rgba(230, 162, 60, 0.18);
    border-color: rgba(230, 162, 60, 0.5);
  }

  .legend-swatch.lifecycle-publish-triggered {
    background: rgba(64, 158, 255, 0.18);
    border-color: rgba(64, 158, 255, 0.5);
  }

  .legend-swatch.lifecycle-published {
    background: rgba(103, 194, 58, 0.18);
    border-color: rgba(103, 194, 58, 0.5);
  }

  .legend-swatch.lifecycle-failed {
    background: rgba(245, 108, 108, 0.18);
    border-color: rgba(245, 108, 108, 0.5);
  }

  .calendar-cell {
    min-height: 94px;
    padding: 6px;
    cursor: pointer;
    display: flex;
    flex-direction: column;
    gap: 6px;
    border-radius: 8px;
    border: 1px solid transparent;
    transition: background-color 0.2s ease, border-color 0.2s ease;
  }

  .calendar-cell.lifecycle-container-processing {
    background: rgba(230, 162, 60, 0.10);
    border-color: rgba(230, 162, 60, 0.35);
  }

  .calendar-cell.lifecycle-publish-triggered {
    background: rgba(64, 158, 255, 0.10);
    border-color: rgba(64, 158, 255, 0.35);
  }

  .calendar-cell.lifecycle-published {
    background: rgba(103, 194, 58, 0.10);
    border-color: rgba(103, 194, 58, 0.35);
  }

  .calendar-cell.lifecycle-failed {
    background: rgba(245, 108, 108, 0.10);
    border-color: rgba(245, 108, 108, 0.35);
  }

  .cell-date {
    font-size: 13px;
    color: $text-primary;
  }

  .cell-events {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .cell-job {
    font-size: 12px;
    color: $text-secondary;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    padding-left: 8px;
    position: relative;
  }

  .cell-job::before {
    content: '';
    position: absolute;
    left: 0;
    top: 6px;
    width: 5px;
    height: 5px;
    border-radius: 999px;
    background: #c0c4cc;
  }

  .cell-job.lifecycle-container-processing::before {
    background: #e6a23c;
  }

  .cell-job.lifecycle-publish-triggered::before {
    background: #409eff;
  }

  .cell-job.lifecycle-published::before {
    background: #67c23a;
  }

  .cell-job.lifecycle-failed::before {
    background: #f56c6c;
  }

  .drawer-job-list {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .drawer-job-card {
    border: 1px solid #ebeef5;
    border-left-width: 4px;
  }

  .drawer-job-card.lifecycle-container-processing {
    border-left-color: #e6a23c;
  }

  .drawer-job-card.lifecycle-publish-triggered {
    border-left-color: #409eff;
  }

  .drawer-job-card.lifecycle-published {
    border-left-color: #67c23a;
  }

  .drawer-job-card.lifecycle-failed {
    border-left-color: #f56c6c;
  }

  .drawer-job-header,
  .drawer-job-actions {
    display: flex;
    justify-content: space-between;
    gap: 12px;
  }

  .drawer-job-header {
    align-items: flex-start;
  }

  .drawer-job-title {
    font-weight: 600;
    color: $text-primary;
  }

  .drawer-job-meta,
  .drawer-line {
    color: $text-secondary;
    font-size: 13px;
    line-height: 1.6;
  }

  .drawer-job-body {
    margin: 12px 0;
  }

  .drawer-tag-list {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin: 8px 0 10px;
  }

  .drawer-message {
    margin-top: 10px;
    padding: 12px;
    border-radius: 6px;
    background: #f5f7fa;
    color: $text-regular;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .drawer-job-actions {
    flex-wrap: wrap;
  }

  @media (max-width: 900px) {
    .page-header {
      flex-direction: column;
    }
  }
}
</style>
