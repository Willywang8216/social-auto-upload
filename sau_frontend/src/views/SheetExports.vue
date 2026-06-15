<template>
  <div class="sheet-exports">
    <el-card>
      <template #header>
        <div class="card-header">
          <h2>Sheet Exports</h2>
        </div>
      </template>

      <el-table :data="exports" stripe>
        <el-table-column prop="sheet_name" label="Sheet Name" min-width="200" />
        <el-table-column prop="row_count" label="Rows" width="80" />
        <el-table-column prop="status" label="Status" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'completed' ? 'success' : row.status === 'failed' ? 'danger' : 'info'">
              {{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="exported_at" label="Exported At" width="180" />
        <el-table-column label="Actions" width="200">
          <template #default="{ row }">
            <el-button
              v-if="row.spreadsheet_url"
              type="primary"
              size="small"
              @click="openSheet(row.spreadsheet_url)"
            >
              Open Sheet
            </el-button>
            <el-button
              v-if="row.error_message"
              type="danger"
              size="small"
              @click="showError(row)"
            >
              View Error
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div v-if="exports.length === 0" class="empty-state">
        <p>No exports yet. Export a campaign to see it here.</p>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessageBox } from 'element-plus'
import { sheetExportApi } from '@/api/sheet-exports'

const exports = ref([])

onMounted(() => {
  loadExports()
})

async function loadExports() {
  try {
    const res = await sheetExportApi.listExports()
    exports.value = res.data || res
  } catch (e) {
    console.error(e)
  }
}

function openSheet(url) {
  window.open(url, '_blank')
}

function showError(row) {
  ElMessageBox.alert(row.error_message, 'Export Error', { type: 'error' })
}
</script>

<style scoped>
.sheet-exports {
  padding: 20px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.empty-state {
  text-align: center;
  padding: 40px;
  color: #909399;
}
</style>
