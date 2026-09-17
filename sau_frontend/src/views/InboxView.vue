<template>
  <div class="inbox-view">
    <div class="page-header">
      <h1>傳入佇列</h1>
      <div class="page-actions">
        <el-button type="primary" @click="load" :loading="loading">
          <el-icon><Refresh /></el-icon>
          重新整理
        </el-button>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="inbox-tabs">
      <el-tab-pane name="ready">
        <template #label>
          <span class="tab-label">待發佈 <span class="tab-count">{{ ready.length }}</span></span>
        </template>
        <el-empty v-if="!loading && ready.length === 0" description="目前沒有待發佈的項目" />
        <div v-else class="inbox-grid">
          <InboxCard
            v-for="item in ready"
            :key="item.id"
            :item="item"
            mode="ready"
            @approve="approveItem"
            @reject="rejectItem"
          />
        </div>
      </el-tab-pane>

      <el-tab-pane name="pending">
        <template #label>
          <span class="tab-label">待確認 <span class="tab-count">{{ pending.length }}</span></span>
        </template>
        <el-empty v-if="!loading && pending.length === 0" description="目前沒有待確認的項目" />
        <div v-else class="inbox-grid">
          <InboxCard v-for="item in pending" :key="item.id" :item="item" mode="readonly" />
        </div>
      </el-tab-pane>

      <el-tab-pane name="quarantined">
        <template #label>
          <span class="tab-label">隔離區 <span class="tab-count">{{ quarantined.length }}</span></span>
        </template>
        <el-empty v-if="!loading && quarantined.length === 0" description="目前沒有被拒絕的項目" />
        <div v-else class="inbox-grid">
          <InboxCard v-for="item in quarantined" :key="item.id" :item="item" mode="readonly" />
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'

import { inboxApi } from '@/api/inbox'
import InboxCard from '@/components/inbox/InboxCard.vue'

const router = useRouter()

const loading = ref(false)
const activeTab = ref('ready')
const ready = ref([])
const pending = ref([])
const quarantined = ref([])

async function load() {
  loading.value = true
  try {
    const res = await inboxApi.list()
    const data = res?.data || {}
    ready.value = data.ready || []
    pending.value = data.pending || []
    quarantined.value = data.quarantined || []
  } catch (error) {
    // http wrapper already surfaces the message; keep console context.
    console.error('載入傳入佇列失敗:', error)
  } finally {
    loading.value = false
  }
}

async function approveItem(item) {
  try {
    await ElMessageBox.confirm(
      `確定發佈「${item.topic || item.id}」嗎?`,
      '發佈確認',
      { type: 'warning', confirmButtonText: '發佈', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    await inboxApi.approve(item.id)
    ElMessage.success('已核准,進入排程')
    await load()
    router.push({ path: '/publish/compose' })
  } catch (error) {
    ElMessage.error(error?.message || '核准失敗')
  }
}

async function rejectItem(item) {
  let reason = ''
  try {
    const { value } = await ElMessageBox.prompt(
      '請輸入拒絕原因(可留空)',
      '拒絕項目',
      {
        confirmButtonText: '拒絕',
        cancelButtonText: '取消',
        inputPlaceholder: '例如:素材畫質不足、內容不合規…',
        inputValidator: (val) => (val == null ? false : true)
      }
    )
    reason = value || ''
  } catch {
    return
  }
  try {
    await inboxApi.reject(item.id, { reason })
    ElMessage.success('已拒絕並移入隔離區')
    await load()
  } catch (error) {
    ElMessage.error(error?.message || '拒絕失敗')
  }
}

onMounted(load)
</script>

<style lang="scss" scoped>
.inbox-view {
  .page-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: var(--space-6);

    h1 {
      font-size: 24px;
      color: var(--text);
      margin: 0;
    }

    .page-actions {
      display: flex;
      gap: var(--space-3);
    }
  }

  .inbox-tabs {
    :deep(.el-tabs__header) {
      margin-bottom: var(--space-6);
    }

    .tab-label {
      font-size: 14px;
      font-weight: 600;
      color: var(--text);
    }

    .tab-count {
      display: inline-block;
      min-width: 20px;
      padding: 0 6px;
      margin-left: 6px;
      text-align: center;
      font-size: 12px;
      font-weight: 600;
      line-height: 20px;
      border-radius: var(--r-full);
      background: var(--accent-soft);
      color: var(--accent);
    }
  }

  .inbox-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    gap: var(--space-4);
  }
}
</style>