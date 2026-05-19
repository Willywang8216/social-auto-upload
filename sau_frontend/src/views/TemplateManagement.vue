<template>
  <div class="template-management">
    <el-card class="tm-card" shadow="never">
      <div class="tm-header">
        <h2>範本管理</h2>
        <div>
          <router-link to="/publish-center" class="tm-link">回到 Publish Center</router-link>
          <el-button type="primary" @click="openCreateDialog">新增範本</el-button>
        </div>
      </div>
      <el-table :data="templatesStore.templates" stripe>
        <el-table-column prop="name" label="名稱" min-width="160" />
        <el-table-column prop="description" label="說明" min-width="220" />
        <el-table-column label="包含設定" min-width="240">
          <template #default="{ row }">
            <el-tag
              v-for="item in row.includedSettings || []"
              :key="item"
              size="small"
              class="tm-tag"
              type="info"
            >
              {{ settingLabel(item) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="updatedAt" label="最近更新" width="180" />
        <el-table-column label="操作" width="220">
          <template #default="{ row }">
            <el-button size="small" @click="openEditDialog(row)">編輯</el-button>
            <el-button size="small" type="primary" @click="useTemplate(row)">使用</el-button>
            <el-button size="small" type="danger" @click="confirmDelete(row)">刪除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog
      v-model="dialogVisible"
      :title="editingTemplate ? '編輯範本' : '新增範本'"
      width="560px"
    >
      <el-form label-position="top">
        <el-form-item label="名稱" required>
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item label="說明">
          <el-input v-model="form.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="包含哪些設定（套用時會覆寫表單中的值）">
          <el-checkbox-group v-model="form.includedSettings">
            <el-checkbox v-for="opt in allSettings" :key="opt.value" :label="opt.value">{{ opt.label }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="範本內容（JSON）">
          <el-input
            v-model="form.configRaw"
            type="textarea"
            :rows="8"
            placeholder='{
  "profileIds": [1, 2],
  "watermark": true,
  ...
}'
          />
          <span class="tm-subtle">通常從 Publish Center 的「另存為範本」開始；這裡僅作進階編輯。</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">儲存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import { usePublishTemplatesStore } from '@/stores/publish-templates'

const templatesStore = usePublishTemplatesStore()
const router = useRouter()

const dialogVisible = ref(false)
const editingTemplate = ref(null)
const saving = ref(false)

const allSettings = [
  { value: 'profileIds', label: '所選 Profiles' },
  { value: 'accountIds', label: '所選帳號' },
  { value: 'watermark', label: '浮水印' },
  { value: 'intro', label: '片頭' },
  { value: 'outro', label: '片尾' },
  { value: 'linkInFirstComment', label: '連結放留言' },
  { value: 'screenshots', label: '截圖設定' },
  { value: 'schedule', label: '排程' },
]

function settingLabel(value) {
  return allSettings.find((opt) => opt.value === value)?.label || value
}

const form = reactive({
  name: '',
  description: '',
  includedSettings: allSettings.map((opt) => opt.value),
  configRaw: '{}',
})

onMounted(async () => {
  try {
    await templatesStore.refresh()
  } catch (err) {
    // surfaced by interceptor
  }
})

function openCreateDialog() {
  editingTemplate.value = null
  form.name = ''
  form.description = ''
  form.includedSettings = allSettings.map((opt) => opt.value)
  form.configRaw = '{}'
  dialogVisible.value = true
}

function openEditDialog(template) {
  editingTemplate.value = template
  form.name = template.name
  form.description = template.description || ''
  form.includedSettings = [...(template.includedSettings || [])]
  try {
    form.configRaw = JSON.stringify(template.config || {}, null, 2)
  } catch (err) {
    form.configRaw = '{}'
  }
  dialogVisible.value = true
}

async function save() {
  let parsedConfig = {}
  try {
    parsedConfig = JSON.parse(form.configRaw || '{}')
  } catch (err) {
    ElMessage.error('範本內容不是有效的 JSON')
    return
  }
  if (!form.name.trim()) {
    ElMessage.warning('請輸入名稱')
    return
  }
  saving.value = true
  try {
    const payload = {
      name: form.name.trim(),
      description: form.description.trim(),
      config: parsedConfig,
      includedSettings: form.includedSettings,
    }
    if (editingTemplate.value) {
      await templatesStore.update(editingTemplate.value.id, payload)
      ElMessage.success('已更新')
    } else {
      await templatesStore.create(payload)
      ElMessage.success('已新增')
    }
    dialogVisible.value = false
  } finally {
    saving.value = false
  }
}

async function confirmDelete(template) {
  try {
    await ElMessageBox.confirm(`刪除範本「${template.name}」？`, '請確認', {
      type: 'warning',
    })
  } catch (cancelled) {
    return
  }
  await templatesStore.remove(template.id)
  ElMessage.success('已刪除')
}

function useTemplate(template) {
  router.push({ path: '/publish-center', query: { templateId: template.id } })
}
</script>

<style scoped>
.template-management {
  padding: 16px;
  max-width: 1100px;
  margin: 0 auto;
}
.tm-card {
  border-radius: 8px;
}
.tm-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}
.tm-link {
  margin-right: 12px;
  color: var(--el-color-primary);
  text-decoration: none;
}
.tm-tag {
  margin: 2px 4px 2px 0;
}
.tm-subtle {
  color: #888;
  font-size: 12px;
}
</style>
