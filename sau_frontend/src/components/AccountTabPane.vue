<template>
  <div class="account-list-container">
    <div class="account-search">
      <el-input
        :model-value="searchKeyword"
        placeholder="输入名称或账号搜索"
        prefix-icon="Search"
        clearable
        @clear="$emit('search', '')"
        @update:model-value="$emit('search', $event)"
      />
      <div class="action-buttons">
        <el-button type="primary" @click="$emit('add')">添加账号</el-button>
        <el-button type="info" :loading="false" @click="$emit('refresh')">
          <el-icon :class="{ 'is-loading': refreshing }"><Refresh /></el-icon>
          <span v-if="refreshing">刷新中</span>
        </el-button>
      </div>
    </div>

    <div v-if="accounts.length > 0" class="account-list">
      <el-table :data="accounts" style="width: 100%">
        <el-table-column label="头像" width="80">
          <template #default="scope">
            <el-avatar :src="getDefaultAvatar(scope.row.name)" :size="40" />
          </template>
        </el-table-column>
        <el-table-column prop="name" label="名称" width="180" />
        <el-table-column prop="platform" label="平台">
          <template #default="scope">
            <el-tag :type="platformTagType(scope.row.platform)" effect="plain">
              {{ scope.row.platform }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态">
          <template #default="scope">
            <el-tag
              :type="statusTagType(scope.row.status)"
              effect="plain"
              :class="{ 'clickable-status': isStatusClickable(scope.row.status) }"
              @click="onStatusClick(scope.row)"
            >
              <el-icon
                v-if="scope.row.status === '验证中'"
                class="is-loading"
              >
                <Loading />
              </el-icon>
              {{ scope.row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作">
          <template #default="scope">
            <el-button size="small" @click="$emit('edit', scope.row)">编辑</el-button>
            <el-button
              size="small"
              type="primary"
              :icon="Download"
              @click="$emit('download-cookie', scope.row)"
            >下载Cookie</el-button>
            <el-button
              size="small"
              type="info"
              :icon="Upload"
              @click="$emit('upload-cookie', scope.row)"
            >上传Cookie</el-button>
            <el-button
              size="small"
              type="danger"
              @click="$emit('delete', scope.row)"
            >删除</el-button>
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

defineProps({
  accounts: { type: Array, required: true },
  searchKeyword: { type: String, default: '' },
  refreshing: { type: Boolean, default: false },
  emptyText: { type: String, default: '暂无账号数据' }
})

const emit = defineEmits([
  'add',
  'edit',
  'delete',
  'download-cookie',
  'upload-cookie',
  'refresh',
  'relogin',
  'search'
])

const PLATFORM_TAG = {
  快手: 'success',
  抖音: 'danger',
  视频号: 'warning',
  小红书: 'info'
}

function platformTagType(platform) {
  return PLATFORM_TAG[platform] || 'info'
}

function statusTagType(status) {
  if (status === '验证中') return 'info'
  if (status === '正常') return 'success'
  return 'danger'
}

function isStatusClickable(status) {
  return status === '异常'
}

function onStatusClick(row) {
  if (isStatusClickable(row.status)) {
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

    .el-input {
      width: 300px;
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
