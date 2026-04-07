<template>
  <div class="social-publish-center">
    <div class="page-header">
      <h1>Reddit / X 发布中心</h1>
      <p>单次配置后可同时发布到多个子版块和 X 账号。</p>
    </div>

    <el-alert
      v-if="loadError"
      :title="loadError"
      type="error"
      show-icon
      :closable="false"
      class="page-alert"
    />

    <div class="page-grid">
      <el-card class="content-card">
        <template #header>
          <div class="card-header">
            <span>共享内容</span>
            <div class="card-actions">
              <el-button type="primary" @click="localUploadVisible = true">
                <el-icon><Upload /></el-icon>
                本地上传
              </el-button>
              <el-button type="success" @click="materialLibraryVisible = true">
                <el-icon><FolderOpened /></el-icon>
                素材库
              </el-button>
            </div>
          </div>
        </template>

        <el-form label-position="top">
          <el-form-item label="标题">
            <el-input v-model="form.title" maxlength="300" show-word-limit placeholder="输入 Reddit/X 共用标题" />
          </el-form-item>

          <el-form-item label="正文 / 说明">
            <el-input
              v-model="form.body"
              type="textarea"
              :rows="6"
              maxlength="5000"
              show-word-limit
              placeholder="输入共用正文。X 默认会回退到这里，Reddit self-post 也会回退到这里。"
            />
          </el-form-item>
        </el-form>

        <div class="attached-files">
          <div class="section-title">已选素材</div>
          <div v-if="attachedFiles.length > 0" class="file-list">
            <div v-for="file in attachedFiles" :key="file.filePath" class="file-item">
              <div class="file-main">
                <el-link :href="file.url" target="_blank" type="primary">{{ file.name }}</el-link>
                <span class="file-size">{{ formatFileSize(file.size) }}</span>
              </div>
              <el-button type="danger" link @click="removeAttachedFile(file.filePath)">移除</el-button>
            </div>
          </div>
          <el-empty v-else description="未选择素材。X 可纯文本发布；Reddit phase 1 仅支持 self/link 帖子。" />
        </div>

        <el-alert
          v-if="form.reddit.enabled && attachedFiles.length > 0"
          title="当前请求包含 Reddit 与媒体素材。后端会继续执行 X 发布，但 Reddit 结果会明确返回“第二阶段实现”。"
          type="warning"
          show-icon
          :closable="false"
          class="page-alert"
        />
      </el-card>

      <div class="destination-column">
        <el-card class="destination-card">
          <template #header>
            <div class="card-header">
              <span>X</span>
              <el-switch v-model="form.x.enabled" />
            </div>
          </template>

          <div v-if="accountsLoading" class="panel-loading">
            <el-icon class="is-loading"><Refresh /></el-icon>
            <span>加载 X 账号中...</span>
          </div>
          <div v-else-if="form.x.enabled">
            <el-form label-position="top">
              <el-form-item label="X 账号">
                <el-select
                  v-model="form.x.accountIds"
                  multiple
                  filterable
                  collapse-tags
                  collapse-tags-tooltip
                  placeholder="选择一个或多个 X 账号"
                  style="width: 100%"
                >
                  <el-option
                    v-for="account in xAccountOptions"
                    :key="account.id"
                    :label="account.name"
                    :value="account.id"
                  />
                </el-select>
              </el-form-item>
              <el-form-item label="X 文案覆盖">
                <el-input
                  v-model="form.x.text"
                  type="textarea"
                  :rows="4"
                  maxlength="280"
                  show-word-limit
                  placeholder="可选。为空时回退到共用正文，再回退到标题。"
                />
              </el-form-item>
            </el-form>
            <el-empty v-if="xAccountOptions.length === 0" description="暂无可用 X 账号，请先到账号管理完成 OAuth 登录。" />
          </div>
          <el-empty v-else description="已关闭 X 发布" />
        </el-card>

        <el-card class="destination-card">
          <template #header>
            <div class="card-header">
              <span>Reddit</span>
              <el-switch v-model="form.reddit.enabled" />
            </div>
          </template>

          <div v-if="accountsLoading" class="panel-loading">
            <el-icon class="is-loading"><Refresh /></el-icon>
            <span>加载 Reddit 账号中...</span>
          </div>
          <div v-else-if="form.reddit.enabled">
            <el-form label-position="top">
              <el-form-item label="Reddit 账号">
                <el-select
                  v-model="form.reddit.accountIds"
                  multiple
                  filterable
                  collapse-tags
                  collapse-tags-tooltip
                  placeholder="选择一个或多个 Reddit 账号"
                  style="width: 100%"
                >
                  <el-option
                    v-for="account in redditAccountOptions"
                    :key="account.id"
                    :label="account.name"
                    :value="account.id"
                  />
                </el-select>
              </el-form-item>

              <el-form-item label="发布类型">
                <el-radio-group v-model="form.reddit.postKind">
                  <el-radio-button label="self">Self Post</el-radio-button>
                  <el-radio-button label="link">Link Post</el-radio-button>
                </el-radio-group>
              </el-form-item>

              <el-form-item label="目标子版块">
                <el-select
                  v-model="form.reddit.subreddits"
                  multiple
                  filterable
                  allow-create
                  default-first-option
                  placeholder="输入 subreddit 名称并回车"
                  style="width: 100%"
                >
                  <el-option
                    v-for="subreddit in form.reddit.subreddits"
                    :key="subreddit"
                    :label="subreddit"
                    :value="subreddit"
                  />
                </el-select>
              </el-form-item>

              <el-form-item v-if="form.reddit.postKind === 'self'" label="Reddit 正文覆盖">
                <el-input
                  v-model="form.reddit.body"
                  type="textarea"
                  :rows="5"
                  maxlength="40000"
                  show-word-limit
                  placeholder="可选。为空时回退到共用正文。"
                />
              </el-form-item>

              <el-form-item v-else label="链接地址">
                <el-input v-model="form.reddit.linkUrl" placeholder="https://example.com/article" />
              </el-form-item>
            </el-form>
            <el-empty v-if="redditAccountOptions.length === 0" description="暂无可用 Reddit 账号，请先到账号管理完成 OAuth 登录。" />
          </div>
          <el-empty v-else description="已关闭 Reddit 发布" />
        </el-card>

        <div class="submit-bar">
          <el-button type="primary" size="large" :loading="submitting" @click="submitPublish">
            发布到 Reddit / X
          </el-button>
        </div>
      </div>
    </div>

    <el-card class="results-card">
      <template #header>
        <div class="card-header">
          <span>发布结果</span>
          <el-button text @click="clearResults">清空</el-button>
        </div>
      </template>

      <el-table v-if="results.length > 0" :data="results" style="width: 100%">
        <el-table-column prop="platform" label="平台" width="120" />
        <el-table-column prop="accountName" label="账号" width="180" />
        <el-table-column label="目标" width="220">
          <template #default="scope">
            <span>{{ scope.row.subreddit || scope.row.text || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="结果" width="120">
          <template #default="scope">
            <el-tag :type="scope.row.success ? 'success' : 'danger'" effect="plain">
              {{ scope.row.success ? '成功' : '失败' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="message" label="消息" />
      </el-table>
      <el-empty v-else description="尚未执行发布" />
    </el-card>

    <el-dialog
      v-model="localUploadVisible"
      title="本地上传"
      width="600px"
      class="local-upload-dialog"
    >
      <el-upload
        class="video-upload"
        drag
        :auto-upload="true"
        :action="`${apiBaseUrl}/upload`"
        :on-success="handleUploadSuccess"
        :on-error="handleUploadError"
        multiple
      >
        <el-icon class="el-icon--upload"><Upload /></el-icon>
        <div class="el-upload__text">
          将文件拖到此处，或<em>点击上传</em>
        </div>
        <template #tip>
          <div class="el-upload__tip">
            X 支持媒体；Reddit phase 1 遇到媒体会返回明确提示。
          </div>
        </template>
      </el-upload>
    </el-dialog>

    <el-dialog
      v-model="materialLibraryVisible"
      title="选择素材"
      width="800px"
    >
      <div v-if="materialsLoading" class="panel-loading">
        <el-icon class="is-loading"><Refresh /></el-icon>
        <span>加载素材中...</span>
      </div>
      <el-checkbox-group v-else v-model="selectedMaterialIds" class="material-checkbox-group">
        <div class="material-list">
          <div v-for="material in materials" :key="material.id" class="material-item">
            <el-checkbox :label="material.id">
              <div class="material-info">
                <div class="material-name">{{ material.filename }}</div>
                <div class="material-meta">{{ material.filesize }}MB</div>
              </div>
            </el-checkbox>
          </div>
        </div>
      </el-checkbox-group>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="materialLibraryVisible = false">取消</el-button>
          <el-button type="primary" @click="appendSelectedMaterials">添加到当前内容</el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { FolderOpened, Refresh, Upload } from '@element-plus/icons-vue'
import { accountApi } from '@/api/account'
import { materialApi } from '@/api/material'
import { socialApi } from '@/api/social'
import { useAccountStore } from '@/stores/account'

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL || 'http://localhost:5409'

const accountStore = useAccountStore()

const accountsLoading = ref(false)
const materialsLoading = ref(false)
const submitting = ref(false)
const loadError = ref('')

const localUploadVisible = ref(false)
const materialLibraryVisible = ref(false)
const selectedMaterialIds = ref([])
const materials = ref([])
const results = ref([])

const form = reactive({
  title: '',
  body: '',
  x: {
    enabled: true,
    accountIds: [],
    text: ''
  },
  reddit: {
    enabled: true,
    accountIds: [],
    subreddits: [],
    postKind: 'self',
    body: '',
    linkUrl: ''
  }
})

const attachedFiles = ref([])

const xAccountOptions = computed(() => {
  return accountStore.accounts.filter(account => account.platform === 'X' && account.status === '正常')
})

const redditAccountOptions = computed(() => {
  return accountStore.accounts.filter(account => account.platform === 'Reddit' && account.status === '正常')
})

const fetchAccounts = async () => {
  accountsLoading.value = true
  try {
    const res = await accountApi.getValidAccounts()
    accountStore.setAccounts(res.data || [])
  } finally {
    accountsLoading.value = false
  }
}

const fetchMaterials = async () => {
  materialsLoading.value = true
  try {
    const res = await materialApi.getAllMaterials()
    materials.value = res.data || []
  } finally {
    materialsLoading.value = false
  }
}

const initializePage = async () => {
  loadError.value = ''
  try {
    await Promise.all([fetchAccounts(), fetchMaterials()])
  } catch (error) {
    console.error('初始化 Reddit / X 发布中心失败:', error)
    loadError.value = '初始化页面失败，请稍后重试。'
  }
}

onMounted(() => {
  initializePage()
})

const formatFileSize = (size) => {
  if (!size) return '0MB'
  return `${(size / 1024 / 1024).toFixed(2)}MB`
}

const upsertAttachedFile = (file) => {
  const exists = attachedFiles.value.some(item => item.filePath === file.filePath)
  if (!exists) {
    attachedFiles.value.push(file)
  }
}

const handleUploadSuccess = (response, file) => {
  if (response.code !== 200 || !response.data) {
    ElMessage.error(response.msg || '上传失败')
    return
  }

  upsertAttachedFile({
    name: file.name,
    size: file.size,
    filePath: response.data,
    url: materialApi.getMaterialPreviewUrl(response.data)
  })
  localUploadVisible.value = false
  ElMessage.success('素材上传成功')
}

const handleUploadError = () => {
  ElMessage.error('素材上传失败')
}

const appendSelectedMaterials = () => {
  const selectedMaterials = materials.value.filter(material => selectedMaterialIds.value.includes(material.id))
  selectedMaterials.forEach(material => {
    upsertAttachedFile({
      name: material.filename,
      size: Number(material.filesize || 0) * 1024 * 1024,
      filePath: material.file_path,
      url: materialApi.getMaterialPreviewUrl(material.file_path)
    })
  })
  selectedMaterialIds.value = []
  materialLibraryVisible.value = false
}

const removeAttachedFile = (filePath) => {
  attachedFiles.value = attachedFiles.value.filter(file => file.filePath !== filePath)
}

const clearResults = () => {
  results.value = []
}

const validateForm = () => {
  if (!form.x.enabled && !form.reddit.enabled) {
    throw new Error('请至少启用一个发布目标')
  }

  if (form.x.enabled) {
    if (form.x.accountIds.length === 0) {
      throw new Error('请选择至少一个 X 账号')
    }
    const xText = (form.x.text || form.body || form.title || '').trim()
    if (!xText) {
      throw new Error('X 文案不能为空')
    }
  }

  if (form.reddit.enabled) {
    if (form.reddit.accountIds.length === 0) {
      throw new Error('请选择至少一个 Reddit 账号')
    }
    if (form.reddit.subreddits.length === 0) {
      throw new Error('请至少填写一个 Reddit 子版块')
    }
    if (!form.title.trim()) {
      throw new Error('Reddit 发布需要标题')
    }
    if (form.reddit.postKind === 'link' && !form.reddit.linkUrl.trim()) {
      throw new Error('Reddit Link Post 需要链接地址')
    }
    if (form.reddit.postKind === 'self' && !(form.reddit.body || form.body).trim()) {
      throw new Error('Reddit Self Post 需要正文')
    }
  }
}

const buildPayload = () => {
  const payload = {
    title: form.title.trim(),
    body: form.body.trim(),
    fileList: attachedFiles.value.map(file => file.filePath),
    destinations: []
  }

  if (form.x.enabled) {
    payload.destinations.push({
      platform: 'x',
      accountIds: form.x.accountIds,
      text: form.x.text.trim()
    })
  }

  if (form.reddit.enabled) {
    payload.destinations.push({
      platform: 'reddit',
      accountIds: form.reddit.accountIds,
      subreddits: form.reddit.subreddits.map(subreddit => subreddit.trim()).filter(Boolean),
      postKind: form.reddit.postKind,
      body: form.reddit.body.trim(),
      linkUrl: form.reddit.linkUrl.trim()
    })
  }

  return payload
}

const submitPublish = async () => {
  try {
    validateForm()
  } catch (error) {
    ElMessage.error(error.message)
    return
  }

  submitting.value = true
  try {
    const res = await socialApi.publish(buildPayload())
    results.value = res.data || []
    if (results.value.some(item => item.success)) {
      ElMessage.success('发布请求已执行')
    } else {
      ElMessage.warning('发布已返回，但没有成功结果')
    }
  } catch (error) {
    console.error('发布失败:', error)
    ElMessage.error('发布失败，请稍后再试')
  } finally {
    submitting.value = false
  }
}
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

.social-publish-center {
  .page-header {
    margin-bottom: 20px;

    h1 {
      margin: 0 0 8px;
      font-size: 24px;
      color: $text-primary;
    }

    p {
      margin: 0;
      color: $text-secondary;
    }
  }

  .page-alert {
    margin-bottom: 16px;
  }

  .page-grid {
    display: grid;
    grid-template-columns: minmax(0, 1.2fr) minmax(340px, 0.8fr);
    gap: 20px;
    margin-bottom: 20px;
  }

  .content-card,
  .destination-card,
  .results-card {
    box-shadow: $box-shadow-light;
  }

  .destination-column {
    display: flex;
    flex-direction: column;
    gap: 20px;
  }

  .card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  .card-actions {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
  }

  .section-title {
    margin-bottom: 12px;
    font-weight: 600;
    color: $text-primary;
  }

  .attached-files {
    margin-top: 8px;
  }

  .file-list,
  .material-list {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .file-item,
  .material-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 12px 14px;
    border: 1px solid $border-color-light;
    border-radius: 6px;
    background-color: #fff;
  }

  .file-main,
  .material-info {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }

  .file-size,
  .material-meta {
    color: $text-secondary;
    font-size: 13px;
  }

  .submit-bar {
    display: flex;
    justify-content: flex-end;
  }

  .panel-loading {
    min-height: 120px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    color: $text-secondary;
  }

  .material-checkbox-group {
    width: 100%;
  }

  .video-upload {
    width: 100%;
  }
}

@media (max-width: 1100px) {
  .social-publish-center {
    .page-grid {
      grid-template-columns: 1fr;
    }
  }
}
</style>
