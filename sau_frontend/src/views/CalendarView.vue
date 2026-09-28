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
    <div class="cal-grid-scroll">
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
            :key="ev.entityId"
            class="cal-ev"
            :class="[`st-${ev.status}`, { err: ev.status === 'failed' }]"
            :title="`${ev.title} · ${ev.time} · ${ev.status}`"
            @click="openEvent(ev)"
          >
            <span class="cd"></span>
            <span class="ct">{{ ev.time }}</span>
            <span class="cl">{{ ev.title }}</span>
            <span class="cp">{{ ev.destinationCount }} targets</span>
          </div>
          <button v-if="cell.hiddenCount" class="cal-more" @click="showDay(cell)">
            +{{ cell.hiddenCount }} more
          </button>
        </div>
      </div>
    </div>

    <!-- Event detail / action dialog -->
    <el-dialog v-model="dayDialogVisible" :title="selectedDayLabel" width="min(720px, 92vw)">
      <div class="day-event-list">
        <button v-for="ev in selectedDayEvents" :key="ev.entityId" class="day-event" @click="dayDialogVisible = false; openEvent(ev)">
          <span>{{ ev.time }} · {{ ev.title }}</span>
          <el-tag :type="statusTagType(ev.status)" effect="plain">{{ statusLabel(ev.status) }}</el-tag>
        </button>
      </div>
    </el-dialog>

    <el-dialog v-model="dialogVisible" :title="selectedEntity?.posts?.find((post) => post.draft?.title || post.draft?.message)?.draft?.title || selectedEntity?.posts?.find((post) => post.draft?.message)?.draft?.message || selectedEntity?.mediaItems?.[0]?.filename || 'Publish details'" width="min(860px, 94vw)" top="5vh">
      <div v-if="detailLoading" class="entity-loading">Loading publish details…</div>
      <template v-else-if="selectedEntity">
        <div class="entity-summary">
          <el-tag :type="statusTagType(selectedEntity.status)" effect="plain">{{ statusLabel(selectedEntity.status) }}</el-tag>
          <span>{{ selectedEntity.profile?.name || 'Multiple profiles' }}</span>
          <span>{{ selectedEntity.scheduledAt ? `Next: ${selectedEntity.scheduledAt}` : 'No schedule' }}</span>
          <span>{{ selectedEntity.jobs?.length || 0 }} destinations</span>
        </div>
        <div v-if="selectedEntity.mediaItems?.length" class="entity-media">
          <article v-for="media in selectedEntity.mediaItems" :key="media.fileRecordId || media.filename" class="entity-media-item">
            <video v-if="media.mediaType === 'video' && media.previewUrl" :src="media.previewUrl" controls preload="metadata" />
            <img v-else-if="media.mediaType === 'image' && media.previewUrl" :src="media.previewUrl" :alt="media.filename" />
            <div v-else class="media-missing">Preview unavailable</div>
            <div class="media-name">{{ media.filename }}</div>
            <a v-if="media.publicUrl" :href="media.publicUrl" target="_blank" rel="noopener">Open media link</a>
          </article>
        </div>
        <section v-for="post in selectedEntity.posts || []" :key="post.id" class="entity-post">
          <header>
            <strong>{{ post.platform }}</strong>
            <span>{{ post.accounts?.map((account) => account.name).filter(Boolean).join(', ') || 'Account unavailable' }}</span>
            <el-tag :type="statusTagType(post.status)" effect="plain">{{ statusLabel(post.status) }}</el-tag>
          </header>
          <div class="post-copy">{{ post.draft?.message || post.draft?.title || 'No copy saved' }}</div>
          <div v-if="editingPostId === post.id" class="copy-editor">
            <el-input v-model="editedCopy" type="textarea" :rows="4" />
            <el-button type="primary" @click="saveCopy(post)">Save copy</el-button>
            <el-button @click="editingPostId = null">Discard</el-button>
          </div>
          <el-button v-else-if="post.status === 'queued' || post.status === 'ready'" size="small" @click="editingPostId = post.id; editedCopy = post.draft?.message || ''">Edit copy</el-button>
          <div v-for="job in (selectedEntity.jobs || []).filter((item) => item.targets?.some((target) => target.fileRef === `campaign_post:${post.id}`))" :key="job.id" class="entity-targets">
            <div v-for="target in job.targets.filter((item) => item.fileRef === `campaign_post:${post.id}`)" :key="target.id" class="entity-target">
              <el-tag :type="statusTagType(target.status)" effect="plain">{{ statusLabel(target.status) }}</el-tag>
              <span>{{ target.accountName }}</span><span>{{ target.scheduleAt || 'Immediate' }}</span>
              <span v-if="target.lastError" class="ev-error">{{ target.lastError }}</span>
              <el-date-picker v-if="target.status === 'pending' || target.status === 'retrying'" v-model="target._editSchedule" type="datetime" format="YYYY-MM-DD HH:mm" value-format="YYYY-MM-DDTHH:mm:00" placeholder="Reschedule" />
              <el-button v-if="target._editSchedule && (target.status === 'pending' || target.status === 'retrying')" size="small" @click="rescheduleEntityTarget(target)">Save time</el-button>
              <el-button v-if="target.status === 'pending' || target.status === 'retrying'" size="small" type="danger" @click="cancelEntityTarget(target)">Cancel</el-button>
              <el-button v-if="target.status === 'failed'" size="small" type="warning" @click="resubmitEntityTarget(target)">Retry</el-button>
            </div>
          </div>
        </section>
        <section v-if="selectedEntity.artifacts?.length" class="entity-artifacts">
          <h4>Published media links</h4>
          <a v-for="artifact in selectedEntity.artifacts.filter((item) => item.url)" :key="artifact.id || artifact.url" :href="artifact.url" target="_blank" rel="noopener">{{ artifact.role || artifact.kind || 'Media' }} · Open link</a>
        </section>
      </template>
      <template #footer><el-button @click="dialogVisible = false">Close</el-button></template>
    </el-dialog>

  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
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
const route = useRoute()
const router = useRouter()
const events = ref([])
const loading = ref(false)
const showAll = ref(false)
const dialogVisible = ref(false)
const detailLoading = ref(false)
const selectedEntity = ref(null)
const editingPostId = ref(null)
const editedCopy = ref('')
const dayDialogVisible = ref(false)
const selectedDayEvents = ref([])
const selectedDayLabel = ref('')
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
        .slice(0, 5),
      hiddenCount: Math.max(0, events.value.filter((e) => e.scheduleAt && e.scheduleAt.slice(0, 10) === key).length - 5)
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
    const month = `${view.value.y}-${String(view.value.m + 1).padStart(2, '0')}`
    const result = await jobsStore.refreshEntities({
      month,
      status: showAll.value ? 'scheduled,queued,publishing,failed,cancelled,published' : 'scheduled,queued,publishing'
    })
    events.value = (result.items || []).map((entity) => {
      const destinations = (entity.jobs || []).flatMap((job) => job.targets || [])
      const stamps = destinations.map((target) => target.scheduleAt).filter(Boolean)
      const stamp = stamps.sort((a, b) => a.localeCompare(b))[0] || entity.scheduledAt
      const mediaName = entity.mediaItems?.[0]?.filename
      const postCopy = entity.posts?.find((post) => post.draft?.title || post.draft?.message)?.draft
      return {
        ...entity,
        scheduleAt: stamp,
        time: stamp ? stamp.slice(11, 16) : '',
        title: postCopy?.title || postCopy?.message || mediaName || `Publish ${entity.entityId}`,
        destinationCount: destinations.length
      }
    }).filter((entity) => entity.scheduleAt)
  } catch (err) {
    ElMessage.error(err?.message || '載入行事曆失敗')
  } finally {
    loading.value = false
  }
}

watch(() => `${view.value.y}-${view.value.m}`, loadEvents)
watch(showAll, loadEvents)
onMounted(async () => {
  await loadEvents()
  if (route.query.entity) {
    openEvent({ entityId: String(route.query.entity) })
    router.replace({ query: { ...route.query, entity: undefined } })
  }
})

const move = (delta) => {
  let m = view.value.m + delta
  let y = view.value.y
  if (m < 0) { m = 11; y-- }
  if (m > 11) { m = 0; y++ }
  view.value = { y, m }
}

const setToday = () => { view.value = { y: today.getFullYear(), m: today.getMonth() } }

function showDay(cell) {
  selectedDayEvents.value = cell.evs
  selectedDayLabel.value = `${MONTHS[view.value.m]} ${cell.d}`
  dayDialogVisible.value = true
}

async function openEvent(ev) {
  selectedEv.value = ev
  selectedEntity.value = null
  dialogVisible.value = true
  detailLoading.value = true
  try {
    selectedEntity.value = await jobsStore.fetchEntity(ev.entityId)
  } catch (error) {
    ElMessage.error(error?.message || 'Unable to load publish details')
  } finally {
    detailLoading.value = false
  }
}

async function saveCopy(post) {
  try {
    await jobsStore.updateEntityPost(selectedEntity.value.entityId, post.id, {
      ...(post.draft || {}),
      message: editedCopy.value
    })
    selectedEntity.value = await jobsStore.fetchEntity(selectedEntity.value.entityId)
    editingPostId.value = null
    ElMessage.success('Copy updated')
    await loadEvents()
  } catch (error) {
    ElMessage.error(error?.message || 'Copy update failed')
  }
}

async function rescheduleEntityTarget(target) {
  const scheduleAt = new Date(target._editSchedule).toISOString().slice(0, 19)
  try {
    await jobsStore.rescheduleTarget(target.id, scheduleAt, target.jobId)
    selectedEntity.value = await jobsStore.fetchEntity(selectedEntity.value.entityId)
    await loadEvents()
    ElMessage.success('Schedule updated')
  } catch (error) {
    ElMessage.error(error?.message || 'Rescheduling failed')
  }
}

async function cancelEntityTarget(target) {
  try {
    await ElMessageBox.confirm(`Cancel target #${target.id}? History will be retained.`, 'Cancel scheduled target', { type: 'warning' })
    await jobsStore.cancelTarget(target.id, target.jobId)
    selectedEntity.value = await jobsStore.fetchEntity(selectedEntity.value.entityId)
    await loadEvents()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error?.message || 'Cancellation failed')
  }
}

async function resubmitEntityTarget(target) {
  try {
    await jobsStore.resubmitTarget(target.id, target.jobId)
    selectedEntity.value = await jobsStore.fetchEntity(selectedEntity.value.entityId)
    await loadEvents()
    ElMessage.success('Target resubmitted')
  } catch (error) {
    ElMessage.error(error?.message || 'Resubmission failed')
  }
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
  scheduled: 'Scheduled',
  queued: 'Queued',
  publishing: 'Publishing',
  published: 'Published',
  prepared: 'Prepared',
  needs_review: 'Needs review',
  pending: 'Pending',
  running: '發佈中',
  retrying: '重試中',
  succeeded: '已完成',
  failed: '失敗',
  cancelled: '已取消'
}
function statusLabel(s) { return STATUS_LABELS[s] || s }
function statusTagType(s) {
  switch (s) {
    case 'published':
    case 'succeeded': return 'success'
    case 'failed': return 'danger'
    case 'cancelled': return 'info'
    case 'publishing':
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
  min-height: 0;
  display: flex;
  flex-direction: column;
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
  overflow: visible;
  min-height: 720px;
  min-width: 900px;
}

.cal-grid-scroll {
  flex: 1;
  min-height: 720px;
  min-width: 0;
  overflow: auto;
  border-radius: var(--r-lg);
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
  padding: 6px 8px;
  position: relative;
  overflow: visible;
  min-height: 112px;
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
  color: var(--text);
}

.cal-more {
  border: 0;
  background: none;
  color: var(--accent);
  cursor: pointer;
  font-size: 11px;
  text-align: left;
  padding: 2px;
}

.day-event-list {
  display: grid;
  gap: 8px;
  max-height: 65vh;
  overflow: auto;
}

.day-event {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  width: 100%;
  padding: 10px 12px;
  color: var(--text);
  text-align: left;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  cursor: pointer;
}

.day-event:hover { background: var(--raised); }

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