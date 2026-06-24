<template>
  <div class="section-layout">
    <SubNav :tabs="tabs" />
    <div class="section-content">
      <router-view v-slot="{ Component }">
        <transition name="fade" mode="out-in">
          <component :is="Component" />
        </transition>
      </router-view>
    </div>
  </div>
</template>

<script setup>
import SubNav from './SubNav.vue'

const tabs = [
  { id: 'overview',  label: 'Overview',   path: '/analytics/overview' },
  { id: 'campaigns', label: 'Campaigns', path: '/analytics/campaigns' },
]
</script>

<style scoped>
.section-layout {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.section-content {
  flex: 1;
  overflow-y: auto;
}
</style>