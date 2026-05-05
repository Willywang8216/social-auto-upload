<template>
  <div class="account-list-container">
    <div class="account-search">
      <el-input
        :model-value="searchKeyword"
        placeholder="輸入名稱、Profile 或平台搜尋"
        prefix-icon="Search"
        clearable
        @clear="$emit('search', '')"
        @update:model-value="$emit('search', $event)"
      />
      <div class="action-buttons">
        <el-button type="primary" @click="$emit('add')">新增帳號</el-button>
        <el-button type="info" :loading="false" @click="$emit('refresh')">
          <el-icon :class="{ 'is-loading': refreshing }"><Refresh /></el-icon>
          <span v-if="refreshing">重新整理中</span>
          <span v-else>重新整理</span>
        </el-button>
        <el-button
          plain
          :disabled="bulkCheckCount < 1"
          :loading="bulkCheckLoading"
          @click="$emit('bulk-check')"
        >檢查全部</el-button>
        <el-button
          plain
          :disabled="bulkRefreshCount < 1"
          :loading="bulkRefreshLoading"
          @click="$emit('bulk-refresh')"
        >刷新全部</el-button>
      </div>
    </div>

    <div v-if="accounts.length > 0" class="account-list">
      <el-table :data="accounts" style="width: 100%">
        <el-table-column label="頭像" width="80">
          <template #default="scope">
            <el-avatar :src="getDefaultAvatar(scope.row.name)" :size="40" />
          </template>
        </el-table-column>
        <el-table-column prop="name" label="名稱" min-width="180" />
        <el-table-column prop="profileName" label="Profile" min-width="120">
          <template #default="scope">
            <el-tag :type="scope.row.profileId ? 'primary' : 'info'" effect="plain">
              {{ scope.row.profileName || 'Legacy' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="platform" label="平台" min-width="120">
          <template #default="scope">
            <el-tag :type="platformTagType(scope.row.platform)" effect="plain">
              {{ scope.row.platform }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="authType" label="登入方式" min-width="100">
          <template #default="scope">
            <span>{{ scope.row.authType || 'cookie' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="狀態" min-width="100">
          <template #default="scope">
            <el-tag
              :type="statusTagType(scope.row.status)"
              effect="plain"
              :class="{ 'clickable-status': isStatusClickable(scope.row) }"
              @click="onStatusClick(scope.row)"
            >
              <el-icon v-if="scope.row.status === '驗證中'" class="is-loading">
                <Loading />
              </el-icon>
              {{ scope.row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="連線健康" min-width="180">
          <template #default="scope">
            <div class="connection-cell">
              <el-tag :type="scope.row.connectionTagType || 'info'" effect="plain">
                {{ scope.row.connectionLabel || '—' }}
              </el-tag>
              <div v-if="scope.row.connectionDetail" class="connection-detail">{{ scope.row.connectionDetail }}</div>
              <div v-if="scope.row.connectionTimestamp" class="connection-timestamp">{{ scope.row.connectionTimestamp }}</div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" min-width="360">
          <template #default="scope">
            <div class="row-actions">
              <el-button size="small" @click="$emit('edit', scope.row)">編輯</el-button>
              <el-button
                v-if="scope.row.supportsCookieActions"
                size="small"
                type="primary"
                :icon="Download"
                @click="$emit('download-cookie', scope.row)"
              >下載 Cookie</el-button>
              <el-button
                v-if="scope.row.supportsCookieActions"
                size="small"
                type="info"
                :icon="Upload"
                @click="$emit('upload-cookie', scope.row)"
              >上傳 Cookie</el-button>
              <el-button
                v-if="scope.row.supportsHealthAction"
                size="small"
                @click="$emit('health-check', scope.row)"
              >{{ scope.row.healthActionKind === 'refresh' ? '刷新' : '檢查' }}</el-button>
              <el-button
                v-if="scope.row.supportsRelogin"
                size="small"
                @click="$emit('relogin', scope.row)"
              >重新登入</el-button>
              <el-button
                size="small"
                type="danger"
                @click="$emit('delete', scope.row)"
              >刪除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div v-else class="empty-data">
      <el-empty :description="emptyText" />
    </div>
  </div>
</template>

<script setup>
import { Refresh, Download, Upload, Loading } from '@element-plus/icons-vue'
import { getPlatformTagType } from '@/utils/platforms'

defineProps({
  accounts: { type: Array, required: true },
  searchKeyword: { type: String, default: '' },
  refreshing: { type: Boolean, default: false },
  bulkCheckLoading: { type: Boolean, default: false },
  bulkRefreshLoading: { type: Boolean, default: false },
  bulkCheckCount: { type: Number, default: 0 },
  bulkRefreshCount: { type: Number, default: 0 },
  emptyText: { type: String, default: '目前沒有帳號資料' }
})

const emit = defineEmits([
  'add',
  'edit',
  'delete',
  'download-cookie',
  'upload-cookie',
  'refresh',
  'relogin',
  'health-check',
  'bulk-check',
  'bulk-refresh',
  'search'
])

function platformTagType(platform) {
  return getPlatformTagType(platform)
}

function statusTagType(status) {
  if (status === '驗證中') return 'info'
  if (status === '正常') return 'success'
  return 'danger'
}

function isStatusClickable(row) {
  return row.status === '異常' && row.supportsRelogin
}

function onStatusClick(row) {
  if (isStatusClickable(row)) {
    emit('relogin', row)
  }
}

function getDefaultAvatar(name) {
  return `https://ui-avatars.com/api/?name=${encodeURIComponent(name)}&background=random`
}
</script>

<style lang="scss" scoped>
.account-list-container {
  .account-search {
    display: flex;
    justify-content: space-between;
    margin-bottom: 20px;
    gap: 12px;

    .el-input {
      width: 320px;
    }

    .action-buttons {
      display: flex;
      gap: 10px;

      .el-icon.is-loading {
        animation: rotate 1s linear infinite;
      }
    }
  }

  .account-list {
    margin-bottom: 20px;

    .connection-cell {
      display: flex;
      flex-direction: column;
      gap: 4px;

      .connection-detail,
      .connection-timestamp {
        font-size: 12px;
        color: #909399;
        line-height: 1.4;
        word-break: break-word;
      }
    }

    .row-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
  }

  .empty-data {
    padding: 40px 0;
  }

  .clickable-status {
    cursor: pointer;
    transition: all 0.3s;

    &:hover {
      transform: scale(1.05);
      box-shadow: 0 0 8px rgba(0, 0, 0, 0.15);
    }
  }
}

@keyframes rotate {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
</style>
