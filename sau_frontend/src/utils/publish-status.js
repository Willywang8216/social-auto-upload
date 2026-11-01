export const JOB_STATUS = Object.freeze({
  PENDING: 'pending', RUNNING: 'running', SUCCEEDED: 'succeeded',
  FAILED: 'failed', CANCELLED: 'cancelled', BLOCKED: 'blocked',
  AWAITING_DELIVERY: 'awaiting_delivery', NEEDS_REVIEW: 'needs_review',
})
export const TARGET_STATUS = Object.freeze({ ...JOB_STATUS, RETRYING: 'retrying' })

const LABELS = {
  pending: '佇列中', queued: '佇列中', scheduled: '已排程',
  preparing: '媒體準備中', prepared: '媒體已準備', ready: '已準備',
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
  if (['running', 'publishing', 'retrying', 'blocked', 'needs_review', 'processing'].includes(status)) return 'warning'
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

export function normalizeTarget(target) {
  return {
    ...target,
    accountRef: target.accountRef ?? target.account_ref,
    fileRef: target.fileRef ?? target.file_ref,
    jobId: target.jobId ?? target.job_id,
    lastError: target.lastError ?? target.last_error,
    nextAttemptAt: target.nextAttemptAt ?? target.next_attempt_at,
    errorCategory: target.errorCategory ?? target.error_category,
    deliveryState: target.deliveryState ?? target.delivery_state,
  }
}

export function targetReliabilityDetails(target) {
  const normalized = normalizeTarget(target)
  return [
    publishActionHint(normalized.status),
    normalized.nextAttemptAt ? `下次嘗試：${normalized.nextAttemptAt}` : '',
    normalized.errorCategory ? `錯誤分類：${normalized.errorCategory}` : '',
    normalized.deliveryState ? `交付：${publishStatusLabel(normalized.deliveryState)}` : '',
  ].filter(Boolean).join(' · ')
}

export function canRetryTarget(target) {
  if (!target || target.status !== 'failed') return false
  const { deliveryState, errorCategory } = normalizeTarget(target)
  // An ambiguous side effect must be reconciled, never blindly sent again.
  return (deliveryState == null || ['not_sent', 'not_started', 'not_attempted', 'failed', 'rejected', 'not_delivered'].includes(deliveryState))
    && !/ambiguous|uncertain|needs_review|unknown/i.test(errorCategory || '')
}
