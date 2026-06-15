<template>
  <div class="batch-upload">
    <el-card class="upload-card">
      <template #header>
        <div class="card-header">
          <h2>Batch Upload</h2>
          <el-button type="primary" @click="triggerUpload">
            <el-icon><Upload /></el-icon> Upload Files
          </el-button>
        </div>
      </template>

      <el-upload
        ref="uploadRef"
        :action="uploadUrl"
        :auto-upload="false"
        :on-change="handleFileChange"
        :file-list="fileList"
        multiple
        drag
        accept="image/*,video/*"
      >
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">
          Drop files here or <em>click to upload</em>
        </div>
        <template #tip>
          <div class="el-upload__tip">Images and videos supported</div>
        </template>
      </el-upload>

      <div v-if="uploading" class="upload-progress">
        <el-progress :percentage="uploadProgress" :status="uploadStatus" />
      </div>
    </el-card>

    <el-card v-if="assets.length > 0" class="assets-card">
      <template #header>
        <div class="card-header">
          <h2>Media Assets ({{ assets.length }})</h2>
          <div class="actions">
            <el-button @click="selectAll">Select All</el-button>
            <el-button type="success" :disabled="selectedAssets.length === 0" @click="createGroup">
              Create Media Group ({{ selectedAssets.length }})
            </el-button>
            <el-button type="warning" :disabled="selectedAssets.length === 0" @click="processSelected">
              Process Selected
            </el-button>
            <el-button type="danger" :disabled="selectedAssets.length === 0" @click="deleteSelected">
              Delete Selected
            </el-button>
          </div>
        </div>
      </template>

      <div class="assets-grid">
        <div
          v-for="asset in assets"
          :key="asset.id"
          class="asset-card"
          :class="{ selected: selectedAssets.includes(asset.id) }"
          @click="toggleSelect(asset.id)"
        >
          <div class="asset-preview">
            <el-image
              v-if="asset.media_type === 'image'"
              :src="getPreviewUrl(asset)"
              fit="cover"
              class="preview-image"
            />
            <div v-else class="video-placeholder">
              <el-icon :size="48"><VideoCamera /></el-icon>
              <span>{{ asset.duration_seconds ? formatDuration(asset.duration_seconds) : '' }}</span>
            </div>
            <el-checkbox
              :model-value="selectedAssets.includes(asset.id)"
              class="select-checkbox"
              @click.stop
              @change="toggleSelect(asset.id)"
            />
          </div>
          <div class="asset-info">
            <div class="asset-name" :title="asset.original_filename">
              {{ asset.original_filename }}
            </div>
            <div class="asset-meta">
              <el-tag :type="statusTagType(asset.upload_status)" size="small">
                {{ asset.upload_status }}
              </el-tag>
              <el-tag :type="processingTagType(asset.processing_status)" size="small">
                {{ asset.processing_status }}
              </el-tag>
            </div>
            <div class="asset-size">{{ formatSize(asset.file_size) }}</div>
          </div>
        </div>
      </div>
    </el-card>

    <!-- Create Group Dialog -->
    <el-dialog v-model="showGroupDialog" title="Create Media Group" width="500px">
      <el-form :model="groupForm" label-width="120px">
        <el-form-item label="Group Name">
          <el-input v-model="groupForm.name" placeholder="Enter group name" />
        </el-form-item>
        <el-form-item label="Content Theme">
          <el-input v-model="groupForm.content_theme" placeholder="e.g. teaching, lifestyle" />
        </el-form-item>
        <el-form-item label="Notes">
          <el-input v-model="groupForm.notes" type="textarea" :rows="3" placeholder="Notes about this group" />
        </el-form-item>
        <el-form-item label="Profile">
          <el-select v-model="groupForm.profile_id" placeholder="Select profile" clearable>
            <el-option
              v-for="p in profiles"
              :key="p.id"
              :label="p.name"
              :value="p.id"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showGroupDialog = false">Cancel</el-button>
        <el-button type="primary" @click="submitGroup">Create</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Upload, UploadFilled, VideoCamera } from '@element-plus/icons-vue'
import { mediaAssetApi } from '@/api/media-assets'
import { profilesApi } from '@/api/profiles'
import { buildApiUrl } from '@/utils/api-url'

const uploadRef = ref(null)
const fileList = ref([])
const assets = ref([])
const selectedAssets = ref([])
const uploading = ref(false)
const uploadProgress = ref(0)
const uploadStatus = ref('')
const showGroupDialog = ref(false)
const profiles = ref([])

const groupForm = ref({
  name: '',
  content_theme: '',
  notes: '',
  profile_id: null,
})

const uploadUrl = buildApiUrl('/api/media/upload/batch')

onMounted(() => {
  loadAssets()
  loadProfiles()
})

async function loadAssets() {
  try {
    const res = await mediaAssetApi.listAssets()
    assets.value = res.data || res
  } catch (e) {
    console.error('Failed to load assets:', e)
  }
}

async function loadProfiles() {
  try {
    const res = await profilesApi.list()
    profiles.value = res.data || res
  } catch (e) {
    console.error('Failed to load profiles:', e)
  }
}

function handleFileChange(file, fileList) {
  // Store files for batch upload
}

async function triggerUpload() {
  const files = uploadRef.value?.uploadFiles
  if (!files || files.length === 0) {
    ElMessage.warning('Please select files first')
    return
  }

  uploading.value = true
  uploadProgress.value = 0
  uploadStatus.value = ''

  try {
    const formData = new FormData()
    files.forEach(f => {
      formData.append('files', f.raw)
    })

    await mediaAssetApi.uploadBatch(formData, (e) => {
      if (e.total) uploadProgress.value = Math.round((e.loaded / e.total) * 100)
    })

    uploadStatus.value = 'success'
    ElMessage.success(`Uploaded ${files.length} files`)
    uploadRef.value?.clearFiles()
    loadAssets()
  } catch (e) {
    uploadStatus.value = 'exception'
    ElMessage.error('Upload failed: ' + (e.message || e))
  } finally {
    uploading.value = false
  }
}

function toggleSelect(id) {
  const idx = selectedAssets.value.indexOf(id)
  if (idx >= 0) {
    selectedAssets.value.splice(idx, 1)
  } else {
    selectedAssets.value.push(id)
  }
}

function selectAll() {
  if (selectedAssets.value.length === assets.value.length) {
    selectedAssets.value = []
  } else {
    selectedAssets.value = assets.value.map(a => a.id)
  }
}

function createGroup() {
  groupForm.value.name = `Group ${new Date().toISOString().split('T')[0]}`
  showGroupDialog.value = true
}

async function submitGroup() {
  try {
    // Create media group via existing API
    const { http } = await import('@/utils/request')
    await http.post('/media-groups', {
      name: groupForm.value.name,
      notes: groupForm.value.notes,
      profile_id: groupForm.value.profile_id,
      content_theme: groupForm.value.content_theme,
      group_type: 'mixed',
      status: 'draft',
    })
    ElMessage.success('Media group created')
    showGroupDialog.value = false
    selectedAssets.value = []
  } catch (e) {
    ElMessage.error('Failed to create group: ' + (e.message || e))
  }
}

async function processSelected() {
  ElMessage.info(`Processing ${selectedAssets.value.length} assets...`)
  let success = 0
  for (const id of selectedAssets.value) {
    try {
      await mediaAssetApi.processAsset(id)
      success++
    } catch (e) {
      console.error(`Failed to process ${id}:`, e)
    }
  }
  ElMessage.success(`Processed ${success}/${selectedAssets.value.length} assets`)
  loadAssets()
}

async function deleteSelected() {
  try {
    await ElMessageBox.confirm(
      `Delete ${selectedAssets.value.length} selected assets?`,
      'Confirm',
      { type: 'warning' }
    )
    for (const id of selectedAssets.value) {
      await mediaAssetApi.deleteAsset(id)
    }
    ElMessage.success('Deleted')
    selectedAssets.value = []
    loadAssets()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error('Delete failed')
  }
}

function getPreviewUrl(asset) {
  if (asset.local_processed_path) return buildApiUrl(`/getFile?filename=${asset.original_filename}`)
  return buildApiUrl(`/getFile?filename=${asset.original_filename}`)
}

function formatDuration(sec) {
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

function formatSize(bytes) {
  if (!bytes) return ''
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / 1048576).toFixed(1) + ' MB'
}

function statusTagType(status) {
  if (status === 'uploaded') return 'success'
  if (status === 'failed') return 'danger'
  return 'info'
}

function processingTagType(status) {
  if (status === 'processed') return 'success'
  if (status === 'processing') return 'warning'
  if (status === 'failed') return 'danger'
  return 'info'
}
</script>

<style scoped>
.batch-upload {
  padding: 20px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.upload-card {
  margin-bottom: 20px;
}
.upload-progress {
  margin-top: 16px;
}
.assets-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 16px;
}
.asset-card {
  border: 2px solid #e4e7ed;
  border-radius: 8px;
  overflow: hidden;
  cursor: pointer;
  transition: border-color 0.2s;
}
.asset-card.selected {
  border-color: #409eff;
}
.asset-preview {
  position: relative;
  height: 150px;
  background: #f5f7fa;
  display: flex;
  align-items: center;
  justify-content: center;
}
.preview-image {
  width: 100%;
  height: 100%;
}
.video-placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  color: #909399;
}
.select-checkbox {
  position: absolute;
  top: 8px;
  right: 8px;
}
.asset-info {
  padding: 8px 12px;
}
.asset-name {
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-bottom: 4px;
}
.asset-meta {
  display: flex;
  gap: 4px;
  margin-bottom: 4px;
}
.asset-size {
  font-size: 11px;
  color: #909399;
}
.actions {
  display: flex;
  gap: 8px;
}
</style>
