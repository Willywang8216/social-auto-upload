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
        <el-select v-model="selectedRiskFilter" style="width: 220px" placeholder="篩選風險">
          <el-option label="全部風險" value="all" />
          <el-option label="24h 內到期" value="expiring_24h" />
          <el-option label="7 天內到期" value="expiring_7d" />
          <el-option label="已逾期" value="overdue" />
          <el-option label="需要重連" value="reconnect_required" />
        </el-select>
        <el-select v-model="selectedSortMode" style="width: 200px" placeholder="排序方式">
          <el-option label="依風險" value="urgency" />
          <el-option label="依到期時間" value="expiry" />
          <el-option label="依平台" value="platform" />
          <el-option label="依 Profile" value="profile" />
          <el-option label="依名稱" value="name" />
        </el-select>
        <el-button plain :loading="maintenanceLoading" :disabled="bulkRefreshTargets.length < 1 && selectedProfileFilter === 'legacy'" @click="runMaintenanceSweep">維護刷新</el-button>
        <el-button type="primary" plain @click="openProfileDialog">新增 Profile</el-button>
      </div>
    </div>

    <div class="maintenance-banner">
      <span>Scheduler：{{ maintenanceStatus.enabled ? 'enabled' : 'disabled' }}</span>
      <span>Running：{{ maintenanceStatus.running ? 'yes' : 'no' }}</span>
      <span>Last run：{{ maintenanceStatus.lastFinishedAt || '—' }}</span>
      <span>Next run：{{ nextMaintenanceRunLabel }}</span>
      <span v-if="maintenanceStatus.lastResult">Refreshed：{{ maintenanceStatus.lastResult.refreshed || 0 }}</span>
      <span v-if="maintenanceStatus.lastError" class="maintenance-error">Last error：{{ maintenanceStatus.lastError }}</span>
    </div>

    <div class="account-tabs">
      <el-tabs v-model="activeTab" class="account-tabs-nav">
        <el-tab-pane label="全部" name="all">
          <AccountTabPane
            :accounts="filteredAccounts"
            :search-keyword="searchKeyword"
            :refreshing="appStore.isAccountRefreshing"
            :bulk-check-loading="bulkCheckLoading"
            :bulk-refresh-loading="bulkRefreshLoading"
            :bulk-check-count="bulkCheckTargets.length"
            :bulk-refresh-count="bulkRefreshTargets.length"
            :sort-mode="selectedSortMode"
            :sort-order="selectedSortOrder"
            empty-text="目前沒有帳號資料"
            @add="handleAddAccount"
            @edit="handleEdit"
            @delete="handleDelete"
            @download-cookie="handleDownloadCookie"
            @upload-cookie="handleUploadCookie"
            @refresh="refreshAccounts"
            @relogin="handleReLogin"
            @health-check="runRowHealthCheck"
            @bulk-check="runBulkHealthCheck"
            @bulk-refresh="runBulkRefresh"
            @search="onSearchChange"
            @sort-change="onTableSortChange"
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
            :bulk-check-loading="bulkCheckLoading"
            :bulk-refresh-loading="bulkRefreshLoading"
            :bulk-check-count="bulkCheckTargets.length"
            :bulk-refresh-count="bulkRefreshTargets.length"
            :sort-mode="selectedSortMode"
            :sort-order="selectedSortOrder"
            :empty-text="`目前沒有${platform.label}帳號資料`"
            @add="handleAddAccount"
            @edit="handleEdit"
            @delete="handleDelete"
            @download-cookie="handleDownloadCookie"
            @upload-cookie="handleUploadCookie"
            @refresh="refreshAccounts"
            @relogin="handleReLogin"
            @health-check="runRowHealthCheck"
            @bulk-check="runBulkHealthCheck"
            @bulk-refresh="runBulkRefresh"
            @search="onSearchChange"
            @sort-change="onTableSortChange"
          />
        </el-tab-pane>
      </el-tabs>
    </div>

    <div class="recent-account-events">
      <div class="section-header">
        <h2>最近帳號操作</h2>
        <el-button text @click="fetchRecentAccountEvents">重新載入</el-button>
      </div>
      <el-table :data="recentAccountEvents" style="width: 100%" v-loading="eventsLoading">
        <el-table-column prop="created_at" label="時間" width="180" />
        <el-table-column prop="platform" label="平台" width="120" />
        <el-table-column prop="account_name" label="帳號" min-width="180" />
        <el-table-column prop="action" label="操作" width="140" />
        <el-table-column label="結果" width="100">
          <template #default="scope">
            <el-tag :type="scope.row.status === 'ok' ? 'success' : 'danger'" effect="plain">{{ scope.row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="summary" label="摘要" min-width="260" />
      </el-table>
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
            <el-form-item label="OAuth health">
              <div class="oauth-health-card">
                <div class="health-row"><span>Username</span><strong>{{ accountForm.redditUserName || '—' }}</strong></div>
                <div class="health-row"><span>Access token</span><strong>{{ accountForm.accessToken ? 'present' : 'missing' }}</strong></div>
                <div class="health-row"><span>Token updated</span><strong>{{ accountForm.accessTokenUpdatedAt || '—' }}</strong></div>
                <div class="health-row"><span>Token expires</span><strong>{{ accountForm.accessTokenExpiresAt || '—' }}</strong></div>
                <div class="health-row"><span>Last manual refresh</span><strong>{{ accountForm.lastManualRefreshAt || '—' }}</strong></div>
              </div>
              <div class="oauth-actions-row">
                <el-button type="primary" plain @click="connectWithReddit" :disabled="!accountForm.id">Connect with Reddit</el-button>
                <el-button plain @click="refreshStructuredToken('reddit')" :disabled="!accountForm.id">Refresh Reddit token</el-button>
                <el-button plain @click="openOauthReviewStatus('reddit')" :disabled="!accountForm.id">Open OAuth status</el-button>
              </div>
            </el-form-item>
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
            <el-form-item label="Connection health">
              <div class="oauth-health-card">
                <div class="health-row"><span>Bot</span><strong>{{ accountForm.telegramBotName || '—' }}</strong></div>
                <div class="health-row"><span>Chat</span><strong>{{ accountForm.telegramChatTitle || '—' }}</strong></div>
                <div class="health-row"><span>Bot token</span><strong>{{ accountForm.botTokenEnv ? 'env-backed' : 'missing' }}</strong></div>
                <div class="health-row"><span>Last check</span><strong>{{ accountForm.lastConnectionCheckAt || '—' }}</strong></div>
              </div>
              <div class="oauth-actions-row">
                <el-button plain @click="checkStructuredConnection('telegram')" :disabled="!accountForm.id">Check Telegram connection</el-button>
              </div>
            </el-form-item>
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
            <el-form-item label="OAuth health">
              <div class="oauth-health-card">
                <div class="health-row"><span>Channel title</span><strong>{{ accountForm.channelTitle || '—' }}</strong></div>
                <div class="health-row"><span>Access token</span><strong>{{ accountForm.accessToken ? 'present' : 'missing' }}</strong></div>
                <div class="health-row"><span>Token updated</span><strong>{{ accountForm.accessTokenUpdatedAt || '—' }}</strong></div>
                <div class="health-row"><span>Token expires</span><strong>{{ accountForm.accessTokenExpiresAt || '—' }}</strong></div>
                <div class="health-row"><span>Last manual refresh</span><strong>{{ accountForm.lastManualRefreshAt || '—' }}</strong></div>
              </div>
              <div class="oauth-actions-row">
                <el-button type="primary" plain @click="connectWithYouTube" :disabled="!accountForm.id">Connect with YouTube</el-button>
                <el-button plain @click="refreshStructuredToken('youtube')" :disabled="!accountForm.id">Refresh YouTube token</el-button>
                <el-button plain @click="openOauthReviewStatus('youtube')" :disabled="!accountForm.id">Open OAuth status</el-button>
              </div>
            </el-form-item>
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
            <el-form-item label="Connection health">
              <div class="oauth-health-card">
                <div class="health-row"><span>Page name</span><strong>{{ accountForm.facebookPageName || '—' }}</strong></div>
                <div class="health-row"><span>Access token</span><strong>{{ accountForm.accessToken ? 'present' : (accountForm.accessTokenEnv ? 'env-backed' : 'missing') }}</strong></div>
                <div class="health-row"><span>Last check</span><strong>{{ accountForm.lastConnectionCheckAt || '—' }}</strong></div>
              </div>
              <div class="oauth-actions-row">
                <el-button type="primary" plain @click="connectWithMeta('facebook')" :disabled="!accountForm.id">Connect with Facebook</el-button>
                <el-button plain @click="refreshStructuredToken('facebook')" :disabled="!accountForm.id">Refresh Facebook token</el-button>
                <el-button plain @click="checkStructuredConnection('facebook')" :disabled="!accountForm.id">Check Facebook connection</el-button>
                <el-button plain @click="openOauthReviewStatus('facebook')" :disabled="!accountForm.id">Open OAuth status</el-button>
              </div>
            </el-form-item>
            <el-form-item label="Page ID">
              <el-input v-model="accountForm.pageId" />
            </el-form-item>
            <el-form-item label="Access Token Env">
              <el-input v-model="accountForm.accessTokenEnv" placeholder="例如：FB_PAGE_TOKEN" />
            </el-form-item>
          </template>

          <template v-else-if="accountForm.platform === 'instagram'">
            <el-divider content-position="left">Instagram 設定</el-divider>
            <el-form-item label="Connection health">
              <div class="oauth-health-card">
                <div class="health-row"><span>Username</span><strong>{{ accountForm.instagramUserName || '—' }}</strong></div>
                <div class="health-row"><span>Access token</span><strong>{{ accountForm.accessToken ? 'present' : (accountForm.accessTokenEnv ? 'env-backed' : 'missing') }}</strong></div>
                <div class="health-row"><span>Last check</span><strong>{{ accountForm.lastConnectionCheckAt || '—' }}</strong></div>
              </div>
              <div class="oauth-actions-row">
                <el-button type="primary" plain @click="connectWithMeta('instagram')" :disabled="!accountForm.id">Connect with Instagram</el-button>
                <el-button plain @click="refreshStructuredToken('instagram')" :disabled="!accountForm.id">Refresh Instagram token</el-button>
                <el-button plain @click="checkStructuredConnection('instagram')" :disabled="!accountForm.id">Check Instagram connection</el-button>
                <el-button plain @click="openOauthReviewStatus('instagram')" :disabled="!accountForm.id">Open OAuth status</el-button>
              </div>
            </el-form-item>
            <el-form-item label="IG User ID">
              <el-input v-model="accountForm.igUserId" />
            </el-form-item>
            <el-form-item label="Access Token Env">
              <el-input v-model="accountForm.accessTokenEnv" placeholder="例如：IG_ACCESS_TOKEN" />
            </el-form-item>
          </template>

          <template v-else-if="accountForm.platform === 'threads'">
            <el-divider content-position="left">Threads 設定</el-divider>
            <el-form-item label="Connection health">
              <div class="oauth-health-card">
                <div class="health-row"><span>Username</span><strong>{{ accountForm.threadsUserName || '—' }}</strong></div>
                <div class="health-row"><span>Access token</span><strong>{{ accountForm.accessToken ? 'present' : (accountForm.accessTokenEnv ? 'env-backed' : 'missing') }}</strong></div>
                <div class="health-row"><span>Last check</span><strong>{{ accountForm.lastConnectionCheckAt || '—' }}</strong></div>
              </div>
              <div class="oauth-actions-row">
                <el-button type="primary" plain @click="connectWithThreads" :disabled="!accountForm.id">Connect with Threads</el-button>
                <el-button plain @click="refreshStructuredToken('threads')" :disabled="!accountForm.id">Refresh Threads token</el-button>
                <el-button plain @click="checkStructuredConnection('threads')" :disabled="!accountForm.id">Check Threads connection</el-button>
                <el-button plain @click="openOauthReviewStatus('threads')" :disabled="!accountForm.id">Open OAuth status</el-button>
              </div>
            </el-form-item>
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
                <div class="health-row"><span>Token expires</span><strong>{{ accountForm.accessTokenExpiresAt || '—' }}</strong></div>
                <div class="health-row"><span>Refresh expires</span><strong>{{ accountForm.refreshTokenExpiresAt || '—' }}</strong></div>
                <div class="health-row"><span>Connected at</span><strong>{{ accountForm.connectedAt || '—' }}</strong></div>
                <div class="health-row"><span>Last token update</span><strong>{{ accountForm.accessTokenUpdatedAt || '—' }}</strong></div>
                <div class="health-row"><span>Last auto refresh</span><strong>{{ accountForm.lastAutoRefreshAt || '—' }}</strong></div>
                <div class="health-row"><span>Last manual refresh</span><strong>{{ accountForm.lastManualRefreshAt || '—' }}</strong></div>
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
            <el-form-item label="Connection health">
              <div class="oauth-health-card">
                <div class="health-row"><span>Webhook name</span><strong>{{ accountForm.discordWebhookName || '—' }}</strong></div>
                <div class="health-row"><span>Channel ID</span><strong>{{ accountForm.discordWebhookChannel || '—' }}</strong></div>
                <div class="health-row"><span>Webhook URL</span><strong>{{ accountForm.webhookUrlEnv ? 'env-backed' : 'missing' }}</strong></div>
                <div class="health-row"><span>Last check</span><strong>{{ accountForm.lastConnectionCheckAt || '—' }}</strong></div>
              </div>
              <div class="oauth-actions-row">
                <el-button plain @click="checkStructuredConnection('discord')" :disabled="!accountForm.id">Check Discord connection</el-button>
              </div>
            </el-form-item>
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
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { CircleCheckFilled, CircleCloseFilled, Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { accountApi } from '@/api/account'
import { metaApi } from '@/api/meta'
import { profilesApi } from '@/api/profiles'
import { redditApi } from '@/api/reddit'
import { threadsApi } from '@/api/threads'
import { tiktokApi } from '@/api/tiktok'
import { youtubeApi } from '@/api/youtube'
import AccountTabPane from '@/components/AccountTabPane.vue'
import { useAccountStore } from '@/stores/account'
import { useAppStore } from '@/stores/app'
import { useProfilesStore } from '@/stores/profiles'
import { buildApiUrl } from '@/utils/api-url'
import { appendAuthQuery, getToken } from '@/utils/auth'
import { http } from '@/utils/request'
import { PROFILE_PLATFORM_OPTIONS, getLegacyPlatformType } from '@/utils/platforms'

const router = useRouter()
const route = useRoute()
const accountStore = useAccountStore()
const appStore = useAppStore()
const profilesStore = useProfilesStore()

const activeTab = ref('all')
const searchKeyword = ref('')
const selectedProfileFilter = ref('all')
const selectedRiskFilter = ref('all')
const selectedSortMode = ref('urgency')
const selectedSortOrder = ref('ascending')

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
  accessTokenExpiresAt: '',
  refreshTokenExpiresAt: '',
  accessTokenUpdatedAt: '',
  connectedAt: '',
  lastManualRefreshAt: '',
  lastAutoRefreshAt: '',
  redditUserName: '',
  channelTitle: '',
  facebookPageName: '',
  instagramUserName: '',
  threadsUserName: '',
  telegramBotName: '',
  telegramChatTitle: '',
  discordWebhookName: '',
  discordWebhookChannel: '',
  lastConnectionCheckAt: '',
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
const recentAccountEvents = ref([])
const eventsLoading = ref(false)
const maintenanceLoading = ref(false)
const maintenanceStatus = ref({ enabled: false, running: false, lastFinishedAt: '', lastResult: null })
let eventSource = null

const filteredAccounts = computed(() => {
  const keyword = searchKeyword.value.trim().toLowerCase()
  const compareUrgency = (left, right) => {
    const leftRank = Number(left.urgencyRank ?? 99)
    const rightRank = Number(right.urgencyRank ?? 99)
    if (leftRank !== rightRank) return leftRank - rightRank

    const leftSeconds = left.secondsRemaining ?? Number.POSITIVE_INFINITY
    const rightSeconds = right.secondsRemaining ?? Number.POSITIVE_INFINITY
    if (leftSeconds !== rightSeconds) return leftSeconds - rightSeconds

    const leftProfile = String(left.profileName || '')
    const rightProfile = String(right.profileName || '')
    if (leftProfile !== rightProfile) return leftProfile.localeCompare(rightProfile)

    return String(left.name || '').localeCompare(String(right.name || ''))
  }

  const compareExpiry = (left, right) => {
    const leftSeconds = left.secondsRemaining ?? Number.POSITIVE_INFINITY
    const rightSeconds = right.secondsRemaining ?? Number.POSITIVE_INFINITY
    if (leftSeconds !== rightSeconds) return leftSeconds - rightSeconds
    return compareUrgency(left, right)
  }

  const comparePlatform = (left, right) => {
    const leftPlatform = String(left.platform || '')
    const rightPlatform = String(right.platform || '')
    if (leftPlatform !== rightPlatform) return leftPlatform.localeCompare(rightPlatform)
    return compareUrgency(left, right)
  }

  const compareProfile = (left, right) => {
    const leftProfile = String(left.profileName || '')
    const rightProfile = String(right.profileName || '')
    if (leftProfile !== rightProfile) return leftProfile.localeCompare(rightProfile)
    return compareUrgency(left, right)
  }

  const compareName = (left, right) => {
    const leftName = String(left.name || '')
    const rightName = String(right.name || '')
    if (leftName !== rightName) return leftName.localeCompare(rightName)
    return compareUrgency(left, right)
  }

  const comparatorByMode = {
    urgency: compareUrgency,
    expiry: compareExpiry,
    platform: comparePlatform,
    profile: compareProfile,
    name: compareName
  }

  const baseComparator = comparatorByMode[selectedSortMode.value] || compareUrgency
  const comparator = selectedSortOrder.value === 'descending'
    ? (left, right) => baseComparator(right, left)
    : baseComparator

  return accountStore.accounts
    .filter((account) => {
      if (selectedProfileFilter.value === 'legacy' && account.profileId != null) return false
      if (selectedProfileFilter.value !== 'all' && selectedProfileFilter.value !== 'legacy') {
        if (String(account.profileId) !== selectedProfileFilter.value) return false
      }

      if (selectedRiskFilter.value === 'expiring_24h' && !account.isExpiringWithin24h) return false
      if (selectedRiskFilter.value === 'expiring_7d' && !account.isExpiringWithin7d) return false
      if (selectedRiskFilter.value === 'overdue' && !account.isOverdue) return false
      if (selectedRiskFilter.value === 'reconnect_required' && !account.reconnectRequired) return false

      if (!keyword) return true
      return [account.name, account.platform, account.profileName]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(keyword))
    })
    .sort(comparator)
})

const getFilteredAccountsByPlatform = (platformLabel) =>
  filteredAccounts.value.filter((account) => account.platform === platformLabel)

const bulkCheckLoading = ref(false)
const bulkRefreshLoading = ref(false)

const currentVisibleAccounts = computed(() => {
  if (activeTab.value === 'all') return filteredAccounts.value
  const tab = accountPlatformTabs.find((item) => item.value === activeTab.value)
  if (!tab) return filteredAccounts.value
  return getFilteredAccountsByPlatform(tab.label)
})

const bulkCheckTargets = computed(() =>
  currentVisibleAccounts.value.filter((account) => account.supportsHealthAction && account.healthActionKind === 'check')
)

const bulkRefreshTargets = computed(() =>
  currentVisibleAccounts.value.filter((account) => account.supportsHealthAction && account.healthActionKind === 'refresh')
)

const nextMaintenanceRunLabel = computed(() => {
  const intervalSeconds = Number(maintenanceStatus.value?.intervalSeconds || 0)
  if (!maintenanceStatus.value?.enabled || intervalSeconds <= 0) return '—'
  if (maintenanceStatus.value?.running) return 'running now'
  const reference = maintenanceStatus.value?.lastFinishedAt || maintenanceStatus.value?.lastStartedAt
  if (!reference) return 'waiting for first run'
  const parsed = new Date(reference)
  if (Number.isNaN(parsed.getTime())) return '—'
  return new Date(parsed.getTime() + intervalSeconds * 1000).toISOString()
})

const applyRouteFilters = () => {
  const risk = Array.isArray(route.query.risk) ? route.query.risk[0] : route.query.risk
  const profile = Array.isArray(route.query.profile) ? route.query.profile[0] : route.query.profile
  const platform = Array.isArray(route.query.platform) ? route.query.platform[0] : route.query.platform
  const sort = Array.isArray(route.query.sort) ? route.query.sort[0] : route.query.sort
  const sortOrder = Array.isArray(route.query.sortOrder) ? route.query.sortOrder[0] : route.query.sortOrder

  const allowedRisk = new Set(['all', 'expiring_24h', 'expiring_7d', 'overdue', 'reconnect_required'])
  selectedRiskFilter.value = allowedRisk.has(String(risk || 'all')) ? String(risk || 'all') : 'all'

  if (profile === 'legacy' || profile === 'all') {
    selectedProfileFilter.value = String(profile)
  } else if (profile != null && profile !== '') {
    selectedProfileFilter.value = String(profile)
  } else {
    selectedProfileFilter.value = 'all'
  }

  const allowedPlatforms = new Set(['all', ...accountPlatformTabs.map((item) => item.value)])
  activeTab.value = allowedPlatforms.has(String(platform || 'all')) ? String(platform || 'all') : 'all'

  const allowedSort = new Set(['urgency', 'expiry', 'platform', 'profile', 'name'])
  selectedSortMode.value = allowedSort.has(String(sort || 'urgency')) ? String(sort || 'urgency') : 'urgency'
  const allowedSortOrder = new Set(['ascending', 'descending'])
  selectedSortOrder.value = allowedSortOrder.has(String(sortOrder || 'ascending')) ? String(sortOrder || 'ascending') : 'ascending'
}

const onSearchChange = (value) => {
  searchKeyword.value = value
}

const onTableSortChange = ({ mode, order }) => {
  selectedSortMode.value = mode || 'urgency'
  selectedSortOrder.value = order || 'ascending'
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
  accountForm.accessTokenExpiresAt = config.accessTokenExpiresAt || ''
  accountForm.refreshTokenExpiresAt = config.refreshTokenExpiresAt || ''
  accountForm.accessTokenUpdatedAt = config.accessTokenUpdatedAt || ''
  accountForm.connectedAt = config.connectedAt || ''
  accountForm.lastManualRefreshAt = config.lastManualRefreshAt || ''
  accountForm.lastAutoRefreshAt = config.lastAutoRefreshAt || ''
  accountForm.redditUserName = config.redditUserName || ''
  accountForm.channelTitle = config.channelTitle || ''
  accountForm.facebookPageName = config.facebookPageName || ''
  accountForm.instagramUserName = config.instagramUserName || ''
  accountForm.threadsUserName = config.threadsUserName || ''
  accountForm.telegramBotName = config.telegramBotName || ''
  accountForm.telegramChatTitle = config.telegramChatTitle || ''
  accountForm.discordWebhookName = config.discordWebhookName || ''
  accountForm.discordWebhookChannel = config.discordWebhookChannel || ''
  accountForm.lastConnectionCheckAt = config.lastConnectionCheckAt || ''
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

const fetchMaintenanceStatus = async () => {
  try {
    const response = await accountApi.getMaintenanceStatus()
    maintenanceStatus.value = response?.data || maintenanceStatus.value
  } catch (error) {
    console.error('取得維護狀態失敗:', error)
  }
}

const runMaintenanceSweep = async () => {
  if (maintenanceLoading.value) return
  const accountIds = bulkRefreshTargets.value.map((row) => row.id)
  if (accountIds.length < 1) {
    ElMessage.info('目前篩選範圍內沒有可維護的 refreshable 帳號')
    return
  }
  maintenanceLoading.value = true
  try {
    const payload = {
      accountIds,
      platforms: [...new Set(bulkRefreshTargets.value.map((row) => row.platformSlug))],
      maxAccounts: accountIds.length,
      expiringWithinSeconds: 3600,
    }
    if (selectedProfileFilter.value !== 'all' && selectedProfileFilter.value !== 'legacy') {
      payload.profileId = Number(selectedProfileFilter.value)
    }
    const response = await accountApi.runMaintenance(payload)
    const data = response?.data || {}
    for (const account of data.accounts || []) {
      accountStore.updateAccount(account.id, account)
    }
    await Promise.all([fetchRecentAccountEvents(), fetchMaintenanceStatus()])
    if ((data.refreshed || 0) > 0) {
      ElMessage.success(`維護刷新完成：${data.refreshed} 個已刷新，${data.skipped || 0} 個略過`)
    } else {
      ElMessage.info(`維護檢查完成：0 個需刷新，${data.skipped || 0} 個略過`)
    }
  } catch (error) {
    console.error('執行維護刷新失敗:', error)
    ElMessage.error(error?.message || '執行維護刷新失敗')
  } finally {
    maintenanceLoading.value = false
  }
}

const fetchRecentAccountEvents = async () => {
  eventsLoading.value = true
  try {
    const params = { limit: 20 }
    if (selectedProfileFilter.value !== 'all' && selectedProfileFilter.value !== 'legacy') {
      params.profileId = Number(selectedProfileFilter.value)
    }
    if (activeTab.value !== 'all') {
      params.platform = activeTab.value
    }
    const response = await accountApi.getRecentEvents(params)
    recentAccountEvents.value = response?.data || []
  } catch (error) {
    console.error('取得帳號操作紀錄失敗:', error)
  } finally {
    eventsLoading.value = false
  }
}

watch([activeTab, selectedProfileFilter], () => {
  fetchRecentAccountEvents()
})

watch(() => route.fullPath, () => {
  applyRouteFilters()
})

onMounted(() => {
  applyRouteFilters()
  fetchAccounts(false)
  fetchRecentAccountEvents()
  fetchMaintenanceStatus()
  window.addEventListener('message', handleTikTokOauthMessage)
  window.addEventListener('message', handleRedditOauthMessage)
  window.addEventListener('message', handleYouTubeOauthMessage)
  window.addEventListener('message', handleThreadsOauthMessage)
  window.addEventListener('message', handleMetaOauthMessage)
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

const executeHealthAction = async (row, { silent = false } = {}) => {
  if (!row || !row.id) return { ok: false, reason: 'missing_id', row }
  try {
    if (row.healthActionKind === 'refresh') {
      const response = await profilesApi.refreshAccountToken(row.id)
      accountStore.updateAccount(row.id, response?.data || row)
      if (!silent) {
        ElMessage.success(`${row.platform} token 已刷新`)
      }
      return { ok: true, kind: 'refresh', row, data: response?.data || row }
    }
    if (row.healthActionKind === 'check') {
      const response = await profilesApi.checkAccountConnection(row.id)
      accountStore.updateAccount(row.id, response?.data || row)
      if (!silent) {
        ElMessage.success(`${row.platform} connection checked`)
      }
      return { ok: true, kind: 'check', row, data: response?.data || row }
    }
    if (!silent) {
      ElMessage.info('此帳號目前沒有可執行的健康檢查')
    }
    return { ok: false, reason: 'unsupported', row }
  } catch (error) {
    console.error('執行帳號健康檢查失敗:', error)
    if (!silent) {
      ElMessage.error(error?.message || '執行帳號健康檢查失敗')
    }
    return { ok: false, reason: 'error', row, error }
  }
}

const runRowHealthCheck = async (row) => {
  await executeHealthAction(row)
}

const runBulkHealthCheck = async () => {
  if (bulkCheckTargets.value.length < 1 || bulkCheckLoading.value) return
  bulkCheckLoading.value = true
  try {
    const response = await profilesApi.batchCheckConnections(bulkCheckTargets.value.map((row) => row.id))
    const data = response?.data || {}
    for (const account of data.accounts || []) {
      accountStore.updateAccount(account.id, account)
    }
    if ((data.failed || 0) > 0) {
      ElMessage.warning(`批次檢查完成：${data.succeeded || 0} 個成功，${data.failed || 0} 個失敗`)
    } else {
      ElMessage.success(`批次檢查完成：${data.succeeded || 0} 個成功`)
    }
  } catch (error) {
    console.error('批次檢查失敗:', error)
    ElMessage.error(error?.message || '批次檢查失敗')
  } finally {
    bulkCheckLoading.value = false
  }
}

const runBulkRefresh = async () => {
  if (bulkRefreshTargets.value.length < 1 || bulkRefreshLoading.value) return
  bulkRefreshLoading.value = true
  try {
    const response = await profilesApi.batchRefreshTokens(bulkRefreshTargets.value.map((row) => row.id))
    const data = response?.data || {}
    for (const account of data.accounts || []) {
      accountStore.updateAccount(account.id, account)
    }
    if ((data.failed || 0) > 0) {
      ElMessage.warning(`批次刷新完成：${data.succeeded || 0} 個成功，${data.failed || 0} 個失敗`)
    } else {
      ElMessage.success(`批次刷新完成：${data.succeeded || 0} 個成功`)
    }
  } catch (error) {
    console.error('批次刷新失敗:', error)
    ElMessage.error(error?.message || '批次刷新失敗')
  } finally {
    bulkRefreshLoading.value = false
  }
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

async function connectWithMeta(expectedPlatform) {
  if (!accountForm.id) {
    ElMessage.warning(`請先儲存 ${expectedPlatform === 'facebook' ? 'Facebook' : 'Instagram'} 帳號，再使用 Connect`)
    return
  }
  if (!['facebook', 'instagram'].includes(expectedPlatform) || accountForm.platform !== expectedPlatform) {
    ElMessage.warning('目前帳號平台與 Connect 操作不符')
    return
  }
  const popup = window.open('', `meta-connect-${expectedPlatform}`, 'width=720,height=820')
  if (!popup) {
    ElMessage.error('瀏覽器阻擋了彈出視窗，請允許 popup 後重試')
    return
  }
  popup.document.write('<p style="font-family: sans-serif; padding: 16px;">Redirecting to Meta...</p>')
  try {
    const scopes = expectedPlatform === 'instagram'
      ? ['pages_show_list', 'instagram_basic', 'instagram_content_publish', 'business_management']
      : ['pages_show_list', 'pages_manage_posts', 'pages_read_engagement', 'pages_manage_metadata', 'business_management']
    const response = await metaApi.startOAuth({
      profileId: accountForm.profileId,
      accountId: accountForm.id,
      accountName: accountForm.name,
      scopes,
    })
    popup.location = response.data.authorizeUrl
  } catch (error) {
    popup.close()
    console.error('Meta connect 啟動失敗:', error)
    ElMessage.error(error?.message || 'Meta connect 啟動失敗')
  }
}

async function connectWithReddit() {
  if (!accountForm.id) {
    ElMessage.warning('請先儲存 Reddit 帳號，再使用 Connect with Reddit')
    return
  }
  const popup = window.open('', 'reddit-connect', 'width=720,height=820')
  if (!popup) {
    ElMessage.error('瀏覽器阻擋了彈出視窗，請允許 popup 後重試')
    return
  }
  popup.document.write('<p style="font-family: sans-serif; padding: 16px;">Redirecting to Reddit...</p>')
  try {
    const response = await redditApi.startOAuth({
      profileId: accountForm.profileId,
      accountId: accountForm.id,
      accountName: accountForm.name,
      scopes: ['identity', 'submit', 'read']
    })
    popup.location = response.data.authorizeUrl
  } catch (error) {
    popup.close()
    console.error('Reddit connect 啟動失敗:', error)
    ElMessage.error(error?.message || 'Reddit connect 啟動失敗')
  }
}

async function connectWithThreads() {
  if (!accountForm.id) {
    ElMessage.warning('請先儲存 Threads 帳號，再使用 Connect with Threads')
    return
  }
  const popup = window.open('', 'threads-connect', 'width=720,height=820')
  if (!popup) {
    ElMessage.error('瀏覽器阻擋了彈出視窗，請允許 popup 後重試')
    return
  }
  popup.document.write('<p style="font-family: sans-serif; padding: 16px;">Redirecting to Threads...</p>')
  try {
    const response = await threadsApi.startOAuth({
      profileId: accountForm.profileId,
      accountId: accountForm.id,
      accountName: accountForm.name,
      scopes: ['threads_basic', 'threads_content_publish']
    })
    popup.location = response.data.authorizeUrl
  } catch (error) {
    popup.close()
    console.error('Threads connect 啟動失敗:', error)
    ElMessage.error(error?.message || 'Threads connect 啟動失敗')
  }
}

async function connectWithYouTube() {
  if (!accountForm.id) {
    ElMessage.warning('請先儲存 YouTube 帳號，再使用 Connect with YouTube')
    return
  }
  const popup = window.open('', 'youtube-connect', 'width=720,height=820')
  if (!popup) {
    ElMessage.error('瀏覽器阻擋了彈出視窗，請允許 popup 後重試')
    return
  }
  popup.document.write('<p style="font-family: sans-serif; padding: 16px;">Redirecting to YouTube...</p>')
  try {
    const response = await youtubeApi.startOAuth({
      profileId: accountForm.profileId,
      accountId: accountForm.id,
      accountName: accountForm.name,
      scopes: ['https://www.googleapis.com/auth/youtube.upload', 'https://www.googleapis.com/auth/youtube.readonly']
    })
    popup.location = response.data.authorizeUrl
  } catch (error) {
    popup.close()
    console.error('YouTube connect 啟動失敗:', error)
    ElMessage.error(error?.message || 'YouTube connect 啟動失敗')
  }
}

async function checkStructuredConnection(expectedPlatform) {
  if (!accountForm.id) {
    ElMessage.warning('請先儲存帳號，再檢查連線')
    return
  }
  if (accountForm.platform !== expectedPlatform) {
    ElMessage.warning('目前帳號平台與檢查操作不符')
    return
  }
  try {
    const response = await profilesApi.checkAccountConnection(accountForm.id)
    const account = response?.data || {}
    const config = account.config || {}
    accountForm.facebookPageName = config.facebookPageName || accountForm.facebookPageName
    accountForm.instagramUserName = config.instagramUserName || accountForm.instagramUserName
    accountForm.threadsUserName = config.threadsUserName || accountForm.threadsUserName
    accountForm.telegramBotName = config.telegramBotName || accountForm.telegramBotName
    accountForm.telegramChatTitle = config.telegramChatTitle || accountForm.telegramChatTitle
    accountForm.discordWebhookName = config.discordWebhookName || accountForm.discordWebhookName
    accountForm.discordWebhookChannel = config.discordWebhookChannel || accountForm.discordWebhookChannel
    accountForm.lastConnectionCheckAt = config.lastConnectionCheckAt || accountForm.lastConnectionCheckAt
    ElMessage.success(`${account.platform} connection checked`)
  } catch (error) {
    console.error('檢查平台連線失敗:', error)
    ElMessage.error(error?.message || '檢查平台連線失敗')
  }
}

async function refreshStructuredToken(expectedPlatform) {
  if (!accountForm.id) {
    ElMessage.warning('請先儲存帳號，再刷新 token')
    return
  }
  if (accountForm.platform !== expectedPlatform) {
    ElMessage.warning('目前帳號平台與刷新操作不符')
    return
  }
  try {
    const response = await profilesApi.refreshAccountToken(accountForm.id)
    const account = response?.data || {}
    const config = account.config || {}
    accountForm.accessToken = config.accessToken || accountForm.accessToken
    accountForm.accessTokenExpiresAt = config.accessTokenExpiresAt || accountForm.accessTokenExpiresAt
    accountForm.accessTokenUpdatedAt = config.accessTokenUpdatedAt || accountForm.accessTokenUpdatedAt
    accountForm.lastManualRefreshAt = config.lastManualRefreshAt || accountForm.lastManualRefreshAt
    accountForm.redditUserName = config.redditUserName || accountForm.redditUserName
    accountForm.channelTitle = config.channelTitle || accountForm.channelTitle
    accountForm.threadUserId = config.threadUserId || accountForm.threadUserId
    accountForm.threadsUserName = config.threadsUserName || accountForm.threadsUserName
    accountForm.pageId = config.pageId || accountForm.pageId
    accountForm.facebookPageName = config.facebookPageName || accountForm.facebookPageName
    accountForm.igUserId = config.igUserId || accountForm.igUserId
    accountForm.instagramUserName = config.instagramUserName || accountForm.instagramUserName
    ElMessage.success(`${account.platform} token 已刷新`)
  } catch (error) {
    console.error('刷新平台 token 失敗:', error)
    ElMessage.error(error?.message || '刷新平台 token 失敗')
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
    accountForm.accessTokenExpiresAt = config.accessTokenExpiresAt || accountForm.accessTokenExpiresAt
    accountForm.refreshTokenExpiresAt = config.refreshTokenExpiresAt || accountForm.refreshTokenExpiresAt
    accountForm.accessTokenUpdatedAt = config.accessTokenUpdatedAt || accountForm.accessTokenUpdatedAt
    accountForm.connectedAt = config.connectedAt || accountForm.connectedAt
    accountForm.lastManualRefreshAt = config.lastManualRefreshAt || accountForm.lastManualRefreshAt
    accountForm.lastAutoRefreshAt = config.lastAutoRefreshAt || accountForm.lastAutoRefreshAt
    await loadTikTokHealth(accountForm.id)
    ElMessage.success('TikTok token 已刷新')
  } catch (error) {
    console.error('刷新 TikTok token 失敗:', error)
    ElMessage.error(error?.message || '刷新 TikTok token 失敗')
  }
}

function openOauthReviewStatus(platform) {
  const query = { platform }
  if (accountForm.id) {
    query.accountId = String(accountForm.id)
  }
  router.push({ path: `/oauth-review/${platform}`, query })
}

function openTikTokReviewStatus() {
  if (accountForm.id) {
    router.push({ path: '/tiktok-review', query: { accountId: String(accountForm.id) } })
    return
  }
  router.push('/tiktok-review')
}

function handleRedditOauthMessage(event) {
  const payload = event?.data
  if (!payload || payload.type !== 'sau:reddit-oauth') return
  if (!payload.ok) {
    ElMessage.error(payload.error || 'Reddit 授權失敗')
    return
  }
  const data = payload.data || {}
  accountForm.accessToken = data.accessToken || accountForm.accessToken
  accountForm.refreshToken = data.refreshToken || accountForm.refreshToken
  accountForm.redditUserName = data.redditUserName || accountForm.redditUserName
  accountForm.accessTokenExpiresAt = data.accessTokenExpiresAt || accountForm.accessTokenExpiresAt
  accountForm.accessTokenUpdatedAt = data.accessTokenUpdatedAt || accountForm.accessTokenUpdatedAt
  accountForm.connectedAt = data.connectedAt || accountForm.connectedAt
  ElMessage.success('Reddit 已連線，可直接儲存帳號設定')
}

function handleYouTubeOauthMessage(event) {
  const payload = event?.data
  if (!payload || payload.type !== 'sau:youtube-oauth') return
  if (!payload.ok) {
    ElMessage.error(payload.error || 'YouTube 授權失敗')
    return
  }
  const data = payload.data || {}
  accountForm.accessToken = data.accessToken || accountForm.accessToken
  accountForm.refreshToken = data.refreshToken || accountForm.refreshToken
  accountForm.channelId = data.channelId || accountForm.channelId
  accountForm.channelTitle = data.channelTitle || accountForm.channelTitle
  accountForm.accessTokenExpiresAt = data.accessTokenExpiresAt || accountForm.accessTokenExpiresAt
  accountForm.accessTokenUpdatedAt = data.accessTokenUpdatedAt || accountForm.accessTokenUpdatedAt
  accountForm.connectedAt = data.connectedAt || accountForm.connectedAt
  ElMessage.success('YouTube 已連線，可直接儲存帳號設定')
}

function handleThreadsOauthMessage(event) {
  const payload = event?.data
  if (!payload || payload.type !== 'sau:threads-oauth') return
  if (!payload.ok) {
    ElMessage.error(payload.error || 'Threads 授權失敗')
    return
  }
  const data = payload.data || {}
  accountForm.accessToken = data.accessToken || accountForm.accessToken
  accountForm.threadUserId = data.threadUserId || accountForm.threadUserId
  accountForm.threadsUserName = data.threadsUserName || accountForm.threadsUserName
  accountForm.accessTokenExpiresAt = data.accessTokenExpiresAt || accountForm.accessTokenExpiresAt
  accountForm.accessTokenUpdatedAt = data.accessTokenUpdatedAt || accountForm.accessTokenUpdatedAt
  accountForm.connectedAt = data.connectedAt || accountForm.connectedAt
  ElMessage.success('Threads 已連線，可直接儲存帳號設定')
}

function handleMetaOauthMessage(event) {
  const payload = event?.data
  if (!payload || payload.type !== 'sau:meta-oauth') return
  if (!payload.ok) {
    ElMessage.error(payload.error || 'Meta 授權失敗')
    return
  }
  const data = payload.data || {}
  accountForm.accessToken = data.accessToken || accountForm.accessToken
  accountForm.accessTokenUpdatedAt = data.accessTokenUpdatedAt || accountForm.accessTokenUpdatedAt
  accountForm.connectedAt = data.connectedAt || accountForm.connectedAt
  if (data.platform === 'facebook') {
    accountForm.pageId = data.pageId || accountForm.pageId
    accountForm.facebookPageName = data.facebookPageName || accountForm.facebookPageName
    ElMessage.success('Facebook 已連線，可直接儲存帳號設定')
    return
  }
  if (data.platform === 'instagram') {
    accountForm.pageId = data.pageId || accountForm.pageId
    accountForm.facebookPageName = data.facebookPageName || accountForm.facebookPageName
    accountForm.igUserId = data.igUserId || accountForm.igUserId
    accountForm.instagramUserName = data.instagramUserName || accountForm.instagramUserName
    ElMessage.success('Instagram 已連線，可直接儲存帳號設定')
  }
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
  accountForm.accessTokenExpiresAt = data.accessTokenExpiresAt || ''
  accountForm.refreshTokenExpiresAt = data.refreshTokenExpiresAt || ''
  accountForm.accessTokenUpdatedAt = data.accessTokenUpdatedAt || ''
  accountForm.connectedAt = data.connectedAt || accountForm.connectedAt
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
      assignIfValue(config, 'accessToken', accountForm.accessToken.trim())
      assignIfValue(config, 'refreshToken', accountForm.refreshToken.trim())
      assignIfValue(config, 'accessTokenExpiresAt', accountForm.accessTokenExpiresAt.trim())
      assignIfValue(config, 'accessTokenUpdatedAt', accountForm.accessTokenUpdatedAt.trim())
      assignIfValue(config, 'connectedAt', accountForm.connectedAt.trim())
      assignIfValue(config, 'redditUserName', accountForm.redditUserName.trim())
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
      assignIfValue(config, 'channelTitle', accountForm.channelTitle.trim())
      assignIfValue(config, 'privacyStatus', accountForm.privacyStatus)
      assignIfValue(config, 'playlistId', accountForm.playlistId.trim())
      assignIfValue(config, 'categoryId', accountForm.categoryId.trim())
      assignIfValue(config, 'clientIdEnv', accountForm.clientIdEnv.trim())
      assignIfValue(config, 'clientSecretEnv', accountForm.clientSecretEnv.trim())
      assignIfValue(config, 'refreshTokenEnv', accountForm.refreshTokenEnv.trim())
      assignIfValue(config, 'accessToken', accountForm.accessToken.trim())
      assignIfValue(config, 'refreshToken', accountForm.refreshToken.trim())
      assignIfValue(config, 'accessTokenExpiresAt', accountForm.accessTokenExpiresAt.trim())
      assignIfValue(config, 'accessTokenUpdatedAt', accountForm.accessTokenUpdatedAt.trim())
      assignIfValue(config, 'connectedAt', accountForm.connectedAt.trim())
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
  window.removeEventListener('message', handleRedditOauthMessage)
  window.removeEventListener('message', handleYouTubeOauthMessage)
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

  .maintenance-banner {
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    margin: 12px 0 20px;
    padding: 10px 12px;
    background: #f5f7fa;
    border-radius: 6px;
    color: $text-secondary;
    font-size: 13px;

    .maintenance-error {
      color: $danger-color;
      word-break: break-word;
    }
  }

  .recent-account-events {
    margin-top: 24px;

    .section-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;

      h2 {
        margin: 0;
        font-size: 18px;
        color: $text-primary;
      }
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

  .oauth-health-card,
  .tiktok-health-card {
    width: 100%;
    background: #f5f7fa;
    border-radius: 6px;
    padding: 12px;

    .health-row {
      display: flex;
      justify-content: space-between;
      gap: 12px;
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

  .oauth-actions-row {
    margin-top: 12px;
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
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
