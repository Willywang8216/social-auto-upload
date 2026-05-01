<template>
  <div class="material-management">
    <div class="page-header">
      <h1>素材庫</h1>
    </div>
    
    <div class="material-list-container">
      <div class="material-search">
        <el-input
          v-model="searchKeyword"
          placeholder="輸入檔案名稱搜尋"
          prefix-icon="Search"
          clearable
          @clear="handleSearch"
          @input="handleSearch"
        />
        <div class="action-buttons">
          <el-button type="primary" @click="handleUploadMaterial">上傳素材</el-button>
          <el-button type="info" @click="fetchMaterials" :loading="false">
            <el-icon :class="{ 'is-loading': isRefreshing }"><Refresh /></el-icon>
            <span v-if="isRefreshing">重新整理中</span>
            <span v-else>重新整理</span>
          </el-button>
        </div>
      </div>
      
      <div v-if="filteredMaterials.length > 0" class="material-list">
        <el-table :data="filteredMaterials" style="width: 100%">
          <el-table-column prop="uuid" label="UUID" width="180" />
          <el-table-column prop="filename" label="檔案名稱" width="300" />
          <el-table-column prop="filesize" label="檔案大小" width="120">
            <template #default="scope">
              {{ scope.row.filesize }} MB
            </template>
          </el-table-column>
          <el-table-column prop="upload_time" label="上傳時間" width="180" />
          <el-table-column label="操作">
            <template #default="scope">
              <el-button size="small" @click="handlePreview(scope.row)">預覽</el-button>
              <el-button size="small" type="danger" @click="handleDelete(scope.row)">刪除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
      
      <div v-else class="empty-data">
        <el-empty description="目前沒有素材資料" />
      </div>
    </div>
    
    <!-- 上传对话框 -->
    <el-dialog
      v-model="uploadDialogVisible"
      title="上傳素材"
      width="40%"
      @close="handleUploadDialogClose"
    >
      <div class="upload-form">
        <el-form label-width="80px">
          <el-form-item label="檔案名稱：">
            <el-input
              v-model="customFilename"
              placeholder="選填（僅單一檔案時生效）"
              :disabled="customFilenameDisabled"
              clearable
            />
          </el-form-item>
          <el-form-item label="選擇檔案">
            <el-upload
              class="upload-demo"
              drag
              multiple
              :auto-upload="false"
              :on-change="handleFileChange"
              :on-remove="handleFileRemove"
              :file-list="fileList"
            >
              <el-icon class="el-icon--upload"><Upload /></el-icon>
              <div class="el-upload__text">
                將檔案拖曳到此處，或<em>點擊上傳</em>
              </div>
              <template #tip>
                <div class="el-upload__tip">
                  支援影片、圖片等格式檔案，可一次選擇多個檔案
                </div>
              </template>
            </el-upload>
          </el-form-item>
          <el-form-item label="上傳清單" v-if="fileList.length > 0">
            <div class="upload-file-list">
              <div v-for="file in fileList" :key="file.uid" class="upload-file-item">
                <span class="file-name">{{ file.name }}</span>
                <el-progress
                  :percentage="uploadProgress[file.uid]?.percentage || 0"
                  :text-inside="true"
                  :stroke-width="20"
                  style="width: 100%; margin-top: 5px;"
                >
                  <span>{{ uploadProgress[file.uid]?.speed || '' }}</span>
                </el-progress>
              </div>
            </div>
          </el-form-item>
        </el-form>
      </div>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="uploadDialogVisible = false">取消</el-button>
          <el-button type="primary" @click="submitUpload" :loading="isUploading">
            {{ isUploading ? '上傳中' : '確認上傳' }}
          </el-button>
        </div>
      </template>
    </el-dialog>
    
    <!-- 預覽对话框 -->
    <el-dialog
      v-model="previewDialogVisible"
      title="素材預覽"
      width="50%"
      :top="'10vh'"
    >
      <div class="preview-container" v-if="currentMaterial">
        <div v-if="isVideoFile(currentMaterial.filename)" class="video-preview">
          <video controls style="max-width: 100%; max-height: 60vh;">
            <source :src="getPreviewUrl(currentMaterial.file_path)" type="video/mp4">
            您的瀏覽器不支援影片播放
          </video>
        </div>
        <div v-else-if="isImageFile(currentMaterial.filename)" class="image-preview">
          <img :src="getPreviewUrl(currentMaterial.file_path)" style="max-width: 100%; max-height: 60vh;" />
        </div>
        <div v-else class="file-info">
          <p>檔案名稱：{{ currentMaterial.filename }}</p>
          <p>檔案大小：{{ currentMaterial.filesize }} MB</p>
          <p>上傳時間：{{ currentMaterial.upload_time }}</p>
          <el-button type="primary" @click="downloadFile(currentMaterial)">下載檔案</el-button>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { Refresh, Upload } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { materialApi } from '@/api/material'
import { useAppStore } from '@/stores/app'

// 获取应用状态管理
const appStore = useAppStore()

// 搜索和状态控制
const searchKeyword = ref('')
const isRefreshing = ref(false)
const isUploading = ref(false)

// 对话框控制
const uploadDialogVisible = ref(false)
const previewDialogVisible = ref(false)
const currentMaterial = ref(null)

// 文件上传
const fileList = ref([])
const customFilename = ref('')
const customFilenameDisabled = computed(() => fileList.value.length > 1)
const uploadProgress = ref({}); // { [uid]: { percentage: 0, speed: '' } }


watch(fileList, (newList) => {
  if (newList.length <= 1) {
    // If you want to clear the custom name when going back to single file, uncomment below
    // customFilename.value = ''
  }
});


// 获取素材列表
const fetchMaterials = async () => {
  isRefreshing.value = true
  try {
    const response = await materialApi.getAllMaterials()
    
    if (response.code === 200) {
      appStore.setMaterials(response.data)
      ElMessage.success('重新整理成功')
    } else {
      ElMessage.error('取得素材清單失敗')
    }
  } catch (error) {
    console.error('取得素材清單失敗:', error)
    ElMessage.error('取得素材清單失敗')
  } finally {
    isRefreshing.value = false
  }
}

// 过滤素材
const filteredMaterials = computed(() => {
  if (!searchKeyword.value) return appStore.materials
  
  const keyword = searchKeyword.value.toLowerCase()
  return appStore.materials.filter(material => 
    material.filename.toLowerCase().includes(keyword)
  )
})

// 搜索处理
const handleSearch = () => {
  // 搜索逻辑已通过计算属性实现
}

// 上傳素材
const handleUploadMaterial = () => {
  // 清空变量
  fileList.value = []
  customFilename.value = ''
  uploadProgress.value = {};
  uploadDialogVisible.value = true
}

// 关闭上传对话框时清空变量
const handleUploadDialogClose = () => {
  fileList.value = []
  customFilename.value = ''
  uploadProgress.value = {};
}

// 文件选择变更
const handleFileChange = (file, uploadFileList) => {
  fileList.value = uploadFileList;
  const newProgress = {};
  for (const f of uploadFileList) {
    newProgress[f.uid] = { percentage: 0, speed: '' };
  }
  uploadProgress.value = newProgress;
}

const handleFileRemove = (file, uploadFileList) => {
  fileList.value = uploadFileList;
  const newProgress = { ...uploadProgress.value };
  delete newProgress[file.uid];
  uploadProgress.value = newProgress;
}

// 提交上传
const submitUpload = async () => {
  if (fileList.value.length === 0) {
    ElMessage.warning('請選擇要上傳的檔案')
    return
  }
  
  isUploading.value = true
  
  for (const file of fileList.value) {
    try {
      // 确保文件对象存在
      if (!file || !file.raw) {
        ElMessage.warning(`檔案 ${file.name} 物件無效，已略過`)
        continue
      }
      
      const formData = new FormData()
      formData.append('file', file.raw)
      
      // 只有当只有一个文件时，自定义檔案名稱才生效
      if (fileList.value.length === 1 && customFilename.value.trim()) {
        formData.append('filename', customFilename.value.trim())
      }
      
      let lastLoaded = 0;
      let lastTime = Date.now();

      const response = await materialApi.uploadMaterial(formData, (progressEvent) => {
        const progressData = uploadProgress.value[file.uid];
        if (!progressData) return;

        const progress = Math.round((progressEvent.loaded * 100) / progressEvent.total)
        progressData.percentage = progress;

        const currentTime = Date.now();
        const timeDiff = (currentTime - lastTime) / 1000; // in seconds
        const loadedDiff = progressEvent.loaded - lastLoaded;

        if (timeDiff > 0.5) { // Update speed every 0.5 seconds
          const speed = loadedDiff / timeDiff; // bytes per second
          if (speed > 1024 * 1024) {
            progressData.speed = (speed / (1024 * 1024)).toFixed(2) + ' MB/s';
          } else {
            progressData.speed = (speed / 1024).toFixed(2) + ' KB/s';
          }
          lastLoaded = progressEvent.loaded;
          lastTime = currentTime;
        }
      })
      
      if (response.code === 200) {
        ElMessage.success(`檔案 ${file.name} 上傳成功`)
        const progressData = uploadProgress.value[file.uid];
        if(progressData) progressData.speed = '完成';
      } else {
        ElMessage.error(`檔案 ${file.name} 上傳失敗： ${response.msg || '未知錯誤'}`)
      }
    } catch (error) {
      console.error(`上傳檔案 ${file.name} 失敗:`, error)
      ElMessage.error(`檔案 ${file.name} 上傳失敗： ${error.message || '未知錯誤'}`)
    }
  }
  
  isUploading.value = false
  // Keep dialog open to show results
  // uploadDialogVisible.value = false 
  await fetchMaterials()
}

// 預覽素材
const handlePreview = async (material) => {
  currentMaterial.value = null
  previewDialogVisible.value = true
  ElMessage.info('載入中...')
  try {
    // 等待一小段时间以确保对话框已打开
    await new Promise(resolve => setTimeout(resolve, 100))
    currentMaterial.value = material
  } catch (error) {
    console.error('預覽素材失敗:', error)
    ElMessage.error('預覽載入失敗')
    previewDialogVisible.value = false
  }
}

// 刪除素材
const handleDelete = (material) => {
  ElMessageBox.confirm(
    `確定要刪除素材 ${material.filename} 嗎？`,
    '警告',
    {
      confirmButtonText: '確定',
      cancelButtonText: '取消',
      type: 'warning',
    }
  )
    .then(async () => {
      try {
        const response = await materialApi.deleteMaterial(material.id)
        
        if (response.code === 200) {
          appStore.removeMaterial(material.id)
          ElMessage.success('刪除成功')
        } else {
          ElMessage.error(response.msg || '刪除失敗')
        }
      } catch (error) {
        console.error('刪除素材失敗:', error)
        ElMessage.error('刪除失敗')
      }
    })
    .catch(() => {
      // 取消刪除
    })
}

// 获取預覽URL
const getPreviewUrl = (filePath) => {
  const filename = filePath.split('/').pop()
  return materialApi.getMaterialPreviewUrl(filename)
}

// 下載檔案
const downloadFile = (material) => {
  const url = materialApi.downloadMaterial(material.file_path)
  window.open(url, '_blank')
}

// 判断文件类型
const isVideoFile = (filename) => {
  const videoExtensions = ['.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv']
  return videoExtensions.some(ext => filename.toLowerCase().endsWith(ext))
}

const isImageFile = (filename) => {
  const imageExtensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']
  return imageExtensions.some(ext => filename.toLowerCase().endsWith(ext))
}

// 组件挂载时获取素材列表
onMounted(() => {
  // 只有store中没有数据时才获取
  if (appStore.materials.length === 0) {
    fetchMaterials()
  }
})
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

@keyframes rotate {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

.material-management {
  
  .page-header {
    margin-bottom: 20px;
    
    h1 {
      font-size: 24px;
      font-weight: 500;
      color: $text-primary;
      margin: 0;
    }
  }
  
  .material-list-container {
    background-color: #fff;
    border-radius: 4px;
    box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.1);
    padding: 20px;
    
    .material-search {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 20px;
      
      .el-input {
        width: 300px;
      }
      
      .action-buttons {
        display: flex;
        gap: 10px;
        
        .is-loading {
          animation: rotate 1s linear infinite;
        }
      }
    }
    
    .material-list {
      margin-top: 20px;
    }
    
    .empty-data {
      padding: 40px 0;
    }
  }
  
  .material-upload {
    width: 100%;
  }
  
  .preview-container {
    display: flex;
    justify-content: center;
    align-items: center;
    flex-direction: column;
    padding: 0 20px;
    
    .file-info {
      text-align: center;
      margin-top: 20px;
    }
  }
}

.upload-form {
  padding: 0 20px;
  
  .form-tip {
    font-size: 12px;
    color: #909399;
    margin-top: 5px;
  }
  
  .upload-demo {
    width: 100%;
  }
}

.dialog-footer {
  padding: 0 20px;
  display: flex;
  justify-content: flex-end;
}

.upload-file-list {
  width: 100%;
}

.upload-file-item {
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  padding: 10px;
  margin-bottom: 10px;
}

.upload-file-item .file-name {
  font-size: 14px;
  color: #606266;
  margin-bottom: 5px;
  display: block;
}

/* 覆盖Element Plus对话框样式 */
:deep(.el-dialog__body) {
  padding: 20px 0;
}

:deep(.el-dialog__header) {
  padding-left: 20px;
  padding-right: 20px;
  margin-right: 0;
}

:deep(.el-dialog__footer) {
  padding-top: 10px;
  padding-bottom: 15px;
}

/* 修改上传进度条样式 */
:deep(.el-progress__text) {
  color: #303133 !important; /* 深灰色字体，确保在各种背景上都可见 */
  font-size: 12px;
}

:deep(.el-progress--line) {
  margin-bottom: 10px;
}

.upload-file-item {
  border: 1px solid #dcdfe6;
  border-radius: 6px; /* 增加圆角 */
  padding: 12px; /* 增加内边距 */
  margin-bottom: 12px; /* 增加外边距 */
  background-color: #fafafa; /* 轻微背景色 */
  transition: box-shadow 0.3s; /* 添加过渡效果 */
}

.upload-file-item:hover {
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1); /* 悬停效果 */
}

.upload-file-item .file-name {
  font-size: 14px;
  color: #303133; /* 深灰色字体 */
  margin-bottom: 8px; /* 增加底部间距 */
  display: block;
  font-weight: 500;
}
</style>
