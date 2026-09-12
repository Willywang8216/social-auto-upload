<script setup>
/*
 * Same reasoning as PrivacyPolicy.vue: the terms of service is a legal
 * document and only one copy of it should exist. The published version is
 * `terms-of-service.html`, served from `/terms`. This route hands off to it.
 */
import { onMounted } from 'vue'

onMounted(() => {
  window.location.replace('/terms')
})
</script>

<template>
  <div class="legal-redirect">
    <p>Opening the <a href="/terms">Terms of Service</a>&hellip;</p>
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
