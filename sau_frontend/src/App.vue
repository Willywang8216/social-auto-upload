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

    <!-- Authenticated pages — sidebar + header layout -->
    <template v-else>
      <div class="app-layout">
        <!-- Sidebar -->
        <aside class="app-sidebar" :class="{ collapsed: isCollapse }">
          <!-- Brand -->
          <div class="sidebar-brand">
            <img src="/socialupload-app-icon.png" alt="Socialupload" class="sidebar-logo" />
            <transition name="fade">
              <span v-show="!isCollapse" class="sidebar-title">Socialupload</span>
            </transition>
          </div>

          <!-- Navigation -->
          <nav class="sidebar-nav">
            <router-link
              v-for="item in navItems"
              :key="item.path"
              :to="item.path"
              class="nav-item"
              :class="{ active: isActive(item.path) }"
            >
              <el-icon class="nav-icon"><component :is="item.icon" /></el-icon>
              <transition name="fade">
                <span v-show="!isCollapse" class="nav-label">{{ item.label }}</span>
              </transition>
            </router-link>
          </nav>

          <!-- Collapse toggle -->
          <div class="sidebar-footer">
            <button class="collapse-btn" @click="toggleSidebar" :title="isCollapse ? 'Expand' : 'Collapse'">
              <el-icon>
                <Fold v-if="!isCollapse" />
                <Expand v-else />
              </el-icon>
            </button>
          </div>
        </aside>

        <!-- Main area -->
        <div class="app-main">
          <!-- Header -->
          <header class="app-header">
            <div class="header-left">
              <el-breadcrumb separator="/">
                <el-breadcrumb-item :to="{ path: '/dashboard' }">Home</el-breadcrumb-item>
                <el-breadcrumb-item v-if="currentNav">{{ currentNav.label }}</el-breadcrumb-item>
              </el-breadcrumb>
            </div>
            <div class="header-right">
              <router-link class="header-link" to="/">Landing</router-link>
              <router-link class="header-link" to="/privacy">Privacy</router-link>
              <router-link class="header-link" to="/terms">Terms</router-link>
            </div>
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
import {
  HomeFilled,
  User,
  Folder,
  Picture,
  Upload,
  Files,
  List,
  TrendCharts,
  Edit,
  Document,
  Connection,
  DataAnalysis,
  Fold,
  Expand
} from '@element-plus/icons-vue'

const route = useRoute()

const usePublicLayout = computed(() => Boolean(route.meta?.publicLayout))
const isCollapse = ref(false)

const toggleSidebar = () => {
  isCollapse.value = !isCollapse.value
}

/* Navigation items — all English labels */
const navItems = [
  { path: '/dashboard',            label: 'Dashboard',       icon: HomeFilled },
  { path: '/account-management',   label: 'Accounts',        icon: User },
  { path: '/profile-management',   label: 'Profiles',        icon: Folder },
  { path: '/material-management',  label: 'Materials',       icon: Picture },
  { path: '/publish-center',       label: 'Publish Center',  icon: Upload },
  { path: '/template-management',  label: 'Templates',       icon: Files },
  { path: '/jobs',                 label: 'Jobs',            icon: List },
  { path: '/video-analytics',      label: 'Analytics',       icon: TrendCharts },
  { path: '/batch-upload',         label: 'Batch Upload',    icon: Upload },
  { path: '/campaign-builder',     label: 'Campaigns',       icon: Edit },
  { path: '/sheet-exports',        label: 'Sheet Exports',   icon: Document },
  { path: '/api-docs',             label: 'API Docs',        icon: Document },
  { path: '/oauth-review',         label: 'OAuth Status',    icon: Connection },
  { path: '/about',                label: 'About',           icon: DataAnalysis },
]

const isActive = (path) => route.path === path || route.path.startsWith(path + '/')

const currentNav = computed(() => navItems.find(item => isActive(item.path)))
</script>

<style lang="scss" scoped>
/* ======================================================================
   Layout Shell
   ====================================================================== */

.app-layout {
  display: flex;
  height: 100vh;
  overflow: hidden;
}


/* ======================================================================
   Sidebar
   ====================================================================== */

.app-sidebar {
  width: var(--sidebar-width);
  background: var(--sidebar-bg);
  display: flex;
  flex-direction: column;
  transition: width var(--transition-slow);
  flex-shrink: 0;
  z-index: 10;

  &.collapsed {
    width: var(--sidebar-width-collapsed);
  }
}

/* Brand */
.sidebar-brand {
  height: var(--header-height);
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 16px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  flex-shrink: 0;
  overflow: hidden;
}

.sidebar-logo {
  width: 28px;
  height: 28px;
  border-radius: 6px;
  flex-shrink: 0;
}

.sidebar-title {
  font-size: 15px;
  font-weight: 700;
  color: #fff;
  white-space: nowrap;
  letter-spacing: -0.02em;
}

/* Navigation */
.sidebar-nav {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 12px 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  color: var(--sidebar-text);
  text-decoration: none;
  font-size: 14px;
  font-weight: 500;
  transition: all var(--transition-fast);
  white-space: nowrap;
  overflow: hidden;

  &:hover {
    background: var(--sidebar-bg-hover);
    color: var(--sidebar-text-active);
  }

  &.active {
    background: var(--sidebar-bg-active);
    color: var(--sidebar-text-active);

    .nav-icon {
      color: var(--color-primary);
    }
  }
}

.nav-icon {
  font-size: 18px;
  flex-shrink: 0;
}

.nav-label {
  overflow: hidden;
  text-overflow: ellipsis;
}

/* Footer */
.sidebar-footer {
  padding: 12px 8px;
  border-top: 1px solid rgba(255, 255, 255, 0.06);
  flex-shrink: 0;
}

.collapse-btn {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  height: 36px;
  border-radius: var(--radius-md);
  border: none;
  background: transparent;
  color: var(--sidebar-text);
  cursor: pointer;
  transition: all var(--transition-fast);

  &:hover {
    background: var(--sidebar-bg-hover);
    color: var(--sidebar-text-active);
  }

  .el-icon {
    font-size: 18px;
  }
}


/* ======================================================================
   Main Area
   ====================================================================== */

.app-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 0;
}

/* Header */
.app-header {
  height: var(--header-height);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  background: var(--color-bg);
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.header-left {
  display: flex;
  align-items: center;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 16px;
}

.header-link {
  font-size: 13px;
  color: var(--color-text-muted);
  text-decoration: none;
  font-weight: 500;
  transition: color var(--transition-fast);

  &:hover {
    color: var(--color-primary);
  }
}

/* Content */
.app-content {
  flex: 1;
  overflow-y: auto;
  background: var(--color-bg-page);
}

/* Footer */
.app-footer {
  height: 48px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  background: var(--color-bg);
  border-top: 1px solid var(--color-border);
  flex-shrink: 0;
}

.footer-brand {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-secondary);
}

.footer-links {
  display: flex;
  gap: 16px;

  a {
    font-size: 13px;
    color: var(--color-text-muted);
    text-decoration: none;

    &:hover {
      color: var(--color-primary);
    }
  }
}


/* ======================================================================
   Responsive
   ====================================================================== */

@media (max-width: 768px) {
  .app-sidebar {
    width: var(--sidebar-width-collapsed);

    .sidebar-title,
    .nav-label {
      display: none;
    }
  }

  .app-header {
    padding: 0 16px;
  }

  .header-right {
    display: none;
  }
}
</style>
