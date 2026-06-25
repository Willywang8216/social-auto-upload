<template>
  <el-card class="advisor-panel">
    <template #header>
      <div class="advisor-header">
        <div class="advisor-title">
          <el-icon><MagicStick /></el-icon>
          <span>AI 分析建議</span>
        </div>
        <el-button
          type="primary"
          :loading="loading"
          @click="$emit('request-advice')"
        >
          <el-icon v-if="!loading"><MagicStick /></el-icon>
          {{ loading ? '分析中...' : '取得建議' }}
        </el-button>
      </div>
    </template>

    <div v-if="!advice && !loading" class="advisor-empty">
      <el-empty description="點擊「取得建議」讓 AI 分析您的影片數據" :image-size="80" />
    </div>

    <div v-if="loading" class="advisor-loading">
      <el-skeleton :rows="6" animated />
    </div>

    <div v-if="advice && !loading" class="advisor-content">
      <div class="advisor-summary">
        <p>{{ advice.summary }}</p>
      </div>

      <div v-if="advice.insights?.length" class="advisor-section">
        <h4><el-icon><Sunny /></el-icon> 關鍵洞察</h4>
        <ul>
          <li v-for="(insight, i) in advice.insights" :key="i">
            {{ insight }}
          </li>
        </ul>
      </div>

      <div v-if="advice.recommendations?.length" class="advisor-section">
        <h4><el-icon><CircleCheck /></el-icon> 行動建議</h4>
        <ul>
          <li v-for="(rec, i) in advice.recommendations" :key="i">
            {{ rec }}
          </li>
        </ul>
      </div>

      <div v-if="advice.platformTips && Object.keys(advice.platformTips).length" class="advisor-section">
        <h4><el-icon><Platform /></el-icon> 平台技巧</h4>
        <div v-for="(tip, platform) in advice.platformTips" :key="platform" class="platform-tip">
          <el-tag size="small" type="info">{{ platform }}</el-tag>
          <span>{{ tip }}</span>
        </div>
      </div>
    </div>
  </el-card>
</template>

<script setup>
import { MagicStick, Sunny, CircleCheck, Platform } from '@element-plus/icons-vue'

defineProps({
  advice: {
    type: Object,
    default: null,
  },
  loading: {
    type: Boolean,
    default: false,
  },
})

defineEmits(['request-advice'])
</script>

<style scoped lang="scss">
.advisor-panel {
  margin-top: var(--space-6);
}

.advisor-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.advisor-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 16px;
  font-weight: 600;
}

.advisor-empty {
  padding: var(--space-6) 0;
}

.advisor-loading {
  padding: 10px 0;
}

.advisor-content {
  .advisor-summary {
    font-size: 14px;
    line-height: 1.6;
    margin-bottom: var(--space-5);
    padding: 12px;
    background: var(--accent-soft);
    border-radius: var(--r-md);
    border-left: 3px solid var(--accent);
  }

  .advisor-section {
    margin-bottom: var(--space-5);

    h4 {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 14px;
      font-weight: 600;
      margin-bottom: 8px;
      color: var(--text);
    }

    ul {
      list-style: none;
      padding: 0;
      margin: 0;

      li {
        padding: 6px 0 6px 20px;
        position: relative;
        font-size: 13px;
        line-height: 1.5;
        color: var(--text-2);

        &::before {
          content: '';
          position: absolute;
          left: 4px;
          top: 13px;
          width: 6px;
          height: 6px;
          border-radius: 50%;
          background: var(--accent);
        }
      }
    }
  }

  .platform-tip {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    margin-bottom: 8px;
    font-size: 13px;
    line-height: 1.5;
    color: var(--text-2);

    .el-tag {
      flex-shrink: 0;
      margin-top: 2px;
    }
  }
}
</style>
