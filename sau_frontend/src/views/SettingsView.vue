<template>
  <div class="settings-view">
    <div class="settings-header">
      <h1>Settings</h1>
      <p class="settings-sub">Workspace, integrations & team</p>
    </div>

    <el-row :gutter="24">
      <!-- Left: settings nav -->
      <el-col :span="6">
        <el-card class="settings-nav-card">
          <div
            v-for="section in sections"
            :key="section.id"
            class="settings-nav-item"
            :class="{ active: activeSection === section.id }"
            @click="activeSection = section.id"
          >
            <el-icon :size="16"><component :is="section.icon" /></el-icon>
            <span>{{ section.label }}</span>
          </div>
        </el-card>
      </el-col>

      <!-- Right: content area -->
      <el-col :span="18">
        <!-- Connections -->
        <el-card v-show="activeSection === 'connections'" class="settings-card">
          <template #header>
            <div class="card-header">
              <h3>Connections</h3>
              <p class="card-sub">Manage platform OAuth connections and cookie-based accounts.</p>
            </div>
          </template>
          <div class="settings-action-row">
            <el-button type="primary" @click="goTo('/oauth-review')">
              <el-icon><Link /></el-icon>
              OAuth & Cookie Status
            </el-button>
            <p class="action-desc">View all connected accounts, refresh tokens, and check cookie expiry.</p>
          </div>
        </el-card>

        <!-- Accounts -->
        <el-card v-show="activeSection === 'accounts'" class="settings-card">
          <template #header>
            <div class="card-header">
              <h3>Accounts</h3>
              <p class="card-sub">Import and export account cookies.</p>
            </div>
          </template>
          <div class="settings-action-row">
            <el-button @click="goTo('/account-management')">
              <el-icon><User /></el-icon>
              Manage Accounts
            </el-button>
            <p class="action-desc">View connected platform accounts and their cookie status.</p>
          </div>
        </el-card>

        <!-- Advanced -->
        <el-card v-show="activeSection === 'advanced'" class="settings-card">
          <template #header>
            <div class="card-header">
              <h3>Advanced</h3>
              <p class="card-sub">Developer tools, exports, and platform review status.</p>
            </div>
          </template>
          <div class="adv-grid">
            <router-link to="/api-docs" class="adv-card">
              <el-icon :size="24"><Document /></el-icon>
              <div>
                <div class="adv-title">API Documentation</div>
                <div class="adv-sub">REST endpoints, tokens & request formats</div>
              </div>
            </router-link>
            <router-link to="/tiktok-review" class="adv-card">
              <el-icon :size="24"><VideoCamera /></el-icon>
              <div>
                <div class="adv-title">TikTok Review Status</div>
                <div class="adv-sub">Callback events, webhooks & OAuth review state</div>
              </div>
            </router-link>
            <router-link to="/sheet-exports" class="adv-card">
              <el-icon :size="24"><Grid /></el-icon>
              <div>
                <div class="adv-title">Sheet Exports</div>
                <div class="adv-sub">Export publish data to Google Sheets</div>
              </div>
            </router-link>
            <router-link to="/about" class="adv-card">
              <el-icon :size="24"><InfoFilled /></el-icon>
              <div>
                <div class="adv-title">About</div>
                <div class="adv-sub">Version, tech stack & system info</div>
              </div>
            </router-link>
          </div>
        </el-card>

        <!-- Appearance -->
        <el-card v-show="activeSection === 'appearance'" class="settings-card">
          <template #header>
            <div class="card-header">
              <h3>Appearance</h3>
              <p class="card-sub">Customize the look and feel of your workspace.</p>
            </div>
          </template>
          <div class="setting-row">
            <div class="setting-label">Theme</div>
            <div class="theme-switch">
              <button
                v-for="t in ['dark', 'light']"
                :key="t"
                class="theme-btn"
                :class="{ on: appStore.theme === t }"
                @click="appStore.setTheme(t)"
              >{{ t === 'dark' ? 'Dark' : 'Light' }}</button>
            </div>
          </div>
          <div class="setting-row">
            <div class="setting-label">Accent Color</div>
            <div class="accent-grid">
              <button
                v-for="a in accents"
                :key="a.id"
                class="accent-btn"
                :class="{ on: appStore.accent === a.id }"
                :style="{ '--btn-accent': a.color }"
                @click="appStore.setAccent(a.id)"
              >
                <span class="accent-dot" :style="{ background: a.color }"></span>
                {{ a.label }}
              </button>
            </div>
          </div>
          <div class="setting-row">
            <div class="setting-label">Density</div>
            <div class="density-btns">
              <button
                v-for="d in ['compact', 'comfortable']"
                :key="d"
                class="density-btn"
                :class="{ on: appStore.density === d }"
                @click="appStore.setDensity(d)"
              >{{ d }}</button>
            </div>
          </div>
        </el-card>

        <!-- Worker -->
        <el-card v-show="activeSection === 'worker'" class="settings-card">
          <template #header>
            <div class="card-header">
              <h3>Worker Status</h3>
              <p class="card-sub">Real-time job queue and worker activity.</p>
            </div>
          </template>
          <div class="worker-status">
            <div class="worker-dot-card">
              <span class="worker-dot" :class="workerOk ? 'green' : 'red'"></span>
              <div>
                <div class="worker-title">{{ workerOk ? 'Worker active' : 'Worker offline' }}</div>
                <div class="worker-sub">{{ pendingJobCount }} jobs queued · 3 concurrent</div>
              </div>
            </div>
            <el-button size="small" @click="goTo('/queue')">View Queue</el-button>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAppStore } from '@/stores/app'
import {
  Link, User, Document, VideoCamera, Grid, InfoFilled,
  Connection, Tools, Monitor, Setting
} from '@element-plus/icons-vue'

const router = useRouter()
const appStore = useAppStore()
const activeSection = ref('connections')
const pendingJobCount = ref(12)
const workerOk = ref(true)

const sections = [
  { id: 'connections', label: 'Connections', icon: Link },
  { id: 'accounts',    label: 'Accounts',    icon: User },
  { id: 'advanced',    label: 'Advanced',     icon: Tools },
  { id: 'appearance',  label: 'Appearance',  icon: Setting },
  { id: 'worker',       label: 'Worker',       icon: Monitor },
]

const accents = [
  { id: 'lime',   label: 'Lime',   color: '#c8f04a' },
  { id: 'coral',  label: 'Coral',  color: '#ff6a3d' },
  { id: 'cyan',   label: 'Cyan',   color: '#37e0e0' },
  { id: 'violet', label: 'Violet', color: '#9a86ff' },
]

function goTo(path) {
  router.push(path)
}
</script>

<style scoped>
.settings-view {
  padding: var(--space-6);
  max-width: 960px;
}
.settings-header {
  margin-bottom: var(--space-6);
}
.settings-header h1 {
  font-family: var(--font-display);
  font-size: 28px;
  margin: 0 0 4px;
}
.settings-sub {
  color: var(--text-2);
  margin: 0;
  font-size: 14px;
}
.settings-nav-card {
  padding: var(--space-2);
}
.settings-nav-item {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: 10px 12px;
  border-radius: var(--r-md);
  cursor: pointer;
  font-size: 13.5px;
  font-weight: 500;
  color: var(--text-2);
  transition: all var(--transition-fast);
}
.settings-nav-item:hover {
  background: var(--raised);
  color: var(--text);
}
.settings-nav-item.active {
  background: var(--accent-soft);
  color: var(--text);
}
.card-header h3 {
  margin: 0 0 4px;
  font-family: var(--font-display);
  font-size: 16px;
}
.card-sub {
  color: var(--text-3);
  margin: 0;
  font-size: 12px;
}
.settings-action-row {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  align-items: flex-start;
}
.action-desc {
  color: var(--text-3);
  font-size: 12px;
  margin: 0;
}
.adv-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-3);
}
.adv-card {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-4);
  background: var(--raised);
  border-radius: var(--r-md);
  color: var(--text);
  text-decoration: none;
  transition: background var(--transition-fast);
}
.adv-card:hover {
  background: var(--panel-2);
}
.adv-title {
  font-size: 13.5px;
  font-weight: 600;
  margin-bottom: 2px;
}
.adv-sub {
  font-size: 11.5px;
  color: var(--text-3);
}
.setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) 0;
  border-bottom: 1px solid var(--line);
}
.setting-row:last-child {
  border-bottom: none;
}
.setting-label {
  font-size: 13.5px;
  font-weight: 500;
}
.theme-switch {
  display: flex;
  gap: var(--space-2);
}
.theme-btn {
  padding: 6px 14px;
  border: 1px solid var(--line);
  background: none;
  border-radius: var(--r-md);
  font-size: 13px;
  cursor: pointer;
  color: var(--text-2);
  transition: all var(--transition-fast);
}
.theme-btn.on {
  border-color: var(--accent);
  color: var(--text);
  background: var(--accent-soft);
}
.accent-grid {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.accent-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border: 1px solid var(--line);
  background: none;
  border-radius: var(--r-md);
  font-size: 12.5px;
  cursor: pointer;
  color: var(--text-2);
  transition: all var(--transition-fast);
}
.accent-btn.on {
  border-color: var(--btn-accent);
  color: var(--text);
  background: color-mix(in srgb, var(--btn-accent) 15%, transparent);
}
.accent-dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
}
.density-btns {
  display: flex;
  gap: var(--space-2);
}
.density-btn {
  padding: 6px 14px;
  border: 1px solid var(--line);
  background: none;
  border-radius: var(--r-md);
  font-size: 13px;
  cursor: pointer;
  color: var(--text-2);
  text-transform: capitalize;
  transition: all var(--transition-fast);
}
.density-btn.on {
  border-color: var(--accent);
  color: var(--text);
  background: var(--accent-soft);
}
.worker-status {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.worker-dot-card {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}
.worker-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}
.worker-dot.green { background: var(--color-success); box-shadow: 0 0 8px var(--color-success); }
.worker-dot.red   { background: var(--color-danger); }
.worker-title { font-size: 14px; font-weight: 600; }
.worker-sub   { font-size: 12px; color: var(--text-3); }
</style>