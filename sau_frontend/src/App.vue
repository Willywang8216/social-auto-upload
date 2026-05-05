<template>
  <div id="app">
    <template v-if="usePublicLayout">
      <router-view />
    </template>
    <template v-else>
      <el-container>
        <el-aside :width="isCollapse ? '64px' : '200px'">
          <div class="sidebar">
            <div class="logo">
              <img v-show="isCollapse" src="/vite.svg" alt="Logo" class="logo-img">
              <h2 v-show="!isCollapse">自媒體自動化營運系統</h2>
            </div>
            <el-menu
              :router="true"
              :default-active="activeMenu"
              :collapse="isCollapse"
              class="sidebar-menu"
              background-color="#001529"
              text-color="#fff"
              active-text-color="#409EFF"
            >
              <el-menu-item index="/">
                <el-icon><HomeFilled /></el-icon>
                <span>儀表板</span>
              </el-menu-item>
              <el-menu-item index="/account-management">
                <el-icon><User /></el-icon>
                <span>帳號管理</span>
              </el-menu-item>
              <el-menu-item index="/material-management">
                <el-icon><Picture /></el-icon>
                <span>素材庫</span>
              </el-menu-item>
              <el-menu-item index="/publish-center">
                <el-icon><Upload /></el-icon>
                <span>發佈中心</span>
              </el-menu-item>
              <el-menu-item index="/jobs">
                <el-icon><List /></el-icon>
                <span>任務中心</span>
              </el-menu-item>
              <el-menu-item index="/tiktok-review">
                <el-icon><Connection /></el-icon>
                <span>TikTok callback</span>
              </el-menu-item>
              <el-menu-item index="/about">
                <el-icon><DataAnalysis /></el-icon>
                <span>說明</span>
              </el-menu-item>
            </el-menu>
          </div>
        </el-aside>
        <el-container>
          <el-header>
            <div class="header-content">
              <div class="header-left">
                <el-icon class="toggle-sidebar" @click="toggleSidebar"><Fold /></el-icon>
              </div>
              <div class="header-right">
                <div class="header-links">
                  <a class="domain-link" href="https://up.iamwillywang.com" target="_blank" rel="noreferrer">up.iamwillywang.com</a>
                  <span>·</span>
                  <router-link to="/privacy">Privacy</router-link>
                  <span>·</span>
                  <router-link to="/terms">Terms</router-link>
                </div>
              </div>
            </div>
          </el-header>
          <el-main>
            <router-view />
          </el-main>
          <el-footer>
            <div class="app-footer">
              <span>Operated via up.iamwillywang.com</span>
              <div class="footer-links">
                <router-link to="/privacy">Privacy Policy</router-link>
                <span>·</span>
                <router-link to="/terms">Terms of Use</router-link>
              </div>
            </div>
          </el-footer>
        </el-container>
      </el-container>
    </template>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRoute } from 'vue-router'
import {
  HomeFilled, User, DataAnalysis,
  Fold, Picture, Upload, List, Connection
} from '@element-plus/icons-vue'

const route = useRoute()

const activeMenu = computed(() => route.path)
const usePublicLayout = computed(() => Boolean(route.meta?.publicLayout))
const isCollapse = ref(false)

const toggleSidebar = () => {
  isCollapse.value = !isCollapse.value
}
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

#app {
  min-height: 100vh;
}

.el-container {
  height: 100vh;
}

.el-aside {
  background-color: #001529;
  color: #fff;
  height: 100vh;
  overflow: hidden;
  transition: width 0.3s;

  .sidebar {
    display: flex;
    flex-direction: column;
    height: 100%;

    .logo {
      height: 60px;
      padding: 0 16px;
      display: flex;
      align-items: center;
      background-color: #002140;
      overflow: hidden;

      .logo-img {
        width: 32px;
        height: 32px;
        margin-right: 12px;
      }

      h2 {
        color: #fff;
        font-size: 16px;
        font-weight: 600;
        white-space: nowrap;
        margin: 0;
      }
    }

    .sidebar-menu {
      border-right: none;
      flex: 1;

      .el-menu-item {
        display: flex;
        align-items: center;

        .el-icon {
          margin-right: 10px;
          font-size: 18px;
        }
      }
    }
  }
}

.el-header {
  background-color: #fff;
  box-shadow: 0 1px 4px rgba(0, 21, 41, 0.08);
  padding: 0;
  height: 60px;

  .header-content {
    display: flex;
    justify-content: space-between;
    align-items: center;
    height: 100%;
    padding: 0 16px;

    .header-left {
      .toggle-sidebar {
        font-size: 20px;
        cursor: pointer;
        color: $text-regular;

        &:hover {
          color: $primary-color;
        }
      }
    }

    .header-right {
      .header-links {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 13px;

        a,
        :deep(a) {
          color: $text-secondary;
          text-decoration: none;

          &:hover {
            color: $primary-color;
          }
        }

        .domain-link {
          font-weight: 500;
        }
      }
    }
  }
}

.el-main {
  background-color: $bg-color-page;
  padding: 20px;
  overflow-y: auto;
}

.el-footer {
  height: 52px;
  background: #fff;
  border-top: 1px solid #ebeef5;
  padding: 0 20px;

  .app-footer {
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    color: $text-secondary;
    font-size: 13px;

    .footer-links {
      display: flex;
      align-items: center;
      gap: 8px;

      a,
      :deep(a) {
        color: $text-secondary;
        text-decoration: none;

        &:hover {
          color: $primary-color;
        }
      }
    }
  }
}
</style>
