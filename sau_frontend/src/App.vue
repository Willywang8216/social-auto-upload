<template>
  <div id="app">
    <!-- Public pages — no chrome -->
    <template v-if="usePublicLayout">
      <router-view v-slot="{ Component }">
        <transition name="fade" mode="out-in">
          <component :is="Component" />
        </transition>
      </router-view>
    </template>

    <!-- Authenticated pages — Control Room layout -->
    <template v-else>
      <div class="app-layout">
        <!-- Sidebar -->
        <aside class="app-sidebar" :class="{ collapsed: isCollapse }">
          <!-- Brand -->
          <div class="sidebar-brand">
            <img src="/socialupload-app-icon.png" alt="Socialupload" class="sidebar-logo" />
            <transition name="fade">
              <div v-show="!isCollapse">
                <div class="sidebar-title">social<b>upload</b></div>
                <div class="sidebar-sub">
                  <span class="wc-dot" style="width:6px;height:6px;box-shadow:none"></span>
                  worker online
                </div>
              </div>
            </transition>
          </div>

          <!-- Navigation — flat list, no group labels -->
          <nav class="sidebar-nav">
            <router-link
              v-for="section in sections"
              :key="section.id"
              :to="section.path"
              class="nav-item"
              :class="{ active: isActive(section.path) }"
              :title="section.label"
            >
              <span class="nav-ic"><component :is="section.icon" /></span>
              <transition name="fade">
                <span v-show="!isCollapse" class="nav-label">{{ section.label }}</span>
              </transition>
              <transition name="fade">
                <span v-show="!isCollapse && section.badge" class="nav-badge">{{ section.badge }}</span>
              </transition>
            </router-link>
          </nav>

          <!-- Footer: Settings + Help nav items + worker card + collapse -->
          <div class="sidebar-footer">
            <router-link
              v-for="item in footerNav"
              :key="item.path"
              :to="item.path"
              class="footer-nav-item"
              :class="{ active: isActive(item.path) }"
            >
              <span class="nav-ic"><component :is="item.icon" /></span>
              <transition name="fade">
                <span v-show="!isCollapse" class="nav-label">{{ item.label }}</span>
              </transition>
            </router-link>
            <div class="worker-card" v-show="!isCollapse">
              <span class="wc-dot"></span>
              <div class="wc-text">
                <div class="t">Worker active</div>
                <div class="s">3 / 3 concurrent · {{ pendingJobCount }} queued</div>
              </div>
            </div>
            <button class="collapse-btn" @click="toggleSidebar" :title="isCollapse ? 'Expand' : 'Collapse'">
              <component :is="isCollapse ? icons.expand : icons.collapse" :width="15" :height="15" />
              <span v-show="!isCollapse" class="sidebar-footer-text">Collapse</span>
            </button>
          </div>
        </aside>

        <!-- Main area -->
        <div class="app-main">
          <!-- Header -->
          <header class="app-header">
            <div class="tb-titles">
              <div class="tb-title">{{ currentTitle }}</div>
              <div class="tb-sub">{{ currentSubtitle }}</div>
            </div>
            <div class="tb-spacer"></div>
            <div class="search-box">
              <component :is="icons.search" :width="16" :height="16" />
              <span>Search accounts, jobs…</span>
              <span class="kbd">⌘K</span>
            </div>
            <button class="icon-btn" title="Notifications">
              <component :is="icons.bell" />
              <span class="dot"></span>
            </button>
            <router-link to="/publish/compose" class="btn-primary">
              <component :is="icons.plus" :width="16" :height="16" /> New Publish
            </router-link>
            <!-- Theme switcher -->
            <div class="theme-switch">
              <button
                v-for="t in themes"
                :key="t"
                class="theme-btn"
                :class="{ on: appStore.theme === t }"
                @click="appStore.setTheme(t)"
                :title="t"
              >{{ t === 'dark' ? '🌙' : '☀️' }}</button>
            </div>
            <div class="avatar">A</div>
          </header>

          <!-- Content -->
          <main class="app-content">
            <router-view v-slot="{ Component }">
              <transition name="fade" mode="out-in">
                <component :is="Component" />
              </transition>
            </router-view>
          </main>

          <!-- Footer -->
          <footer class="app-footer">
            <span class="footer-brand">Socialupload</span>
            <div class="footer-links">
              <router-link to="/privacy">Privacy</router-link>
              <router-link to="/terms">Terms</router-link>
            </div>
          </footer>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRoute } from 'vue-router'
import { useAppStore } from '@/stores/app'
import { icons } from '@/utils/icons'

const route = useRoute()
const appStore = useAppStore()

const usePublicLayout = computed(() => Boolean(route.meta?.publicLayout))
const isCollapse = ref(false)
const toggleSidebar = () => { isCollapse.value = !isCollapse.value }

const themes = ['dark', 'light']

/* Flat sidebar sections — 5 top-level only, matching SO2 redesign NAV */
const sections = [
  { id: 'dashboard', label: 'Dashboard',   icon: icons.dashboard, path: '/dashboard' },
  { id: 'publish',  label: 'Publish',    icon: icons.publish,   path: '/publish',   badge: '3' },
  { id: 'library',  label: 'Library',    icon: icons.materials, path: '/library',   badge: null },
  { id: 'accounts', label: 'Accounts',   icon: icons.accounts, path: '/accounts' },
  { id: 'analytics',label: 'Analytics', icon: icons.analytics,path: '/analytics', badge: null },
]

/* Footer nav — Settings and Help, not daily workflow */
const footerNav = [
  { id: 'settings', label: 'Settings',       icon: icons.settings, path: '/settings' },
  { id: 'help',    label: 'Help & Support', icon: icons.help,     path: '/help' },
]

/* Current page title/subtitle for the topbar */
const titleMap = {
  '/dashboard':             ['Dashboard',       'Your publishing operation at a glance'],
  '/publish':              ['Publish Center',  'Compose once, distribute everywhere'],
  '/publish/compose':      ['Publish Center',  'Compose once, distribute everywhere'],
  '/publish/calendar':     ['Calendar',        'Everything scheduled, at a glance'],
  '/publish/queue':        ['Queue',           'Live publishing activity & history'],
  '/library':              ['Library',         'Your videos, images, templates & brands'],
  '/library/media':         ['Media Library',   'Your videos, images & posts'],
  '/library/templates':     ['Templates',       'Reusable publish presets'],
  '/library/brands':       ['Brands',          'The brands & people you publish as'],
  '/accounts':             ['Accounts',        'Connected social platform identities'],
  '/analytics':            ['Analytics',       'Performance across platforms'],
  '/analytics/overview':   ['Analytics',       'Performance across platforms'],
  '/analytics/campaigns': ['Campaigns',       'Coordinated multi-platform pushes'],
  '/settings':             ['Settings',         'Workspace, integrations & team'],
  '/help':                 ['Help & Support',  'Guides, status & contact'],
  // internal pages
  '/jobs':                 ['Jobs',            'Async publish queue & worker activity'],
  '/sheet-exports':        ['Sheet Exports',   'Scheduled data exports'],
  '/batch-upload':         ['Batch Upload',    'Batch media processing'],
  '/api-docs':             ['API Docs',        'REST endpoints & tokens'],
  '/oauth-review':         ['OAuth Status',    'Platform review & connection state'],
  '/about':                ['About',           'System info & version'],
  '/tiktok-review':        ['TikTok Review',   'Callback events & webhook status'],
  // backward-compat (redirected but title needed if accessed directly)
  '/publish-center':        ['Publish Center',  'Compose once, distribute everywhere'],
  '/calendar':             ['Calendar',        'Everything scheduled, at a glance'],
  '/queue':                ['Queue',           'Live publishing activity & history'],
  '/account-management':   ['Accounts',        'Connected social platform identities'],
  '/profile-management':   ['Brands',          'The brands & people you publish as'],
  '/material-management':  ['Media Library',   'Your videos, images & posts'],
  '/template-management':  ['Templates',       'Reusable publish presets'],
  '/video-analytics':       ['Analytics',       'Performance across platforms'],
  '/campaign-builder':     ['Campaigns',       'Coordinated multi-platform pushes'],
}

const currentTitle = computed(() => titleMap[route.path]?.[0] || 'Socialupload')
const currentSubtitle = computed(() => titleMap[route.path]?.[1] || '')
const pendingJobCount = ref(0)

const isActive = (path) => route.path === path || route.path.startsWith(path + '/')
</script>

<style scoped>
/* Theme switch buttons in the header */
.theme-switch {
  display: flex;
  gap: 2px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  padding: 3px;
}
.theme-btn {
  width: 28px;
  height: 28px;
  border: none;
  background: none;
  border-radius: 6px;
  font-size: 14px;
  cursor: pointer;
  transition: .14s;
  display: grid;
  place-items: center;
}
.theme-btn.on {
  background: var(--raised);
}
.theme-btn:hover:not(.on) {
  background: var(--raised);
}

/* Footer nav items */
.footer-nav-item {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 9px 10px;
  border-radius: var(--r-md);
  color: var(--text-2);
  font-size: 13.5px;
  font-weight: 500;
  text-decoration: none;
  transition: .15s;
  margin-bottom: 1px;
}
.footer-nav-item:hover {
  background: var(--raised);
  color: var(--text);
}
.footer-nav-item.active {
  background: var(--accent-soft);
  color: var(--text);
}

/* Nav badge */
.nav-badge {
  margin-left: auto;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 10px;
  font-weight: 700;
  padding: 1px 6px;
  border-radius: var(--r-full);
  line-height: 1.5;
}
</style>