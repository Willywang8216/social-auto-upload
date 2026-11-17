// Private preview invariants. SHA identifies reviewed bytes, not approval.
export const TEACHING_ACCOUNTS = [
  { id: 43, label: 'Instagram' }, { id: 58, label: 'Facebook' },
  { id: 61, label: 'Threads' }, { id: 125, label: 'Bluesky' },
]
const HASH = /^[a-f0-9]{64}$/
const MAX_ASSET = 128 * 1024 * 1024
const MIMES = new Set(['image/png', 'image/jpeg', 'video/mp4', 'text/plain', 'application/json'])

export async function verifyPreviewBlob(blob, asset, signal) {
  if (!(blob instanceof Blob) || !asset || !HASH.test(asset.sha256 || '')
      || !Number.isSafeInteger(asset.size_bytes) || asset.size_bytes < 1 || asset.size_bytes > MAX_ASSET
      || !MIMES.has(asset.mime) || blob.size !== asset.size_bytes
      || blob.type.split(';')[0].trim().toLowerCase() !== asset.mime) {
    throw new Error('preview_bytes_unverified')
  }
  if (!globalThis.crypto?.subtle) throw new Error('preview_crypto_unavailable')
  if (signal?.aborted) throw new DOMException('Preview cancelled', 'AbortError')
  const raw = await blob.arrayBuffer()
  const digest = await crypto.subtle.digest('SHA-256', raw)
  if (signal?.aborted) throw new DOMException('Preview cancelled', 'AbortError')
  const hash = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('')
  if (hash !== asset.sha256) throw new Error('preview_checksum_changed')
  return blob
}

export function deliveryPlan(videos) {
  if (!Array.isArray(videos) || new Set(videos).size !== videos.length || videos.some(id => !TEACHING_ACCOUNTS.some(a => a.id === id))) {
    throw new Error('delivery_scope')
  }
  return TEACHING_ACCOUNTS.flatMap(({ id }) => [`${id}:cards`, ...(videos.includes(id) ? [`${id}:video`] : [])])
}

export function notionSourceUrl(pageId) {
  return /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(pageId || '')
    ? `https://www.notion.so/${pageId.replaceAll('-', '')}` : null
}

export function taipeiTime(value) {
  if (!value) return '尚未排定'
  const raw = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`
  const date = new Date(raw)
  if (!Number.isFinite(date.getTime())) return '排程資料無法確認'
  return new Intl.DateTimeFormat('zh-TW', { timeZone: 'Asia/Taipei', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(date) + ' 台北'
}

export function preparationLabel(state) {
  return { queued: '等待準備', running: '準備中', prepared: '已準備，待審閱', held: '準備暫停', cancelled: '已取消' }[state] || '狀態待確認'
}

export function previewError(error) {
  const code = error?.response?.data?.msg || error?.message || 'request_failed'
  return {
    preview_bytes_unverified: '媒體大小或格式不符，未顯示預覽。',
    preview_checksum_changed: '媒體 SHA 與審閱版本不同，未顯示預覽。',
    preview_crypto_unavailable: '目前頁面無法執行安全的 SHA 檢查，請使用 HTTPS 或 localhost。',
    repurpose_disabled: '重製功能尚未啟用。',
    notion_source_owner_required: '只有原 Notion 準備流程能建立新的準備工作。',
    released_source_requires_reconciliation_not_regeneration: '已有交付紀錄；請核對原工作，不建立新版本重發。',
    repurpose_delivery_capacity: '可用資源不足，保留原素材並暫停。',
  }[code] || String(code)
}
