<template>
  <div class="jobs-view">
    <section class="entity-queue">
<header class="entity-queue-head">
          <div class="entity-queue-head-title"><h2>發佈佇列</h2><p>{{ entityTotal }} 個內容項目 · 每組媒體一張卡片</p></div>
          <el-button @click="allDates = !allDates">{{ allDates ? '顯示全部日期' : '今天' }}</el-button>
          <div class="entity-filter-row">
            <el-date-picker v-model="entityDateRange" type="daterange" value-format="YYYY-MM-DD" start-placeholder="開始日期" end-placeholder="結束日期" />
            <el-select v-model="entityPlatformFilters" multiple collapse-tags clearable placeholder="平台">
              <el-option v-for="platform in platformOptions" :key="platform.value" :label="platform.label" :value="platform.value" />
            </el-select>
            <el-select v-model="entityProfileFilters" multiple collapse-tags clearable placeholder="個人檔案">
              <el-option v-for="profile in profiles" :key="profile.id" :label="profile.name" :value="profile.id" />
            </el-select>
            <el-select v-model="entityAccountFilters" multiple collapse-tags clearable placeholder="帳號">
              <el-option v-for="account in profileAccounts" :key="account.id" :label="`${account.profileName} · ${account.platform} · ${account.nickname || account.accountName}`" :value="account.id" />
            </el-select>
            <el-input v-model="entityKeyword" clearable placeholder="搜尋標題、文案或媒體名稱" />
            <el-select v-model="entityStatusFilter" clearable placeholder="狀態" @change="loadEntities(true)">
              <el-option v-for="status in entityStatusOptions" :key="status" :label="entityStatusLabel(status)" :value="status" />
            </el-select>
            <el-button @click="loadJobs">工作紀錄</el-button>
            <el-button type="primary" @click="drainNow" :loading="draining">排空佇列</el-button>
          </div>
        </header>
      <div v-if="entityLoading && !entities.length" class="entity-loading">正在載入排程…</div>
      <el-empty v-else-if="entities.length === 0" description="目前沒有符合條件的排程" />
      <div v-else class="entity-card-grid">
        <article v-for="entity in entities" :key="entity.entityId" class="entity-card">
          <div class="entity-card-media-grid">
            <div
              v-for="media in entity.mediaItems?.slice(0, 3) || []"
              :key="media.fileRecordId || media.filename"
              class="entity-card-media-item"
              :title="`${media.filename} · ${mediaStateLabel(media)}`"
            >
              <template v-if="mediaPreviewSource(media) && media.mediaType === 'video'">
                <video :src="mediaPreviewSource(media)" preload="metadata" muted />
              </template>
              <img
                v-else-if="mediaPreviewSource(media)"
                :src="mediaPreviewSource(media)"
                :alt="media.filename"
                loading="lazy"
              />
              <!-- No preview is not the same as a broken image: the file is
                   usually offloaded to Drive, so say so rather than leaving a
                   blank tile that reads as a failed load. -->
              <span
                v-else
                class="entity-card-media-placeholder"
                :class="{ archived: Boolean(media.archive?.archived) }"
              >
                <span class="entity-card-media-badge">{{ media.archive?.archived ? '已封存' : '無預覽' }}</span>
                <span class="entity-card-media-name">{{ media.filename }}</span>
                <a
                  v-if="media.archive?.openUrl"
                  :href="media.archive.openUrl"
                  target="_blank"
                  rel="noopener"
                  class="entity-card-media-link"
                  :title="`在 ${media.archive.label || '雲端'} 開啟 ${media.filename}`"
                  @click.stop
                >{{ media.archive.label || '雲端' }}</a>
              </span>
            </div>
            <span v-if="(entity.mediaItems?.length || 0) > 3" class="entity-card-media-overflow">+{{ entity.mediaItems.length - 3 }}</span>
          </div>
          <div class="entity-card-body">
            <div class="entity-card-heading">
              <strong>{{ entity.posts?.find((post) => post.draft?.title || post.draft?.message)?.draft?.title || entity.mediaItems?.[0]?.filename || entity.entityId }}</strong>
              <el-tag :type="entityTagType(entity.status)" effect="plain">{{ entityStatusLabel(entity.status) }}</el-tag>
            </div>
            <span>{{ entity.scheduledAt || '尚未排程' }}</span>
            <div class="entity-platforms">
              <el-tag v-for="post in entity.posts || []" :key="post.id" size="small" effect="plain">{{ post.platform }} · {{ post.accounts?.map((account) => account.name).filter(Boolean).join(', ') || '—' }}</el-tag>
            </div>
            <p v-if="entity.jobs?.some((job) => job.failedTargets)" class="entity-failure-count">{{ entity.jobs.reduce((total, job) => total + job.failedTargets, 0) }} 個目的地發佈失敗</p>
            <div class="entity-card-links">
              <a v-for="link in artifactLinks(entity.artifacts)" :key="link.key" :href="link.url" target="_blank" rel="noopener">{{ link.text }}</a>
              <button @click="openEntity(entity)">管理內容</button>
            </div>
          </div>
        </article>
      </div>
      <div class="jobs-pagination" v-if="entityHasMore"><span>Showing {{ entities.length }} of {{ entityTotal }} entities</span><el-button :loading="entityLoadingMore" @click="loadMoreEntities">Load more entities</el-button></div>
    </section>

    <el-drawer
      v-model="entityDrawerVisible"
      :title="entityDetails?.mediaGroup?.name || entityDetails?.entityId || 'Publish entity'"
      direction="rtl"
      size="min(760px, 95vw)"
    >
      <div v-if="entityDetails" class="entity-drawer-content">
        <section class="entity-drawer-section">
          <h3>整體資訊</h3>
          <div class="entity-summary-row">
            <el-tag :type="entityTagType(entityDetails.status)" effect="plain">{{ entityStatusLabel(entityDetails.status) }}</el-tag>
            <span>{{ entityDetails.profiles?.map((profile) => profile.name).filter(Boolean).join('、') || entityDetails.profile?.name || '未指定個人檔案' }}</span>
            <span>{{ entityDetails.scheduledAt || '尚未排程' }}</span>
          </div>
        </section>
        <section class="entity-drawer-section">
          <h3>本次媒體</h3>
          <div class="drawer-media-grid">
            <div v-for="media in entityDetails.mediaItems || []" :key="media.fileRecordId || media.filename">
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
              <span>{{ media.filename }} · {{ mediaStateLabel(media) }}</span>
              <a v-if="media.archive?.openUrl" :href="media.archive.openUrl" target="_blank" rel="noopener">在 {{ media.archive.label || '雲端' }} 開啟</a>
              <a v-else-if="media.publicUrl" :href="media.publicUrl" target="_blank" rel="noopener">開啟媒體</a>
            </div>
          </div>
        </section>
        <section v-for="post in entityDetails.posts || []" :key="post.id" class="entity-post-detail">
          <header><h3>{{ post.platform }} · {{ post.accounts?.map((account) => account.name).filter(Boolean).join(', ') || '未設定帳號' }}</h3><el-tag :type="entityTagType(post.status)" effect="plain">{{ entityStatusLabel(post.status) }}</el-tag></header>
          <div v-if="editingEntityPostId === post.id"><el-input v-model="editingEntityCopy" type="textarea" :rows="4" /><el-button type="primary" @click="saveEntityCopy(post)">儲存文案</el-button><el-button @click="editingEntityPostId = null">取消</el-button></div>
          <div v-else class="entity-copy-block"><span>此平台文案</span><p>{{ post.draft?.message || '尚未填寫文案' }}</p><el-button v-if="post.status === 'queued' || post.status === 'ready'" size="small" @click="editingEntityPostId = post.id; editingEntityCopy = post.draft?.message || ''">編輯文案</el-button><el-button v-if="post.status === 'queued' || post.status === 'ready'" size="small" type="danger" @click="cancelPostTargets(post)">取消此貼文排程</el-button></div>
          <div v-for="job in (entityDetails.jobs || []).filter((item) => item.targets?.some((target) => target.fileRef === `campaign_post:${post.id}`))" :key="job.id"><div v-for="target in job.targets.filter((item) => item.fileRef === `campaign_post:${post.id}`)" :key="target.id" class="entity-target-row"><el-tag :type="entityTagType(target.status)" effect="plain">{{ entityStatusLabel(target.status) }}</el-tag><span>{{ target.accountName }} · {{ target.scheduleAt || '立即發佈' }}</span><span v-if="target.lastError" class="entity-error">{{ target.lastError }}</span><el-date-picker v-if="target.status === 'pending' || target.status === 'retrying'" v-model="target._editSchedule" type="datetime" format="YYYY-MM-DD HH:mm" value-format="YYYY-MM-DDTHH:mm:00" /><el-button v-if="target._editSchedule && (target.status === 'pending' || target.status === 'retrying')" size="small" @click="manageTarget('reschedule', target)">儲存時間</el-button><el-button v-if="target.status === 'pending' || target.status === 'retrying'" size="small" type="danger" @click="manageTarget('cancel', target)">取消</el-button><el-button v-if="target.status === 'failed'" size="small" type="warning" @click="manageTarget('retry', target)">重試</el-button></div></div>
        </section>
        <section v-if="entityDetails.artifacts?.some((item) => item.url)" class="entity-drawer-section entity-links"><h3>媒體連結</h3><a v-for="link in artifactLinks(entityDetails.artifacts, 5)" :key="link.key" :href="link.url" target="_blank" rel="noopener">{{ link.text === '開啟媒體' ? '開啟媒體' : link.text + ' · 開啟連結' }}</a></section>
      </div>
    </el-drawer>

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
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import PublishJobProgress from '@/components/PublishJobProgress.vue'
import { jobsApi } from '@/api/jobs'
import { useJobsStore, JOB_STATUS } from '@/stores/jobs'
import { useProfilesStore } from '@/stores/profiles'
import { getPlatformLabel, getPlatformTagType, PUBLISH_PLATFORM_OPTIONS } from '@/utils/platforms'
import { mediaPreviewSource, mediaStateLabel } from '@/utils/mediaState'
import { artifactLinks } from '@/utils/entityLinks'

const profilesStore = useProfilesStore()
const profiles = computed(() => profilesStore.profiles)
const profileAccounts = computed(() => profiles.value.flatMap((profile) => (profilesStore.accountsByProfile[profile.id] || []).map((account) => ({ ...account, profileName: profile.name }))))
const entityDateRange = ref([])
const entityPlatformFilters = ref([])
const entityProfileFilters = ref([])
const entityAccountFilters = ref([])
const entityKeyword = ref('')
const jobsStore = useJobsStore()
const route = useRoute()
const router = useRouter()
const loading = ref(false)
const loadingMore = ref(false)
const hasMore = ref(false)
const pageSize = 50
const draining = ref(false)
const statusFilter = ref('')
const platformFilter = ref('')
const entityDrawerVisible = ref(false)
const entityDetails = ref(null)
const editingEntityPostId = ref(null)
const editingEntityCopy = ref('')
const entities = ref([])
const entityTotal = ref(0)
const entityHasMore = ref(false)
const entityStatusFilter = ref('')
const allDates = ref(false)
const entityLoading = ref(false)
const entityLoadingMore = ref(false)
const entityPageSize = 50
const entityStatusOptions = ['scheduled', 'queued', 'publishing', 'published', 'failed', 'cancelled', 'needs_review']
const currentDate = new Date()
const todayDate = `${currentDate.getFullYear()}-${String(currentDate.getMonth() + 1).padStart(2, '0')}-${String(currentDate.getDate()).padStart(2, '0')}`
let entitySearchTimer = null
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

watch([entityDateRange, entityPlatformFilters, entityProfileFilters, entityAccountFilters, entityKeyword, entityStatusFilter, allDates], () => {
  if (entitySearchTimer) window.clearTimeout(entitySearchTimer)
  entitySearchTimer = window.setTimeout(() => loadEntities(true), 250)
})

// One source for the current filter set: the list, "load more" and the
// background refresh all have to ask the same question.
function entityQuery(overrides = {}) {
  return {
    status: entityStatusFilter.value || undefined,
    date: allDates.value ? undefined : todayDate,
    from: entityDateRange.value?.[0],
    to: entityDateRange.value?.[1],
    platforms: entityPlatformFilters.value.join(','),
    profileIds: entityProfileFilters.value.join(','),
    accountIds: entityAccountFilters.value.join(','),
    q: entityKeyword.value.trim() || undefined,
    ...overrides
  }
}

async function loadEntities(reset = false) {
  if (entityLoading.value) return
  if (reset) {
    entities.value = []
    entityHasMore.value = false
  }
  entityLoading.value = true
  try {
    const response = await jobsStore.refreshEntities(
      entityQuery({ limit: entityPageSize, offset: reset ? 0 : entities.value.length })
    )
    entities.value = reset ? response.items || [] : [...entities.value, ...(response.items || [])]
    entityTotal.value = response.total || 0
    entityHasMore.value = Boolean(response.hasMore)
  } catch (error) {
    ElMessage.error(error?.message || 'Unable to load content entities')
  } finally {
    entityLoading.value = false
  }
}

// The 30s poll used to call loadEntities(true), which empties `entities` before
// refetching — so the whole grid blanked and repainted every half minute, which
// reads as constant flashing and makes the list hard to read. Refresh in place
// instead: same filters, never fewer cards than are already on screen, no
// spinner, and silent on failure because a background poll is not a user action.
async function refreshEntitiesQuietly() {
  if (entityLoading.value || entityLoadingMore.value || document.hidden) return
  try {
    const response = await jobsStore.refreshEntities(
      entityQuery({ limit: Math.max(entityPageSize, entities.value.length), offset: 0 })
    )
    entities.value = response?.items || []
    entityTotal.value = response?.total ?? entityTotal.value
    entityHasMore.value = Boolean(response?.hasMore)
  } catch {
    // Keep what is on screen; the next tick tries again.
  }
}

async function loadMoreEntities() {
  if (entityLoadingMore.value || !entityHasMore.value) return
  entityLoadingMore.value = true
  try {
    const response = await jobsStore.refreshEntities(entityQuery({
      limit: entityPageSize,
      offset: entities.value.length,
      q: entityKeyword.value.trim() || undefined
    }))
    entities.value.push(...(response.items || []))
    entityTotal.value = response.total || 0
    entityHasMore.value = Boolean(response.hasMore)
  } catch (error) {
    ElMessage.error(error?.message || 'Unable to load more entities')
  } finally {
    entityLoadingMore.value = false
  }
}

async function openEntity(entity) {
  entityDrawerVisible.value = true
  try {
    entityDetails.value = await jobsStore.fetchEntity(entity.entityId)
    if (entityDetails.value?.entityId) {
      router.replace({ query: { ...route.query, entity: entityDetails.value.entityId } })
    }
  } catch (error) {
    ElMessage.error(error?.message || 'Unable to load entity')
  }
}

async function cancelPostTargets(post) {
  const targets = (entityDetails.value?.jobs || []).flatMap((job) => job.targets || [])
    .filter((target) => target.fileRef === `campaign_post:${post.id}` && ['pending', 'retrying'].includes(target.status))
  if (!targets.length) return
  try {
    await ElMessageBox.confirm(`Cancel ${targets.length} pending target(s) for this post? History will be retained.`, 'Cancel post', { type: 'warning' })
    for (const target of targets) await jobsStore.cancelTarget(target.id, target.jobId)
    entityDetails.value = await jobsStore.fetchEntity(entityDetails.value.entityId)
    await loadEntities(true)
    ElMessage.success('Pending targets cancelled')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error?.message || 'Post cancellation failed')
  }
}

async function saveEntityCopy(post) {
  try {
    await jobsStore.updateEntityPost(entityDetails.value.entityId, post.id, {
      ...(post.draft || {}),
      message: editingEntityCopy.value
    })
    entityDetails.value = await jobsStore.fetchEntity(entityDetails.value.entityId)
    editingEntityPostId.value = null
    await loadEntities(true)
    ElMessage.success('Copy updated')
  } catch (error) {
    ElMessage.error(error?.message || 'Copy update failed')
  }
}

async function manageTarget(action, target) {
  try {
    if (action === 'reschedule') {
      await jobsStore.rescheduleTarget(target.id, new Date(target._editSchedule).toISOString().slice(0, 19), target.jobId)
    } else if (action === 'cancel') {
      await ElMessageBox.confirm('Cancel this target and keep its history?', 'Cancel scheduled target', { type: 'warning' })
      await jobsStore.cancelTarget(target.id, target.jobId)
    } else {
      await jobsStore.resubmitTarget(target.id, target.jobId)
    }
    entityDetails.value = await jobsStore.fetchEntity(entityDetails.value.entityId)
    await loadEntities(true)
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error?.message || 'Target update failed')
  }
}

function entityTagType(status) {
  if (['published', 'succeeded'].includes(status)) return 'success'
  if (status === 'failed') return 'danger'
  if (['publishing', 'retrying'].includes(status)) return 'warning'
  return 'info'
}

function entityStatusLabel(status) {
  return ({ scheduled: '已排程', queued: '佇列中', publishing: '發佈中', published: '已發佈', prepared: '已準備', needs_review: '需要檢查', failed: '失敗', cancelled: '已取消', pending: '待處理', retrying: '重試中', succeeded: '已完成' })[status] || status || '未知'
}

async function loadJobs() {
  loading.value = true
  try {
    const response = await jobsStore.refreshList({
      status: statusFilter.value || undefined,
      platform: platformFilter.value || undefined,
      limit: pageSize,
      offset: 0
    })
    hasMore.value = response.hasMore
  } catch (error) {
    console.error('載入任務清單失敗:', error)
  } finally {
    loading.value = false
  }
}

async function loadMore() {
  if (loadingMore.value || !hasMore.value) return
  loadingMore.value = true
  try {
    const result = await jobsStore.refreshList({
      status: statusFilter.value || undefined,
      platform: platformFilter.value || undefined,
      limit: pageSize,
      offset: jobs.value.length
    })
    hasMore.value = result.hasMore
  } catch (error) {
    ElMessage.error(error?.message || '載入更多任務失敗')
  } finally {
    loadingMore.value = false
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

onMounted(async () => {
  await profilesStore.refreshProfiles()
  await Promise.all(profiles.value.map((profile) => profilesStore.fetchAccountsForProfile(profile.id)))
  await loadEntities(true)
  if (route.query.entity) {
    await openEntity({ entityId: String(route.query.entity) })
    router.replace({ query: { ...route.query, entity: undefined } })
  } else if (route.query.job) {
    const jobId = Number(route.query.job)
    if (Number.isInteger(jobId) && jobId > 0) {
      await openDetail({ id: jobId, status: 'pending' })
    }
  }
  // Refresh recent jobs and entity cards while the page remains open.
  refreshTimer = window.setInterval(refreshEntitiesQuietly, 30000)
})

onBeforeUnmount(() => {
  if (entitySearchTimer) window.clearTimeout(entitySearchTimer)
  if (refreshTimer) {
    window.clearInterval(refreshTimer)
    refreshTimer = null
  }
  jobsStore.stopAllPolling()
})
</script>

<style lang="scss" scoped>
.jobs-view {
  .page-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: var(--space-6);

    h1 {
      font-size: 24px;
      color: var(--text);
      margin: 0;
    }

    .page-actions {
      display: flex;
      gap: var(--space-3);
    }
  }

  .jobs-list {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--r-lg);
    padding: var(--space-6);
  }

  .entity-queue {
    margin-bottom: var(--space-6);
  }

  .entity-queue-head {
    display:flex;
    flex-direction:column;
    align-items:stretch;
    gap:12px;
    margin-bottom:16px;
  }
  .entity-queue-head-title h2 { font-size:18px; margin:0; }
  .entity-queue-head-title p { color:var(--text-2); margin:3px 0 0; font-size:13px; }

  .entity-card-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: var(--space-3);
  }

  .entity-card {
    display: flex;
    min-width: 0;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: var(--r-lg);
    background: var(--panel);
  }

  .entity-card-media-grid { width: 126px; min-height: 138px; flex-shrink: 0; display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); grid-auto-rows:minmax(58px,1fr); gap:2px; position:relative; background:var(--raised); overflow:hidden; }
  .entity-card-media-item { min-width:0; min-height:0; overflow:hidden; background:var(--raised); display:grid; place-items:center; }
  .entity-card-media-item img, .entity-card-media-item video { width:100%; height:100%; object-fit:cover; }
  .entity-card-media-placeholder { color:var(--text-3); font-size:11px; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:2px; padding:4px; text-align:center; overflow:hidden; min-width:0; }
  .entity-card-media-badge { font-size:9px; line-height:1.4; padding:0 4px; border-radius:8px; border:1px solid var(--line); background:var(--bg-2); color:var(--text-2); max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .entity-card-media-placeholder.archived .entity-card-media-badge { color:var(--accent); border-color:var(--accent); }
  .entity-card-media-name { color:var(--text-2); font-size:10px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:100%; }
  .entity-card-media-link { color:var(--accent); font-size:10px; text-decoration:none; max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .entity-card-media-link:hover { text-decoration:underline; }
  .media-missing { color:var(--text-3); font-size:11px; display:grid; place-items:center; min-height:60px; border-radius:6px; background:var(--raised); }
  .entity-card-media-overflow { position:absolute; right:4px; bottom:4px; padding:2px 5px; border-radius:10px; background:rgba(0,0,0,.68); color:#fff; font-size:11px; }

  .entity-card-body { flex: 1; min-width: 0; padding: 12px; }
  .entity-card-heading { display: flex; justify-content: space-between; gap: 8px; align-items: flex-start; }
  .entity-card-heading strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .entity-card-body p { margin: 6px 0; color: var(--text-2); font-size: 12px; }
  .entity-platforms { display: flex; gap: 5px; flex-wrap: wrap; }
  .entity-failure-count { color: var(--color-danger) !important; }
  .entity-card-links { display: flex; gap: 12px; margin-top: 10px; }
  .entity-card-links a, .entity-card-links button, .drawer-artifacts a { color: var(--accent); font-size: 12px; }
  .entity-card-links button { border: 0; background: none; cursor: pointer; }
  .entity-filter-row {
    display:grid;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    align-items:center;
    gap:8px;
  }
  .entity-filter-row .el-input { min-width:180px; }
  .entity-drawer-content { padding: 16px; }
  .entity-drawer-section { padding: 16px; margin-bottom: 14px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }
  .entity-drawer-section h3 { margin: 0 0 12px; font-size: 15px; }
  .entity-summary-row { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
  .drawer-media-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 12px; }
  .drawer-media-grid > div { display: grid; gap: 6px; align-content: start; font-size: 12px; overflow-wrap: anywhere; }
  .drawer-media-grid img, .drawer-media-grid video { width: 100%; max-height: 150px; object-fit: cover; border-radius: 6px; }
  .entity-post-detail { padding: 16px; margin-bottom: 14px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }
  .entity-post-detail header { display:flex; justify-content:space-between; align-items:center; gap:10px; margin-bottom:12px; }
  .entity-post-detail h3 { margin:0; font-size:15px; }
  .entity-copy-block { padding: 12px; border-radius: 6px; background: var(--raised); }
  .entity-copy-block > span { color: var(--text-3); font-size: 12px; }
  .entity-copy-block p { white-space: pre-wrap; overflow-wrap: anywhere; }
  .entity-target-row { display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:10px 0; border-top:1px solid var(--line); }
  .entity-error { color:var(--color-danger); overflow-wrap:anywhere; }
  .entity-links { display:flex; flex-direction:column; align-items:flex-start; gap:8px; }
  .entity-links a { color:var(--accent); }
  .entity-filter-row { display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
  .entity-filter-row .el-input { width:240px; }
  @media (max-width: 760px) { .entity-filter-row { justify-content:flex-start; } .entity-filter-row > * { min-width: 140px; flex: 1 1 auto; } .entity-filter-row .el-input { width:auto; } }

  @media (max-width: 680px) {
    .entity-card-grid { grid-template-columns: 1fr; }
    .entity-card { flex-direction: column; }
    .entity-card-media-grid { width:100%; height:180px; min-height:180px; grid-template-columns:repeat(3,minmax(0,1fr)); grid-auto-rows:1fr; }
  }

  .jobs-pagination {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: var(--space-4);
    color: var(--text-2);
    font-size: 13px;
  }

  .job-progress-cell {
    display: flex;
    flex-direction: column;
    gap: 4px;

    .counters {
      font-size: 12px;
      color: var(--text-2);
    }
  }
}
</style>
