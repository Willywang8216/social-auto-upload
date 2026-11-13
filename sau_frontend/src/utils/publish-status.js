export const JOB_STATUS = Object.freeze({
  PENDING: 'pending', RUNNING: 'running', SUCCEEDED: 'succeeded',
  FAILED: 'failed', CANCELLED: 'cancelled', BLOCKED: 'blocked',
  AWAITING_DELIVERY: 'awaiting_delivery', NEEDS_REVIEW: 'needs_review',
})
export const TARGET_STATUS = Object.freeze({ ...JOB_STATUS, RETRYING: 'retrying' })

const LABELS = {
  pending: '佇列中', queued: '佇列中', scheduled: '已排程',
  preparing: '媒體準備中', prepared: '媒體已準備', ready: '已準備',
  waiting_media: '等待媒體驗證', waiting_dependency: '等待依賴恢復',
  waiting_capacity: '等待可用容量', waiting_configuration: '等待路由驗證', blocked_intent: '發佈意圖受阻', configured_unverified: '已設定・尚未驗證',
  running: '發佈中', publishing: '發佈中', retrying: '等待重試',
  succeeded: '已完成', published: '已發佈', failed: '失敗', cancelled: '已取消',
  blocked: '受阻（需處理）', awaiting_delivery: '等待平台確認', needs_review: '需人工確認',
  processing: '平台處理中', uploading: '上傳中', publish_complete: '已發佈',
  not_sent: '尚未送交平台', submitted: '已送交平台', delivered: '已確認交付', unknown: '交付結果未確認',
}

export function publishStatusLabel(status) {
  return LABELS[status] || status || '未知'
}

export function publishStatusType(status) {
  if (['succeeded', 'published', 'publish_complete'].includes(status)) return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'awaiting_delivery') return 'primary'
  if (['running', 'publishing', 'retrying', 'blocked', 'needs_review', 'processing', 'waiting_dependency', 'waiting_capacity', 'blocked_intent'].includes(status)) return 'warning'
  return 'info'
}

export function isTerminalJobStatus(status) {
  return ['succeeded', 'failed', 'cancelled'].includes(status)
}

export function needsPublishAction(status) {
  return ['blocked', 'needs_review'].includes(status)
}

export function publishActionHint(status) {
  if (status === 'needs_review') return '平台可能已收到內容；請先至平台核對，勿直接重試。'
  if (status === 'blocked') return '請排除帳號、設定或媒體問題後再確認狀態。'
  if (status === 'awaiting_delivery') return '已送交平台，尚未確認發佈；請勿重複送出。'
  return ''
}

function object(value) {
  if (typeof value === 'string') {
    try { value = JSON.parse(value) } catch { return null }
  }
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null
}

function safeDetail(value) {
  return String(value || '')
    .replace(/\b(?:Bearer|Basic)\s+\S+/gi, '[redacted credential]')
    .replace(/\b(?:token|secret|authorization|password|cookie|config(?:_json)?)\s*[:=].*/gi, '[redacted context]')
    .replace(/\b[a-z][a-z0-9+.-]*:\/\/[^\s<>"']+/gi, '[redacted-url]')
    .replace(/(?:[A-Z]:[\\/]|\/(?:home|tmp|var|app|media|mnt|opt)\/)[^\s<>"']+/gi, '[redacted-path]')
    .slice(0, 180)
}

export function normalizeTarget(target) {
  return {
    ...target,
    accountRef: target.accountRef ?? target.account_ref,
    fileRef: target.fileRef ?? target.file_ref,
    jobId: target.jobId ?? target.job_id,
    lastError: target.lastError ?? target.last_error,
    nextAttemptAt: target.nextAttemptAt ?? target.next_attempt_at,
    scheduleAt: target.scheduleAt ?? target.schedule_at,
    errorCategory: target.errorCategory ?? target.error_category,
    deliveryState: target.deliveryState ?? target.delivery_state,
    retryEligibility: object(target.retryEligibility ?? target.retry_eligibility),
  }
}

export function publishReadinessDetails(value) {
  const stage = object(value)
  if (!stage) return ''
  // A stored stage is not a delivery receipt and never replaces the job label.
  const verified = Date.parse(stage.verifiedAt)
  const fresh = Number.isFinite(verified) && verified <= Date.now() && Date.now() - verified < 900000
  const declared = stage.phase || stage.status
  const phase = declared === 'ready'
    ? (fresh && stage.evidenceFresh !== false && stage.artifactFingerprint && stage.destinationFingerprint && stage.provider ? 'ready' : 'configured_unverified')
    : ['preparing', 'waiting_configuration', 'waiting_media', 'waiting_dependency', 'waiting_capacity', 'blocked_intent'].includes(declared) ? declared : 'configured_unverified'
  const progress = object(stage.partProgress ?? stage.part_progress)
  return [
    `階段：${publishStatusLabel(phase)}`,
    stage.provider ? `路由：${safeDetail(stage.provider)}` : '',
    stage.reasonCode ? `原因：${safeDetail(stage.reasonCode)}` : '',
    safeDetail(stage.reason),
    stage.verifiedAt || stage.checkedAt ? `驗證：${safeDetail(stage.verifiedAt || stage.checkedAt)}` : '',
    stage.expiresAt ? `證據到期：${safeDetail(stage.expiresAt)}` : '',
    stage.nextAttemptAt ? `下次重試／解除等待：${safeDetail(stage.nextAttemptAt)}` : '',
    stage.nextSlot ? `下個時段：${safeDetail(stage.nextSlot)}` : '',
    progress && Number.isInteger(progress.completeParts) && Number.isInteger(progress.totalParts) && progress.completeParts >= 0 && progress.completeParts <= progress.totalParts
      ? `完整分段：${progress.completeParts}/${progress.totalParts}` : '',
  ].filter(Boolean).join(' · ')
}

export function targetReliabilityDetails(target) {
  const normalized = normalizeTarget(target)
  const checkpoint = object(target.checkpoint ?? target.checkpoint_json)
  const stage = target.readiness ?? checkpoint?.readiness
  const eligibility = normalized.retryEligibility
  return [
    publishActionHint(normalized.status),
    publishReadinessDetails(stage),
    normalized.nextAttemptAt ? `下次嘗試：${safeDetail(normalized.nextAttemptAt)}` : '',
    normalized.scheduleAt ? `下個時段：${safeDetail(normalized.scheduleAt)}` : '',
    normalized.errorCategory ? `錯誤分類：${safeDetail(normalized.errorCategory)}` : '',
    normalized.deliveryState ? `交付：${publishStatusLabel(normalized.deliveryState)}` : '',
    eligibility?.allowed === false ? `重試受阻：${safeDetail(eligibility.reason || eligibility.reasonCode || eligibility.reason_code)}` : '',
  ].filter(Boolean).join(' · ')
}

export function canRetryTarget(target) {
  if (!target) return false
  // Backend evidence-backed recovery is the sole positive authorization. Older
  // API responses fail closed; failure/cancellation labels cannot grant retry.
  return normalizeTarget(target).retryEligibility?.allowed === true
}
