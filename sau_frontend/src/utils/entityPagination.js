// /publish-entities is capped at 200. Offset counts server rows, not deduped cards.
export const ENTITY_PAGE_LIMIT = 200

export function mergeEntities(...pages) {
  const byId = new Map()
  for (const entity of pages.flat()) byId.set(entity.entityId, entity)
  return [...byId.values()]
}

function pageError(message, partial) {
  return Object.assign(new Error(message), { code: 'ERR_ENTITY_PAGINATION', partial })
}

// The injected reader keeps this helper independent of the store and the DOM.
// count is the retained server range for a quiet refresh; Infinity drains pages.
export async function fetchEntityPages(readPage, params = {}, {
  count = Infinity, pageSize = ENTITY_PAGE_LIMIT, maxPages = 100,
} = {}) {
  const start = Number.isInteger(params.offset) && params.offset >= 0 ? params.offset : 0
  const size = Math.max(1, Math.min(ENTITY_PAGE_LIMIT, Math.floor(pageSize) || ENTITY_PAGE_LIMIT))
  let result = { items: [], total: 0, nextOffset: start, hasMore: true }
  let pages = 0
  while (result.hasMore && result.nextOffset - start < count) {
    if (++pages > maxPages) throw pageError('內容清單尚未完整載入：已達分頁上限，請縮小篩選範圍或重新載入。', result)
    const offset = result.nextOffset
    const limit = Math.min(size, count - (offset - start))
    let page
    try {
      page = await readPage({ ...params, limit, offset })
    } catch (error) {
      // Preserve valid earlier pages, but never turn a read failure into "empty".
      if (!error || typeof error !== 'object') throw pageError('內容清單讀取失敗；請重新載入。', result)
      error.partial = result
      throw error
    }
    if (!page || !Array.isArray(page.items)
      || !page.items.every((item) => item && typeof item.entityId === 'string' && item.entityId)
      || page.offset !== offset || !Number.isInteger(page.total) || page.total < 0
      || typeof page.hasMore !== 'boolean' || !Number.isInteger(page.limit)
      || page.limit < 1 || page.limit > limit || page.items.length > page.limit
      || page.hasMore !== (offset + page.items.length < page.total)) {
      throw pageError('內容清單分頁回應無效；請重新載入，勿將目前清單視為完整。', result)
    }
    const items = mergeEntities(result.items, page.items)
    const nextOffset = offset + page.items.length
    if ((page.hasMore && nextOffset <= offset)
      || (page.items.length && items.length === result.items.length)) {
      throw pageError('內容清單分頁未前進；目前僅顯示已載入的項目，請重新載入。', result)
    }
    result = { items, total: page.total, nextOffset, hasMore: page.hasMore }
  }
  return result
}
