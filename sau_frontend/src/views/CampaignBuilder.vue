<template>
  <div class="campaign-builder">
    <el-steps :active="currentStep" finish-status="success" align-center>
      <el-step title="Select Profile" />
      <el-step title="Select Media" />
      <el-step title="Generate Content" />
      <el-step title="Review & Approve" />
      <el-step title="Export / Publish" />
    </el-steps>

    <!-- Step 1: Select Profile -->
    <el-card v-if="currentStep === 0" class="step-card">
      <h3>Select Profile</h3>
      <el-select v-model="selectedProfileId" placeholder="Choose a profile" size="large" style="width: 100%">
        <el-option
          v-for="p in profiles"
          :key="p.id"
          :label="p.name"
          :value="p.id"
        >
          <span>{{ p.name }}</span>
          <span style="float: right; color: #8492a6; font-size: 12px">{{ p.slug }}</span>
        </el-option>
      </el-select>
      <div v-if="selectedProfile" class="profile-preview">
        <p><strong>System Prompt:</strong> {{ selectedProfile.settings?.system_prompt || selectedProfile.system_prompt || '(not set)' }}</p>
        <p><strong>Writing Style:</strong> {{ selectedProfile.settings?.writing_style_prompt || selectedProfile.writing_style_prompt || '(not set)' }}</p>
        <p><strong>Default CTA:</strong> {{ selectedProfile.settings?.default_cta || selectedProfile.default_cta || '(not set)' }}</p>
      </div>
      <el-button type="primary" :disabled="!selectedProfileId" @click="currentStep = 1" style="margin-top: 16px">
        Next
      </el-button>
    </el-card>

    <!-- Step 2: Select Media Groups -->
    <el-card v-if="currentStep === 1" class="step-card">
      <h3>Select Media Groups</h3>
      <el-checkbox-group v-model="selectedMediaGroupIds">
        <div v-for="mg in mediaGroups" :key="mg.id" class="media-group-option">
          <el-checkbox :value="mg.id">
            <strong>{{ mg.name }}</strong>
            <span v-if="mg.content_theme" class="theme-tag"> — {{ mg.content_theme }}</span>
            <span v-if="mg.notes" class="notes-preview"> — {{ mg.notes.substring(0, 60) }}...</span>
          </el-checkbox>
        </div>
      </el-checkbox-group>
      <div class="step-actions">
        <el-button @click="currentStep = 0">Back</el-button>
        <el-button type="primary" :disabled="selectedMediaGroupIds.length === 0" @click="currentStep = 2">
          Next ({{ selectedMediaGroupIds.length }} groups)
        </el-button>
      </div>
    </el-card>

    <!-- Step 3: Generate Content -->
    <el-card v-if="currentStep === 2" class="step-card">
      <h3>Generate Content</h3>
      <p>Generate platform-specific posts for the selected media groups using the LLM.</p>

      <el-form label-width="140px">
        <el-form-item label="Target Platforms">
          <el-checkbox-group v-model="targetPlatforms">
            <el-checkbox v-for="p in availablePlatforms" :key="p" :value="p" :label="p" />
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="Content Notes">
          <el-input v-model="generationNotes" type="textarea" :rows="3" placeholder="Additional context for the LLM" />
        </el-form-item>
      </el-form>

      <div class="step-actions">
        <el-button @click="currentStep = 1">Back</el-button>
        <el-button type="primary" :loading="generating" @click="generateContent">
          {{ generating ? 'Generating...' : 'Generate Content' }}
        </el-button>
      </div>
    </el-card>

    <!-- Step 4: Review & Approve -->
    <el-card v-if="currentStep === 3" class="step-card">
      <h3>Review & Approve</h3>

      <div v-if="generatedPosts.length === 0" class="empty-state">
        <p>No posts generated yet. Go back and generate content.</p>
      </div>

      <div v-for="post in generatedPosts" :key="post.id" class="post-review-card">
        <div class="post-header">
          <el-tag :type="platformTagType(post.platform)">{{ post.platform }}</el-tag>
          <el-tag v-if="post.target_name">{{ post.target_name }}</el-tag>
          <el-tag :type="statusTagType(post.status)">{{ post.status }}</el-tag>
          <span class="char-count">{{ post.char_count || 0 }} chars</span>
        </div>

        <div v-if="post.validation_errors && post.validation_errors.length > 0" class="validation-errors">
          <el-alert
            v-for="(err, i) in post.validation_errors"
            :key="i"
            :title="err"
            type="warning"
            :closable="false"
            show-icon
          />
        </div>

        <el-input
          v-model="post.message"
          type="textarea"
          :rows="4"
          @change="updatePost(post)"
        />

        <div v-if="post.title !== undefined" class="post-field">
          <label>Title:</label>
          <el-input v-model="post.title" @change="updatePost(post)" />
        </div>
        <div v-if="post.description !== undefined" class="post-field">
          <label>Description:</label>
          <el-input v-model="post.description" type="textarea" :rows="2" @change="updatePost(post)" />
        </div>
        <div v-if="post.first_comment" class="post-field">
          <label>First Comment:</label>
          <el-input v-model="post.first_comment" @change="updatePost(post)" />
        </div>

        <div class="post-actions">
          <el-button size="small" @click="regeneratePost(post)">Regenerate</el-button>
          <el-button size="small" type="success" :disabled="post.validation_errors?.length > 0" @click="approvePost(post)">
            Approve
          </el-button>
        </div>
      </div>

      <div class="step-actions">
        <el-button @click="currentStep = 2">Back</el-button>
        <el-button type="success" @click="approveAll">Approve All Valid</el-button>
        <el-button type="primary" :disabled="approvedCount === 0" @click="currentStep = 4">
          Next ({{ approvedCount }} approved)
        </el-button>
      </div>
    </el-card>

    <!-- Step 5: Export / Publish -->
    <el-card v-if="currentStep === 4" class="step-card">
      <h3>Export / Publish</h3>

      <el-descriptions :column="2" border>
        <el-descriptions-item label="Profile">{{ selectedProfile?.name }}</el-descriptions-item>
        <el-descriptions-item label="Approved Posts">{{ approvedCount }}</el-descriptions-item>
        <el-descriptions-item label="Media Groups">{{ selectedMediaGroupIds.length }}</el-descriptions-item>
      </el-descriptions>

      <div class="export-actions">
        <el-button type="primary" size="large" :loading="exporting" @click="exportToSheet">
          <el-icon><Document /></el-icon>
          Export to Google Sheet
        </el-button>
        <el-button type="success" size="large" @click="downloadCsv">
          <el-icon><Download /></el-icon>
          Download CSV
        </el-button>
      </div>

      <div v-if="lastExport" class="export-result">
        <el-result icon="success" title="Export Complete">
          <template #extra>
            <p>Sheet: <strong>{{ lastExport.sheet_name }}</strong></p>
            <p>Rows: {{ lastExport.row_count }}</p>
            <el-button type="primary" @click="openSheet(lastExport.spreadsheet_url)">Open Google Sheet</el-button>
          </template>
        </el-result>
      </div>

      <div class="step-actions">
        <el-button @click="currentStep = 3">Back</el-button>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Document, Download } from '@element-plus/icons-vue'
import { profilesApi } from '@/api/profiles'
import { campaignExtendedApi } from '@/api/campaign-extended'
import { buildApiUrl } from '@/utils/api-url'

const currentStep = ref(0)
const profiles = ref([])
const selectedProfileId = ref(null)
const selectedMediaGroupIds = ref([])
const mediaGroups = ref([])
const targetPlatforms = ref(['twitter', 'instagram', 'facebook', 'threads', 'tiktok', 'youtube'])
const generationNotes = ref('')
const generating = ref(false)
const generatedPosts = ref([])
const exporting = ref(false)
const lastExport = ref(null)

const availablePlatforms = [
  'twitter', 'instagram', 'facebook', 'threads', 'tiktok',
  'youtube', 'reddit', 'telegram', 'discord', 'patreon',
]

const selectedProfile = computed(() =>
  profiles.value.find(p => p.id === selectedProfileId.value)
)

const approvedCount = computed(() =>
  generatedPosts.value.filter(p => p.status === 'approved').length
)

onMounted(() => {
  loadProfiles()
  loadMediaGroups()
})

async function loadProfiles() {
  try {
    const res = await profilesApi.list()
    profiles.value = res.data || res
  } catch (e) {
    console.error(e)
  }
}

async function loadMediaGroups() {
  try {
    const { http } = await import('@/utils/request')
    const res = await http.get('/media-groups')
    mediaGroups.value = res.data || res
  } catch (e) {
    console.error(e)
  }
}

async function generateContent() {
  generating.value = true
  try {
    // For each selected media group, create a campaign and generate
    for (const mgId of selectedMediaGroupIds.value) {
      const { http } = await import('@/utils/request')
      // Create campaign
      const campaignRes = await http.post('/campaigns/prepare', {
        profile_id: selectedProfileId.value,
        media_group_id: mgId,
        selected_account_ids: [],
      })
      const campaign = campaignRes.data || campaignRes

      // Generate content
      const genRes = await campaignExtendedApi.generate(campaign.id, {
        platforms: targetPlatforms.value,
      })
      const genData = genRes.data || genRes
      generatedPosts.value.push(...(genData.posts || []))
    }
    ElMessage.success(`Generated ${generatedPosts.value.length} posts`)
    currentStep.value = 3
  } catch (e) {
    ElMessage.error('Generation failed: ' + (e.message || e))
  } finally {
    generating.value = false
  }
}

async function updatePost(post) {
  try {
    // Find the campaign ID from the post
    await campaignExtendedApi.updatePost(post.campaign_id, post.id, {
      message: post.message,
      title: post.title,
      description: post.description,
      first_comment: post.first_comment,
    })
  } catch (e) {
    console.error('Update failed:', e)
  }
}

async function approvePost(post) {
  post.status = 'approved'
  await updatePost(post)
}

async function approveAll() {
  for (const post of generatedPosts.value) {
    if (!post.validation_errors || post.validation_errors.length === 0) {
      post.status = 'approved'
    }
  }
  ElMessage.success(`Approved ${approvedCount.value} posts`)
}

async function regeneratePost(post) {
  try {
    // Just re-generate for this one platform
    const genRes = await campaignExtendedApi.generate(post.campaign_id, {
      platforms: [post.platform],
    })
    const genData = genRes.data || genRes
    if (genData.posts && genData.posts.length > 0) {
      const idx = generatedPosts.value.findIndex(p => p.id === post.id)
      if (idx >= 0) {
        generatedPosts.value[idx] = genData.posts[0]
      }
    }
    ElMessage.success('Regenerated')
  } catch (e) {
    ElMessage.error('Regenerate failed')
  }
}

async function exportToSheet() {
  exporting.value = true
  try {
    const profileSlug = selectedProfile.value?.slug || 'default'
    // Export for the first campaign (simplified)
    const campaignId = generatedPosts.value[0]?.campaign_id
    if (!campaignId) throw new Error('No campaign ID')

    const res = await campaignExtendedApi.exportSheet(campaignId, {
      profile_slug: profileSlug,
    })
    lastExport.value = res.data || res
    ElMessage.success('Exported to Google Sheet')
  } catch (e) {
    ElMessage.error('Export failed: ' + (e.message || e))
  } finally {
    exporting.value = false
  }
}

function downloadCsv() {
  const campaignId = generatedPosts.value[0]?.campaign_id
  if (!campaignId) return
  window.open(buildApiUrl(campaignExtendedApi.exportCsvUrl(campaignId)), '_blank')
}

function openSheet(url) {
  if (url) window.open(url, '_blank')
}

function platformTagType(platform) {
  const types = {
    twitter: 'primary', instagram: 'danger', facebook: 'info',
    threads: '', tiktok: 'warning', youtube: 'danger',
    reddit: 'warning', telegram: 'info', discord: 'info',
  }
  return types[platform] || ''
}

function statusTagType(status) {
  if (status === 'approved') return 'success'
  if (status === 'needs_review') return 'warning'
  if (status === 'failed') return 'danger'
  return 'info'
}
</script>

<style scoped>
.campaign-builder {
  padding: 20px;
  max-width: 1000px;
  margin: 0 auto;
}
.step-card {
  margin-top: 20px;
}
.step-actions {
  margin-top: 20px;
  display: flex;
  gap: 12px;
}
.profile-preview {
  margin-top: 16px;
  padding: 12px;
  background: #f5f7fa;
  border-radius: 4px;
}
.profile-preview p {
  margin: 4px 0;
  font-size: 13px;
}
.media-group-option {
  padding: 8px 0;
  border-bottom: 1px solid #ebeef5;
}
.theme-tag {
  color: #409eff;
}
.notes-preview {
  color: #909399;
}
.post-review-card {
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 16px;
  margin-bottom: 16px;
}
.post-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.char-count {
  margin-left: auto;
  font-size: 12px;
  color: #909399;
}
.validation-errors {
  margin-bottom: 12px;
}
.validation-errors .el-alert {
  margin-bottom: 4px;
}
.post-field {
  margin-top: 8px;
}
.post-field label {
  font-size: 12px;
  color: #606266;
  margin-bottom: 4px;
  display: block;
}
.post-actions {
  margin-top: 12px;
  display: flex;
  gap: 8px;
}
.export-actions {
  margin-top: 24px;
  display: flex;
  gap: 16px;
  justify-content: center;
}
.export-result {
  margin-top: 24px;
}
.empty-state {
  text-align: center;
  padding: 40px;
  color: #909399;
}
</style>
