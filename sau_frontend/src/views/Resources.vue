<template>
  <div class="resources">
    <el-card class="rs-card rs-card--header" shadow="never">
      <h2>LGBTQIA+ 友善資源</h2>
      <p class="rs-subtle">
        台灣 LGBTQIA+ 與性別友善社群組織的精選名錄，涵蓋諮詢熱線、健康篩檢、愛滋支持、教育倡議與在地社群中心。
        每張卡片下方的「資料來源」連結指向我們整理時所引用的官方頁面。
      </p>
      <div class="rs-filters">
        <el-tag
          :type="activeFilter === 'all' ? '' : 'info'"
          :effect="activeFilter === 'all' ? 'dark' : 'plain'"
          class="rs-chip"
          @click="activeFilter = 'all'"
        >
          全部 ({{ resources.length }})
        </el-tag>
        <el-tag
          v-for="region in regionFilters"
          :key="region"
          :type="activeFilter === region ? '' : 'info'"
          :effect="activeFilter === region ? 'dark' : 'plain'"
          class="rs-chip"
          @click="activeFilter = region"
        >
          {{ region }} ({{ regionCounts[region] }})
        </el-tag>
      </div>
    </el-card>

    <div class="rs-grid">
      <el-card
        v-for="org in filteredResources"
        :key="org.id"
        class="rs-card rs-card--org"
        shadow="hover"
      >
        <div class="rs-org-header">
          <div>
            <h3>{{ org.nameZh }}</h3>
            <div v-if="org.nameEn" class="rs-name-en">{{ org.nameEn }}</div>
          </div>
          <el-tag size="small" type="info">{{ org.region }}</el-tag>
        </div>

        <p v-if="org.intro" class="rs-intro">{{ org.intro }}</p>
        <p v-else class="rs-intro rs-subtle">（暫無官方簡介資料）</p>

        <div v-if="org.services && org.services.length" class="rs-services">
          <el-tag
            v-for="service in org.services"
            :key="service"
            size="small"
            effect="plain"
            class="rs-service-tag"
          >
            {{ service }}
          </el-tag>
        </div>

        <div class="rs-meta">
          <div v-if="org.website" class="rs-meta-item">
            <el-icon><Link /></el-icon>
            <a :href="org.website" target="_blank" rel="noopener noreferrer">{{ shortenUrl(org.website) }}</a>
          </div>
          <div v-if="org.phone" class="rs-meta-item">
            <el-icon><Phone /></el-icon>
            <span>{{ org.phone }}</span>
          </div>
          <div v-if="org.email" class="rs-meta-item">
            <el-icon><Message /></el-icon>
            <a :href="`mailto:${org.email}`">{{ org.email }}</a>
          </div>
        </div>

        <div v-if="hasSocial(org.social)" class="rs-social">
          <a
            v-if="org.social.facebook"
            :href="org.social.facebook"
            target="_blank"
            rel="noopener noreferrer"
            class="rs-social-link"
          >Facebook</a>
          <a
            v-if="org.social.instagram"
            :href="org.social.instagram"
            target="_blank"
            rel="noopener noreferrer"
            class="rs-social-link"
          >Instagram</a>
          <a
            v-if="org.social.threads"
            :href="org.social.threads"
            target="_blank"
            rel="noopener noreferrer"
            class="rs-social-link"
          >Threads</a>
          <a
            v-if="org.social.linkedin"
            :href="org.social.linkedin"
            target="_blank"
            rel="noopener noreferrer"
            class="rs-social-link"
          >LinkedIn</a>
        </div>

        <div v-if="org.source" class="rs-source">
          <a :href="org.source" target="_blank" rel="noopener noreferrer">資料來源</a>
        </div>
      </el-card>
    </div>

    <div v-if="filteredResources.length === 0" class="rs-empty">
      <p>此地區暫無資料</p>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { Link, Phone, Message } from '@element-plus/icons-vue'

import data from '@/assets/lgbtqia-taiwan.json'

const resources = ref(data)
const activeFilter = ref('all')

const regionFilters = computed(() => {
  const seen = new Set()
  for (const org of resources.value) {
    if (org.region) seen.add(org.region)
  }
  return Array.from(seen).sort()
})

const regionCounts = computed(() => {
  const counts = {}
  for (const org of resources.value) {
    if (!org.region) continue
    counts[org.region] = (counts[org.region] || 0) + 1
  }
  return counts
})

const filteredResources = computed(() => {
  if (activeFilter.value === 'all') return resources.value
  return resources.value.filter((org) => org.region === activeFilter.value)
})

function hasSocial(social) {
  if (!social) return false
  return Object.values(social).some((v) => Boolean(v))
}

function shortenUrl(url) {
  try {
    const parsed = new URL(url)
    return parsed.host + (parsed.pathname === '/' ? '' : parsed.pathname)
  } catch (e) {
    return url
  }
}
</script>

<style scoped>
.resources {
  padding: 16px;
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.rs-card {
  border-radius: 8px;
}
.rs-card--header h2 {
  margin: 0 0 8px;
  font-size: 22px;
}
.rs-subtle {
  color: #888;
  font-size: 13px;
  line-height: 1.6;
}
.rs-filters {
  margin-top: 14px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.rs-chip {
  cursor: pointer;
  user-select: none;
}
.rs-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: 14px;
}
.rs-card--org :deep(.el-card__body) {
  display: flex;
  flex-direction: column;
  gap: 10px;
  height: 100%;
}
.rs-org-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 10px;
}
.rs-org-header h3 {
  margin: 0;
  font-size: 17px;
  line-height: 1.4;
}
.rs-name-en {
  color: #666;
  font-size: 13px;
  margin-top: 2px;
}
.rs-intro {
  margin: 0;
  font-size: 14px;
  line-height: 1.7;
  color: #333;
}
.rs-services {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.rs-service-tag {
  margin: 0;
}
.rs-meta {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
}
.rs-meta-item {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #555;
}
.rs-meta-item a {
  color: var(--el-color-primary);
  text-decoration: none;
  word-break: break-all;
}
.rs-meta-item a:hover {
  text-decoration: underline;
}
.rs-social {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 12px;
}
.rs-social-link {
  color: var(--el-color-primary);
  text-decoration: none;
}
.rs-social-link:hover {
  text-decoration: underline;
}
.rs-source {
  margin-top: auto;
  padding-top: 8px;
  border-top: 1px dashed #eee;
  font-size: 12px;
}
.rs-source a {
  color: #999;
  text-decoration: none;
}
.rs-source a:hover {
  color: var(--el-color-primary);
  text-decoration: underline;
}
.rs-empty {
  text-align: center;
  color: #999;
  padding: 40px 0;
}
</style>
