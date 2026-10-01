import { describe, expect, it } from 'vitest'

import { artifactLinks } from '../entityLinks'

describe('artifactLinks', () => {
  it('drops entries with no usable url', () => {
    expect(artifactLinks([{ id: 1 }, { id: 2, url: '' }, { id: 3, url: '   ' }])).toEqual([])
  })

  it('returns a single link labelled 開啟媒體', () => {
    const links = artifactLinks([{ id: 1, url: 'https://cdn/a.mp4', role: 'video' }])
    expect(links).toHaveLength(1)
    expect(links[0].text).toBe('開啟媒體')
    expect(links[0].url).toBe('https://cdn/a.mp4')
  })

  it('collapses the duplicate url that produced two identical 開啟媒體 links', () => {
    // The real payload: one artifact per campaign, same file, same url.
    const links = artifactLinks([
      { id: 1, url: 'https://cdn/a.mp4', role: 'video' },
      { id: 2, url: 'https://cdn/a.mp4', role: 'video' }
    ])
    expect(links).toHaveLength(1)
    expect(links[0].text).toBe('開啟媒體')
  })

  it('labels distinct links so they are not indistinguishable', () => {
    const links = artifactLinks([
      { id: 1, url: 'https://cdn/a.mp4', role: 'video' },
      { id: 2, url: 'https://cdn/b.jpg', role: 'thumbnail' }
    ])
    expect(links.map((l) => l.text)).toEqual(['video 1', 'thumbnail 2'])
  })

  it('falls back to 媒體 for a link with no role', () => {
    const links = artifactLinks([
      { id: 1, url: 'https://cdn/a.mp4' },
      { id: 2, url: 'https://cdn/b.mp4' }
    ])
    expect(links.map((l) => l.text)).toEqual(['媒體 1', '媒體 2'])
  })

  it('treats a urls differing only in surrounding whitespace as one link', () => {
    const links = artifactLinks([
      { id: 1, url: 'https://cdn/a.mp4' },
      { id: 2, url: ' https://cdn/a.mp4 ' }
    ])
    expect(links).toHaveLength(1)
  })

  it('honours the limit and stops scanning', () => {
    const many = Array.from({ length: 6 }, (_, i) => ({ id: i, url: `https://cdn/${i}.mp4` }))
    expect(artifactLinks(many, 2)).toHaveLength(2)
    expect(artifactLinks(many, 0)).toHaveLength(0)
  })

  it('degrades safely on missing input', () => {
    expect(artifactLinks(undefined)).toEqual([])
    expect(artifactLinks(null)).toEqual([])
    expect(artifactLinks([null, undefined])).toEqual([])
  })
})
