<template>
  <div class="calendar-view">
    <!-- Calendar header -->
    <div class="cal-head">
      <div class="cal-title">{{ MONTHS[view.m] }} {{ view.y }}</div>
      <div class="cal-nav">
        <button class="cal-nav-btn" @click="move(-1)" title="上個月">
          <component :is="icons.collapse" :width="15" :height="15" />
        </button>
        <button class="cal-nav-btn cal-today" @click="setToday">今天</button>
        <button class="cal-nav-btn" @click="move(1)" title="下個月">
          <component :is="icons.expand" :width="15" :height="15" />
        </button>
      </div>
      <div class="spacer"></div>
      <el-switch
        v-model="showAll"
        style="--el-switch-on-color: var(--color-primary);"
        active-text="包含失敗"
        inactive-text="僅顯示待發佈"
        title="顯示已失敗的排程"
      />
      <router-link to="/publish/compose" class="btn-primary">
        <component :is="icons.plus" :width="16" :height="16" /> 排程貼文
      </router-link>
    </div>

    <div class="cal-filters">
      <el-select v-model="profileFilters" multiple collapse-tags clearable placeholder="個人檔案">
        <el-option v-for="profile in profiles" :key="profile.id" :label="profile.name" :value="profile.id" />
      </el-select>
      <el-select v-model="platformFilters" multiple collapse-tags clearable placeholder="平台">
        <el-option v-for="platform in platformOptions" :key="platform.value" :label="platform.label" :value="platform.value" />
      </el-select>
      <el-select v-model="accountFilters" multiple collapse-tags clearable placeholder="帳號">
        <el-option v-for="account in profileAccounts" :key="account.id" :label="`${account.profileName} · ${account.platform} · ${account.nickname || account.accountName}`" :value="account.id" />
      </el-select>
      <el-input v-model="keyword" clearable placeholder="搜尋標題、文案或媒體名稱" />
      <el-button v-if="activeFilterCount" class="cal-filter-clear" @click="clearFilters">
        清除篩選（{{ activeFilterCount }}）
      </el-button>
    </div>

    <!-- Calendar grid -->
    <div class="cal-grid-scroll">
      <div v-if="loading" class="cal-loading">正在載入排程…</div>
      <el-empty v-else-if="events.length === 0" description="本月沒有排程" />
      <div v-else class="cal-grid">
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
            v-for="ev in cell.evs"
            :key="ev.eventId"
            class="cal-ev"
            :class="[`st-${ev.targetStatus}`, { err: ev.targetStatus === 'failed' }]"
            :title="`${ev.time} · ${ev.destinationLabel} · ${ev.title}`"
            @click="openEvent(ev)"
          >
            <span class="cd"></span>
            <span class="ct">{{ ev.time }}</span>
            <span class="cl">{{ ev.summary }}</span>
            <button
              v-if="ev.hasMore"
              class="cal-ev-more"
              :title="`檢視完整內容：${ev.title}`"
              @click.stop="openEvent(ev)"
            >更多</button>
          </div>
          <button v-if="cell.hiddenCount" class="cal-more" @click="showDay(cell)">查看更多（{{ cell.hiddenCount }}）</button>
        </div>
      </div>
    </div>

    <!-- Event detail / action dialog -->
    <el-dialog v-model="dayDialogVisible" :title="selectedDayLabel" width="min(720px, 92vw)">
      <div class="day-event-list">
        <button v-for="ev in selectedDayEvents" :key="ev.eventId" class="day-event" @click="dayDialogVisible = false; openEvent(ev)">
          <span>{{ ev.time }} · {{ ev.destinationLabel }} · {{ ev.summary }}</span>
          <el-tag :type="statusTagType(ev.targetStatus)" effect="plain">{{ statusLabel(ev.targetStatus) }}</el-tag>
        </button>
      </div>
    </el-dialog>

    <el-dialog v-model="dialogVisible" :title="selectedEntity?.posts?.find((post) => post.draft?.title || post.draft?.message)?.draft?.title || selectedEntity?.posts?.find((post) => post.draft?.message)?.draft?.message || selectedEntity?.mediaItems?.[0]?.filename || '排程內容詳情'" width="min(860px, 94vw)" top="5vh">
          <div v-if="detailLoading" class="entity-loading">正在載入內容詳情…</div>
          <template v-else-if="selectedEntity">
            <div class="detail-toolbar">
              <el-button @click="copyListUrl">複製此內容連結</el-button>
            </div>
        <div v-if="selectedEntity" class="entity-summary">
          <section class="detail-section detail-overview">
            <h3>整體資訊與個人檔案</h3>
        <section class="entity-summary-row">
              <el-tag :type="statusTagType(selectedEntity.status)" effect="plain">{{ statusLabel(selectedEntity.status) }}</el-tag>
              <span>{{ selectedEntity.profiles?.map((profile) => profile.name).filter(Boolean).join('、') || selectedEntity.profile?.name || '未指定個人檔案' }}</span>
              <span>{{ selectedEntity.scheduledAt || '尚未排程' }}</span>
            </section>
          </section>
          <div class="entity-media detail-section">
            <h3>媒體組</h3>
          <article v-for="media in selectedEntity.mediaItems" :key="media.fileRecordId || media.filename" class="entity-media-item">
            <template v-if="mediaPreviewSource(media) && media.mediaType === 'video'">
              <video :src="mediaPreviewSource(media)" controls preload="metadata" />
            </template>
            <img
              v-else-if="mediaPreviewSource(media)"
              :src="mediaPreviewSource(media)"
              :alt="media.filename"
              loading="lazy"
            />
            <div v-else class="media-missing">預覽不可用</div>
            <div class="media-name">{{ media.filename }}<span class="media-state"> · {{ mediaStateLabel(media) }}</span></div>
            <a v-if="media.archive?.openUrl" :href="media.archive.openUrl" target="_blank" rel="noopener">在 {{ media.archive.label || '雲端' }} 開啟</a>
            <a v-else-if="media.publicUrl" :href="media.publicUrl" target="_blank" rel="noopener">開啟媒體</a>
          </article>
        </div>
          <section v-for="post in selectedEntity.posts || []" :key="post.id" class="entity-post detail-section">
            <header><h3>{{ platformLabel(post.platform) }} · {{ post.accounts?.map((account) => account.name).filter(Boolean).join(', ') || '帳號未設定' }}</h3><el-tag :type="statusTagType(post.status)" effect="plain">{{ statusLabel(post.status) }}</el-tag></header>
          <div class="post-copy"><span class="copy-label">目的地文案</span>{{ post.draft?.message || '尚未填寫文案' }}</div>
          <div v-if="editingPostId === post.id" class="copy-editor">
            <el-input v-model="editedCopy" type="textarea" :rows="4" />
            <el-button type="primary" @click="saveCopy(post)">儲存文案</el-button>
            <el-button @click="editingPostId = null">取消</el-button>
          </div>
          <el-button v-else-if="post.status === 'queued' || post.status === 'ready'" size="small" @click="editingPostId = post.id; editedCopy = post.draft?.message || ''">編輯文案</el-button>
          <div v-for="job in (selectedEntity.jobs || []).filter((item) => item.targets?.some((target) => target.fileRef === `campaign_post:${post.id}`))" :key="job.id" class="entity-targets">
            <div v-for="target in job.targets.filter((item) => item.fileRef === `campaign_post:${post.id}`)" :key="target.id" class="entity-target">
              <el-tag :type="statusTagType(target.status)" effect="plain">{{ statusLabel(target.status) }}</el-tag>
              <span>{{ target.accountName || '未設定帳號' }}</span><span>{{ target.scheduleAt || '立即發佈' }}</span>
              <span v-if="target.lastError" class="ev-error">{{ target.lastError }}</span>
              <el-date-picker v-if="target.status === 'pending' || target.status === 'retrying'" v-model="target._editSchedule" type="datetime" format="YYYY-MM-DD HH:mm" value-format="YYYY-MM-DDTHH:mm:00" placeholder="選擇新時間" />
              <el-button v-if="target._editSchedule && (target.status === 'pending' || target.status === 'retrying')" size="small" @click="rescheduleEntityTarget(target)">儲存時間</el-button>
              <el-button v-if="target.status === 'pending' || target.status === 'retrying'" size="small" type="danger" @click="cancelEntityTarget(target)">取消排程</el-button>
              <el-button v-if="target.status === 'failed'" size="small" type="warning" @click="resubmitEntityTarget(target)">重試發佈</el-button>
            </div>
          </div>
        </section>
        <section v-if="selectedEntity.artifacts?.length" class="entity-artifacts">
          <h4>已準備的媒體連結</h4>
          <a v-for="link in artifactLinks(selectedEntity.artifacts, 5)" :key="link.key" :href="link.url" target="_blank" rel="noopener">{{ link.text === '開啟媒體' ? '開啟媒體' : link.text + ' · 開啟連結' }}</a>
        </section>
        </div>
      </template>
      <template #footer><el-button @click="dialogVisible = false">關閉</el-button></template>
    </el-dialog>

  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { icons } from '@/utils/icons'
import { useJobsStore } from '@/stores/jobs'
import { useProfilesStore } from '@/stores/profiles'
import { getPlatformLabel, getPlatformTagType, PUBLISH_PLATFORM_OPTIONS } from '@/utils/platforms'
import { mediaPreviewSource, mediaStateLabel } from '@/utils/mediaState'
import { artifactLinks } from '@/utils/entityLinks'
import {
  applyCalendarFiltersToQuery,
  currentMonthKey,
  parseCalendarFilters,
  sameCalendarFilterQuery
} from '@/utils/calendarFilters'

const DOW = ['週日', '週一', '週二', '週三', '週四', '週五', '週六']
// Entries render on ONE line; a longer title gets a 更多 button that opens the
// full detail dialog instead of stretching the day cell.
const EVENT_SUMMARY_LENGTH = 34
const MONTHS = ['1 月', '2 月', '3 月', '4 月', '5 月', '6 月',
  '7 月', '8 月', '9 月', '10 月', '11 月', '12 月']

const today = new Date()
const view = ref({ y: today.getFullYear(), m: today.getMonth() })
// The visible month as the URL/API spell it; the watcher below keeps the query
// in step so a month view is shareable and survives a refresh.
const monthKey = computed(() => `${view.value.y}-${String(view.value.m + 1).padStart(2, '0')}`)

const jobsStore = useJobsStore()
const profilesStore = useProfilesStore()
const profiles = computed(() => profilesStore.profiles)
const profileAccounts = computed(() => profiles.value.flatMap((profile) =>
  (profilesStore.accountsByProfile[profile.id] || []).map((account) => ({ ...account, profileName: profile.name }))
))
const platformOptions = PUBLISH_PLATFORM_OPTIONS
const route = useRoute()
const router = useRouter()
const initialFilters = parseCalendarFilters(route.query)
// A ?month=YYYY-MM link opens that month; otherwise today's.
if (initialFilters.month) {
  const [year, month] = initialFilters.month.split('-').map(Number)
  view.value = { y: year, m: month - 1 }
}
const profileFilters = ref(initialFilters.profiles)
const platformFilters = ref(initialFilters.platforms)
const accountFilters = ref(initialFilters.accounts)
const keyword = ref(initialFilters.q)
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
let filterTimer = null
let requestSequence = 0
let activeController = null

function syncFilterQuery() {
  const next = applyCalendarFiltersToQuery(route.query, {
    profiles: profileFilters.value,
    platforms: platformFilters.value,
    accounts: accountFilters.value,
    q: keyword.value,
    month: monthKey.value
  })
  // The no-op guard is also what stops the URL -> state watcher from bouncing
  // back into another navigation.
  if (sameCalendarFilterQuery(next, route.query)) return
  // A filter tweak is a refinement, not a new place: replace, so Back does not
  // step through every checkbox.
  router.replace({ query: next })
}

watch([profileFilters, platformFilters, accountFilters, keyword], () => {
  if (filterTimer) window.clearTimeout(filterTimer)
  filterTimer = window.setTimeout(() => { syncFilterQuery(); loadEvents() }, 250)
})

// Back/forward, or a pasted link, drives the filters and the month too.
watch(() => route.query, (query) => {
  const next = parseCalendarFilters(query)
  if (next.profiles.join(',') !== profileFilters.value.join(',')) profileFilters.value = next.profiles
  if (next.platforms.join(',') !== profileFilters.value.join(',')) platformFilters.value = next.platforms
  if (next.accounts.join(',') !== accountFilters.value.join(',')) accountFilters.value = next.accounts
  if (next.q !== keyword.value) keyword.value = next.q
  // An absent month means the current one (sync omits it as the default).
  const wantedMonth = next.month || currentMonthKey()
  if (wantedMonth !== monthKey.value) {
    const [year, month] = wantedMonth.split('-').map(Number)
    view.value = { y: year, m: month - 1 }
  }
})

const first = computed(() => new Date(view.value.y, view.value.m, 1).getDay())
const days = computed(() => new Date(view.value.y, view.value.m + 1, 0).getDate())
const prevDays = computed(() => new Date(view.value.y, view.value.m, 0).getDate())

const cells = computed(() => {
  const result = []
  const eventsByDay = new Map()
  for (const event of events.value) {
    const key = event.scheduleAt?.slice(0, 10)
    if (!key) continue
    const dayEvents = eventsByDay.get(key) || []
    dayEvents.push(event)
    eventsByDay.set(key, dayEvents)
  }
  const start = prevDays.value - first.value + 1
  for (let i = 0; i < first.value; i++) result.push({ d: start + i, dim: true, evs: [], today: false, loading: false })
  for (let d = 1; d <= days.value; d++) {
    const isToday = view.value.y === today.getFullYear() && view.value.m === today.getMonth() && d === today.getDate()
    const dayEvents = (eventsByDay.get(keyOf(d)) || []).sort((a, b) => a.scheduleAt.localeCompare(b.scheduleAt))
    result.push({
      d,
      dim: false,
      today: isToday,
      loading: loading.value,
      evs: dayEvents.slice(0, 5),
      hiddenCount: Math.max(0, dayEvents.length - 5)
    })
  }
  while (result.length % 7) result.push({ d: 1, dim: true, evs: [], today: false, loading: false })
  return result
})

function keyOf(d) {
  return `${view.value.y}-${String(view.value.m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`
}

async function loadEvents() {
  const sequence = ++requestSequence
  activeController?.abort()
  activeController = new AbortController()
  const controller = activeController
  loading.value = true
  try {
    const month = `${view.value.y}-${String(view.value.m + 1).padStart(2, '0')}`
    const result = await jobsStore.refreshEntities({
      month,
      status: showAll.value ? 'scheduled,queued,publishing,failed,cancelled,published' : 'scheduled,queued,publishing',
      profileIds: profileFilters.value.join(','),
      platforms: platformFilters.value.join(','),
      accountIds: accountFilters.value.join(','),
      q: keyword.value.trim(),
      limit: 200
    }, { signal: controller.signal })
    if (sequence !== requestSequence) return
    events.value = (result.items || []).flatMap((entity) => {
      const postsByRef = new Map((entity.posts || []).map((post) => [`campaign_post:${post.id}`, post]))
      const destinations = (entity.jobs || []).flatMap((job) => (job.targets || []).map((target) => ({
        ...target,
        jobId: target.jobId || job.id,
        post: postsByRef.get(target.fileRef)
      }))).filter((target) => target.scheduleAt)
      const mediaName = entity.mediaItems?.[0]?.filename
      const postCopy = entity.posts?.find((post) => post.draft?.title || post.draft?.message)?.draft
      const title = postCopy?.title || postCopy?.message || mediaName || `Publish ${entity.entityId}`
      if (!destinations.length && entity.scheduledAt) {
        destinations.push({
          id: `entity:${entity.entityId}`,
          scheduleAt: entity.scheduledAt,
          status: entity.status,
          accountName: '',
          post: null
        })
      }
      return destinations.map((target) => {
        const targetDraft = target.post?.draft
        const targetTitle = targetDraft?.title || targetDraft?.message || title
        const normalizedTitle = String(targetTitle).replace(/\s+/g, ' ').trim()
        return {
          ...entity,
          eventId: `${entity.entityId}:${target.jobId || 'entity'}:${target.id}`,
          targetId: target.id,
          jobId: target.jobId,
          targetStatus: target.status,
          targetFileRef: target.fileRef,
          scheduleAt: target.scheduleAt,
          time: target.scheduleAt.slice(11, 16),
          title: targetTitle,
          destinationLabel: [target.post?.platform, target.accountName].filter(Boolean).join(' · ') || '內容排程',
          summary: normalizedTitle.slice(0, EVENT_SUMMARY_LENGTH),
          hasMore: normalizedTitle.length > EVENT_SUMMARY_LENGTH
        }
      })
    })
  } catch (err) {
    if (sequence === requestSequence && err?.code !== 'ERR_CANCELED' && err?.name !== 'CanceledError') {
      ElMessage.error(err?.message || '載入行事曆失敗')
    }
  } finally {
    if (sequence === requestSequence) loading.value = false
  }
}

watch(monthKey, () => { syncFilterQuery(); loadEvents() })
watch(showAll, loadEvents)
onMounted(async () => {
  await profilesStore.refreshProfiles()
  await Promise.all(profiles.value.map((profile) => profilesStore.fetchAccountsForProfile(profile.id)))
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
  const key = keyOf(cell.d)
  selectedDayEvents.value = events.value
    .filter((event) => event.scheduleAt?.slice(0, 10) === key)
    .sort((a, b) => a.scheduleAt.localeCompare(b.scheduleAt))
  selectedDayLabel.value = `${MONTHS[view.value.m]} ${cell.d}`
  dayDialogVisible.value = true
}

async function copyListUrl() {
  if (!selectedEntity.value) return
  try {
    await navigator.clipboard.writeText(`${window.location.origin}/#/publish/calendar?entity=${encodeURIComponent(selectedEntity.value.entityId)}`)
    ElMessage.success('已複製內容連結')
  } catch {
    ElMessage.error('無法複製連結，請從網址列複製')
  }
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
  scheduled: '已排程',
  queued: '佇列中',
  publishing: '發佈中',
  published: '已發佈',
  prepared: '已準備',
  needs_review: '需要檢查',
  pending: '待處理',
  running: '發佈中',
  retrying: '重試中',
  succeeded: '已完成',
  failed: '失敗',
  cancelled: '已取消'
}
function statusLabel(s) { return STATUS_LABELS[s] || s }

const activeFilterCount = computed(() =>
  profileFilters.value.length
  + platformFilters.value.length
  + accountFilters.value.length
  + (keyword.value.trim() ? 1 : 0)
)

function clearFilters() {
  profileFilters.value = []
  platformFilters.value = []
  accountFilters.value = []
  keyword.value = ''
}

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
  flex-wrap: wrap;
}

.cal-filters {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 8px;
  margin: -10px 0 var(--space-4);
}

.cal-filter-clear { justify-self: start; }

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
  grid-template-columns: repeat(7, minmax(0, 1fr));
  gap: 1px;
  background: var(--line);
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  overflow: hidden;
  min-height: 0;
  min-width: 0;
}

.cal-grid-scroll {
  flex: 1;
  min-height: 0;
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
  padding: 6px 7px;
  position: relative;
  overflow: hidden;
  min-width: 0;
  min-height: 104px;
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
  min-width: 0;
  gap: 4px;
  font-size: 11px;
  line-height: 1.2;
  padding: 2px 3px;
  border-radius: 4px;
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
  /* One line per entry: a wrapped summary made day cells tall and the calendar
     hard to scan. The full text is one click away (the entry, or 更多). */
  white-space: nowrap;
}

.cal-ev-more {
  flex-shrink: 0;
  border: 0;
  background: none;
  padding: 0 2px;
  color: var(--accent);
  cursor: pointer;
  font-size: 10px;
  line-height: 1.2;
}

.cal-ev-more:hover { text-decoration: underline; }

.cal-ev .cp {
  background: var(--bg-2);
  border-radius: 4px;
  padding: 0 4px;
  font-size: 10px;
  color: var(--text-2);
  flex-shrink: 0;
}

  .detail-toolbar { display:flex; justify-content:flex-end; margin-bottom:10px; }
  .entity-summary {
    display: grid;
  gap: 14px;
  max-height: 68vh;
  overflow-y: auto;
  padding: 2px 4px 16px;
}

.detail-section {
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
}

.detail-section h3 { margin: 0 0 12px; font-size: 15px; }
.entity-media { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 12px; }
.entity-media-item { display: grid; align-content: start; gap: 8px; min-width: 0; font-size: 12px; }
.entity-media-item img, .entity-media-item video { width: 100%; max-height: 180px; object-fit: cover; border-radius: 6px; background: var(--raised); }
.media-name { overflow-wrap: anywhere; }
.media-state { color: var(--text-2); }
.copy-label { display: block; margin-bottom: 6px; color: var(--text-3); font-size: 12px; }
.entity-post header { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 10px; }
.post-copy { padding: 12px; white-space: pre-wrap; overflow-wrap: anywhere; background: var(--raised); border-radius: 6px; }
.entity-targets { display: grid; gap: 8px; margin-top: 12px; }
  .entity-target { display: grid; grid-template-columns: auto minmax(90px,1fr) minmax(120px,auto) minmax(100px,1fr) auto auto; gap: 8px; align-items: center; padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--raised); }
  .entity-target .ev-error { grid-column: 1 / -1; }
  .entity-post { display: grid; gap: 12px; }
  .entity-post header { margin-bottom: 0; }
  .entity-post h3 { overflow-wrap: anywhere; }
  .entity-artifacts { padding: 16px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }
  .entity-artifacts h4 { margin: 0 0 10px; }
  @media (max-width: 680px) {
    .entity-target { grid-template-columns: 1fr; }
    .entity-summary { max-height: 68vh; }
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