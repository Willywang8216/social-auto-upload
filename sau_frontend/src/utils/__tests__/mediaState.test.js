import { describe, expect, it } from 'vitest'

import { mediaPreviewSource, mediaStateLabel } from '../mediaState'

describe('mediaStateLabel', () => {
  it('reports a locally cached file', () => {
    expect(mediaStateLabel({ availableLocally: true, previewUrl: '/getFile?filename=a.mp4' }))
      .toBe('本機快取')
  })

  it('names the storage a file was archived to', () => {
    expect(mediaStateLabel({
      availableLocally: false,
      archive: { archived: true, provider: 'rclone', label: 'Google Drive' }
    })).toBe('已封存至 Google Drive')
  })

  it('falls back to the provider when the storage has no friendly label', () => {
    expect(mediaStateLabel({ archive: { archived: true, provider: 'do_spaces' } }))
      .toBe('已封存至 do_spaces')
  })

  it('reports a remote-only file with a public link', () => {
    expect(mediaStateLabel({ availableLocally: false, publicUrl: 'https://cdn.example/a.mp4' }))
      .toBe('雲端連結')
  })

  it('reports a file that is neither local nor archived', () => {
    expect(mediaStateLabel({ filename: 'gone.mp4' })).toBe('本機檔案不存在')
  })

  it('degrades safely on missing input', () => {
    expect(mediaStateLabel(null)).toBe('未知')
  })
})

describe('mediaPreviewSource', () => {
  it('prefers the local preview URL', () => {
    expect(mediaPreviewSource({
      availableLocally: true,
      previewUrl: '/getFile?filename=a.mp4',
      publicUrl: 'https://cdn.example/a.mp4'
    })).toBe('/getFile?filename=a.mp4')
  })

  it('ignores a preview URL whose bytes are gone, using the public link instead', () => {
    // The offloader case: the record still carries a path, but the file moved.
    expect(mediaPreviewSource({
      availableLocally: false,
      previewUrl: '/getFile?filename=a.mp4',
      publicUrl: 'https://cdn.example/a.mp4'
    })).toBe('https://cdn.example/a.mp4')
  })

  it('keeps the preview URL when the backend predates availableLocally', () => {
    expect(mediaPreviewSource({ previewUrl: '/getFile?filename=a.mp4' }))
      .toBe('/getFile?filename=a.mp4')
  })

  it('returns an empty source for archived media so the UI shows the placeholder', () => {
    expect(mediaPreviewSource({
      availableLocally: false,
      previewUrl: '/getFile?filename=a.mp4',
      archive: { archived: true, provider: 'rclone' }
    })).toBe('')
  })

  it('degrades safely on missing input', () => {
    expect(mediaPreviewSource(undefined)).toBe('')
  })
})
