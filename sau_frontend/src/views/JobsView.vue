<template>
  <div class="jobs-view">
    <div class="page-header">
      <h1>任務中心</h1>
      <div class="page-actions">
        <el-select v-model="statusFilter" placeholder="全部狀態" clearable @change="loadJobs()">
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

    <section class="entity-queue">
      <header class="entity-queue-head">
        <div><h2>Content queue</h2><p>{{ entityTotal }} content entities · one card per shared media set</p></div>
        <el-select v-model="entityStatusFilter" clearable placeholder="All entity statuses" @change="loadEntities(true)">
          <el-option v-for="status in entityStatusOptions" :key="status" :label="entityStatusLabel(status)" :value="status" />
        </el-select>
        <el-button :loading="entityLoading" @click="loadEntities(true)">Refresh entities</el-button>
      </header>
      <el-empty v-if="!entityLoading && entities.length === 0" description="No content entities" />
      <div class="entity-card-grid">
        <article v-for="entity in entities" :key="entity.entityId" class="entity-card">
          <div class="entity-card-media">
            <video v-if="entity.mediaItems?.[0]?.mediaType === 'video' && entity.mediaItems?.[0]?.previewUrl" :src="entity.mediaItems[0].previewUrl" preload="metadata" />
            <img v-else-if="entity.mediaItems?.[0]?.previewUrl" :src="entity.mediaItems[0].previewUrl" :alt="entity.mediaItems[0].filename" />
            <span v-else>Media unavailable</span>
          </div>
          <div class="entity-card-body">
            <div class="entity-card-heading">
              <strong>{{ entity.posts?.find((post) => post.draft?.title || post.draft?.message)?.draft?.title || entity.posts?.find((post) => post.draft?.message)?.draft?.message || entity.mediaItems?.[0]?.filename || entity.entityId }}</strong>
              <el-tag :type="entityTagType(entity.status)" effect="plain">{{ entityStatusLabel(entity.status) }}</el-tag>
            </div>
            <p>{{ [...new Set((entity.campaigns || []).map((campaign) => campaign.profileId))].length > 1 ? `${new Set((entity.campaigns || []).map((campaign) => campaign.profileId)).size} profiles` : entity.profile?.name || 'Profile unavailable' }} · {{ (entity.jobs || []).flatMap((job) => job.targets || []).length }} destinations · {{ entity.scheduledAt || 'No schedule' }}</p>
            <div class="entity-platforms">
              <el-tag v-for="post in entity.posts || []" :key="post.id" size="small" effect="plain">{{ post.platform }} · {{ post.accounts?.map((account) => account.name).filter(Boolean).join(', ') || '—' }}</el-tag>
            </div>
            <p v-if="entity.jobs?.some((job) => job.failedTargets)" class="entity-failure-count">{{ entity.jobs.reduce((total, job) => total + job.failedTargets, 0) }} failed destination(s)</p>
            <div class="entity-card-links">
              <a v-for="artifact in entity.artifacts?.filter((item) => item.url).slice(0, 2)" :key="artifact.id || artifact.url" :href="artifact.url" target="_blank" rel="noopener">Open media</a>
              <button @click="openEntity(entity)">Manage entity</button>
            </div>
          </div>
        </article>
      </div>
      <div class="jobs-pagination" v-if="entityHasMore"><span>Showing {{ entities.length }} of {{ entityTotal }} entities</span><el-button :loading="entityLoadingMore" @click="loadMoreEntities">Load more entities</el-button></div>
    </section>

    <el-empty v-if="!loading && jobs.length === 0" description="目前沒有任務" />

    <div v-if="jobs.length" class="jobs-list">
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
            {{ scope.row.title || scope.row.payload?.title || '—' }}
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
      <div class="jobs-pagination">
        <span>Showing {{ jobs.length }} jobs</span>
        <el-button v-if="hasMore" :loading="loadingMore" @click="loadMore">Load more</el-button>
      </div>
    </div>

    <el-drawer
      v-model="entityDrawerVisible"
      :title="entityDetails?.mediaGroup?.name || entityDetails?.entityId || 'Publish entity'"
      direction="rtl"
      size="min(760px, 95vw)"
    >
      <div v-if="entityDetails" class="entity-drawer-content">
        <div class="entity-summary-row"><el-tag :type="entityTagType(entityDetails.status)" effect="plain">{{ entityStatusLabel(entityDetails.status) }}</el-tag><span>{{ entityDetails.profile?.name || 'Multiple profiles' }}</span><span>{{ entityDetails.scheduledAt || 'No schedule' }}</span></div>
        <div class="drawer-media-grid">
          <div v-for="media in entityDetails.mediaItems || []" :key="media.fileRecordId || media.filename">
            <video v-if="media.mediaType === 'video' && media.previewUrl" :src="media.previewUrl" controls preload="metadata" />
            <img v-else-if="media.mediaType === 'image' && media.previewUrl" :src="media.previewUrl" :alt="media.filename" />
            <span>{{ media.filename }}</span>
          </div>
        </div>
        <section v-for="post in entityDetails.posts || []" :key="post.id" class="entity-post-detail">
          <h3>{{ post.platform }} · {{ post.accounts?.map((account) => account.name).filter(Boolean).join(', ') || 'Account unavailable' }}</h3>
          <el-tag :type="entityTagType(post.status)" effect="plain">{{ entityStatusLabel(post.status) }}</el-tag>
          <template v-if="editingEntityPostId === post.id">
            <el-input v-model="editingEntityCopy" type="textarea" :rows="4" />
            <el-button type="primary" @click="saveEntityCopy(post)">Save copy</el-button>
            <el-button @click="editingEntityPostId = null">Discard</el-button>
          </template>
          <template v-else>
            <p>{{ post.draft?.message || 'No copy saved' }}</p>
            <el-button v-if="post.status === 'queued' || post.status === 'ready'" size="small" @click="editingEntityPostId = post.id; editingEntityCopy = post.draft?.message || ''">Edit copy</el-button>
            <el-button v-if="post.status === 'queued' || post.status === 'ready'" size="small" type="danger" @click="cancelPostTargets(post)">Cancel post</el-button>
        </template>
          <div v-for="job in (entityDetails.jobs || []).filter((item) => item.targets?.some((target) => target.fileRef === `campaign_post:${post.id}`))" :key="job.id">
            <div v-for="target in job.targets.filter((item) => item.fileRef === `campaign_post:${post.id}`)" :key="target.id" class="entity-target-row">
              <el-tag :type="entityTagType(target.status)" effect="plain">{{ entityStatusLabel(target.status) }}</el-tag><span>{{ target.accountName }} · {{ target.scheduleAt || 'Immediate' }}</span>
              <span v-if="target.lastError" class="entity-error">{{ target.lastError }}</span>
              <el-date-picker v-if="target.status === 'pending' || target.status === 'retrying'" v-model="target._editSchedule" type="datetime" format="YYYY-MM-DD HH:mm" value-format="YYYY-MM-DDTHH:mm:00" />
              <el-button v-if="target._editSchedule && (target.status === 'pending' || target.status === 'retrying')" size="small" @click="manageTarget('reschedule', target)">Save time</el-button>
              <el-button v-if="target.status === 'pending' || target.status === 'retrying'" size="small" type="danger" @click="manageTarget('cancel', target)">Cancel</el-button>
              <el-button v-if="target.status === 'failed'" size="small" type="warning" @click="manageTarget('retry', target)">Retry</el-button>
            </div>
          </div>
        </section>
        <section class="drawer-artifacts"><a v-for="artifact in entityDetails.artifacts?.filter((item) => item.url)" :key="artifact.id || artifact.url" :href="artifact.url" target="_blank" rel="noopener">{{ artifact.role || artifact.kind || 'Media' }} · Open public link</a></section>
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
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import PublishJobProgress from '@/components/PublishJobProgress.vue'
import { jobsApi } from '@/api/jobs'
import { useJobsStore, JOB_STATUS } from '@/stores/jobs'
import { getPlatformLabel, getPlatformTagType, PUBLISH_PLATFORM_OPTIONS } from '@/utils/platforms'

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
const entityLoading = ref(false)
const entityLoadingMore = ref(false)
const entityPageSize = 50
const entityStatusOptions = ['scheduled', 'queued', 'publishing', 'published', 'failed', 'cancelled', 'needs_review']
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

async function loadEntities(reset = false) {
  if (entityLoading.value) return
  if (reset) {
    entities.value = []
    entityHasMore.value = false
  }
  entityLoading.value = true
  try {
    const offset = reset ? 0 : entities.value.length
    const response = await jobsStore.refreshEntities({ limit: entityPageSize, offset, status: entityStatusFilter.value || undefined })
    entities.value = reset ? response.items || [] : [...entities.value, ...(response.items || [])]
    entityTotal.value = response.total || 0
    entityHasMore.value = Boolean(response.hasMore)
  } catch (error) {
    ElMessage.error(error?.message || 'Unable to load content entities')
  } finally {
    entityLoading.value = false
  }
}

async function loadMoreEntities() {
  if (entityLoadingMore.value || !entityHasMore.value) return
  entityLoadingMore.value = true
  try {
    const response = await jobsStore.refreshEntities({ limit: entityPageSize, offset: entities.value.length, status: entityStatusFilter.value || undefined })
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
  return ({ scheduled: 'Scheduled', queued: 'Queued', publishing: 'Publishing', published: 'Published', prepared: 'Prepared', needs_review: 'Needs review', failed: 'Failed', cancelled: 'Cancelled', pending: 'Pending', retrying: 'Retrying', succeeded: 'Succeeded' })[status] || status || 'Unknown'
}

async function loadJobs() {
  loading.value = true
  try {
    const result = await jobsStore.refreshList({
      status: statusFilter.value || undefined,
      platform: platformFilter.value || undefined,
      limit: pageSize,
      offset: 0
    })
    hasMore.value = result.hasMore
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
  await Promise.all([loadJobs(), loadEntities(true)])
  if (route.query.entity) {
    await openEntity({ entityId: String(route.query.entity) })
  } else if (route.query.job) {
    const jobId = Number(route.query.job)
    if (Number.isInteger(jobId) && jobId > 0) {
      await openDetail({ id: jobId, status: 'pending' })
    }
  }
  // Refresh recent jobs and entity cards while the page remains open.
  refreshTimer = window.setInterval(() => { loadJobs(); loadEntities(true) }, 5000)
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
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: var(--space-4);

    h2 { font-size: 18px; margin: 0; }
    p { color: var(--text-2); margin: 3px 0 0; font-size: 13px; }
  }

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

  .entity-card-media {
    width: 110px;
    min-height: 138px;
    flex-shrink: 0;
    display: grid;
    place-items: center;
    background: var(--raised);
    color: var(--text-3);
    font-size: 12px;
    overflow: hidden;

    img, video { width: 100%; height: 100%; object-fit: cover; }
  }

  .entity-card-body { flex: 1; min-width: 0; padding: 12px; }
  .entity-card-heading { display: flex; justify-content: space-between; gap: 8px; align-items: flex-start; }
  .entity-card-heading strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .entity-card-body p { margin: 6px 0; color: var(--text-2); font-size: 12px; }
  .entity-platforms { display: flex; gap: 5px; flex-wrap: wrap; }
  .entity-failure-count { color: var(--color-danger) !important; }
  .entity-card-links { display: flex; gap: 12px; margin-top: 10px; }
  .entity-card-links a, .entity-card-links button, .drawer-artifacts a { color: var(--accent); font-size: 12px; }
  .entity-card-links button { border: 0; background: none; cursor: pointer; }
  .entity-drawer-content { padding-bottom: 28px; }
  .entity-summary-row { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-bottom: 16px; }
  .drawer-media-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 10px; margin-bottom: 18px; }
  .drawer-media-grid > div { display: grid; gap: 5px; font-size: 12px; overflow: hidden; }
  .drawer-media-grid img, .drawer-media-grid video { width: 100%; max-height: 140px; object-fit: cover; border-radius: 8px; }
  .entity-post-detail { padding: 14px; border: 1px solid var(--line); border-radius: var(--r-md); margin: 12px 0; }
  .entity-post-detail h3 { font-size: 14px; margin: 0 0 8px; }
  .entity-post-detail p { white-space: pre-wrap; }
  .entity-target-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px 0; border-top: 1px solid var(--line); }
  .entity-error { color: var(--color-danger); overflow-wrap: anywhere; }
  .drawer-artifacts { display: flex; flex-wrap: wrap; gap: 12px; }

  @media (max-width: 680px) {
    .entity-card-grid { grid-template-columns: 1fr; }
    .entity-card { flex-direction: column; }
    .entity-card-media { width: 100%; height: 180px; }
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
