const normalizeObject = (value) => (value && typeof value === 'object' && !Array.isArray(value) ? value : {})

const pickFirstText = (...values) => {
  for (const value of values) {
    const text = String(value || '').trim()
    if (text) {
      return text
    }
  }
  return ''
}

export const getMergedPublishDetails = (job) => {
  const metadata = normalizeObject(job?.metadata)
  const publishDetails = normalizeObject(metadata.publishDetails)
  const publishStatus = normalizeObject(metadata.publishStatus)
  const statusDetails = normalizeObject(publishStatus.details)
  return { ...publishDetails, ...statusDetails }
}

export const getLifecycleTags = (job) => {
  const metadata = normalizeObject(job?.metadata)
  const details = getMergedPublishDetails(job)
  const tags = []

  const publishStage = pickFirstText(details.publishStage)
  if (publishStage) {
    tags.push({ key: `stage-${publishStage}`, label: `階段：${publishStage}`, type: 'info' })
  }

  const containerStatusCode = pickFirstText(details.containerStatusCode)
  if (containerStatusCode) {
    tags.push({ key: `container-${containerStatusCode}`, label: `Container：${containerStatusCode}`, type: 'warning' })
  }

  const publishAt = pickFirstText(details.publishAt, normalizeObject(metadata.youtube).publishAt)
  if (publishAt) {
    tags.push({ key: `publishAt-${publishAt}`, label: `發布時間：${publishAt}`, type: 'success' })
  }

  if (details.thumbnail?.etag || details.thumbnail?.source || details.thumbnailUploaded) {
    tags.push({ key: 'thumbnail-uploaded', label: '縮圖已上傳', type: 'success' })
  }

  if (details.captions?.captionId || details.captionsUploaded) {
    tags.push({ key: 'captions-uploaded', label: '字幕已上傳', type: 'success' })
  }

  const premiereMode = pickFirstText(details.premiereMode)
  if (premiereMode) {
    tags.push({ key: `premiere-${premiereMode}`, label: `首映模式：${premiereMode}`, type: 'danger' })
  }

  const syncError = pickFirstText(metadata.lastStatusSyncError)
  if (syncError) {
    tags.push({ key: `sync-error-${syncError}`, label: `同步錯誤：${syncError}`, type: 'danger' })
  }

  return tags
}

export const getLifecycleLines = (job) => {
  const metadata = normalizeObject(job?.metadata)
  const details = getMergedPublishDetails(job)
  const lines = []

  if (details.containerError) {
    lines.push(`Container 錯誤：${details.containerError}`)
  }
  if (details.failReason) {
    lines.push(`平台原因：${details.failReason}`)
  }
  if (details.thumbnail?.sourceName) {
    lines.push(`縮圖來源：${details.thumbnail.sourceName}`)
  }
  if (details.captions?.sourceName) {
    lines.push(`字幕來源：${details.captions.sourceName}`)
  }
  if (details.premiereRequestedAt) {
    lines.push(`首映需求時間：${details.premiereRequestedAt}`)
  }
  if (metadata.publishedUrl) {
    lines.push(`已發布連結：${metadata.publishedUrl}`)
  }

  return lines
}

export const createDefaultYoutubeMetadata = (youtube = {}) => ({
  thumbnailUrl: youtube.thumbnailUrl || '',
  thumbnailPath: youtube.thumbnailPath || '',
  captionsPath: youtube.captionsPath || '',
  captionsLanguage: youtube.captionsLanguage || 'en',
  captionsName: youtube.captionsName || 'Default captions',
  premiereAt: youtube.premiereAt || ''
})
