import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createHash, webcrypto } from 'node:crypto'
import { deliveryPlan, notionSourceUrl, taipeiTime, verifyPreviewBlob } from '../teaching-repurpose'

const bytes = new Uint8Array([1, 2, 3, 4])
const asset = { name: 'deck-01.jpg', mime: 'image/jpeg', size_bytes: 4,
  sha256: createHash('sha256').update(bytes).digest('hex') }
beforeEach(() => vi.stubGlobal('crypto', webcrypto))
afterEach(() => vi.unstubAllGlobals())

describe('private Teaching preview bytes (not a codec or public delivery test)', () => {
  it('accepts only exact reviewed SHA/size/MIME', async () => {
    const blob = new Blob([bytes], { type: 'image/jpeg' })
    await expect(verifyPreviewBlob(blob, asset)).resolves.toBe(blob)
    await expect(verifyPreviewBlob(new Blob([new Uint8Array([4, 3, 2, 1])], { type: 'image/jpeg' }), asset)).rejects.toThrow('checksum_changed')
    await expect(verifyPreviewBlob(new Blob([bytes], { type: 'image/png' }), asset)).rejects.toThrow('bytes_unverified')
    await expect(verifyPreviewBlob(new Blob([bytes]), asset)).rejects.toThrow('bytes_unverified')
    await expect(verifyPreviewBlob(blob, { ...asset, size_bytes: 3 })).rejects.toThrow('bytes_unverified')
  })
  it('rejects missing/oversized/HTML descriptors before reading bytes', async () => {
    const blob = new Blob([bytes], { type: 'image/jpeg' }); const read = vi.spyOn(blob, 'arrayBuffer')
    for (const descriptor of [null, { ...asset, sha256: 'claimed' }, { ...asset, size_bytes: 129 * 1024**2 },
      { ...asset, size_bytes: true }, { ...asset, mime: 'text/html' }]) {
      await expect(verifyPreviewBlob(blob, descriptor)).rejects.toThrow('bytes_unverified')
    }
    expect(read).not.toHaveBeenCalled()
  })
  it('fails closed without WebCrypto and when cancelled', async () => {
    vi.stubGlobal('crypto', {})
    await expect(verifyPreviewBlob(new Blob([bytes], { type: 'image/jpeg' }), asset)).rejects.toThrow('crypto_unavailable')
    vi.stubGlobal('crypto', webcrypto)
    const controller = new AbortController(); controller.abort()
    await expect(verifyPreviewBlob(new Blob([bytes], { type: 'image/jpeg' }), asset, controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
  })
  it('honors cancellation during asynchronous digest too', async () => {
    const controller = new AbortController()
    vi.stubGlobal('crypto', { subtle: { digest: async () => { controller.abort(); return new ArrayBuffer(32) } } })
    await expect(verifyPreviewBlob(new Blob([bytes], { type: 'image/jpeg' }), asset, controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
  })
})
describe('fixed Teaching plan and display', () => {
  it('keeps four required card destinations and defaults to IG/FB video', () => {
    expect(deliveryPlan([43, 58])).toEqual(['43:cards', '43:video', '58:cards', '58:video', '61:cards', '125:cards'])
    expect(deliveryPlan([])).toEqual(['43:cards', '58:cards', '61:cards', '125:cards'])
    expect(deliveryPlan([125, 61, 58, 43])).toHaveLength(8)
    for (const invalid of [[77], ['43'], [43, 43], null]) expect(() => deliveryPlan(invalid)).toThrow('delivery_scope')
  })
  it('does not turn a caller URL or invalid page ID into a Notion link', () => {
    expect(notionSourceUrl('00000000-0000-4000-8000-000000000004')).toBe('https://www.notion.so/00000000000040008000000000000004')
    expect(notionSourceUrl('javascript:alert(1)')).toBeNull()
  })
  it('displays stored naive UTC without shifting the original schedule', () => {
    expect(taipeiTime('2030-01-01T12:00:00')).toContain('20:00')
    expect(taipeiTime('2030-01-01T12:00:00Z')).toBe(taipeiTime('2030-01-01T20:00:00+08:00'))
    expect(taipeiTime('garbage')).toBe('排程資料無法確認')
  })
})
