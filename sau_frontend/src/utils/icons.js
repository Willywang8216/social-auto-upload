/**
 * SVG stroke icon components ported from the redesign prototype.
 * Each returns an SVG element — use in templates as <component :is="icons.dashboard" />
 * or call programmatically as icons.dashboard().
 *
 * All icons use currentColor for stroke, so they inherit text color from parents.
 */
import { h } from 'vue'

const S = (extra) => ({
  width: 18, height: 18, viewBox: '0 0 24 24', fill: 'none',
  stroke: 'currentColor', 'stroke-width': 1.8, 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
  ...extra,
})

const s = (tag, attrs, children) => h(tag, attrs, children)

export const icons = {
  dashboard: (p) => h('svg', S(p), [
    s('rect', { x: 3, y: 3, width: 7, height: 9, rx: 1.5 }),
    s('rect', { x: 14, y: 3, width: 7, height: 5, rx: 1.5 }),
    s('rect', { x: 14, y: 12, width: 7, height: 9, rx: 1.5 }),
    s('rect', { x: 3, y: 16, width: 7, height: 5, rx: 1.5 }),
  ]),
  publish: (p) => h('svg', S(p), [
    s('path', { d: 'M12 19V6' }),
    s('path', { d: 'm5 12 7-7 7 7' }),
    s('path', { d: 'M5 21h14' }),
  ]),
  jobs: (p) => h('svg', S(p), [
    s('path', { d: 'M8 6h13' }),
    s('path', { d: 'M8 12h13' }),
    s('path', { d: 'M8 18h13' }),
    s('circle', { cx: 3.5, cy: 6, r: 1.2 }),
    s('circle', { cx: 3.5, cy: 12, r: 1.2 }),
    s('circle', { cx: 3.5, cy: 18, r: 1.2 }),
  ]),
  accounts: (p) => h('svg', S(p), [
    s('circle', { cx: 9, cy: 8, r: 3.2 }),
    s('path', { d: 'M3.5 20a5.5 5.5 0 0 1 11 0' }),
    s('path', { d: 'M16 8.5a3 3 0 0 1 0 5' }),
    s('path', { d: 'M18.5 20a4.5 4.5 0 0 0-3-4.2' }),
  ]),
  profiles: (p) => h('svg', S(p), [
    s('path', { d: 'M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z' }),
  ]),
  materials: (p) => h('svg', S(p), [
    s('rect', { x: 3, y: 4, width: 18, height: 16, rx: 2 }),
    s('circle', { cx: 8.5, cy: 9.5, r: 1.8 }),
    s('path', { d: 'm4 17 4.5-4 3 2.5L16 11l4 4.5' }),
  ]),
  templates: (p) => h('svg', S(p), [
    s('rect', { x: 3, y: 3, width: 18, height: 18, rx: 2 }),
    s('path', { d: 'M3 9h18M9 9v12' }),
  ]),
  analytics: (p) => h('svg', S(p), [
    s('path', { d: 'M4 20V4' }),
    s('path', { d: 'M4 20h16' }),
    s('path', { d: 'm7 15 3.5-4 3 2.5L20 7' }),
  ]),
  campaigns: (p) => h('svg', S(p), [
    s('path', { d: 'm3 11 16-7-4 16-4.5-5z' }),
    s('path', { d: 'M10.5 15 8 21' }),
  ]),
  sheet: (p) => h('svg', S(p), [
    s('rect', { x: 4, y: 3, width: 16, height: 18, rx: 2 }),
    s('path', { d: 'M4 9h16M4 15h16M10 3v18' }),
  ]),
  api: (p) => h('svg', S(p), [
    s('path', { d: 'm8 9-3 3 3 3' }),
    s('path', { d: 'm16 9 3 3-3 3' }),
    s('path', { d: 'm13 6-2 12' }),
  ]),
  oauth: (p) => h('svg', S(p), [
    s('path', { d: 'M12 2 4 5v6c0 5 3.4 8.5 8 11 4.6-2.5 8-6 8-11V5z' }),
    s('path', { d: 'm9 12 2 2 4-4' }),
  ]),
  about: (p) => h('svg', S(p), [
    s('circle', { cx: 12, cy: 12, r: 9 }),
    s('path', { d: 'M12 11v5M12 8h.01' }),
  ]),
  search: (p) => h('svg', S(p), [
    s('circle', { cx: 11, cy: 11, r: 7 }),
    s('path', { d: 'm20 20-3.2-3.2' }),
  ]),
  bell: (p) => h('svg', S(p), [
    s('path', { d: 'M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9' }),
    s('path', { d: 'M13.7 21a2 2 0 0 1-3.4 0' }),
  ]),
  plus: (p) => h('svg', S(p), [
    s('path', { d: 'M12 5v14M5 12h14' }),
  ]),
  collapse: (p) => h('svg', S(p), [
    s('path', { d: 'm15 6-6 6 6 6' }),
  ]),
  expand: (p) => h('svg', S(p), [
    s('path', { d: 'm9 6 6 6-6 6' }),
  ]),
  arrow: (p) => h('svg', S(p), [
    s('path', { d: 'M5 12h14M13 6l6 6-6 6' }),
  ]),
  warn: (p) => h('svg', S(p), [
    s('path', { d: 'M10.3 3.5 2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.5a2 2 0 0 0-3.4 0z' }),
    s('path', { d: 'M12 9v4M12 17h.01' }),
  ]),
  video: (p) => h('svg', S(p), [
    s('rect', { x: 2, y: 6, width: 14, height: 12, rx: 2 }),
    s('path', { d: 'm16 10 6-3v10l-6-3z' }),
  ]),
  upload: (p) => h('svg', S(p), [
    s('path', { d: 'M12 16V4m0 0L7 9m5-5 5 5' }),
    s('path', { d: 'M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2' }),
  ]),
  check: (p) => h('svg', S(p), [
    s('path', { d: 'm5 12 4.5 4.5L19 7' }),
  ]),
  spark: (p) => h('svg', S(p), [
    s('path', { d: 'M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M18 6l-2.5 2.5M8.5 15.5 6 18' }),
  ]),
  users: (p) => h('svg', S(p), [
    s('circle', { cx: 9, cy: 8, r: 3.2 }),
    s('path', { d: 'M3.5 20a5.5 5.5 0 0 1 11 0' }),
    s('circle', { cx: 17.5, cy: 8, r: 2.5 }),
    s('path', { d: 'M16 20a4.5 4.5 0 0 0-3-4.2' }),
  ]),
  link: (p) => h('svg', S(p), [
    s('path', { d: 'M10 13a4 4 0 0 0 6 .5l3-3a4 4 0 0 0-5.7-5.7L11.5 6' }),
    s('path', { d: 'M14 11a4 4 0 0 0-6-.5l-3 3A4 4 0 0 0 10.7 19l1.8-1.8' }),
  ]),
  film: (p) => h('svg', S(p), [
    s('rect', { x: 3, y: 4, width: 18, height: 16, rx: 2 }),
    s('path', { d: 'M3 9h18M3 15h18M8 4v16M16 4v16' }),
  ]),
  clock: (p) => h('svg', S(p), [
    s('circle', { cx: 12, cy: 12, r: 9 }),
    s('path', { d: 'M12 7v5l3 2' }),
  ]),
  calendar: (p) => h('svg', S(p), [
    s('rect', { x: 3, y: 5, width: 18, height: 16, rx: 2 }),
    s('path', { d: 'M3 10h18M8 3v4M16 3v4' }),
  ]),
  settings: (p) => h('svg', S(p), [
    s('circle', { cx: 12, cy: 12, r: 3 }),
    s('path', { d: 'M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z' }),
  ]),
  image: (p) => h('svg', S(p), [
    s('rect', { x: 3, y: 4, width: 18, height: 16, rx: 2 }),
    s('circle', { cx: 8.5, cy: 9.5, r: 1.8 }),
    s('path', { d: 'm4 17 4.5-4 3 2.5L16 11l4 4.5' }),
  ]),
  trash: (p) => h('svg', S(p), [
    s('path', { d: 'M3 6h18' }),
    s('path', { d: 'M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2' }),
  ]),
}

export default icons
