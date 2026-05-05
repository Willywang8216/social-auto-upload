<template>
  <div class="account-management">
    <div class="page-header">
      <h1>帳號管理</h1>
      <div class="profile-toolbar">
        <el-select v-model="selectedProfileFilter" style="width: 260px" placeholder="篩選 Profile">
          <el-option label="全部帳號" value="all" />
          <el-option label="Legacy 帳號" value="legacy" />
          <el-option
            v-for="profile in profileOptions"
            :key="profile.id"
            :label="profile.name"
            :value="String(profile.id)"
          />
        </el-select>
        <el-button type="primary" plain @click="openProfileDialog">新增 Profile</el-button>
      </div>
    </div>

    <div class="account-tabs">
      <el-tabs v-model="activeTab" class="account-tabs-nav">
        <el-tab-pane label="全部" name="all">
          <AccountTabPane
            :accounts="filteredAccounts"
            :search-keyword="searchKeyword"
            :refreshing="appStore.isAccountRefreshing"
            empty-text="目前沒有帳號資料"
            @add="handleAddAccount"
            @edit="handleEdit"
            @delete="handleDelete"
            @download-cookie="handleDownloadCookie"
            @upload-cookie="handleUploadCookie"
            @refresh="refreshAccounts"
            @relogin="handleReLogin"
            @search="onSearchChange"
          />
        </el-tab-pane>
        <el-tab-pane
          v-for="platform in accountPlatformTabs"
          :key="platform.value"
          :label="platform.label"
          :name="platform.value"
        >
          <AccountTabPane
            :accounts="getFilteredAccountsByPlatform(platform.label)"
            :search-keyword="searchKeyword"
            :refreshing="appStore.isAccountRefreshing"
            :empty-text="`目前沒有${platform.label}帳號資料`"
            @add="handleAddAccount"
            @edit="handleEdit"
            @delete="handleDelete"
            @download-cookie="handleDownloadCookie"
            @upload-cookie="handleUploadCookie"
            @refresh="refreshAccounts"
            @relogin="handleReLogin"
            @search="onSearchChange"
          />
        </el-tab-pane>
      </el-tabs>
    </div>

    <el-dialog
      v-model="profileDialogVisible"
      title="新增 Profile"
      width="560px"
      :close-on-click-modal="false"
    >
      <el-form :model="profileForm" label-width="100px" ref="profileFormRef">
        <el-form-item label="名稱" required>
          <el-input v-model="profileForm.name" placeholder="例如：Brand A" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="profileForm.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="System Prompt">
          <el-input v-model="profileForm.systemPrompt" type="textarea" :rows="4" />
        </el-form-item>
        <el-form-item label="浮水印">
          <el-input v-model="profileForm.watermark" placeholder="文字浮水印" />
        </el-form-item>
        <el-form-item label="聯絡資訊">
          <el-input v-model="profileForm.contactDetails" />
        </el-form-item>
        <el-form-item label="CTA">
          <el-input v-model="profileForm.ctaText" />
        </el-form-item>
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="profileDialogVisible = false">取消</el-button>
          <el-button type="primary" @click="submitProfileForm">建立 Profile</el-button>
        </span>
      </template>
    </el-dialog>

    <el-dialog
      v-model="dialogVisible"
      :title="dialogType === 'add' ? '新增帳號' : '編輯帳號'"
      width="720px"
      :close-on-click-modal="false"
      :close-on-press-escape="!sseConnecting"
      :show-close="!sseConnecting"
    >
      <el-form :model="accountForm" label-width="110px" :rules="rules" ref="accountFormRef">
        <el-form-item label="Profile">
          <el-select
            v-model="accountForm.profileId"
            clearable
            filterable
            placeholder="留空代表 Legacy 帳號"
            style="width: 100%"
            :disabled="sseConnecting"
          >
            <el-option
              v-for="profile in profileOptions"
              :key="profile.id"
              :label="profile.name"
              :value="profile.id"
            />
          </el-select>
          <div class="field-hint">選擇 Profile 後會使用新的 profile/account registry；留空則維持舊版 QR login 帳號。</div>
        </el-form-item>

        <el-form-item label="平台" prop="platform">
          <el-select
            v-model="accountForm.platform"
            placeholder="請選擇平台"
            style="width: 100%"
            :disabled="sseConnecting"
          >
            <el-option
              v-for="platform in accountPlatformTabs"
              :key="platform.value"
              :label="platform.label"
              :value="platform.value"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="名稱" prop="name">
          <el-input
            v-model="accountForm.name"
            placeholder="請輸入帳號名稱"
            :disabled="sseConnecting"
          />
        </el-form-item>

        <template v-if="isStructuredAccountForm">
          <el-form-item label="登入方式">
            <el-select v-model="accountForm.authType" style="width: 100%">
              <el-option label="cookie" value="cookie" />
              <el-option label="oauth" value="oauth" />
              <el-option label="manual" value="manual" />
            </el-select>
          </el-form-item>

          <el-form-item label="啟用狀態">
            <el-switch v-model="accountForm.enabled" active-text="啟用" inactive-text="停用" />
          </el-form-item>

          <el-form-item v-if="accountForm.authType === 'cookie'" label="Cookie 路徑">
            <el-input
              v-model="accountForm.cookiePath"
              placeholder="可留空，後端會依 Profile/平台自動產生"
            />
          </el-form-item>

          <el-form-item label="Sheet Preset">
            <el-input v-model="accountForm.sheetPostPreset" placeholder="對應 Google Sheet / 排程工具 preset 名稱" />
          </el-form-item>

          <template v-if="accountForm.platform === 'reddit'">
            <el-divider content-position="left">Reddit 設定</el-divider>
            <el-form-item label="Subreddits">
              <el-input
                v-model="accountForm.subredditsText"
                type="textarea"
                :rows="3"
                placeholder="用逗號或換行分隔，例如：suba, subb"
              />
            </el-form-item>
            <el-form-item label="Client ID Env">
              <el-input v-model="accountForm.clientIdEnv" placeholder="例如：REDDIT_CLIENT_ID" />
            </el-form-item>
            <el-form-item label="Client Secret Env">
              <el-input v-model="accountForm.clientSecretEnv" placeholder="例如：REDDIT_CLIENT_SECRET" />
            </el-form-item>
            <el-form-item label="Refresh Token Env">
              <el-input v-model="accountForm.refreshTokenEnv" placeholder="例如：REDDIT_REFRESH_TOKEN" />
            </el-form-item>
            <el-form-item label="User Agent">
              <el-input v-model="accountForm.userAgent" placeholder="可選，自訂 Reddit User-Agent" />
            </el-form-item>
          </template>

          <template v-else-if="accountForm.platform === 'telegram'">
            <el-divider content-position="left">Telegram 設定</el-divider>
            <el-form-item label="Chat ID">
              <el-input v-model="accountForm.chatId" placeholder="例如：@channel_name 或 -100123456" />
            </el-form-item>
            <el-form-item label="Bot Token Env">
              <el-input v-model="accountForm.botTokenEnv" placeholder="例如：TELEGRAM_BOT_TOKEN" />
            </el-form-item>
            <el-form-item label="Parse Mode">
              <el-select v-model="accountForm.parseMode" clearable style="width: 100%">
                <el-option label="HTML" value="HTML" />
                <el-option label="MarkdownV2" value="MarkdownV2" />
              </el-select>
            </el-form-item>
            <el-form-item label="靜默發送">
              <el-switch v-model="accountForm.silent" />
            </el-form-item>
            <el-form-item label="關閉預覽">
              <el-switch v-model="accountForm.disableWebPreview" />
            </el-form-item>
          </template>

          <template v-else-if="accountForm.platform === 'youtube'">
            <el-divider content-position="left">YouTube 設定</el-divider>
            <el-form-item label="Channel ID">
              <el-input v-model="accountForm.channelId" placeholder="例如：UCxxxx" />
            </el-form-item>
            <el-form-item label="隱私狀態">
              <el-select v-model="accountForm.privacyStatus" style="width: 100%">
                <el-option label="private" value="private" />
                <el-option label="unlisted" value="unlisted" />
                <el-option label="public" value="public" />
              </el-select>
            </el-form-item>
            <el-form-item label="Playlist ID">
              <el-input v-model="accountForm.playlistId" placeholder="可選，自動加入播放清單" />
            </el-form-item>
            <el-form-item label="Category ID">
              <el-input v-model="accountForm.categoryId" placeholder="預設 22" />
            </el-form-item>
            <el-form-item label="Client ID Env">
              <el-input v-model="accountForm.clientIdEnv" placeholder="例如：YT_CLIENT_ID" />
            </el-form-item>
            <el-form-item label="Client Secret Env">
              <el-input v-model="accountForm.clientSecretEnv" placeholder="例如：YT_CLIENT_SECRET" />
            </el-form-item>
            <el-form-item label="Refresh Token Env">
              <el-input v-model="accountForm.refreshTokenEnv" placeholder="例如：YT_REFRESH_TOKEN" />
            </el-form-item>
          </template>

          <template v-else-if="accountForm.platform === 'facebook'">
            <el-divider content-position="left">Facebook 設定</el-divider>
            <el-form-item label="Page ID">
              <el-input v-model="accountForm.pageId" />
            </el-form-item>
            <el-form-item label="Access Token Env">
              <el-input v-model="accountForm.accessTokenEnv" placeholder="例如：FB_PAGE_TOKEN" />
            </el-form-item>
          </template>

          <template v-else-if="accountForm.platform === 'instagram'">
            <el-divider content-position="left">Instagram 設定</el-divider>
            <el-form-item label="IG User ID">
              <el-input v-model="accountForm.igUserId" />
            </el-form-item>
            <el-form-item label="Access Token Env">
              <el-input v-model="accountForm.accessTokenEnv" placeholder="例如：IG_ACCESS_TOKEN" />
            </el-form-item>
          </template>

          <template v-else-if="accountForm.platform === 'threads'">
            <el-divider content-position="left">Threads 設定</el-divider>
            <el-form-item label="User ID">
              <el-input v-model="accountForm.threadUserId" />
            </el-form-item>
            <el-form-item label="Access Token Env">
              <el-input v-model="accountForm.accessTokenEnv" placeholder="例如：THREADS_ACCESS_TOKEN" />
            </el-form-item>
          </template>

          <template v-else-if="accountForm.platform === 'tiktok'">
            <el-divider content-position="left">TikTok 設定</el-divider>
            <el-form-item label="Connect with TikTok">
              <div class="tiktok-connect-row">
                <el-button type="primary" @click="connectWithTikTok">Connect with TikTok</el-button>
                <el-button plain @click="refreshTikTokToken" :disabled="!accountForm.id">Refresh TikTok token</el-button>
                <el-button plain @click="openTikTokReviewStatus">Open callback status</el-button>
              </div>
              <div class="field-hint">這會走 TikTok Login Kit for Web，並使用 https://up.iamwillywang.com/oauth/tiktok/callback。</div>
            </el-form-item>
            <el-form-item label="Connected account">
              <div class="tiktok-connected-preview">
                <el-avatar v-if="accountForm.tiktokAvatarUrl" :src="accountForm.tiktokAvatarUrl" :size="40" />
                <div class="tiktok-connected-text">
                  <div><strong>{{ accountForm.tiktokDisplayName || 'Not connected yet' }}</strong></div>
                  <div class="field-hint">Open ID: {{ accountForm.openId || '—' }}</div>
                  <div class="field-hint">Scope: {{ accountForm.tiktokScope || '—' }}</div>
                </div>
              </div>
            </el-form-item>
            <el-form-item label="Connection health">
              <div class="tiktok-health-card">
                <div class="health-row"><span>Access token</span><strong>{{ accountForm.accessToken ? 'present' : 'missing' }}</strong></div>
                <div class="health-row"><span>Refresh token</span><strong>{{ accountForm.refreshToken ? 'present' : 'missing' }}</strong></div>
                <div class="health-row"><span>Last OAuth start</span><strong>{{ tiktokHealth.lastRequest?.requestedAt || '—' }}</strong></div>
                <div class="health-row"><span>Last callback</span><strong>{{ tiktokHealth.lastCallback?.receivedAt || '—' }}</strong></div>
                <div class="health-row"><span>Last refresh</span><strong>{{ tiktokHealth.lastRefresh?.receivedAt || '—' }}</strong></div>
                <div class="health-row"><span>Last webhook</span><strong>{{ tiktokHealth.lastWebhook?.receivedAt || '—' }}</strong></div>
                <div class="health-row"><span>Webhook signature</span><strong>{{ tiktokHealth.lastWebhook?.signatureStatus || '—' }}</strong></div>
              </div>
            </el-form-item>
            <el-form-item label="Access Token">
              <el-input v-model="accountForm.accessToken" placeholder="由 TikTok Connect 自動填入，或手動貼上" type="textarea" :rows="2" />
            </el-form-item>
            <el-form-item label="Refresh Token">
              <el-input v-model="accountForm.refreshToken" placeholder="由 TikTok Connect 自動填入" type="textarea" :rows="2" />
            </el-form-item>
            <el-form-item label="Access Token Env">
              <el-input v-model="accountForm.accessTokenEnv" placeholder="例如：TIKTOK_ACCESS_TOKEN；若已直連可留空" />
            </el-form-item>
            <el-form-item label="Publish Mode">
              <el-select v-model="accountForm.publishMode" style="width: 100%">
                <el-option label="direct" value="direct" />
                <el-option label="draft" value="draft" />
              </el-select>
            </el-form-item>
            <el-form-item label="Privacy Level">
              <el-select v-model="accountForm.privacyLevel" style="width: 100%">
                <el-option label="PUBLIC_TO_EVERYONE" value="PUBLIC_TO_EVERYONE" />
                <el-option label="MUTUAL_FOLLOW_FRIENDS" value="MUTUAL_FOLLOW_FRIENDS" />
                <el-option label="SELF_ONLY" value="SELF_ONLY" />
              </el-select>
            </el-form-item>
            <el-form-item label="關閉留言">
              <el-switch v-model="accountForm.disableComment" />
            </el-form-item>
            <el-form-item label="關閉 Duet">
              <el-switch v-model="accountForm.disableDuet" />
            </el-form-item>
            <el-form-item label="關閉 Stitch">
              <el-switch v-model="accountForm.disableStitch" />
            </el-form-item>
            <el-form-item label="自動配樂（圖片）">
              <el-switch v-model="accountForm.autoAddMusic" />
            </el-form-item>
            <el-form-item label="封面時間 ms">
              <el-input v-model="accountForm.videoCoverTimestampMs" placeholder="例如：1000" />
            </el-form-item>
            <div class="field-hint">注意：TikTok 官方 Content Posting API 不允許品牌/促銷浮水印內容。</div>
          </template>

          <template v-else-if="accountForm.platform === 'discord'">
            <el-divider content-position="left">Discord 設定</el-divider>
            <el-form-item label="Webhook URL Env">
              <el-input v-model="accountForm.webhookUrlEnv" placeholder="例如：DISCORD_WEBHOOK_URL" />
            </el-form-item>
          </template>

          <template v-else-if="accountForm.platform === 'patreon'">
            <el-divider content-position="left">Patreon 設定</el-divider>
            <el-form-item label="Campaign ID">
              <el-input v-model="accountForm.patreonCampaignId" />
            </el-form-item>
          </template>

          <el-form-item label="進階 JSON">
            <el-input
              v-model="accountForm.advancedConfigText"
              type="textarea"
              :rows="6"
              placeholder='如需額外設定，可填入 JSON，會與上方欄位合併'
            />
          </el-form-item>
        </template>

        <div v-else class="legacy-login-hint">
          Legacy 帳號使用現有 QR Login / Cookie 流程。若是 Facebook、Instagram、Reddit、Telegram、YouTube、TikTok、Threads 等新平台，請先建立 Profile，再新增該平台帳號。
        </div>

        <div v-if="sseConnecting" class="qrcode-container">
          <div v-if="qrCodeData && !loginStatus" class="qrcode-wrapper">
            <p class="qrcode-tip">請使用對應平台 App 掃描 QR Code 登入</p>
            <img :src="qrCodeData" alt="登入 QR Code" class="qrcode-image" />
          </div>
          <div v-else-if="!qrCodeData && !loginStatus" class="loading-wrapper">
            <el-icon class="is-loading"><Refresh /></el-icon>
            <span>載入中...</span>
          </div>
          <div v-else-if="loginStatus === '200'" class="success-wrapper">
            <el-icon><CircleCheckFilled /></el-icon>
            <span>新增成功</span>
          </div>
          <div v-else-if="loginStatus === '500'" class="error-wrapper">
            <el-icon><CircleCloseFilled /></el-icon>
            <span>新增失敗，請稍後再試</span>
          </div>
        </div>
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="dialogVisible = false">取消</el-button>
          <el-button
            type="primary"
            @click="submitAccountForm"
            :loading="sseConnecting"
            :disabled="sseConnecting"
          >
            {{ sseConnecting ? '處理中' : '確認' }}
          </el-button>
        </span>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { CircleCheckFilled, CircleCloseFilled, Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { accountApi } from '@/api/account'
import { profilesApi } from '@/api/profiles'
import { tiktokApi } from '@/api/tiktok'
import AccountTabPane from '@/components/AccountTabPane.vue'
import { useAccountStore } from '@/stores/account'
import { useAppStore } from '@/stores/app'
import { useProfilesStore } from '@/stores/profiles'
import { buildApiUrl } from '@/utils/api-url'
import { appendAuthQuery, getToken } from '@/utils/auth'
import { http } from '@/utils/request'
import { PROFILE_PLATFORM_OPTIONS, getLegacyPlatformType } from '@/utils/platforms'

const router = useRouter()
const accountStore = useAccountStore()
const appStore = useAppStore()
const profilesStore = useProfilesStore()

const activeTab = ref('all')
const searchKeyword = ref('')
const selectedProfileFilter = ref('all')

const accountPlatformTabs = PROFILE_PLATFORM_OPTIONS
const profileOptions = computed(() => profilesStore.profiles)

const dialogVisible = ref(false)
const dialogType = ref('add')
const accountFormRef = ref(null)
const profileDialogVisible = ref(false)
const profileFormRef = ref(null)

const makeEmptyAccountForm = () => ({
  id: null,
  profileId: null,
  name: '',
  platform: '',
  authType: 'cookie',
  enabled: true,
  cookiePath: '',
  sheetPostPreset: '',
  subredditsText: '',
  clientIdEnv: '',
  clientSecretEnv: '',
  refreshTokenEnv: '',
  userAgent: '',
  chatId: '',
  botTokenEnv: '',
  parseMode: '',
  silent: false,
  disableWebPreview: false,
  channelId: '',
  privacyStatus: 'private',
  playlistId: '',
  categoryId: '22',
  pageId: '',
  igUserId: '',
  threadUserId: '',
  accessToken: '',
  refreshToken: '',
  openId: '',
  tiktokScope: '',
  tiktokDisplayName: '',
  tiktokAvatarUrl: '',
  accessTokenEnv: '',
  publishMode: 'direct',
  privacyLevel: 'PUBLIC_TO_EVERYONE',
  disableComment: false,
  disableDuet: false,
  disableStitch: false,
  autoAddMusic: true,
  videoCoverTimestampMs: '',
  webhookUrlEnv: '',
  patreonCampaignId: '',
  advancedConfigText: '',
  status: '正常'
})

const accountForm = reactive(makeEmptyAccountForm())

const profileForm = reactive({
  name: '',
  description: '',
  systemPrompt: '',
  watermark: '',
  contactDetails: '',
  ctaText: ''
})

const rules = {
  platform: [{ required: true, message: '請選擇平台', trigger: 'change' }],
  name: [{ required: true, message: '請輸入帳號名稱', trigger: 'blur' }]
}

const isStructuredAccountForm = computed(() => Boolean(accountForm.profileId))

const sseConnecting = ref(false)
const qrCodeData = ref('')
const loginStatus = ref('')
const tiktokHealth = reactive({
  accountId: null,
  lastRequest: null,
  lastCallback: null,
  lastRefresh: null,
  lastWebhook: null,
})
let eventSource = null

const filteredAccounts = computed(() => {
  const keyword = searchKeyword.value.trim().toLowerCase()
  return accountStore.accounts.filter((account) => {
    if (selectedProfileFilter.value === 'legacy' && account.profileId != null) return false
    if (selectedProfileFilter.value !== 'all' && selectedProfileFilter.value !== 'legacy') {
      if (String(account.profileId) !== selectedProfileFilter.value) return false
    }

    if (!keyword) return true
    return [account.name, account.platform, account.profileName]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(keyword))
  })
})

const getFilteredAccountsByPlatform = (platformLabel) =>
  filteredAccounts.value.filter((account) => account.platform === platformLabel)

const onSearchChange = (value) => {
  searchKeyword.value = value
}

const assignIfValue = (target, key, value) => {
  if (value !== '' && value != null) {
    target[key] = value
  }
}

const splitListField = (value) =>
  String(value || '')
    .split(/[,\n]/)
    .map((item) => item.trim())
    .filter(Boolean)

const resetAccountForm = () => {
  Object.assign(accountForm, makeEmptyAccountForm(), {
    profileId: selectedProfileFilter.value !== 'all' && selectedProfileFilter.value !== 'legacy'
      ? Number(selectedProfileFilter.value)
      : null,
    platform: activeTab.value !== 'all' ? activeTab.value : ''
  })
}

const loadStructuredFieldsFromConfig = (config) => {
  accountForm.sheetPostPreset = config.sheetPostPreset || ''
  accountForm.subredditsText = Array.isArray(config.subreddits) ? config.subreddits.join(', ') : ''
  accountForm.clientIdEnv = config.clientIdEnv || ''
  accountForm.clientSecretEnv = config.clientSecretEnv || ''
  accountForm.refreshTokenEnv = config.refreshTokenEnv || ''
  accountForm.userAgent = config.userAgent || ''
  accountForm.chatId = config.chatId || ''
  accountForm.botTokenEnv = config.botTokenEnv || ''
  accountForm.parseMode = config.parseMode || ''
  accountForm.silent = Boolean(config.silent)
  accountForm.disableWebPreview = Boolean(config.disableWebPreview)
  accountForm.channelId = config.channelId || ''
  accountForm.privacyStatus = config.privacyStatus || 'private'
  accountForm.playlistId = config.playlistId || ''
  accountForm.categoryId = config.categoryId || '22'
  accountForm.pageId = config.pageId || ''
  accountForm.igUserId = config.igUserId || ''
  accountForm.threadUserId = config.threadUserId || ''
  accountForm.accessToken = config.accessToken || ''
  accountForm.refreshToken = config.refreshToken || ''
  accountForm.openId = config.openId || ''
  accountForm.tiktokScope = config.scope || ''
  accountForm.tiktokDisplayName = config.displayName || ''
  accountForm.tiktokAvatarUrl = config.avatarUrl || ''
  accountForm.accessTokenEnv = config.accessTokenEnv || ''
  accountForm.publishMode = config.publishMode || 'direct'
  accountForm.privacyLevel = config.privacyLevel || 'PUBLIC_TO_EVERYONE'
  accountForm.disableComment = Boolean(config.disableComment)
  accountForm.disableDuet = Boolean(config.disableDuet)
  accountForm.disableStitch = Boolean(config.disableStitch)
  accountForm.autoAddMusic = config.autoAddMusic !== false
  accountForm.videoCoverTimestampMs = config.videoCoverTimestampMs != null ? String(config.videoCoverTimestampMs) : ''
  accountForm.webhookUrlEnv = config.webhookUrlEnv || ''
  accountForm.patreonCampaignId = config.campaignId || ''
}

const openProfileDialog = () => {
  Object.assign(profileForm, {
    name: '',
    description: '',
    systemPrompt: '',
    watermark: '',
    contactDetails: '',
    ctaText: ''
  })
  profileDialogVisible.value = true
}

const submitProfileForm = async () => {
  if (!profileForm.name.trim()) {
    ElMessage.error('請輸入 Profile 名稱')
    return
  }

  try {
    const created = await profilesApi.create({
      name: profileForm.name,
      description: profileForm.description,
      settings: {
        systemPrompt: profileForm.systemPrompt,
        watermark: profileForm.watermark,
        contactDetails: profileForm.contactDetails,
        ctaText: profileForm.ctaText
      }
    })
    await profilesStore.refreshProfiles()
    profileDialogVisible.value = false
    selectedProfileFilter.value = String(created.data.id)
    ElMessage.success('Profile 建立成功')
  } catch (error) {
    console.error('建立 Profile 失敗:', error)
    ElMessage.error(error?.message || '建立 Profile 失敗')
  }
}

const fetchAccounts = async (validateLegacy = true) => {
  if (appStore.isAccountRefreshing) return
  appStore.setAccountRefreshing(true)

  try {
    const profiles = await profilesStore.refreshProfiles()
    const legacyResponse = validateLegacy
      ? await accountApi.getValidAccounts()
      : await accountApi.getAccounts()
    const legacyAccounts = legacyResponse?.data || []

    const structuredGroups = await Promise.all(
      profiles.map(async (profile) => {
        const items = await profilesStore.fetchAccountsForProfile(profile.id)
        return items.map((item) => ({ ...item, profileName: profile.name }))
      })
    )

    accountStore.setAccounts([...legacyAccounts, ...structuredGroups.flat()])
    if (validateLegacy) {
      ElMessage.success('帳號資料取得成功')
      if (appStore.isFirstTimeAccountManagement) {
        appStore.setAccountManagementVisited()
      }
    }
  } catch (error) {
    console.error('取得帳號資料失敗:', error)
    if (validateLegacy) {
      ElMessage.error('取得帳號資料失敗')
    }
  } finally {
    appStore.setAccountRefreshing(false)
  }
}

const refreshAccounts = () => fetchAccounts(true)

onMounted(() => {
  fetchAccounts(false)
  window.addEventListener('message', handleTikTokOauthMessage)
  setTimeout(() => {
    fetchAccounts(true)
  }, 100)
})

const resetTikTokHealth = () => {
  Object.assign(tiktokHealth, {
    accountId: null,
    lastRequest: null,
    lastCallback: null,
    lastRefresh: null,
    lastWebhook: null,
  })
}

const loadTikTokHealth = async (accountId = null) => {
  try {
    const response = await tiktokApi.getStatus(accountId)
    const data = response?.data || {}
    Object.assign(tiktokHealth, {
      accountId: data.accountId || accountId || null,
      lastRequest: data.lastRequest || null,
      lastCallback: data.lastCallback || null,
      lastRefresh: data.lastRefresh || null,
      lastWebhook: data.lastWebhook || null,
    })
  } catch (error) {
    console.error('載入 TikTok health 失敗:', error)
  }
}

const handleAddAccount = () => {
  dialogType.value = 'add'
  resetAccountForm()
  resetTikTokHealth()
  sseConnecting.value = false
  qrCodeData.value = ''
  loginStatus.value = ''
  dialogVisible.value = true
}

const handleEdit = (row) => {
  dialogType.value = 'edit'
  Object.assign(accountForm, makeEmptyAccountForm(), {
    id: row.id,
    profileId: row.profileId,
    name: row.name,
    platform: row.platformSlug || row.platform,
    authType: row.authType || 'cookie',
    enabled: row.enabled !== false,
    cookiePath: row.filePath || '',
    advancedConfigText: row.config && Object.keys(row.config).length > 0
      ? JSON.stringify(row.config, null, 2)
      : '',
    status: row.status
  })
  loadStructuredFieldsFromConfig(row.config || {})
  sseConnecting.value = false
  qrCodeData.value = ''
  loginStatus.value = ''
  dialogVisible.value = true
  if ((row.platformSlug || row.platform) === 'tiktok') {
    loadTikTokHealth(row.id)
  } else {
    resetTikTokHealth()
  }
}

const handleDelete = (row) => {
  ElMessageBox.confirm(`確定要刪除帳號 ${row.name} 嗎？`, '警告', {
    confirmButtonText: '確定',
    cancelButtonText: '取消',
    type: 'warning'
  })
    .then(async () => {
      try {
        const response = await accountApi.deleteAccount(row.id)
        if (response.code === 200) {
          accountStore.deleteAccount(row.id)
          ElMessage.success('刪除成功')
        }
      } catch (error) {
        console.error('刪除帳號失敗:', error)
        ElMessage.error('刪除帳號失敗')
      }
    })
    .catch(() => {})
}

const handleDownloadCookie = async (row) => {
  try {
    const response = await fetch(
      buildApiUrl(`/downloadCookie?filePath=${encodeURIComponent(row.filePath)}`),
      {
        headers: getToken() ? { Authorization: `Bearer ${getToken()}` } : {}
      }
    )
    if (!response.ok) {
      ElMessage.error(response.status === 401 ? '未授權，請重新登入' : '下載失敗')
      return
    }
    const blob = await response.blob()
    const objectUrl = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = objectUrl
    link.download = `${row.name}_cookie.json`
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    URL.revokeObjectURL(objectUrl)
  } catch (error) {
    console.error('下載 Cookie 失敗:', error)
    ElMessage.error('下載 Cookie 失敗')
  }
}

const handleUploadCookie = (row) => {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.json'
  input.style.display = 'none'
  document.body.appendChild(input)

  input.onchange = async (event) => {
    const file = event.target.files[0]
    if (!file) return
    if (!file.name.endsWith('.json')) {
      ElMessage.error('請選擇 JSON 格式的 Cookie 檔案')
      document.body.removeChild(input)
      return
    }

    try {
      const formData = new FormData()
      formData.append('file', file)
      formData.append('id', row.id)
      formData.append('platform', row.platformSlug || row.platform)
      await http.upload('/uploadCookie', formData)
      ElMessage.success('Cookie 檔案上傳成功')
      await refreshAccounts()
    } catch (error) {
      ElMessage.error('Cookie 檔案上傳失敗')
    } finally {
      document.body.removeChild(input)
    }
  }

  input.click()
}

const handleReLogin = (row) => {
  if (!row.supportsRelogin) {
    ElMessage.warning('此帳號不支援舊版 QR 重新登入，請改用 Cookie / OAuth 更新')
    return
  }

  dialogType.value = 'edit'
  Object.assign(accountForm, makeEmptyAccountForm(), {
    id: row.id,
    profileId: null,
    name: row.name,
    platform: row.platformSlug || row.platform,
    authType: 'cookie',
    enabled: true,
    cookiePath: row.filePath || '',
    status: row.status
  })

  sseConnecting.value = false
  qrCodeData.value = ''
  loginStatus.value = ''
  dialogVisible.value = true

  setTimeout(() => {
    connectSSE(accountForm.platform, accountForm.name)
  }, 300)
}

async function connectWithTikTok() {
  if (!accountForm.profileId) {
    ElMessage.warning('請先選擇 Profile，再使用 TikTok Connect')
    return
  }
  if (!accountForm.name.trim()) {
    ElMessage.warning('請先輸入帳號名稱')
    return
  }

  const popup = window.open('', 'tiktok-connect', 'width=560,height=760')
  if (!popup) {
    ElMessage.error('瀏覽器阻擋了彈出視窗，請允許 popup 後重試')
    return
  }
  popup.document.write('<p style="font-family: sans-serif; padding: 16px;">Redirecting to TikTok...</p>')

  try {
    const response = await tiktokApi.startOAuth({
      profileId: accountForm.profileId,
      accountId: accountForm.id,
      accountName: accountForm.name,
      scopes: ['user.info.basic', 'video.publish']
    })
    popup.location = response.data.authorizeUrl
  } catch (error) {
    popup.close()
    console.error('TikTok connect 啟動失敗:', error)
    ElMessage.error(error?.message || 'TikTok connect 啟動失敗')
  }
}

async function refreshTikTokToken() {
  if (!accountForm.id) {
    ElMessage.warning('請先儲存 TikTok 帳號，再刷新 token')
    return
  }
  try {
    const response = await profilesApi.refreshAccountToken(accountForm.id)
    const account = response?.data || {}
    const config = account.config || {}
    accountForm.accessToken = config.accessToken || accountForm.accessToken
    accountForm.refreshToken = config.refreshToken || accountForm.refreshToken
    accountForm.openId = config.openId || accountForm.openId
    accountForm.tiktokScope = config.scope || accountForm.tiktokScope
    accountForm.tiktokDisplayName = config.displayName || accountForm.tiktokDisplayName
    accountForm.tiktokAvatarUrl = config.avatarUrl || accountForm.tiktokAvatarUrl
    await loadTikTokHealth(accountForm.id)
    ElMessage.success('TikTok token 已刷新')
  } catch (error) {
    console.error('刷新 TikTok token 失敗:', error)
    ElMessage.error(error?.message || '刷新 TikTok token 失敗')
  }
}

function openTikTokReviewStatus() {
  if (accountForm.id) {
    router.push({ path: '/tiktok-review', query: { accountId: String(accountForm.id) } })
    return
  }
  router.push('/tiktok-review')
}

function handleTikTokOauthMessage(event) {
  const payload = event?.data
  if (!payload || payload.type !== 'sau:tiktok-oauth') return
  if (!payload.ok) {
    ElMessage.error(payload.error || 'TikTok 授權失敗')
    return
  }
  const data = payload.data || {}
  accountForm.accessToken = data.accessToken || ''
  accountForm.refreshToken = data.refreshToken || ''
  accountForm.openId = data.openId || ''
  accountForm.tiktokScope = data.scope || ''
  accountForm.tiktokDisplayName = data.displayName || ''
  accountForm.tiktokAvatarUrl = data.avatarUrl || ''
  loadTikTokHealth(accountForm.id || null)
  ElMessage.success('TikTok 已連線，可直接儲存帳號設定')
}

const closeSSEConnection = () => {
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }
}

const connectSSE = (platform, name) => {
  closeSSEConnection()
  sseConnecting.value = true
  qrCodeData.value = ''
  loginStatus.value = ''

  const type = String(getLegacyPlatformType(platform) || 1)
  const url = appendAuthQuery(buildApiUrl(`/login?type=${type}&id=${encodeURIComponent(name)}`))
  eventSource = new EventSource(url)

  eventSource.onmessage = (event) => {
    const data = event.data
    if (!qrCodeData.value && data.length > 100) {
      qrCodeData.value = data.startsWith('data:image') ? data : `data:image/png;base64,${data}`
    } else if (data === '200' || data === '500') {
      loginStatus.value = data
      if (data === '200') {
        setTimeout(() => {
          closeSSEConnection()
          setTimeout(() => {
            dialogVisible.value = false
            sseConnecting.value = false
            ElMessage.success(dialogType.value === 'edit' ? '重新登入成功' : '帳號新增成功')
            ElMessage({ type: 'info', message: '正在同步帳號資訊...', duration: 0 })
            refreshAccounts().then(() => {
              ElMessage.closeAll()
              ElMessage.success('帳號資訊已更新')
            })
          }, 1000)
        }, 1000)
      } else {
        closeSSEConnection()
        setTimeout(() => {
          sseConnecting.value = false
          qrCodeData.value = ''
          loginStatus.value = ''
        }, 2000)
      }
    }
  }

  eventSource.onerror = (error) => {
    console.error('SSE 連線錯誤:', error)
    ElMessage.error('連線伺服器失敗，請稍後再試')
    closeSSEConnection()
    sseConnecting.value = false
  }
}

const buildStructuredConfig = () => {
  let config = {}
  if (accountForm.advancedConfigText.trim()) {
    try {
      config = JSON.parse(accountForm.advancedConfigText)
    } catch (error) {
      throw new Error('進階 JSON 格式錯誤')
    }
  }

  assignIfValue(config, 'sheetPostPreset', accountForm.sheetPostPreset.trim())

  switch (accountForm.platform) {
    case 'reddit':
      assignIfValue(config, 'subreddits', splitListField(accountForm.subredditsText))
      assignIfValue(config, 'clientIdEnv', accountForm.clientIdEnv.trim())
      assignIfValue(config, 'clientSecretEnv', accountForm.clientSecretEnv.trim())
      assignIfValue(config, 'refreshTokenEnv', accountForm.refreshTokenEnv.trim())
      assignIfValue(config, 'userAgent', accountForm.userAgent.trim())
      break
    case 'telegram':
      assignIfValue(config, 'chatId', accountForm.chatId.trim())
      assignIfValue(config, 'botTokenEnv', accountForm.botTokenEnv.trim())
      assignIfValue(config, 'parseMode', accountForm.parseMode)
      if (accountForm.silent) config.silent = true
      if (accountForm.disableWebPreview) config.disableWebPreview = true
      break
    case 'youtube':
      assignIfValue(config, 'channelId', accountForm.channelId.trim())
      assignIfValue(config, 'privacyStatus', accountForm.privacyStatus)
      assignIfValue(config, 'playlistId', accountForm.playlistId.trim())
      assignIfValue(config, 'categoryId', accountForm.categoryId.trim())
      assignIfValue(config, 'clientIdEnv', accountForm.clientIdEnv.trim())
      assignIfValue(config, 'clientSecretEnv', accountForm.clientSecretEnv.trim())
      assignIfValue(config, 'refreshTokenEnv', accountForm.refreshTokenEnv.trim())
      break
    case 'facebook':
      assignIfValue(config, 'pageId', accountForm.pageId.trim())
      assignIfValue(config, 'accessTokenEnv', accountForm.accessTokenEnv.trim())
      break
    case 'instagram':
      assignIfValue(config, 'igUserId', accountForm.igUserId.trim())
      assignIfValue(config, 'accessTokenEnv', accountForm.accessTokenEnv.trim())
      break
    case 'threads':
      assignIfValue(config, 'userId', accountForm.threadUserId.trim())
      assignIfValue(config, 'accessTokenEnv', accountForm.accessTokenEnv.trim())
      break
    case 'tiktok':
      assignIfValue(config, 'publishMode', accountForm.publishMode)
      assignIfValue(config, 'accessToken', accountForm.accessToken.trim())
      assignIfValue(config, 'refreshToken', accountForm.refreshToken.trim())
      assignIfValue(config, 'openId', accountForm.openId.trim())
      assignIfValue(config, 'scope', accountForm.tiktokScope.trim())
      assignIfValue(config, 'displayName', accountForm.tiktokDisplayName.trim())
      assignIfValue(config, 'avatarUrl', accountForm.tiktokAvatarUrl.trim())
      assignIfValue(config, 'accessTokenEnv', accountForm.accessTokenEnv.trim())
      assignIfValue(config, 'privacyLevel', accountForm.privacyLevel)
      if (accountForm.disableComment) config.disableComment = true
      if (accountForm.disableDuet) config.disableDuet = true
      if (accountForm.disableStitch) config.disableStitch = true
      if (accountForm.autoAddMusic === false) config.autoAddMusic = false
      if (accountForm.videoCoverTimestampMs.trim()) config.videoCoverTimestampMs = Number(accountForm.videoCoverTimestampMs.trim())
      break
    case 'discord':
      assignIfValue(config, 'webhookUrlEnv', accountForm.webhookUrlEnv.trim())
      break
    case 'patreon':
      assignIfValue(config, 'campaignId', accountForm.patreonCampaignId.trim())
      break
    default:
      break
  }

  return config
}

const submitStructuredAccount = async () => {
  const payload = {
    profileId: accountForm.profileId,
    platform: accountForm.platform,
    accountName: accountForm.name,
    authType: accountForm.authType,
    enabled: accountForm.enabled,
    config: buildStructuredConfig()
  }
  if (accountForm.authType === 'cookie' && accountForm.cookiePath.trim()) {
    payload.cookiePath = accountForm.cookiePath.trim()
  }

  const validation = await profilesApi.validateAccountConfig({
    ...payload,
    performLiveChecks: true
  })
  const result = validation?.data || {}
  if (!result.valid) {
    throw new Error((result.errors || []).join('；') || '帳號設定驗證失敗')
  }
  if (Array.isArray(result.warnings) && result.warnings.length > 0) {
    ElMessage.warning(result.warnings.join('；'))
  }

  if (dialogType.value === 'add') {
    await profilesApi.createAccount(accountForm.profileId, payload)
  } else {
    await profilesApi.updateAccount(accountForm.id, payload)
  }
}

const submitLegacyAccount = async () => {
  const legacyType = getLegacyPlatformType(accountForm.platform)
  if (legacyType == null) {
    throw new Error('非舊版平台帳號必須先指定 Profile')
  }

  if (dialogType.value === 'add') {
    connectSSE(accountForm.platform, accountForm.name)
    return
  }

  await accountApi.updateAccount({
    id: accountForm.id,
    type: legacyType,
    userName: accountForm.name
  })
}

const submitAccountForm = () => {
  accountFormRef.value.validate(async (valid) => {
    if (!valid) return false

    try {
      if (isStructuredAccountForm.value) {
        await submitStructuredAccount()
        dialogVisible.value = false
        ElMessage.success(dialogType.value === 'add' ? '帳號新增成功' : '帳號更新成功')
        await refreshAccounts()
      } else {
        await submitLegacyAccount()
        if (dialogType.value === 'edit') {
          dialogVisible.value = false
          ElMessage.success('更新成功')
          await refreshAccounts()
        }
      }
    } catch (error) {
      console.error('提交帳號失敗:', error)
      ElMessage.error(error?.message || '提交帳號失敗')
    }
  })
}

onBeforeUnmount(() => {
  closeSSEConnection()
  window.removeEventListener('message', handleTikTokOauthMessage)
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

.account-management {
  .page-header {
    margin-bottom: 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;

    h1 {
      font-size: 24px;
      color: $text-primary;
      margin: 0;
    }

    .profile-toolbar {
      display: flex;
      align-items: center;
      gap: 12px;
    }
  }

  .account-tabs {
    background-color: #fff;
    border-radius: 4px;
    box-shadow: $box-shadow-light;

    .account-tabs-nav {
      padding: 20px;
    }
  }

  .field-hint,
  .legacy-login-hint {
    margin-top: 6px;
    color: #909399;
    font-size: 13px;
    line-height: 1.6;
  }

  .legacy-login-hint {
    margin-bottom: 12px;
    padding: 10px 12px;
    background: #f5f7fa;
    border-radius: 4px;
  }

  .tiktok-connect-row {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
  }

  .tiktok-connected-preview {
    display: flex;
    align-items: center;
    gap: 12px;

    .tiktok-connected-text {
      min-width: 0;
    }
  }

  .tiktok-health-card {
    width: 100%;
    background: #f5f7fa;
    border-radius: 6px;
    padding: 12px;

    .health-row {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      font-size: 13px;
      color: #606266;
      margin-bottom: 8px;

      &:last-child {
        margin-bottom: 0;
      }

      span {
        color: #909399;
      }

      strong {
        text-align: right;
        word-break: break-word;
      }
    }
  }

  .qrcode-container {
    margin-top: 20px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 250px;

    .qrcode-wrapper {
      text-align: center;

      .qrcode-tip {
        margin-bottom: 15px;
        color: #606266;
      }

      .qrcode-image {
        max-width: 200px;
        max-height: 200px;
        border: 1px solid #ebeef5;
        background-color: black;
      }
    }

    .loading-wrapper,
    .success-wrapper,
    .error-wrapper {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 10px;

      .el-icon {
        font-size: 48px;

        &.is-loading {
          animation: rotate 1s linear infinite;
        }
      }

      span {
        font-size: 16px;
      }
    }

    .success-wrapper .el-icon {
      color: #67c23a;
    }

    .error-wrapper .el-icon {
      color: #f56c6c;
    }
  }
}
</style>
