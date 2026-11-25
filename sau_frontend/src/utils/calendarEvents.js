import { normalizeTarget } from './publish-status'

// Expand first: an entity-level failed/cancelled rollup may have pending siblings.
export function calendarEntityEvents(entities, {
  showAll = false, month, platforms = [], accountIds = [], profileIds = [], summaryLength = 34,
} = {}) {
  return entities.flatMap((entity) => {
    const postsByRef = new Map((entity.posts || []).map((post) => [`campaign_post:${post.id}`, post]))
    const scheduled = (entity.jobs || []).flatMap((job) => (job.targets || []).map((raw) => {
      const target = normalizeTarget(raw)
      const post = postsByRef.get(target.fileRef)
      return { ...target, post, jobId: target.jobId || job.id,
        status: target.status || job.status || entity.status,
        platform: post?.platform || job.platform, profileId: post?.profileId || job.profileId,
        jobTitle: job.title }
    })).filter((target) => target.scheduleAt)
    const draft = entity.posts?.find((post) => post.draft?.title || post.draft?.message)?.draft
    const title = draft?.title || draft?.message || entity.mediaItems?.[0]?.filename || entity.entityId
    if (!scheduled.length && entity.scheduledAt) {
      scheduled.push({ id: `entity:${entity.entityId}`, scheduleAt: entity.scheduledAt,
        status: entity.status, profileId: entity.profileId })
    }
    return scheduled.filter((target) => {
      if (!showAll && ['failed', 'cancelled'].includes(target.status)) return false
      if (month && target.scheduleAt.slice(0, 7) !== month) return false
      if (platforms.length && !platforms.includes(target.platform)) return false
      if (profileIds.length && !profileIds.some((id) => String(id) === String(target.profileId))) return false
      const accountId = target.accountId ?? String(target.accountRef || '').match(/^account:(\d+)$/)?.[1]
      if (accountIds.length && !accountIds.some((id) => String(id) === String(accountId))) return false
      return true
    }).map((target) => {
      const targetTitle = target.post?.draft?.title || target.post?.draft?.message || target.jobTitle || title
      const normalized = String(targetTitle).replace(/\s+/g, ' ').trim()
      return {
        ...entity,
        eventId: `${entity.entityId}:${target.jobId || 'entity'}:${target.id}`,
        targetId: target.id, jobId: target.jobId, targetStatus: target.status,
        targetFileRef: target.fileRef, scheduleAt: target.scheduleAt,
        time: target.scheduleAt.slice(11, 16), title: targetTitle,
        destinationLabel: [target.platform, target.accountName].filter(Boolean).join(' · ') || '內容排程',
        summary: normalized.slice(0, summaryLength), hasMore: normalized.length > summaryLength,
      }
    })
  })
}
