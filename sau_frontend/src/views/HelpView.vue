<template>
  <div class="help-view">
    <!-- Hero -->
    <div class="help-hero">
      <div class="hh-ic"><component :is="icons.help" /></div>
      <div class="hh-text">
        <h3>How can we help?</h3>
        <p>Guides for every part of Socialupload — publishing, connections, analytics and the API.</p>
      </div>
      <div class="spacer"></div>
      <button class="btn-primary">
        <component :is="icons.bell" :width="16" :height="16" /> Contact support
      </button>
    </div>

    <!-- Help topic cards -->
    <div class="help-grid">
      <router-link
        v-for="link in HELP_LINKS"
        :key="link.name"
        :to="link.route"
        class="help-card"
      >
        <div class="hc-ic"><component :is="icons[link.ic]" /></div>
        <div class="hc-body">
          <div class="hc-name">{{ link.name }}</div>
          <div class="hc-desc">{{ link.desc }}</div>
        </div>
        <div class="hc-arrow"><component :is="icons.arrow" :width="16" :height="16" /></div>
      </router-link>
    </div>
  </div>
</template>

<script setup>
import { icons } from '@/utils/icons'

const HELP_LINKS = [
  { ic: 'publish',   name: 'Publishing guide',          desc: 'Compose, schedule & fan-out to every platform',         route: '/publish/compose' },
  { ic: 'oauth',     name: 'Connecting accounts',       desc: 'QR login & cookie import, step by step',             route: '/accounts' },
  { ic: 'clock',     name: 'Fixing expired sessions',   desc: 'Why cookies expire and how to re-auth fast',        route: '/settings' },
  { ic: 'templates', name: 'Templates & brands',        desc: 'Reuse presets and publish as the right brand',       route: '/library/templates' },
  { ic: 'analytics', name: 'Reading analytics',         desc: 'What each metric means across platforms',             route: '/analytics/overview' },
  { ic: 'api',       name: 'API & integrations',         desc: 'Tokens, webhooks & REST access for developers',      route: '/settings' },
]
</script>

<style scoped>
.help-view {
  padding: var(--space-6);
}

.help-hero {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-6);
  background: var(--panel);
  border-radius: var(--r-xl);
  margin-bottom: var(--space-6);
  border: 1px solid var(--line);
}

.hh-ic {
  width: 48px;
  height: 48px;
  background: var(--accent-soft);
  border-radius: var(--r-lg);
  display: grid;
  place-items: center;
  color: var(--accent);
  flex-shrink: 0;
}

.hh-text h3 {
  font-family: var(--font-display);
  font-size: 18px;
  margin: 0 0 4px;
}

.hh-text p {
  color: var(--text-2);
  margin: 0;
  font-size: 13.5px;
}

.spacer { flex: 1; }

.help-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: var(--space-3);
}

.help-card {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-4) var(--space-5);
  background: var(--panel);
  border-radius: var(--r-lg);
  border: 1px solid var(--line);
  color: var(--text);
  text-decoration: none;
  transition: all var(--transition-fast);
}

.help-card:hover {
  background: var(--raised);
  border-color: var(--line-2);
}

.hc-ic {
  width: 40px;
  height: 40px;
  background: var(--raised);
  border-radius: var(--r-md);
  display: grid;
  place-items: center;
  color: var(--text-2);
  flex-shrink: 0;
}

.hc-body {
  flex: 1;
  min-width: 0;
}

.hc-name {
  font-size: 13.5px;
  font-weight: 600;
  margin-bottom: 2px;
}

.hc-desc {
  font-size: 12px;
  color: var(--text-3);
}

.hc-arrow {
  color: var(--text-3);
  flex-shrink: 0;
}
</style>