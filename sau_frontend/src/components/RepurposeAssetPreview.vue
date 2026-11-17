<template>
  <section class="asset-preview" :aria-busy="loading" aria-label="套件媒體預覽">
    <div v-if="!asset" class="placeholder">這個工作尚無可預覽媒體。</div>
    <div v-else-if="loading" class="placeholder" role="status">載入並核對媒體 SHA…</div>
    <div v-else-if="error" class="preview-error" role="alert">{{ error }}</div>
    <template v-else-if="url">
      <video v-if="asset.mime === 'video/mp4'" :src="url" :poster="posterUrl || undefined"
        controls playsinline preload="metadata" aria-label="摘要影片預覽" />
      <img v-else-if="asset.mime.startsWith('image/')" :src="url" :alt="alt || asset.name" />
      <pre v-else class="text-preview">{{ content }}</pre>
      <div class="asset-caption">
        <span>已核對 {{ asset.name }} · {{ (asset.size_bytes / 1024).toFixed(1) }} KiB</span>
        <a :href="url" :download="asset.name">下載這份檔案</a>
      </div>
      <p class="verification-note">檔案與套件 SHA 一致；這不是人為核准或送達證明。</p>
    </template>
  </section>
</template>

<script setup>
import { ref, watch, onBeforeUnmount } from 'vue'
import { teachingRepurposeApi } from '@/api/teaching-repurpose'
import { getAuthRevision } from '@/utils/auth'
import { verifyPreviewBlob, previewError } from '@/utils/teaching-repurpose'

const props = defineProps({ taskId: Number, asset: Object, poster: Object, alt: String })
const url = ref(''), posterUrl = ref(''), content = ref(''), loading = ref(false), error = ref('')
let epoch = 0, controller, disposed = false
function releaseUrls() {
  for (const value of [url.value, posterUrl.value]) if (value) URL.revokeObjectURL(value)
  url.value = ''; posterUrl.value = ''; content.value = ''
}
async function load() {
  const sequence = ++epoch, revision = getAuthRevision()
  controller?.abort(); releaseUrls(); error.value = ''
  if (!props.taskId || !props.asset) { loading.value = false; return }
  controller = new AbortController()
  const signal = controller.signal, asset = { ...props.asset }, poster = props.poster && { ...props.poster }
  loading.value = true
  const created = []
  try {
    const fetchVerified = async descriptor => verifyPreviewBlob(
      await teachingRepurposeApi.asset(props.taskId, descriptor.name, { signal }), descriptor, signal,
    )
    const [blob, posterBlob] = await Promise.all([fetchVerified(asset), poster ? fetchVerified(poster) : null])
    const text = asset.mime === 'text/plain' || asset.mime === 'application/json' ? await blob.text() : ''
    if (disposed || sequence !== epoch || signal.aborted || revision !== getAuthRevision()) return
    created.push(URL.createObjectURL(blob))
    if (posterBlob) created.push(URL.createObjectURL(posterBlob))
    url.value = created[0]; posterUrl.value = created[1] || ''; content.value = text
  } catch (failure) {
    for (const value of created) URL.revokeObjectURL(value)
    if (!disposed && sequence === epoch && !signal.aborted && revision === getAuthRevision()) {
      error.value = previewError(failure)
    }
  } finally {
    if (sequence === epoch && !disposed) loading.value = false
  }
}
watch(() => [props.taskId, props.asset?.name, props.asset?.sha256, props.asset?.size_bytes, props.asset?.mime,
  props.poster?.sha256, props.poster?.size_bytes], load, { immediate: true })
watch(getAuthRevision, () => {
  epoch++; controller?.abort(); releaseUrls(); loading.value = false
  error.value = '登入狀態已切換，舊預覽已清除。'
}, { flush: 'sync' })
onBeforeUnmount(() => { disposed = true; epoch++; controller?.abort(); releaseUrls() })
</script>

<style scoped>
.asset-preview { min-width: 0; }
.placeholder, .preview-error { display: grid; place-content: center; min-height: 300px; padding: 24px;
  text-align: center; background: var(--panel-2); border: 1px dashed var(--line-2); border-radius: 12px; color: var(--text-2); }
.preview-error { color: var(--color-danger); }
img, video { display: block; width: 100%; max-width: 430px; max-height: 620px; object-fit: contain;
  margin: 0 auto; border-radius: 8px; background: #1c2330; }
.text-preview { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 520px; overflow: auto;
  padding: 20px; border-radius: 12px; background: var(--panel-2); color: var(--text); font-size: 13px; }
.asset-caption { display: flex; flex-wrap: wrap; gap: 8px; justify-content: space-between;
  margin-top: 14px; color: var(--text-2); font-size: 12px; }
.asset-caption a { text-decoration: underline; text-underline-offset: 3px; }
.verification-note { margin: 8px 0 0; color: var(--text-2); font-size: 11px; }
</style>
