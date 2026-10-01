/**
 * Public media links shown for a publish entity.
 *
 * The payload carries one artifact per campaign, so an entity spread over
 * several campaigns produced two links with the same URL *and* the same label —
 * "開啟媒體" twice, with nothing to tell them apart — while entities with no
 * public artifact showed no link at all. Deduplicate on the URL and label each
 * one, so the row is never a repeat and never ambiguous.
 *
 * @param {Array<{id?: any, url?: string, role?: string, kind?: string}>} artifacts
 * @param {number} limit most links to return (the card has room for two)
 * @returns {Array<{key: any, url: string, role: string, text: string}>}
 */
export function artifactLinks(artifacts, limit = 2) {
  const max = Number.isFinite(limit) ? Math.max(0, Math.floor(limit)) : 2
  const seen = new Set()
  const links = []
  for (const artifact of Array.isArray(artifacts) ? artifacts : []) {
    if (links.length >= max) {
      break
    }
    const url = typeof artifact?.url === 'string' ? artifact.url.trim() : ''
    if (!url || seen.has(url)) {
      continue
    }
    seen.add(url)
    links.push({
      key: artifact.id ?? url,
      url,
      role: artifact.role || artifact.kind || ''
    })
  }
  // One link needs no distinguishing label; several do, or they read as
  // duplicates of each other.
  return links.map((link, index) => ({
    ...link,
    text: links.length <= 1 ? '開啟媒體' : `${link.role || '媒體'} ${index + 1}`
  }))
}
