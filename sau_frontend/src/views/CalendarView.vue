<template>
  <div class="calendar-view">
    <!-- Calendar header -->
    <div class="cal-head">
      <div class="cal-title">{{ MONTHS[view.m] }} {{ view.y }}</div>
      <div class="cal-nav">
        <button class="cal-nav-btn" @click="move(-1)" title="Previous">
          <component :is="icons.collapse" :width="15" :height="15" />
        </button>
        <button class="cal-nav-btn cal-today" @click="setToday">Today</button>
        <button class="cal-nav-btn" @click="move(1)" title="Next">
          <component :is="icons.expand" :width="15" :height="15" />
        </button>
      </div>
      <div class="spacer"></div>
      <el-switch
        v-model="showAll"
        style="--el-switch-on-color: var(--color-primary);"
        active-text="含失敗"
        inactive-text="僅待發"
        title="顯示已失敗的排程"
      />
      <router-link to="/publish/compose" class="btn-primary">
        <component :is="icons.plus" :width="16" :height="16" /> Schedule post
      </router-link>
    </div>

    <!-- Calendar grid -->
    <div class="cal-grid">
      <div v-for="dow in DOW" :key="dow" class="cal-dow">{{ dow }}</div>
      <div
        v-for="(cell, i) in cells"
        :key="i"
        class="cal-cell"
        :class="{ dim: cell.dim, today: cell.today }"
      >
        <div class="cal-date">
          <span v-if="cell.today" class="dn">{{ cell.d }}</span>
          <span v-else>{{ cell.dim ? '' : cell.d }}</span>
        </div>
        <div v-if="cell.loading" class="cal-cell-loading">…</div>
        <div
          v-for="(ev, j) in cell.evs"
          :key="ev.targetId ?? j"
          class="cal-ev"
          :class="[`st-${ev.status}`, { err: ev.status === 'failed' }]"
          :title="`${ev.title} · ${ev.time} · ${ev.accountRef} · ${ev.status}`"
          @click="openEvent(ev)"
        >
          <span class="cd"></span>
          <span class="ct">{{ ev.time }}</span>
          <span class="cl">{{ ev.title }}</span>
          <span class="cp">{{ platformLabel(ev.platform) }}</span>
        </div>
      </div>
    </div>

    <!-- Event detail / action dialog -->
    <el-dialog
      v-model="dialogVisible"
      :title="selectedEv ? selectedEv.title : ''"
      width="520px"
    >
      <template v-if="selectedEv">
        <div class="ev-meta">
          <div class="ev-meta-row">
            <span class="ev-label">平台</span>
            <el-tag :type="platformTagType(selectedEv.platform)" effect="plain">
              {{ platformLabel(selectedEv.platform) }}
            </el-tag>
          </div>
          <div class="ev-meta-row">
            <span class="ev-label">狀態</span>
            <el-tag :type="statusTagType(selectedEv.status)" effect="plain">
              {{ statusLabel(selectedEv.status) }}
            </el-tag>
          </div>
          <div class="ev-meta-row"><span class="ev-label">帳號</span>{{ selectedEv.accountRef }}</div>
          <div class="ev-meta-row"><span class="ev-label">檔案</span>{{ selectedEv.fileRef || '—' }}</div>
          <div class="ev-meta-row"><span class="ev-label">Job</span>#{{ selectedEv.jobId }}</div>
          <div v-if="selectedEv.lastError" class="ev-error">
            {{ selectedEv.lastError }}
          </div>
        </div>

        <div class="ev-reschedule">
          <el-date-picker
            v-model="rescheduleTime"
            type="datetime"
            format="YYYY-MM-DD HH:mm"
            value-format="YYYY-MM-DDTHH:mm:00"
            placeholder="選擇新的排程時間"
            style="width: 100%"
          />
          <el-button
            type="primary"
            @click="doReschedule"
            :disabled="selectedEv.status === 'succeeded' || !rescheduleTime"
          >重新排程</el-button>
        </div>
      </template>

      <template #footer>
        <div class="ev-actions">
          <el-button
            v-if="selectedEv && (selectedEv.status === 'pending' || selectedEv.status === 'retrying')"
            type="danger"
            @click="doCancelTarget"
          >取消此排程</el-button>
          <el-button
            v-if="selectedEv && selectedEv.status === 'failed'"
            type="warning"
            @click="doResubmit"
          >重新送出發佈</el-button>
        </div>
        <el-button @click="dialogVisible = false">關閉</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { icons } from '@/utils/icons'
import { useJobsStore } from '@/stores/jobs'
import { getPlatformLabel, getPlatformTagType } from '@/utils/platforms'

const DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December']

const today = new Date()
const view = ref({ y: today.getFullYear(), m: today.getMonth() })

const jobsStore = useJobsStore()
const events = ref([])
const loading = ref(false)
const showAll = ref(false)
const dialogVisible = ref(false)
const selectedEv = ref(null)
const rescheduleTime = ref('')

const first = computed(() => new Date(view.value.y, view.value.m, 1).getDay())
const days = computed(() => new Date(view.value.y, view.value.m + 1, 0).getDate())
const prevDays = computed(() => new Date(view.value.y, view.value.m, 0).getDate())

const cells = computed(() => {
  const result = []
  const start = prevDays.value - first.value + 1
  for (let i = 0; i < first.value; i++) result.push({ d: start + i, dim: true, evs: [], today: false, loading: false })
  for (let d = 1; d <= days.value; d++) {
    const isToday = view.value.y === today.getFullYear() && view.value.m === today.getMonth() && d === today.getDate()
    const key = keyOf(d)
    result.push({
      d,
      dim: false,
      today: isToday,
      loading: loading.value,
      evs: events.value
        .filter((e) => e.scheduleAt && e.scheduleAt.slice(0, 10) === key)
        .sort((a, b) => a.scheduleAt.localeCompare(b.scheduleAt))
        .slice(0, 6)
    })
  }
  while (result.length % 7) result.push({ d: 1, dim: true, evs: [], today: false, loading: false })
  return result
})

function keyOf(d) {
  return `${view.value.y}-${String(view.value.m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`
}

async function loadEvents() {
  loading.value = true
  try {
    const items = await jobsStore.refreshCalendar({
      month: `${view.value.y}-${String(view.value.m + 1).padStart(2, '0')}`,
      status: showAll.value ? 'pending,retrying,failed' : 'pending,retrying'
    })
    events.value = items.map((e) => ({
      ...e,
      time: e.scheduleAt ? e.scheduleAt.slice(11, 16) : ''
    }))
  } catch (err) {
    ElMessage.error(err?.message || '載入行事曆失敗')
  } finally {
    loading.value = false
  }
}

watch(() => `${view.value.y}-${view.value.m}`, loadEvents)
watch(showAll, loadEvents)
onMounted(loadEvents)

const move = (delta) => {
  let m = view.value.m + delta
  let y = view.value.y
  if (m < 0) { m = 11; y-- }
  if (m > 11) { m = 0; y++ }
  view.value = { y, m }
}

const setToday = () => { view.value = { y: today.getFullYear(), m: today.getMonth() } }

function openEvent(ev) {
  selectedEv.value = ev
  rescheduleTime.value = ev.scheduleAt ? ev.scheduleAt.slice(0, 16) : ''
  dialogVisible.value = true
}

async function doReschedule() {
  if (!selectedEv.value || !rescheduleTime.value) {
    ElMessage.warning('請先選擇時間')
    return
  }
  const utc = new Date(rescheduleTime.value).toISOString().slice(0, 19)
  try {
    await ElMessageBox.confirm(`確定將 #${selectedEv.value.targetId} 改期到 ${utc} (UTC)?`, '提示', { type: 'warning' })
  } catch { return }
  try {
    await jobsStore.rescheduleTarget(selectedEv.value.targetId, utc, selectedEv.value.jobId)
    ElMessage.success('已改期')
    dialogVisible.value = false
    await loadEvents()
  } catch (err) {
    ElMessage.error(err?.message || '改期失敗')
  }
}

async function doCancelTarget() {
  if (!selectedEv.value) return
  try {
    await ElMessageBox.confirm(`確定取消排程 #${selectedEv.value.targetId}?`, '提示', { type: 'warning' })
  } catch { return }
  try {
    await jobsStore.cancelTarget(selectedEv.value.targetId, selectedEv.value.jobId)
    ElMessage.success('已取消此排程')
    dialogVisible.value = false
    await loadEvents()
  } catch (err) {
    ElMessage.error(err?.message || '取消失敗')
  }
}

async function doResubmit() {
  if (!selectedEv.value) return
  try {
    await ElMessageBox.confirm(`確定重新送出 #${selectedEv.value.targetId}?`, '提示', { type: 'warning' })
  } catch { return }
  try {
    await jobsStore.resubmitTarget(selectedEv.value.targetId, selectedEv.value.jobId)
    ElMessage.success('已重新送出')
    dialogVisible.value = false
    await loadEvents()
  } catch (err) {
    ElMessage.error(err?.message || '重新送出失敗')
  }
}

const platformLabel = (p) => getPlatformLabel(p)
const platformTagType = (p) => getPlatformTagType(p)

const STATUS_LABELS = {
  pending: '待發佈',
  running: '發佈中',
  retrying: '重試中',
  succeeded: '已完成',
  failed: '失敗',
  cancelled: '已取消'
}
function statusLabel(s) { return STATUS_LABELS[s] || s }
function statusTagType(s) {
  switch (s) {
    case 'succeeded': return 'success'
    case 'failed': return 'danger'
    case 'cancelled': return 'info'
    case 'running':
    case 'retrying': return 'warning'
    default: return 'info'
  }
}
</script>

<style scoped>
.calendar-view {
  padding: var(--space-6);
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.cal-head {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  margin-bottom: var(--space-6);
  flex-shrink: 0;
}

.cal-title {
  font-size: 20px;
  font-weight: 600;
}

.cal-nav {
  display: flex;
  gap: 6px;
}

.cal-nav-btn {
  border: 1px solid var(--line);
  background: none;
  border-radius: 8px;
  padding: 4px 8px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
}

.cal-nav-btn:hover { background: var(--bg-2); }

.cal-today {
  font-size: 13px;
  padding: 6px 12px;
}

.spacer { flex: 1; }

.cal-grid {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 1px;
  background: var(--line);
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  overflow: hidden;
  flex: 1;
  min-height: 0;
}

.cal-dow {
  background: var(--panel);
  text-align: center;
  font-size: 12px;
  color: var(--text-2);
  padding: 8px 0;
}

.cal-cell {
  background: var(--panel);
  min-height: 92px;
  padding: 6px 8px;
  position: relative;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.cal-cell.dim { opacity: 0.45; }
.cal-cell.today .cal-date { color: var(--color-primary, #0077ff); font-weight: 700; }

.cal-date {
  font-size: 13px;
  color: var(--text);
}

.cal-cell-loading {
  font-size: 12px;
  color: var(--text-2);
}

.cal-ev {
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  line-height: 1.2;
  padding: 1px 2px;
  border-radius: 4px;
  white-space: nowrap;
  overflow: hidden;
  color: var(--text);
}

.cal-ev:hover { background: var(--bg-2); }

.cal-ev .cd { width: 7px; height: 7px; border-radius: 50%; background: var(--color-success, #2ecc71); flex-shrink: 0; }
.cal-ev.st-failed .cd, .cal-ev.err .cd { background: var(--color-danger, #e74c3c); }
.cal-ev.st-running .cd, .cal-ev.st-retrying .cd { background: var(--color-warning, #f39c12); }
.cal-ev.st-cancelled .cd { background: var(--text-3, #999); }

.cal-ev .ct { color: var(--text-2); font-variant-numeric: tabular-nums; }

.cal-ev .cl {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.cal-ev .cp {
  background: var(--bg-2);
  border-radius: 4px;
  padding: 0 4px;
  font-size: 10px;
  color: var(--text-2);
  flex-shrink: 0;
}

.ev-meta {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-bottom: 16px;
}

.ev-meta-row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
}

.ev-label {
  width: 56px;
  color: var(--text-2);
}

.ev-error {
  background: var(--color-danger-bg, rgba(231, 76, 60, 0.1));
  border: 1px solid var(--color-danger, #e74c3c);
  border-radius: 8px;
  padding: 8px 10px;
  color: var(--color-danger, #c0392b);
  font-size: 12px;
  word-break: break-all;
  max-height: 120px;
  overflow: auto;
}

.ev-reschedule {
  display: flex;
  gap: 10px;
  align-items: center;
}

.ev-actions {
  float: left;
  display: inline-flex;
  gap: 8px;
}
</style>