<template>
  <el-card class="inbox-card" shadow="never" :class="`kind-${item.kind || 'unknown'}`">
    <div class="card-head">
      <div class="thumb-wrap">
        <img
          v-if="thumbSrc"
          :src="thumbSrc"
          class="thumb"
          loading="lazy"
          alt=""
        />
        <div v-else class="thumb thumb-placeholder">
          <el-icon :size="22"><VideoCamera v-if="kindIsVideo" /><Picture v-else /></el-icon>
        </div>
      </div>
      <div class="head-tags">
        <el-tag size="small" effect="plain" class="persona-tag">{{ personaLabel }}</el-tag>
        <el-tag size="small" :type="kindIsVideo ? 'success' : 'primary'" effect="plain">
          {{ kindIsVideo ? 'video' : 'img' }}
        </el-tag>
      </div>
    </div>

    <div class="card-body">
      <div class="meta-line">
        <span class="topic" :title="item.topic">{{ item.topic || '—' }}</span>
        <el-tag
          size="small"
          :type="isSfw ? 'success' : 'danger'"
          effect="dark"
          class="sfw-tag"
        >
          {{ isSfw ? 'sfw' : 'nsfw' }}
        </el-tag>
      </div>

      <p v-if="item.brief" class="brief" :class="{ expanded: briefExpanded }">
        {{ item.brief }}
        <el-button
          v-if="item.brief && item.brief.length > BRIEF_TRUNCATE"
          link
          type="primary"
          @click="briefExpanded = !briefExpanded"
        >
          {{ briefExpanded ? '收合' : '展開' }}
        </el-button>
      </p>
      <p v-else class="brief muted">(無簡介)</p>

      <p v-if="item.contentNote" class="content-note">{{ item.contentNote }}</p>

      <div class="footer-line">
        <span class="time">{{ formatTime(item.createdAt) }}</span>
        <div v-if="item.reason" class="reason" :title="item.reason">
          <span class="reason-label">原因</span>
          <span class="reason-text">{{ item.reason }}</span>
        </div>
      </div>
    </div>

    <div v-if="mode === 'ready'" class="card-actions">
      <el-button size="small" type="primary" @click="$emit('approve', item)">發佈</el-button>
      <el-button size="small" type="danger" plain @click="$emit('reject', item)">拒絕</el-button>
    </div>
  </el-card>
</template>

<script setup>
import { computed, ref } from 'vue'
import { Picture, VideoCamera } from '@element-plus/icons-vue'

import { buildApiUrl } from '@/utils/api-url'

const props = defineProps({
  item: { type: Object, required: true },
  // 'ready' shows approve/reject actions; anything else is read-only.
  mode: { type: String, default: 'readonly' }
})

defineEmits(['approve', 'reject'])

const BRIEF_TRUNCATE = 160

const PERSONA_MAP = { nw: 'NW', sw: 'SW', teaching: 'Teaching', msl: 'MSL' }

const briefExpanded = ref(false)

const personaLabel = computed(() => PERSONA_MAP[props.item.persona] || props.item.persona || '—')
const kindIsVideo = computed(() => props.item.kind === 'video')
const isSfw = computed(() => props.item.sfwFlag === 'sfw' || props.item.sfwFlag === true)

const thumbSrc = computed(() => {
  const path = props.item.thumbPath
  if (!path) return ''
  // Public http(s) URLs (e.g. CDN / signed) pass through untouched.
  if (/^https?:\/\//.test(path)) return path
  return buildApiUrl(`/getFile?filename=${encodeURIComponent(path)}`)
})

function formatTime(iso) {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return String(iso)
  const pad = (n) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}
</script>

<style lang="scss" scoped>
.inbox-card {
  --card-bg: var(--panel);
  background: var(--card-bg);
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  transition: border-color var(--transition-fast), transform var(--transition-fast);
  overflow: hidden;

  &:hover {
    border-color: var(--line-2);
    transform: translateY(-1px);
  }

  :deep(.el-card__body) {
    padding: var(--space-4);
  }
}

.card-head {
  display: flex;
  gap: var(--space-3);
  align-items: center;
  margin-bottom: var(--space-3);

  .thumb-wrap {
    flex-shrink: 0;
  }

  .thumb {
    width: 72px;
    height: 48px;
    object-fit: cover;
    border-radius: var(--r-md);
    background: var(--raised);
    display: block;
  }

  .thumb-placeholder {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-3);
  }

  .head-tags {
    display: flex;
    gap: var(--space-2);
    flex-wrap: wrap;

    .persona-tag {
      color: var(--accent);
      border-color: var(--accent-soft);
      background: var(--accent-soft);
    }
  }
}

.card-body {
  .meta-line {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    margin-bottom: var(--space-2);

    .topic {
      font-size: 14px;
      font-weight: 600;
      color: var(--text);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .sfw-tag {
      flex-shrink: 0;
    }
  }

  .brief {
    font-size: 13px;
    line-height: 1.5;
    color: var(--text-2);
    margin: 0 0 var(--space-2);
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;

    &.expanded {
      display: block;
      -webkit-line-clamp: unset;
      overflow: visible;
    }

    &.muted {
      color: var(--text-3);
    }

    .el-button {
      margin-left: 6px;
      padding: 0;
      height: auto;
      vertical-align: baseline;
    }
  }

  .content-note {
    font-size: 12px;
    color: var(--text-3);
    margin: 0 0 var(--space-2);
  }

  .footer-line {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    font-size: 12px;
    color: var(--text-3);

    .reason {
      display: flex;
      align-items: center;
      gap: 6px;
      min-width: 0;

      .reason-label {
        flex-shrink: 0;
        color: var(--color-danger, #ef4444);
      }

      .reason-text {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
    }
  }
}

.card-actions {
  display: flex;
  gap: var(--space-2);
  padding-top: var(--space-3);
  border-top: 1px solid var(--line);
}
</style>