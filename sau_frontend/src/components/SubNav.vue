<template>
  <div class="sub-nav" v-if="tabs && tabs.length">
    <router-link
      v-for="tab in tabs"
      :key="tab.id"
      :to="tab.path"
      class="sub-nav-item"
      :class="{ active: isActive(tab.path) }"
    >
      {{ tab.label }}
      <span v-if="tab.badge" class="sb-badge">{{ tab.badge }}</span>
    </router-link>
  </div>
</template>

<script setup>
import { useRoute } from 'vue-router'

const props = defineProps({
  tabs: {
    type: Array,
    default: () => [],
  },
})

const route = useRoute()

const isActive = (path) => route.path === path || route.path.startsWith(path + '/')
</script>

<style scoped>
.sub-nav {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 0 var(--space-4);
  border-bottom: 1px solid var(--line);
  background: var(--panel);
  height: 42px;
  flex-shrink: 0;
}

.sub-nav-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border-radius: var(--r-md);
  font-size: 13px;
  font-weight: 500;
  color: var(--text-2);
  text-decoration: none;
  transition: all var(--transition-fast);
  white-space: nowrap;
}

.sub-nav-item:hover {
  background: var(--raised);
  color: var(--text);
}

.sub-nav-item.active {
  background: var(--accent-soft);
  color: var(--text);
}

.sb-badge {
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 10px;
  font-weight: 700;
  padding: 1px 6px;
  border-radius: var(--r-full);
  line-height: 1.5;
}
</style>