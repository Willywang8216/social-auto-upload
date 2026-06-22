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

          <!-- Navigation -->
          <nav class="sidebar-nav">
            <div v-for="group in navGroups" :key="group.label">
              <div class="nav-group-label">{{ group.label }}</div>
              <router-link
                v-for="item in group.items"
                :key="item.path"
                :to="item.path"
                class="nav-item"
                :class="{ active: isActive(item.path) }"
              >
                <span class="nav-ic"><component :is="item.icon" /></span>
                <transition name="fade">
                  <span v-show="!isCollapse" class="nav-label">{{ item.label }}</span>
                </transition>
              </router-link>
            </div>
          </nav>

          <!-- Footer: worker card + collapse -->
          <div class="sidebar-footer">
            <div class="worker-card">
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
            <router-link to="/publish-center" class="btn-primary">
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

/* Navigation groups — matches the redesign sidebar structure */
const navGroups = [
  {
    label: 'Workspace',
    items: [
      { path: '/dashboard',        label: 'Dashboard',      icon: icons.dashboard },
      { path: '/publish-center',   label: 'Publish Center', icon: icons.publish },
      { path: '/jobs',             label: 'Jobs',           icon: icons.jobs },
    ],
  },
  {
    label: 'Library',
    items: [
      { path: '/account-management',  label: 'Accounts',   icon: icons.accounts },
      { path: '/profile-management',  label: 'Profiles',   icon: icons.profiles },
      { path: '/material-management', label: 'Materials',  icon: icons.materials },
      { path: '/template-management', label: 'Templates',  icon: icons.templates },
    ],
  },
  {
    label: 'Insights',
    items: [
      { path: '/video-analytics',   label: 'Analytics',     icon: icons.analytics },
      { path: '/campaign-builder',  label: 'Campaigns',     icon: icons.campaigns },
      { path: '/sheet-exports',     label: 'Sheet Exports', icon: icons.sheet },
      { path: '/batch-upload',      label: 'Batch Upload',  icon: icons.upload },
    ],
  },
  {
    label: 'System',
    items: [
      { path: '/api-docs',     label: 'API Docs',     icon: icons.api },
      { path: '/oauth-review', label: 'OAuth Status', icon: icons.oauth },
      { path: '/about',        label: 'About',        icon: icons.about },
    ],
  },
]

/* Current page title/subtitle for the topbar */
const titleMap = {
  '/dashboard':            ['Dashboard',       'Your publishing operation at a glance'],
  '/publish-center':       ['Publish Center',  'Compose once, distribute everywhere'],
  '/jobs':                 ['Jobs',            'Async publish queue & worker activity'],
  '/account-management':   ['Accounts',        'Connected social platform identities'],
  '/profile-management':   ['Profiles',        'Brands & people you publish as'],
  '/material-management':  ['Materials',       'Your media library'],
  '/template-management':  ['Templates',       'Reusable publish presets'],
  '/video-analytics':      ['Analytics',       'Performance across platforms'],
  '/campaign-builder':     ['Campaigns',       'Coordinated multi-platform pushes'],
  '/sheet-exports':        ['Sheet Exports',   'Scheduled data exports'],
  '/batch-upload':         ['Batch Upload',    'Batch media processing'],
  '/api-docs':             ['API Docs',        'REST endpoints & tokens'],
  '/oauth-review':         ['OAuth Status',    'Platform review & connection state'],
  '/about':                ['About',           'System info & version'],
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
</style>
