// Isolated, opt-in UI check. Real local generated files, fake read-only API.
// Never opens Notion, calls a provider, or enables a publication service.
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFile, mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { chromium } from '@playwright/test'

assert.equal(process.version, 'v22.21.1', 'Use the pinned Node toolchain')
const [manifestPath, output] = process.argv.slice(2)
assert(manifestPath && output && process.env.PI_SCRATCH_DIR, 'Private manifest and scratch output are required')
const scratch = path.resolve(process.env.PI_SCRATCH_DIR)
const inside = value => path.resolve(value).startsWith(scratch + path.sep)
assert(inside(manifestPath) && inside(output), 'Fixtures/screenshots must stay in session scratch')
const root = fileURLToPath(new URL('../', import.meta.url))
const raw = await readFile(manifestPath); assert(raw.length <= 512 * 1024)
const manifest = JSON.parse(raw), assets = new Map()
for (const descriptor of manifest.material.assets) {
  assert(/^[a-z0-9][a-z0-9.-]{0,199}$/.test(descriptor.name) && !descriptor.name.includes('..'))
  const bytes = await readFile(path.join(path.dirname(manifestPath), descriptor.name))
  assert.equal(bytes.length, descriptor.size_bytes)
  assert.equal(createHash('sha256').update(bytes).digest('hex'), descriptor.sha256)
  assets.set(descriptor.name, { descriptor, bytes })
}
const summary = manifest.material.assets.find(a => a.name === 'summary.mp4')
assert(summary?.decoded, 'Use an already decoded real fixture, not arbitrary movie bytes')
await mkdir(output, { recursive: true })
const entry = path.join(output, 'entry'); await mkdir(entry, { recursive: true })
await writeFile(path.join(entry, 'index.html'), `<!doctype html><html lang="zh-Hant" data-theme="light" data-accent="coral"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app"></div><script type="module">import { createApp } from 'vue';import Page from '@/views/TeachingRepurpose.vue';import '@/styles/modern.css';import { setToken } from '@/utils/auth';setToken('isolated-preview-only');createApp(Page).mount('#app');</script></body></html>`)
const server = await createServer({ configFile: false, root: entry, envFile: false, cacheDir: path.join(entry, '.vite'),
  plugins: [vue()], resolve: { alias: { '@': path.join(root, 'src'), vue: path.join(root, 'node_modules/vue/dist/vue.runtime.esm-bundler.js') } },
  server: { host: '127.0.0.1', port: 0, open: false, proxy: {}, fs: { allow: [entry, path.join(root, 'src'), path.join(root, 'node_modules')] } } })
let browser
try {
  await server.listen(); const address = server.httpServer.address(); const origin = `http://127.0.0.1:${address.port}`
  browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } })
  const failures = [], outside = [], mutations = [], requests = []
  let tamper = null
  page.on('pageerror', failure => failures.push(failure.message))
  const task = { id: 1, request_id: 'isolated-real-media', state: 'prepared', attempts: 1, reason_code: '',
    source: manifest.material.source, kit_version: manifest.material.kit_version, package_version: manifest.package_version,
    delivery_ids: manifest.material.deliveries.map(d => d.id), article_read_only: true, requires_new_notion_review: true,
    package: manifest, releases: [] }
  const safe = { ...task, source: { ...task.source } }; delete safe.source.manuscript; delete safe.package
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin !== origin) { outside.push(url.origin); await route.abort(); return }
    if (!url.pathname.startsWith('/api/')) { await route.continue(); return }
    requests.push(url.pathname)
    if (request.method() !== 'GET') { mutations.push(request.method()); await route.abort(); return }
    assert.equal(request.headers().authorization, 'Bearer isolated-preview-only')
    const base = '/api/publish/teaching-repurpose'
    const json = data => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ code: 200, data }) })
    if (url.pathname === base + '/status') return json({ enabled: false, release_enabled: false, generation_owner: true,
      approval_location: 'notion', profile_id: 4, account_ids: [43, 58, 61, 125] })
    if (url.pathname === base + '/kits') return json([{ version: task.kit_version, theme: 'ink' }])
    if (url.pathname === base + '/tasks') return json([safe])
    if (url.pathname === base + '/tasks/1') return json(task)
    const name = url.pathname.startsWith(base + '/tasks/1/assets/') ? decodeURIComponent(url.pathname.split('/').pop()) : null
    if (name && assets.has(name)) {
      const { bytes, descriptor } = assets.get(name); const body = Buffer.from(bytes)
      if (tamper === name) body[0] ^= 1
      return route.fulfill({ status: 200, contentType: descriptor.mime, body, headers: { 'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff' } })
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{"code":404,"msg":"isolated_route_not_found"}' })
  })
  await page.goto(origin, { waitUntil: 'networkidle' })
  await page.locator('.asset-caption').filter({ hasText: 'deck-01.jpg' }).waitFor()
  const dimensions = async () => page.locator('.asset-preview img').evaluate(image => ({ width: image.naturalWidth, height: image.naturalHeight }))
  assert.deepEqual(await dimensions(), { width: 1080, height: 1350 })
  assert(await page.getByRole('button', { name: '重新準備衍生套件', exact: true }).isDisabled())
  assert.equal(await page.locator('textarea,[contenteditable="true"]').count(), 0)
  const noOverflow = async () => assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
  await noOverflow(); await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true })
  await page.getByRole('button', { name: 'Bluesky 四卡', exact: true }).click()
  await page.locator('.asset-caption').filter({ hasText: 'bluesky-01.jpg' }).waitFor()
  assert.equal(await page.locator('.pagination button').count(), 4)
  await page.getByLabel('圖卡編碼').selectOption('png')
  await page.locator('.asset-caption').filter({ hasText: 'bluesky-01.png' }).waitFor(); assert.deepEqual(await dimensions(), { width: 1080, height: 1350 })
  await page.getByRole('button', { name: '教學圖卡', exact: true }).click(); await page.getByLabel('圖卡編碼').selectOption('jpg')
  await page.locator('.asset-caption').filter({ hasText: 'deck-01.jpg' }).waitFor()
  tamper = 'deck-02.jpg'; await page.getByRole('button', { name: '2', exact: true }).click()
  await page.getByRole('alert').filter({ hasText: '媒體 SHA' }).waitFor(); assert.equal(await page.locator('.asset-preview img').count(), 0)
  tamper = null; await page.getByRole('button', { name: '摘要影片', exact: true }).click()
  await page.locator('.asset-caption').filter({ hasText: 'summary.mp4' }).waitFor()
  await page.waitForFunction(() => document.querySelector('video')?.readyState >= 1)
  const movie = await page.locator('video').evaluate(video => ({ seconds: video.duration, controls: video.controls, autoplay: video.autoplay,
    width: video.videoWidth, height: video.videoHeight, poster: video.poster.startsWith('blob:') }))
  assert(movie.seconds >= 45 && movie.seconds <= 90 && movie.controls && !movie.autoplay && movie.poster)
  assert.equal(movie.width, 1080); assert.equal(movie.height, 1920)
  await page.screenshot({ path: path.join(output, 'video.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 }); await noOverflow()
  await page.getByRole('button', { name: '教學圖卡', exact: true }).click()
  await page.locator('.asset-caption').filter({ hasText: 'deck-01.jpg' }).waitFor()
  await page.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true })
  await page.getByRole('button', { name: '來源文章', exact: true }).click()
  assert.equal(await page.locator('.article-text').textContent(), manifest.material.source.manuscript)
  assert.equal(await page.locator('.article-text script').count(), 0)
  assert.equal(mutations.length, 0); assert.equal(outside.length, 0); assert.deepEqual(failures, [])
  const receipt = { node: process.version, viewport_checks: ['desktop', 'mobile'], real_jpeg_png_verified: true,
    real_video_seconds: movie.seconds, tampered_bytes_held: true, article_read_only: true,
    api_gets: requests.length, outside_requests: outside.length, mutations: mutations.length, page_errors: failures.length }
  await writeFile(path.join(output, 'receipt.json'), JSON.stringify(receipt, null, 2) + '\n')
  console.log(JSON.stringify(receipt))
} finally { await browser?.close(); await server.close() }
