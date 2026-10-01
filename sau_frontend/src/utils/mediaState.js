/**
 * Human-readable state of one media item in the queue / calendar.
 *
 * The offloader moves published media to remote storage and deletes the local
 * copy, so an entity whose file_records row still has a preview path can have
 * no thumbnail simply because the bytes are in Drive. The backend flags this
 * with `availableLocally` (is the file on this box) and `archive` (where it
 * went); this turns those into a label an operator can act on.
 *
 * @param {object} media entity.mediaItems[] entry
 * @returns {string}
 */
export function mediaStateLabel(media) {
  if (!media || typeof media !== 'object') {
    return '未知'
  }
  if (media.availableLocally) {
    return '本機快取'
  }
  if (media.archive?.archived) {
    const where = media.archive.label || media.archive.provider || '雲端'
    return `已封存至 ${where}`
  }
  if (media.publicUrl) {
    return '雲端連結'
  }
  return '本機檔案不存在'
}

/**
 * The URL to show a thumbnail from, preferring the local copy.
 *
 * Returns '' when there is nothing the browser can render, which is the cue to
 * show the placeholder (and, for archived media, the storage link) rather than
 * a broken image element.
 *
 * @param {object} media entity.mediaItems[] entry
 * @returns {string}
 */
export function mediaPreviewSource(media) {
  if (!media || typeof media !== 'object') {
    return ''
  }
  if (media.previewUrl && media.availableLocally !== false) {
    return media.previewUrl
  }
  return media.publicUrl || ''
}
