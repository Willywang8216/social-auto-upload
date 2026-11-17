<template>
  <main class="teaching-page">
    <header class="page-heading">
      <div><p class="eyebrow">威威學長 · iamwillywang</p><h1>文章重製工作台</h1>
        <p class="subtitle">文章留在 Notion。這裡查看圖卡、摘要影片與交付紀錄。</p></div>
      <button type="button" class="button" :disabled="loading || mutating" @click="refresh()">{{ loading ? '更新中…' : '重新整理' }}</button>
    </header>
    <div v-if="status && !status.enabled" class="notice">重製準備尚未啟用。既有紀錄可查看；此頁不啟用服務或建立公開貼文。</div>
    <div v-else-if="status && !status.generation_owner" class="notice">目前為唯讀查看。新準備工作由原 Notion 準備流程管理。</div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" class="notice" role="status">{{ notice }}</p>
    <div v-if="pending" class="notice">請求 {{ pending.request_id }} 的結果尚未確認。請重新整理查詢原請求；不自動重新產生。</div>

    <div class="workspace">
      <aside class="task-list" aria-label="準備工作">
        <div class="list-heading"><h2>準備工作</h2><span>{{ tasks.length }}</span></div>
        <p v-if="!tasks.length" class="empty-list">{{ loading ? '正在讀取…' : '尚無已選入的重製工作。請從既有 Notion 準備流程加入。' }}</p>
        <button v-for="row in tasks" :key="row.id" type="button" class="task-row" :class="{ selected: selectedId === row.id }"
          :disabled="loading || mutating" :aria-current="selectedId === row.id ? 'true' : undefined" @click="select(row.id)">
          <span class="task-title">{{ row.source.title }}</span>
          <span class="task-meta">#{{ row.id }} · {{ preparationLabel(row.state) }}</span>
          <span class="task-meta">{{ row.delivery_ids.length }} 個交付格式</span>
        </button>
      </aside>
      <section v-if="!task" class="empty-workspace"><span class="empty-icon" aria-hidden="true">▤</span>
        <h2>{{ loading ? '讀取工作中' : '選一篇文章查看' }}</h2><p>只讀取原 Notion 文章快照，不另建文章編輯器。</p></section>
      <section v-else class="task-detail">
        <header class="detail-heading"><div><h2>{{ task.source.title }}</h2>
          <p>#{{ task.id }} · {{ preparationLabel(task.state) }} · 實際準備 attempts {{ task.attempts }}</p></div>
          <a v-if="notionLink" :href="notionLink" target="_blank" rel="noopener noreferrer">Notion 文章主檔 ↗</a></header>
        <p v-if="!fresh" class="notice">目前只顯示上次快照；最新狀態未確認，操作已停用。</p>
        <p v-if="task.reason_code || task.preview_hold" class="error">暫停原因：{{ task.preview_hold || task.reason_code }}</p>
        <div class="version-strip"><span>來源 SHA <code>{{ short(task.source.article_sha256) }}</code></span>
          <span>套件 <code>{{ task.package_version ? short(task.package_version) : '尚未封存' }}</code></span>
          <span>核准位置：Notion</span></div>
        <nav class="detail-tabs" aria-label="工作檢視">
          <button v-for="item in tabs" :key="item.id" type="button" :class="{ active: tab === item.id }"
            :aria-current="tab === item.id ? 'page' : undefined" @click="tab = item.id">{{ item.label }}</button>
        </nav>
        <div class="detail-body">
          <section v-if="tab === 'article'" class="article-panel">
            <h3>原文章快照 · 唯讀</h3><p class="muted">重新準備只改衍生套件；不寫回文章、核准欄位或網站排程。</p>
            <dl class="identity"><dt>來源版本</dt><dd><code>{{ task.source.article_version }}</code></dd>
              <dt>完整 SHA</dt><dd><code>{{ task.source.article_sha256 }}</code></dd><dt>Slug</dt><dd>{{ task.source.slug }}</dd></dl>
            <pre class="article-text">{{ task.source.manuscript }}</pre>
          </section>
          <section v-else-if="tab === 'captions'">
            <div v-if="!material" class="empty-section">套件準備完成後才有已封存的文案。</div>
            <article v-for="delivery in material?.deliveries || []" :key="delivery.id" class="caption-card">
              <h3>{{ accountLabel(delivery.account_id) }} · {{ delivery.format === 'cards' ? '圖卡' : '摘要影片' }}</h3>
              <pre>{{ delivery.message }}</pre><p class="muted">唯讀 · 修改文案需要新的準備與 Notion 審閱。</p>
            </article>
          </section>
          <section v-else-if="tab === 'checks'">
            <p class="muted">這些是工具檢查紀錄，不取代來源核對、數學驗證或本人核准。</p>
            <div v-for="check in material?.checks || []" :key="check.name" class="check-row">
              <div><strong>{{ check.name }}</strong><p>exit {{ check.exit_code }} · <code>{{ short(check.evidence_sha256) }}</code></p></div>
              <button type="button" class="button" @click="showRecord(check)">查看封存紀錄</button>
            </div>
            <p v-if="!material" class="empty-section">準備尚未完成，或套件檔案無法確認。</p>
            <RepurposeAssetPreview v-if="recordAsset && fresh" :key="recordAsset.sha256" :task-id="task.id" :asset="recordAsset" />
          </section>
          <section v-else-if="tab === 'delivery'">
            <p v-if="!task.releases?.length" class="empty-section">尚未交接。社群交付須先通過原網站流程的實際上線核對。</p>
            <article v-for="release in task.releases || []" :key="release.release_id" class="release-card">
              <h3>Release {{ release.release_id }} <span :class="['receipt-state', { confirmed: allConfirmed(release) }]">
                {{ allConfirmed(release) ? '全部確認送達' : '尚未全部確認' }}</span></h3>
              <p v-if="release.errors?.length" class="error">{{ release.errors.join(' · ') }}</p>
              <div v-for="outcome in release.outcomes || []" :key="outcome.delivery_id" class="delivery-row">
                <div><strong>{{ accountLabel(outcome.account_id) }} · {{ outcome.delivery_id.endsWith(':video') ? '影片' : '圖卡' }}</strong>
                  <p>Job #{{ outcome.job_id }} · targets {{ outcome.target_ids.join(', ') }}</p></div>
                <div class="delivery-state"><span :class="{ confirmed: outcome.actual_delivery_confirmed === true && outcome.status === 'succeeded' }">
                  {{ outcome.actual_delivery_confirmed === true && outcome.status === 'succeeded' ? '已確認送達' : outcome.status === 'active' ? '排程／等待核對' : '未知，需核對' }}</span>
                  <p>{{ taipeiTime(outcome.schedule_at) }}</p></div>
              </div>
              <p class="muted">接受請求、排程與 HTTP 成功都不算送達；未知結果不在此重送。</p>
            </article>
          </section>
          <div v-else class="media-layout">
            <section class="media-panel">
              <div class="media-toolbar"><div class="mode-buttons" aria-label="媒體種類">
                <button v-for="mode in mediaModes" :key="mode.id" type="button" :class="{ active: mediaMode === mode.id }"
                  @click="mediaMode = mode.id; assetIndex = 0">{{ mode.label }}</button></div>
                <select v-if="['deck','bluesky'].includes(mediaMode)" v-model="encoding" aria-label="圖卡編碼">
                  <option value="jpg">實際交付 JPEG</option><option value="png">審閱／封存 PNG</option></select>
              </div>
              <p v-if="mediaMode === 'bluesky'" class="muted">獨立審閱的四卡摘要，不是從較長圖卡直接截取。</p>
              <p v-if="mediaMode === 'video'" class="muted">摘要影片；無自動旁白，不宣稱完整朗讀或完整證明。</p>
              <RepurposeAssetPreview :key="task.id" :task-id="task.id" :asset="fresh ? currentAsset : null" :poster="videoPoster" :alt="currentSlide?.alt_text" />
              <div v-if="mediaAssets.length > 1" class="pagination" aria-label="圖卡頁次">
                <button v-for="(asset, index) in mediaAssets" :key="asset.name" type="button" :class="{ active: assetIndex === index }"
                  :aria-current="assetIndex === index ? 'true' : undefined" @click="assetIndex = index">{{ index + 1 }}</button>
              </div>
              <div v-if="currentAsset" class="asset-facts"><span>{{ currentAsset.width ? `${currentAsset.width} × ${currentAsset.height}` : currentAsset.mime }}</span>
                <code>{{ short(currentAsset.sha256) }}</code></div>
              <details v-if="currentSlide" class="script-details"><summary>查看這張的腳本與原段落參照</summary><h3>{{ currentSlide.title }}</h3>
                <p v-for="(line, index) in currentSlide.lines" :key="index">{{ line }}</p>
                <pre>{{ JSON.stringify(currentSlide.source_refs, null, 2) }}</pre></details>
            </section>
            <aside class="configuration">
              <TeachingBrandKitPicker v-model="theme" :disabled="!canRegenerate" />
              <fieldset class="format-plan" :disabled="!canRegenerate"><legend>新版本交付格式</legend>
                <label v-for="account in TEACHING_ACCOUNTS" :key="account.id" class="format-row">
                  <span>{{ account.label }}<small>圖卡必選 · account {{ account.id }}</small></span>
                  <span><input type="checkbox" :checked="videos.includes(account.id)" @change="toggleVideo(account.id, $event.target.checked)" /> 另發摘要影片</span>
                </label></fieldset>
              <p class="muted">Threads／Bluesky 影片仍須通過實際路由能力。所有格式在核准前準備，交付時不再重寫。</p>
              <button type="button" class="button primary" :disabled="!canRegenerate" @click="regenerate">{{ mutating ? '處理中…' : '重新準備衍生套件' }}</button>
              <p class="muted">新請求、新套件、新審閱。Notion 主文章與網站排程保持不變。</p>
              <p v-if="task.releases?.length" class="notice">已有交接紀錄，停止重製重發；請查看原交付核對。</p>
              <button v-if="['queued','running'].includes(task.state)" type="button" class="button danger" :disabled="!canWrite" @click="cancel">取消這個準備工作</button>
            </aside>
          </div>
        </div>
      </section>
    </div>
  </main>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import TeachingBrandKitPicker from '@/components/TeachingBrandKitPicker.vue'
import RepurposeAssetPreview from '@/components/RepurposeAssetPreview.vue'
import { teachingRepurposeApi as api } from '@/api/teaching-repurpose'
import { getAuthRevision } from '@/utils/auth'
import { TEACHING_ACCOUNTS, deliveryPlan, notionSourceUrl, preparationLabel, previewError, taipeiTime } from '@/utils/teaching-repurpose'

const status = ref(null), kits = ref([]), tasks = ref([]), task = ref(null), selectedId = ref(null)
const loading = ref(false), mutating = ref(false), fresh = ref(false), error = ref(''), notice = ref(''), pending = ref(null)
const tab = ref('media'), mediaMode = ref('deck'), encoding = ref('jpg'), assetIndex = ref(0), recordAsset = ref(null)
const theme = ref(''), videos = ref([43, 58])
const tabs = [{ id: 'media', label: '媒體與設定' }, { id: 'article', label: '來源文章' }, { id: 'captions', label: '平台文案' },
  { id: 'checks', label: '檢查紀錄' }, { id: 'delivery', label: '交付核對' }]
const mediaModes = [{ id: 'deck', label: '教學圖卡' }, { id: 'bluesky', label: 'Bluesky 四卡' },
  { id: 'video', label: '摘要影片' }, { id: 'companion', label: '影片文字稿' }]
let disposed = false, epoch = 0, controller, mutationController, timer
const material = computed(() => task.value?.package?.material || null)
const notionLink = computed(() => notionSourceUrl(task.value?.source?.page_id))
const canWrite = computed(() => status.value?.enabled === true && status.value?.generation_owner === true && fresh.value && !loading.value && !mutating.value)
const canRegenerate = computed(() => canWrite.value && !pending.value && task.value && ['prepared', 'held', 'cancelled'].includes(task.value.state) && !task.value.releases?.length)
const mediaAssets = computed(() => (material.value?.assets || []).filter(a =>
  ['deck', 'bluesky'].includes(mediaMode.value) ? a.kind === mediaMode.value && a.mime === (encoding.value === 'jpg' ? 'image/jpeg' : 'image/png')
    : a.kind === (mediaMode.value === 'video' ? 'summary' : 'companion')))
const currentAsset = computed(() => mediaAssets.value[assetIndex.value] || null)
const currentSlide = computed(() => ['deck', 'bluesky'].includes(mediaMode.value) ? material.value?.storyboard?.[mediaMode.value]?.[assetIndex.value] : null)
const videoPoster = computed(() => fresh.value && mediaMode.value === 'video' ? material.value?.assets.find(a => a.kind === 'poster') : null)
const short = value => value ? value.slice(0, 12) + '…' : '—'
const accountLabel = id => TEACHING_ACCOUNTS.find(a => a.id === id)?.label || '目的地待確認'
function allConfirmed(release) {
  return release.receipt_valid === true && !release.errors?.length && release.state === 'succeeded' && release.all_deliveries_confirmed === true
    && release.outcomes?.length === task.value.delivery_ids.length
    && release.outcomes.every(o => o.status === 'succeeded' && o.actual_delivery_confirmed === true)
}
function configure(value) {
  theme.value = kits.value.find(k => k.version === value.kit_version)?.theme || ''
  videos.value = TEACHING_ACCOUNTS.filter(a => value.delivery_ids.includes(`${a.id}:video`)).map(a => a.id)
  assetIndex.value = 0; recordAsset.value = null
}
function matchesIntent(value, intent) {
  return Number.isSafeInteger(value?.id) && value.id > 0 && value.request_id === intent.request_id
    && value.source?.page_id === intent.page_id && value.source?.article_sha256 === intent.article_sha256
    && value.kit_version === intent.kit_version && JSON.stringify(value.delivery_ids) === JSON.stringify(intent.deliveries)
    && value.requires_new_notion_review === true && value.article_read_only === true
}
async function refresh(id = selectedId.value) {
  if (loading.value || mutating.value || disposed) return
  const sequence = ++epoch, revision = getAuthRevision()
  controller?.abort(); controller = new AbortController(); loading.value = true; error.value = ''
  try {
    const config = { signal: controller.signal }
    const state = await api.status(config)
    if (disposed || sequence !== epoch || revision !== getAuthRevision()) return
    if (state.data?.profile_id !== 4 || JSON.stringify(state.data.account_ids) !== JSON.stringify([43, 58, 61, 125])) throw new Error('teaching_scope_unverified')
    status.value = state.data
    if (state.data.storage_configured === false) {
      task.value = null; tasks.value = []; kits.value = []; selectedId.value = null; fresh.value = false
      if (state.data.enabled) error.value = '私有儲存尚未設定，準備操作已停用。'
      return
    }
    const [layouts, rows] = await Promise.all([api.kits(config), api.tasks(config)])
    if (disposed || sequence !== epoch || revision !== getAuthRevision()) return
    if (!Array.isArray(layouts.data) || !Array.isArray(rows.data)) throw new Error('teaching_scope_unverified')
    kits.value = layouts.data; tasks.value = rows.data
    if (pending.value) {
      const found = tasks.value.find(row => matchesIntent(row, pending.value))
      if (found) { id = found.id; pending.value = null; notice.value = `已找到原請求 #${id}，不建立另一個工作。` }
    }
    if (!id || !tasks.value.some(row => row.id === id)) id = tasks.value[0]?.id || null
    if (!id) { selectedId.value = null; task.value = null; fresh.value = true; return }
    const response = await api.task(id, config)
    if (disposed || sequence !== epoch || revision !== getAuthRevision()) return
    const changed = task.value?.id !== id
    if (response.data?.id !== id || response.data.article_read_only !== true) throw new Error('preparation_identity_unverified')
    selectedId.value = id; task.value = response.data; fresh.value = true
    if (changed) configure(task.value)
  } catch (failure) {
    if (!disposed && sequence === epoch && !controller.signal.aborted) { fresh.value = false; error.value = previewError(failure) }
  } finally {
    if (sequence === epoch) loading.value = false
    if (revision !== getAuthRevision()) { task.value = null; tasks.value = []; status.value = null; fresh.value = false }
  }
}
async function select(id) { await refresh(id) }
function toggleVideo(id, checked) { videos.value = checked ? [...new Set([...videos.value, id])] : videos.value.filter(value => value !== id) }
function showRecord(check) { recordAsset.value = material.value?.assets.find(a => a.name === `check-${check.name}.json`) || null }
async function regenerate() {
  if (!canRegenerate.value) return
  const original = task.value, revision = getAuthRevision(), plan = deliveryPlan(videos.value)
  mutating.value = true; error.value = ''; notice.value = ''; mutationController = new AbortController()
  try {
    const config = { signal: mutationController.signal }
    let kit = original.kit_version
    const oldTheme = kits.value.find(k => k.version === kit)?.theme
    if (theme.value && theme.value !== oldTheme) {
      const response = await api.createKit(theme.value, config)
      if (!/^[a-f0-9]{64}$/.test(response.data?.version || '')) throw new Error('kit_version_unverified')
      kit = response.data.version
    }
    if (disposed || revision !== getAuthRevision()) return
    if (!crypto.randomUUID) throw new Error('secure_request_id_unavailable')
    const intent = { request_id: crypto.randomUUID(), kit_version: kit, deliveries: plan,
      page_id: original.source.page_id, article_sha256: original.source.article_sha256 }
    pending.value = intent
    const response = await api.regenerate(original.id, { request_id: intent.request_id, kit_version: kit, deliveries: plan }, config)
    if (disposed || revision !== getAuthRevision()) return
    if (!matchesIntent(response.data, intent)) throw new Error('preparation_response_unverified')
    pending.value = null; selectedId.value = response.data.id
    notice.value = `已建立新準備請求 #${response.data.id}；完成後仍須回 Notion 重新審閱。`
  } catch (failure) {
    if (!disposed && revision === getAuthRevision()) {
      const code = failure.response?.status
      if (code >= 400 && code < 500) pending.value = null
      error.value = previewError(failure)
    }
  } finally {
    mutating.value = false
    if (!disposed && revision === getAuthRevision()) {
      const message = error.value, unresolved = Boolean(pending.value)
      await refresh()
      if (message && !error.value && (!unresolved || pending.value)) error.value = message
    }
  }
}
async function cancel() {
  if (!canWrite.value || !['queued', 'running'].includes(task.value.state)) return
  mutating.value = true; error.value = ''; mutationController = new AbortController()
  try { await api.cancel(task.value.id, { signal: mutationController.signal }); notice.value = '已要求取消；保留準備與嘗試紀錄。' }
  catch (failure) { error.value = `取消結果待核對：${previewError(failure)}` }
  finally {
    mutating.value = false
    const message = error.value
    if (!disposed) { await refresh(); if (message && !error.value) error.value = message }
  }
}
watch(() => [mediaMode.value, encoding.value], () => { assetIndex.value = 0 })
onMounted(() => { refresh(); timer = setInterval(() => { if (tasks.value.some(t => ['queued', 'running'].includes(t.state))
  || task.value?.releases?.some(r => r.state !== 'succeeded')) refresh() }, 30000) })
watch(getAuthRevision, () => {
  epoch++; controller?.abort(); mutationController?.abort()
  task.value = null; tasks.value = []; kits.value = []; status.value = null; selectedId.value = null
  pending.value = null; fresh.value = false; loading.value = false; mutating.value = false; notice.value = ''
  error.value = '登入狀態已切換，請重新整理後查看。'
}, { flush: 'sync' })
onBeforeUnmount(() => { disposed = true; epoch++; clearInterval(timer); controller?.abort(); mutationController?.abort() })
</script>

<style scoped>
.teaching-page { max-width: 1480px; padding: 28px; margin: 0 auto; color: var(--text); }
.page-heading, .detail-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
.eyebrow { color: #c0633f; font-size: 12px; font-weight: 600; margin: 0 0 8px; letter-spacing: .08em; }
h1 { font-size: 28px; } h2 { font-size: 18px; } h3 { font-size: 14px; }
.subtitle, .muted, .detail-heading p { color: var(--text-2); font-size: 12px; line-height: 1.8; }
.subtitle { font-size: 13px; margin: 10px 0 20px; }
.button { border: 1px solid var(--line-2); border-radius: 8px; padding: 9px 14px; color: var(--text); background: var(--raised); font-size: 12px; }
.button:disabled, .task-row:disabled { opacity: .55; cursor: not-allowed; }
.button.primary { width: 100%; background: #c0633f; color: #fff; border-color: #c0633f; margin-top: 10px; }
.button.danger { color: var(--color-danger); }
.notice, .error { padding: 12px 16px; border-radius: 9px; font-size: 12px; line-height: 1.8; overflow-wrap: anywhere; }
.notice { background: var(--color-warning-light); color: var(--text); border-left: 3px solid #c0633f; }
.error { background: var(--color-danger-light); color: var(--color-danger); }
.workspace { display: grid; grid-template-columns: 230px minmax(0, 1fr); gap: 22px; margin-top: 20px; align-items: start; }
.task-list, .task-detail { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; overflow: hidden; }
.list-heading { display: flex; justify-content: space-between; padding: 18px 16px; border-bottom: 1px solid var(--line); }
.list-heading h2 { font-size: 13px; } .list-heading span { color: var(--text-2); font-size: 12px; }
.task-row { width: 100%; display: grid; gap: 6px; padding: 16px; color: var(--text); text-align: left; background: transparent;
  border: 0; border-bottom: 1px solid var(--line); border-left: 3px solid transparent; }
.task-row.selected { background: var(--panel-2); border-left-color: #c0633f; }
.task-title { font-size: 13px; line-height: 1.6; } .task-meta { font-size: 11px; color: var(--text-2); }
.empty-list { padding: 16px; color: var(--text-2); font-size: 12px; line-height: 1.8; }
.empty-workspace { min-height: 410px; display: grid; align-content: center; justify-items: center; padding: 30px;
  border: 1px dashed var(--line-2); border-radius: 14px; text-align: center; color: var(--text-2); }
.empty-workspace p { font-size: 12px; } .empty-icon { color: #c0633f; font-size: 44px; margin-bottom: 18px; }
.detail-heading { padding: 24px 24px 12px; } .detail-heading a { font-size: 12px; flex-shrink: 0; }
.task-detail > .notice, .task-detail > .error { margin: 12px 24px; }
.version-strip { display: flex; flex-wrap: wrap; gap: 16px; padding: 4px 24px 20px; font-size: 11px; color: var(--text-2); }
.detail-tabs, .mode-buttons { display: flex; flex-wrap: wrap; gap: 4px; }
.detail-tabs { border-bottom: 1px solid var(--line); padding: 0 20px; }
.detail-tabs button, .mode-buttons button, .pagination button { border: 0; background: transparent; color: var(--text-2); padding: 10px; font-size: 12px; }
.detail-tabs button.active { color: var(--text); border-bottom: 2px solid #c0633f; }
.detail-body { padding: 24px; } .media-layout { display: grid; grid-template-columns: minmax(0, 1fr) 285px; gap: 24px; }
.media-panel, .configuration { min-width: 0; } .media-toolbar { display: grid; gap: 10px; margin-bottom: 16px; }
.mode-buttons { border-radius: 8px; background: var(--panel-2); padding: 3px; }
.mode-buttons button.active, .pagination button.active { background: var(--raised); color: var(--text); border-radius: 6px; }
select { max-width: 220px; padding: 8px; border: 1px solid var(--line-2); background: var(--panel-2); color: var(--text); border-radius: 7px; font-size: 12px; }
.pagination { display: flex; justify-content: center; gap: 3px; flex-wrap: wrap; margin-top: 12px; }
.asset-facts { display: flex; justify-content: space-between; gap: 8px; font-size: 11px; color: var(--text-2); margin-top: 12px; }
.script-details { margin-top: 20px; font-size: 12px; line-height: 1.8; } summary { cursor: pointer; color: var(--text-2); }
.format-plan { padding: 0; border: 0; margin: 24px 0 12px; } legend { font-weight: 600; font-size: 13px; margin-bottom: 10px; }
.format-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; font-size: 12px; padding: 12px 0; border-bottom: 1px solid var(--line); }
.format-row small { display: block; font-size: 10px; color: var(--text-2); margin-top: 4px; } input { accent-color: #c0633f; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font: 13px/1.9 var(--font-sans); color: var(--text); }
.article-text { max-height: 650px; overflow: auto; padding: 22px; background: var(--panel-2); border-radius: 10px; }
.identity { font-size: 12px; } dt { color: var(--text-2); margin-top: 12px; } dd { margin: 3px 0 0; overflow-wrap: anywhere; }
.caption-card, .release-card { border: 1px solid var(--line); padding: 20px; border-radius: 10px; margin: 0 0 16px; }
.check-row, .delivery-row { display: flex; justify-content: space-between; gap: 14px; padding: 16px 0; border-bottom: 1px solid var(--line); font-size: 12px; }
.check-row p, .delivery-row p { margin: 6px 0 0; color: var(--text-2); font-size: 11px; }.delivery-state { text-align: right; }
.receipt-state { display: inline-block; margin-left: 10px; font-size: 11px; color: var(--text-2); }.confirmed { color: var(--color-success); }
.empty-section { color: var(--text-2); font-size: 13px; padding: 35px 0; } button:focus-visible, select:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
@media (max-width: 1150px) { .media-layout { grid-template-columns: minmax(0, 1fr); } .configuration { padding-top: 22px; border-top: 1px solid var(--line); } }
@media (max-width: 760px) { .teaching-page { padding: 18px 12px; }.workspace { grid-template-columns: minmax(0, 1fr); gap: 16px; }
  .task-list { max-height: 240px; overflow: auto; }.page-heading, .detail-heading { flex-wrap: wrap; }.detail-body { padding: 16px; }
  .detail-heading { padding: 20px 16px 10px; }.version-strip { padding: 4px 16px 16px; }.detail-tabs { padding: 0 10px; }
  .delivery-row { flex-wrap: wrap; }.delivery-state { text-align: left; }h1 { font-size: 24px; } }
</style>
