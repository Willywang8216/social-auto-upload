<script setup>
/*
 * This route used to render its own copy of the privacy policy as a Vue
 * component. Two copies of a legal document drift apart — this one still said
 * "Last updated: 2026-05-10" and had no Google user data disclosure long after
 * the published policy gained both, which is exactly what an OAuth reviewer
 * checks. The published policy now lives in one place, `privacy-policy.html`,
 * served from `/privacy` as a plain document so it is readable without
 * JavaScript. Hand off to it instead of rendering a second copy.
 */
import { onMounted } from 'vue'

onMounted(() => {
  window.location.replace('/privacy')
})
</script>

<template>
  <div class="legal-redirect">
    <p>Opening the <a href="/privacy">Privacy Policy</a>&hellip;</p>
  </div>
</template>

<style scoped>
.legal-redirect {
  max-width: 900px;
  margin: 80px auto;
  padding: 0 20px;
  font-family: 'Inter', system-ui, sans-serif;
  color: #333844;
}
.legal-redirect a { color: #0b57d0; }
</style>
