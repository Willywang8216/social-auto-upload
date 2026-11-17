import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, reactive, ref } from 'vue'
import { createHash, webcrypto } from 'node:crypto'
import Preview from '../RepurposeAssetPreview.vue'
import { flush, mount } from './test-renderer'

const calls = vi.hoisted(() => ({ asset: vi.fn(), revision: vi.fn(() => 0) }))
vi.mock('@/api/teaching-repurpose', () => ({ teachingRepurposeApi: { asset: calls.asset } }))
vi.mock('@/utils/auth', () => ({ getAuthRevision: calls.revision }))
const descriptor = bytes => ({ name: 'deck-01.jpg', mime: 'image/jpeg', size_bytes: bytes.length,
  sha256: createHash('sha256').update(bytes).digest('hex') })
let view, props, created, revoked
beforeEach(() => {
  vi.clearAllMocks(); vi.stubGlobal('crypto', webcrypto); calls.revision.mockReturnValue(0)
  created = vi.spyOn(URL, 'createObjectURL').mockImplementation(() => 'blob:unit-private-' + created.mock.calls.length)
  revoked = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
})
afterEach(() => { view?.unmount(); view = null; vi.restoreAllMocks(); vi.unstubAllGlobals() })
async function open(bytes) {
  props = reactive({ taskId: 1, asset: descriptor(bytes) })
  view = mount(defineComponent({ setup: () => () => h(Preview, props) }))
  await flush(); return view.vm.$.subTree.component.setupState
}

describe('authenticated preview lifecycle; local byte fixtures, not a real codec', () => {
  it('creates a Blob URL only after SHA validation and revokes it on unmount', async () => {
    const bytes = new Uint8Array([1, 2, 3]); calls.asset.mockResolvedValue(new Blob([bytes], { type: 'image/jpeg' }))
    const state = await open(bytes); await vi.waitFor(() => expect(state.url).toContain('blob:unit-private-'))
    expect(calls.asset.mock.calls[0][0]).toBe(1); expect(calls.asset.mock.calls[0][1]).toBe('deck-01.jpg')
    expect(calls.asset.mock.calls[0][2].signal).toBeInstanceOf(AbortSignal)
    const url = state.url; view.unmount(); view = null; expect(revoked).toHaveBeenCalledWith(url)
  })
  it('tampered or MIME-mismatched bytes never become a preview URL', async () => {
    const bytes = new Uint8Array([1, 2, 3]); calls.asset.mockResolvedValue(new Blob([new Uint8Array([3, 2, 1])], { type: 'image/jpeg' }))
    const state = await open(bytes); await vi.waitFor(() => expect(state.error).toContain('SHA'))
    expect(created).not.toHaveBeenCalled(); expect(state.url).toBe('')
  })
  it('a switched asset aborts the old load and ignores its late response', async () => {
    const oldBytes = new Uint8Array([1, 2, 3]), newBytes = new Uint8Array([3, 2, 1]); let resolve
    calls.asset.mockImplementationOnce(() => new Promise(done => { resolve = done }))
      .mockResolvedValueOnce(new Blob([newBytes], { type: 'image/jpeg' }))
    const state = await open(oldBytes); const signal = calls.asset.mock.calls[0][2].signal
    props.asset = descriptor(newBytes); await flush(); await vi.waitFor(() => expect(state.url).not.toBe(''))
    const current = state.url; resolve(new Blob([oldBytes], { type: 'image/jpeg' })); await flush()
    expect(signal.aborted).toBe(true); expect(state.url).toBe(current); expect(created).toHaveBeenCalledOnce()
  })
  it('changed authentication or leaving during a fetch produces no private URL', async () => {
    const bytes = new Uint8Array([1, 2, 3]); let resolve
    calls.asset.mockImplementation(() => new Promise(done => { resolve = done }))
    await open(bytes); const signal = calls.asset.mock.calls[0][2].signal
    calls.revision.mockReturnValue(1); view.unmount(); view = null
    resolve(new Blob([bytes], { type: 'image/jpeg' })); await flush()
    expect(signal.aborted).toBe(true); expect(created).not.toHaveBeenCalled()
  })
  it('revokes an already loaded private URL immediately on an auth change', async () => {
    const revision = ref(0); calls.revision.mockImplementation(() => revision.value)
    const bytes = new Uint8Array([1, 2, 3]); calls.asset.mockResolvedValue(new Blob([bytes], { type: 'image/jpeg' }))
    const state = await open(bytes); await vi.waitFor(() => expect(state.url).not.toBe(''))
    const old = state.url; revision.value++; await flush()
    expect(state.url).toBe(''); expect(revoked).toHaveBeenCalledWith(old)
    expect(state.error).toContain('舊預覽已清除')
  })
})
