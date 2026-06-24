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
  { id: 'compose',  label: 'Compose',  path: '/publish/compose' },
  { id: 'calendar', label: 'Calendar', path: '/publish/calendar' },
  { id: 'queue',   label: 'Queue',   path: '/publish/queue', badge: '12' },
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