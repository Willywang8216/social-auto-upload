import { describe, expect, it, vi } from 'vitest'
import { fetchEntityPages, mergeEntities } from '../entityPagination'

const entity = (id, revision = 1) => ({ entityId: `job-${id}`, revision })
const page = (items, offset, total, limit) => ({ items, offset, total, limit, hasMore: offset + items.length < total })

describe('bounded publish entity pagination', () => {
  it('loads all 201 calendar entities using advancing <=200-row pages', async () => {
    const all = Array.from({ length: 201 }, (_, index) => entity(index + 1))
    const read = vi.fn(async ({ offset, limit }) => page(all.slice(offset, offset + limit), offset, all.length, limit))
    const result = await fetchEntityPages(read, { month: '2026-11' })
    expect(result.items).toEqual(all)
    expect(result).toMatchObject({ total: 201, nextOffset: 201, hasMore: false })
    expect(read.mock.calls.map(([query]) => [query.offset, query.limit])).toEqual([[0, 200], [200, 200]])
    expect(read.mock.calls.every(([query]) => query.month === '2026-11')).toBe(true)
  })

  it('refreshes a retained >200-row range without a clamped one-shot request', async () => {
    const all = Array.from({ length: 300 }, (_, index) => entity(index + 1, 2))
    const read = vi.fn(async ({ offset, limit }) => page(all.slice(offset, offset + limit), offset, all.length, limit))
    const query = { status: 'awaiting_delivery', platforms: 'twitter', q: 'campaign' }
    const result = await fetchEntityPages(read, query, { count: 250 })
    expect(result.items).toEqual(all.slice(0, 250))
    expect(result).toMatchObject({ total: 300, nextOffset: 250, hasMore: true })
    expect(read.mock.calls.map(([value]) => [value.offset, value.limit])).toEqual([[0, 200], [200, 50]])
    expect(read.mock.calls.every(([value]) => value.status === query.status && value.platforms === query.platforms && value.q === query.q)).toBe(true)
  })

  it('deduplicates identities using the latest value while offsets count raw server rows', async () => {
    const read = vi.fn().mockResolvedValueOnce(page([entity(1), entity(2)], 0, 5, 2))
      .mockResolvedValueOnce(page([entity(2, 2), entity(3)], 2, 5, 2))
    const result = await fetchEntityPages(read, {}, { count: 4, pageSize: 2 })
    expect(result.items).toEqual([entity(1), entity(2, 2), entity(3)])
    expect(result).toMatchObject({ nextOffset: 4, hasMore: true })
    expect(mergeEntities(result.items, [entity(3, 2), entity(4)])).toEqual([entity(1), entity(2, 2), entity(3, 2), entity(4)])
  })

  it.each(['empty', 'repeated-offset', 'repeated-identities'])('stops a %s page without a pagination loop', async (failure) => {
    const read = vi.fn().mockResolvedValueOnce(page([entity(1)], 0, 2, 1))
    if (failure === 'empty') read.mockResolvedValue(page([], 1, 2, 1))
    else if (failure === 'repeated-offset') read.mockResolvedValue(page([entity(2)], 0, 2, 1))
    else read.mockResolvedValue(page([entity(1)], 1, 2, 1))
    await expect(fetchEntityPages(read, {}, { pageSize: 1 })).rejects.toMatchObject({
      code: 'ERR_ENTITY_PAGINATION', partial: { items: [entity(1)], nextOffset: 1 },
    })
    expect(read).toHaveBeenCalledTimes(2)
  })

  it('preserves earlier valid pages on a subsequent read error', async () => {
    const error = Object.assign(new Error('offline'), { code: 'ERR_NETWORK' })
    const read = vi.fn().mockResolvedValueOnce(page([entity(1)], 0, 2, 1)).mockRejectedValueOnce(error)
    await expect(fetchEntityPages(read, {}, { pageSize: 1 })).rejects.toMatchObject({
      code: 'ERR_NETWORK', partial: { items: [entity(1)], nextOffset: 1, hasMore: true },
    })
    expect(read).toHaveBeenCalledTimes(2)
  })

  it.each([undefined, {}, { items: [] }, { items: [null], offset: 0, total: 1, limit: 1, hasMore: false }])('rejects malformed page metadata: %j', async (response) => {
    const read = vi.fn().mockResolvedValue(response)
    await expect(fetchEntityPages(read)).rejects.toMatchObject({ code: 'ERR_ENTITY_PAGINATION' })
    expect(read).toHaveBeenCalledOnce()
  })

  it('rejects a false end-of-list flag instead of silently truncating remaining entities', async () => {
    const read = vi.fn().mockResolvedValue({ items: [entity(1)], offset: 0, total: 201, limit: 200, hasMore: false })
    await expect(fetchEntityPages(read)).rejects.toMatchObject({ code: 'ERR_ENTITY_PAGINATION' })
    expect(read).toHaveBeenCalledOnce()
  })

  it('rejects a server page larger than the requested retained-range chunk', async () => {
    const read = vi.fn().mockResolvedValue(page([entity(1)], 0, 300, 200))
    await expect(fetchEntityPages(read, {}, { count: 50 })).rejects.toMatchObject({ code: 'ERR_ENTITY_PAGINATION' })
    expect(read.mock.calls[0][0].limit).toBe(50)
  })

  it('reports a visible partial result when its page budget is exhausted', async () => {
    const read = vi.fn(async ({ offset, limit }) => page([entity(offset + 1)], offset, 100, limit))
    await expect(fetchEntityPages(read, {}, { pageSize: 1, maxPages: 2 })).rejects.toMatchObject({
      code: 'ERR_ENTITY_PAGINATION', partial: { items: [entity(1), entity(2)], nextOffset: 2 },
    })
    expect(read).toHaveBeenCalledTimes(2)
  })
})
