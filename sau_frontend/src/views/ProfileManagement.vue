<template>
  <div class="profile-management">
    <div class="page-header">
      <h1>Profile 設定</h1>
    </div>

    <div class="profile-list-container">
      <div class="profile-toolbar">
        <el-input
          v-model="searchKeyword"
          placeholder="輸入 Profile 名稱搜尋"
          prefix-icon="Search"
          clearable
        />
        <div class="action-buttons">
          <el-button type="primary" @click="openCreateDialog">新增 Profile</el-button>
          <el-button type="info" plain @click="downloadExampleProfilesYaml">
            下載 Example YAML
          </el-button>
          <el-button type="primary" plain @click="triggerImportProfilesYaml" :loading="isImportingProfiles">
            匯入 YAML
          </el-button>
          <el-button type="success" plain @click="exportProfilesYaml">
            匯出 YAML
          </el-button>
          <el-button type="warning" plain @click="openBackupDialog">
            YAML 備份
          </el-button>
          <el-button type="warning" plain @click="openGoogleSheetDialog">Google 試算表連線</el-button>
          <el-button type="warning" plain @click="openDirectPublishersDialog">Direct Publishers</el-button>
          <el-button type="info" @click="fetchProfiles" :loading="isRefreshing">
            <el-icon :class="{ 'is-loading': isRefreshing }"><Refresh /></el-icon>
            <span>{{ isRefreshing ? '重新整理中' : '重新整理' }}</span>
          </el-button>
        </div>
      </div>

      <div v-if="filteredProfiles.length > 0" class="profile-list">
        <el-table :data="filteredProfiles" style="width: 100%">
          <el-table-column prop="name" label="Profile" min-width="180" />
          <el-table-column label="綁定帳號" min-width="220">
            <template #default="scope">
              <div class="account-tags">
                <el-tag
                  v-for="accountId in scope.row.accountIds"
                  :key="accountId"
                  class="account-tag"
                >
                  {{ getAccountName(accountId) }}
                </el-tag>
                <span v-if="scope.row.accountIds.length === 0" class="muted-text">尚未綁定</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="OneDrive / 儲存" min-width="220">
            <template #default="scope">
              <div class="cell-lines">
                <div>{{ scope.row.settings?.storage?.remoteName || '-' }}</div>
                <div class="muted-text">{{ scope.row.settings?.storage?.remotePath || '-' }}</div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="Google 試算表" min-width="220">
            <template #default="scope">
              <div class="cell-lines">
                <div>{{ scope.row.settings?.googleSheet?.spreadsheetId || '-' }}</div>
                <div class="muted-text">匯出時自動建立：日期-Profile 名稱</div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="320">
            <template #default="scope">
              <el-button size="small" @click="openEditDialog(scope.row)">編輯</el-button>
              <el-button size="small" type="success" @click="openGenerateDialog(scope.row)">批次產生</el-button>
              <el-button size="small" type="danger" @click="handleDelete(scope.row)">刪除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div v-else class="empty-data">
        <el-empty description="目前沒有 Profile 資料" />
      </div>
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="dialogType === 'create' ? '新增 Profile' : '編輯 Profile'"
      width="900px"
      class="profile-dialog"
    >
      <el-form :model="profileForm" label-width="140px">
        <el-form-item label="Profile 名稱">
          <el-input v-model="profileForm.name" placeholder="例如：運動品牌-主帳號群組" />
        </el-form-item>

        <el-form-item label="預設帳號群組">
          <el-select
            v-model="profileForm.accountIds"
            multiple
            filterable
            placeholder="選擇這個 Profile 預設要用的帳號"
            style="width: 100%"
          >
            <el-option
              v-for="account in accountStore.accounts"
              :key="account.id"
              :label="`${account.name}（${account.platform}）`"
              :value="account.id"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="系統提示詞">
          <el-input
            v-model="profileForm.systemPrompt"
            type="textarea"
            :rows="4"
            placeholder="定義這個 Profile 的寫作風格、語氣、禁忌、目標受眾與輸出格式"
          />
        </el-form-item>

        <el-form-item label="聯絡資訊">
          <el-input
            v-model="profileForm.contactDetails"
            type="textarea"
            :rows="2"
            placeholder="例如：Telegram、Email、Website"
          />
        </el-form-item>

        <el-form-item label="CTA / 行動呼籲">
          <el-input
            v-model="profileForm.cta"
            type="textarea"
            :rows="2"
            placeholder="例如：追蹤、加入 Patreon、私訊合作"
          />
        </el-form-item>

        <el-divider>內容帳號客製化</el-divider>

        <el-form-item label="內容帳號設定">
          <div class="content-account-config">
            <div class="content-account-toolbar">
              <div class="muted-text">
                這裡可以建立每個 Profile 自己的內容帳號，例如兩個 X 帳號、兩個 Facebook 帳號。每個帳號都能有獨立提示詞、聯絡資訊、CTA 與 Post Preset。
              </div>
              <el-button type="primary" plain @click="addContentAccount">新增內容帳號</el-button>
            </div>

            <div v-if="profileForm.settings.contentAccounts.length > 0" class="content-account-list">
              <div
                v-for="(contentAccount, index) in profileForm.settings.contentAccounts"
                :key="contentAccount.id"
                class="content-account-card"
              >
                <div class="content-account-card-header">
                  <strong>內容帳號 {{ index + 1 }}</strong>
                  <el-button type="danger" link @click="removeContentAccount(index)">刪除</el-button>
                </div>

                <el-form-item label="平台" label-width="120px">
                  <el-select v-model="contentAccount.platform" style="width: 100%">
                    <el-option
                      v-for="option in contentAccountPlatformOptions"
                      :key="option.value"
                      :label="option.label"
                      :value="option.value"
                    />
                  </el-select>
                </el-form-item>

                <el-form-item label="帳號名稱" label-width="120px">
                  <el-input v-model="contentAccount.name" placeholder="例如：光光 X 主帳" />
                </el-form-item>

                <el-form-item label="專屬提示詞" label-width="120px">
                  <el-input
                    v-model="contentAccount.prompt"
                    type="textarea"
                    :rows="3"
                    placeholder="只套用在這個內容帳號，例如語氣、禁忌、用字與受眾要求"
                  />
                </el-form-item>

                <el-form-item label="聯絡資訊覆寫" label-width="120px">
                  <el-input
                    v-model="contentAccount.contactDetails"
                    placeholder="留空則沿用 Profile 的聯絡資訊"
                  />
                </el-form-item>

                <el-form-item label="CTA 覆寫" label-width="120px">
                  <el-input
                    v-model="contentAccount.cta"
                    placeholder="留空則沿用 Profile 的 CTA"
                  />
                </el-form-item>

                <el-form-item label="Post Preset" label-width="120px">
                  <el-input
                    v-model="contentAccount.postPreset"
                    placeholder="匯出 Google Sheet 時，此帳號要套用的 Post Preset"
                  />
                </el-form-item>

                <el-form-item
                  v-if="supportsDirectPublisherTarget(contentAccount.platform)"
                  label="Direct Target"
                  label-width="120px"
                >
                  <el-select
                    v-model="contentAccount.publisherTargetId"
                    clearable
                    filterable
                    placeholder="選擇對應的直發 target"
                    style="width: 100%"
                  >
                    <el-option
                      v-for="target in getAvailableDirectPublisherTargets(contentAccount.platform)"
                      :key="target.id"
                      :label="target.name"
                      :value="target.id"
                    />
                  </el-select>
                  <div class="muted-text">
                    可在上方「Direct Publishers」管理 Telegram / Discord / Reddit / X 的直發目標。
                  </div>
                </el-form-item>
              </div>
            </div>

            <div v-else class="muted-text">
              尚未建立內容帳號。若要讓同一個 Profile 底下有兩個 X、兩個 Facebook 等不同帳號，請在這裡新增。
            </div>
          </div>
        </el-form-item>

        <el-divider>LLM 與轉錄</el-divider>

        <el-form-item label="API Base URL">
          <el-input v-model="profileForm.settings.llm.apiBaseUrl" placeholder="https://llmapi.iamwillywang.com/" />
        </el-form-item>

        <el-form-item label="轉錄模型">
          <el-input v-model="profileForm.settings.llm.transcriptionModel" placeholder="Audio-Speech-Group" />
        </el-form-item>

        <el-form-item label="文案生成模型">
          <el-input v-model="profileForm.settings.llm.generationModel" placeholder="reasoning / Multimodal-Generation-Groups" />
        </el-form-item>

        <el-divider>素材同步與 OneDrive</el-divider>

        <el-form-item label="Rclone Remote">
          <el-input v-model="profileForm.settings.storage.remoteName" placeholder="Onedrive-Yahooforsub-Tao" />
        </el-form-item>

        <el-form-item label="Remote Path">
          <el-input v-model="profileForm.settings.storage.remotePath" placeholder="Scripts-ssh-ssl-keys/SocialUpload" />
        </el-form-item>

        <el-form-item label="公開網址範本">
          <el-input
            v-model="profileForm.settings.storage.publicUrlTemplate"
            placeholder="選填，例如：https://cdn.example.com/{relative_path}"
          />
        </el-form-item>

        <el-divider>浮水印</el-divider>

        <el-form-item label="啟用浮水印">
          <el-switch v-model="profileForm.settings.watermark.enabled" />
        </el-form-item>

        <el-form-item label="模板名稱">
          <el-input
            v-model="profileForm.settings.watermark.templateName"
            placeholder="例如：主品牌斜向網格"
          />
        </el-form-item>

        <el-form-item label="浮水印類型">
          <el-radio-group v-model="profileForm.settings.watermark.type">
            <el-radio label="text">文字</el-radio>
            <el-radio label="image">圖片</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item label="浮水印模式">
          <el-radio-group v-model="profileForm.settings.watermark.mode">
            <el-radio label="static">固定</el-radio>
            <el-radio label="dynamic">隨機</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item label="模式說明">
          <div class="muted-text">
            固定模式會套用同一版面；隨機模式會對圖片與影片切換不同偏移版本。文字型可使用重複斜線模板；圖片型仍採單一圖像浮水印。
          </div>
        </el-form-item>

        <el-form-item label="浮水印文字" v-if="profileForm.settings.watermark.type === 'text'">
          <el-input v-model="profileForm.settings.watermark.text" placeholder="例如：@brandname" />
        </el-form-item>

        <el-form-item label="浮水印圖片路徑" v-else>
          <el-input v-model="profileForm.settings.watermark.imagePath" placeholder="本機可存取路徑，例如：C:/logo.png" />
        </el-form-item>

        <el-form-item label="模板樣式" v-if="profileForm.settings.watermark.type === 'text'">
          <el-radio-group v-model="profileForm.settings.watermark.pattern">
            <el-radio label="single">單一位置</el-radio>
            <el-radio label="repeat-slanted">重複斜線</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item
          label="重複行數"
          v-if="profileForm.settings.watermark.type === 'text' && profileForm.settings.watermark.pattern === 'repeat-slanted'"
        >
          <el-select v-model="profileForm.settings.watermark.repeatLines" style="width: 100%">
            <el-option label="2 行" :value="2" />
            <el-option label="3 行" :value="3" />
            <el-option label="4 行" :value="4" />
            <el-option label="5 行" :value="5" />
          </el-select>
        </el-form-item>

        <el-form-item
          label="斜角"
          v-if="profileForm.settings.watermark.type === 'text' && profileForm.settings.watermark.pattern === 'repeat-slanted'"
        >
          <el-slider
            v-model="profileForm.settings.watermark.angle"
            :min="-80"
            :max="80"
            :step="1"
            show-input
          />
        </el-form-item>

        <el-form-item
          label="間距"
          v-if="profileForm.settings.watermark.type === 'text' && profileForm.settings.watermark.pattern === 'repeat-slanted'"
        >
          <el-slider
            v-model="profileForm.settings.watermark.spacing"
            :min="40"
            :max="600"
            :step="10"
            show-input
          />
        </el-form-item>

        <el-form-item label="字體大小" v-if="profileForm.settings.watermark.type === 'text'">
          <el-slider
            v-model="profileForm.settings.watermark.fontSize"
            :min="12"
            :max="80"
            :step="1"
            show-input
          />
        </el-form-item>

        <el-form-item label="字體顏色" v-if="profileForm.settings.watermark.type === 'text'">
          <el-color-picker v-model="profileForm.settings.watermark.color" />
        </el-form-item>

        <el-form-item
          label="預設位置"
          v-if="profileForm.settings.watermark.type === 'image' || profileForm.settings.watermark.pattern === 'single'"
        >
          <el-select v-model="profileForm.settings.watermark.position" style="width: 100%">
            <el-option label="右下角" value="bottom-right" />
            <el-option label="左下角" value="bottom-left" />
            <el-option label="右上角" value="top-right" />
            <el-option label="左上角" value="top-left" />
            <el-option label="居中" value="center" />
          </el-select>
        </el-form-item>

        <el-form-item label="透明度">
          <el-slider v-model="profileForm.settings.watermark.opacity" :min="0.1" :max="1" :step="0.05" show-input />
        </el-form-item>

        <el-form-item label="即時預覽" v-if="profileForm.settings.watermark.enabled">
          <div class="watermark-preview">
            <div class="preview-canvas">
              <div
                v-if="profileForm.settings.watermark.type === 'text' && profileForm.settings.watermark.pattern === 'repeat-slanted'"
                class="preview-repeat-layer"
                :style="watermarkPreviewLayerStyle"
              >
                <div
                  v-for="tile in watermarkPreviewTileArray"
                  :key="tile"
                  class="preview-repeat-tile"
                >
                  <div
                    v-for="line in watermarkPreviewLineArray"
                    :key="line"
                    class="preview-repeat-line"
                  >
                    {{ watermarkPreviewText }}
                  </div>
                </div>
              </div>

              <div
                v-else
                class="preview-single-mark"
                :style="watermarkSinglePreviewStyle"
              >
                {{ profileForm.settings.watermark.type === 'image' ? 'LOGO' : watermarkPreviewText }}
              </div>
            </div>

            <div class="muted-text">
              預覽只用來幫你感受密度、角度與透明度。實際輸出仍會依圖片 / 影片尺寸與動態模式略作調整。
            </div>
          </div>
        </el-form-item>

        <el-divider>Google 試算表</el-divider>

        <el-form-item label="Spreadsheet ID">
          <el-input v-model="profileForm.settings.googleSheet.spreadsheetId" placeholder="Google Sheet ID" />
        </el-form-item>

        <el-form-item label="工作表命名規則">
          <el-alert
            title="匯出時會自動建立「YYYY-MM-DD-Profile 名稱」工作表"
            type="info"
            :closable="false"
            show-icon
          />
        </el-form-item>

        <el-divider>CSV / Import 預設值</el-divider>

        <el-form-item label="預設連結">
          <el-input v-model="profileForm.settings.socialImport.defaultLink" placeholder="https://example.com" />
        </el-form-item>

        <el-form-item label="Category">
          <el-input v-model="profileForm.settings.socialImport.category" placeholder="選填" />
        </el-form-item>

        <el-form-item label="Watermark 名稱">
          <el-input v-model="profileForm.settings.socialImport.watermarkName" placeholder="例如：Default" />
        </el-form-item>

        <el-form-item label="Hashtag Group">
          <el-input v-model="profileForm.settings.socialImport.hashtagGroup" placeholder="排程工具中既有的 Hashtag Group 名稱" />
        </el-form-item>

        <el-form-item label="CTA Group">
          <el-input v-model="profileForm.settings.socialImport.ctaGroup" placeholder="排程工具中既有的 CTA Group 名稱" />
        </el-form-item>

        <el-form-item label="首則留言">
          <el-input v-model="profileForm.settings.socialImport.firstComment" placeholder="Facebook / Instagram / LinkedIn / Bluesky / Threads 首則留言" />
        </el-form-item>

        <el-form-item label="Story">
          <el-switch v-model="profileForm.settings.socialImport.story" />
        </el-form-item>

        <el-form-item label="Pinterest Board">
          <el-input v-model="profileForm.settings.socialImport.pinterestBoard" placeholder="選填" />
        </el-form-item>

        <el-form-item label="Alt Text">
          <el-input v-model="profileForm.settings.socialImport.altText" type="textarea" :rows="2" />
        </el-form-item>

        <el-divider>Post Preset</el-divider>

        <el-form-item label="X / Twitter Preset">
          <el-input v-model="profileForm.settings.postPresets.twitter" />
        </el-form-item>

        <el-form-item label="Threads Preset">
          <el-input v-model="profileForm.settings.postPresets.threads" />
        </el-form-item>

        <el-form-item label="Instagram Preset">
          <el-input v-model="profileForm.settings.postPresets.instagram" />
        </el-form-item>

        <el-form-item label="Facebook Preset">
          <el-input v-model="profileForm.settings.postPresets.facebook" />
        </el-form-item>

        <el-form-item label="YouTube Preset">
          <el-input v-model="profileForm.settings.postPresets.youtube" />
        </el-form-item>

        <el-form-item label="TikTok Preset">
          <el-input v-model="profileForm.settings.postPresets.tiktok" />
        </el-form-item>
      </el-form>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="dialogVisible = false">取消</el-button>
          <el-button type="primary" @click="submitProfile" :loading="isSubmitting">
            {{ isSubmitting ? '儲存中' : '儲存' }}
          </el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog
      v-model="googleSheetDialogVisible"
      title="Google 試算表連線設定"
      width="900px"
    >
      <el-alert
        :title="googleSheetConfig.configured ? '已找到可用的 Google service account 設定' : '尚未設定 Google service account'"
        :type="googleSheetConfig.configured ? 'success' : 'warning'"
        :closable="false"
        show-icon
      />

      <div class="result-block">
        <h3>目前狀態</h3>
        <div class="cell-lines">
          <div>來源：{{ buildGoogleSheetSourceLabel(googleSheetConfig.source) }}</div>
          <div>Service Account Email：{{ googleSheetConfig.clientEmail || '尚未設定' }}</div>
          <div>Project ID：{{ googleSheetConfig.projectId || '尚未設定' }}</div>
          <div class="muted-text">若使用環境變數，會優先於此頁面儲存的檔案設定。</div>
        </div>
      </div>

      <el-form :model="googleSheetForm" label-width="180px" class="google-sheet-form">
        <el-form-item label="Service Account JSON">
          <el-input
            v-model="googleSheetForm.serviceAccountJson"
            type="textarea"
            :rows="12"
            placeholder="貼上完整 Google service account JSON"
          />
        </el-form-item>

        <el-form-item label="測試 Spreadsheet ID">
          <el-input
            v-model="googleSheetForm.spreadsheetId"
            placeholder="貼上 Google Sheet URL 中 /d/ 與 /edit 之間那段 ID"
          />
        </el-form-item>
      </el-form>

      <div v-if="googleSheetValidationResult" class="result-block">
        <h3>驗證結果</h3>
        <el-alert
          :title="`已連線到「${googleSheetValidationResult.title}」`"
          type="success"
          :closable="false"
          show-icon
        />
        <div class="cell-lines validation-details">
          <div>Spreadsheet ID：{{ googleSheetValidationResult.spreadsheetId }}</div>
          <div>可見工作表數：{{ googleSheetValidationResult.worksheetCount }}</div>
          <div>工作表：{{ (googleSheetValidationResult.worksheets || []).join('、') || '無' }}</div>
        </div>
      </div>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="googleSheetDialogVisible = false">關閉</el-button>
          <el-button @click="validateGoogleSheetConfig" :loading="isValidatingGoogleSheet">測試連線</el-button>
          <el-button type="primary" @click="saveGoogleSheetConfig" :loading="isSavingGoogleSheet">
            儲存設定
          </el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog
      v-model="directPublishersDialogVisible"
      title="Direct Publishers 設定"
      width="900px"
    >
      <el-alert
        title="這裡管理 Telegram / Discord / Reddit / X 的直發目標。內容帳號會用 target id 綁到這裡的設定。"
        type="info"
        :closable="false"
        show-icon
      />

      <div class="content-account-toolbar direct-target-toolbar">
        <div class="muted-text">
          Telegram 使用 Bot Token + Chat ID；Discord 使用 Webhook；Reddit 使用 refresh token；X 使用 API key + access token。
        </div>
        <el-button type="primary" plain @click="addDirectPublisherTarget">新增 target</el-button>
      </div>

      <div v-if="directPublishersForm.targets.length" class="content-account-list">
        <div
          v-for="(target, index) in directPublishersForm.targets"
          :key="target.id"
          class="content-account-card"
        >
          <div class="content-account-card-header">
            <strong>Target {{ index + 1 }}</strong>
            <el-button type="danger" link @click="removeDirectPublisherTarget(index)">刪除</el-button>
          </div>

          <el-form :model="target" label-width="120px">
            <el-form-item label="平台">
              <el-select v-model="target.platform" style="width: 100%" @change="handleDirectPublisherPlatformChange(target)">
                <el-option label="Telegram" value="telegram" />
                <el-option label="Discord" value="discord" />
                <el-option label="Reddit" value="reddit" />
                <el-option label="X / Twitter" value="twitter" />
              </el-select>
            </el-form-item>

            <el-form-item label="名稱">
              <el-input v-model="target.name" placeholder="例如：主 Telegram 頻道 / X 主帳" />
            </el-form-item>

            <el-form-item label="啟用">
              <el-switch v-model="target.enabled" />
            </el-form-item>

            <template v-if="target.platform === 'telegram'">
              <el-form-item label="Bot Token">
                <el-input v-model="target.config.botToken" type="password" show-password />
              </el-form-item>
              <el-form-item label="Chat ID">
                <el-input v-model="target.config.chatId" placeholder="@channel 或 -100..." />
              </el-form-item>
            </template>

            <template v-else-if="target.platform === 'discord'">
              <el-form-item label="Webhook URL">
                <el-input v-model="target.config.webhookUrl" type="password" show-password />
              </el-form-item>
              <el-form-item label="Webhook 名稱">
                <el-input v-model="target.config.username" placeholder="留空則沿用 Discord 預設 webhook 名稱" />
              </el-form-item>
            </template>

            <template v-else-if="target.platform === 'reddit'">
              <el-form-item label="Client ID">
                <el-input v-model="target.config.clientId" type="password" show-password />
              </el-form-item>
              <el-form-item label="Client Secret">
                <el-input v-model="target.config.clientSecret" type="password" show-password />
              </el-form-item>
              <el-form-item label="Refresh Token">
                <el-input v-model="target.config.refreshToken" type="password" show-password />
              </el-form-item>
              <el-form-item label="Subreddit">
                <el-input v-model="target.config.subreddit" placeholder="例如：mysubreddit" />
              </el-form-item>
            </template>

            <template v-else-if="target.platform === 'twitter'">
              <el-form-item label="API Key">
                <el-input v-model="target.config.apiKey" type="password" show-password />
              </el-form-item>
              <el-form-item label="API Key Secret">
                <el-input v-model="target.config.apiKeySecret" type="password" show-password />
              </el-form-item>
              <el-form-item label="Access Token">
                <el-input v-model="target.config.accessToken" type="password" show-password />
              </el-form-item>
              <el-form-item label="Access Token Secret">
                <el-input v-model="target.config.accessTokenSecret" type="password" show-password />
              </el-form-item>
            </template>
          </el-form>
        </div>
      </div>

      <div v-else class="muted-text">
        尚未設定任何 direct publisher target。
      </div>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="directPublishersDialogVisible = false">取消</el-button>
          <el-button type="primary" @click="submitSaveDirectPublishersConfig" :loading="isSavingDirectPublishers">
            {{ isSavingDirectPublishers ? '儲存中' : '儲存設定' }}
          </el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog
      v-model="generateDialogVisible"
      title="批次產生文案並匯出 Google 試算表"
      width="900px"
      class="generate-dialog"
    >
      <el-form :model="generateForm" label-width="140px">
        <el-form-item label="Profile">
          <el-input :model-value="currentProfile?.name || ''" disabled />
        </el-form-item>

        <el-form-item label="素材">
          <el-select
            v-model="generateForm.materialIds"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            placeholder="可一次選擇多個圖片或影片素材"
            style="width: 100%"
          >
            <el-option
              v-for="material in materials"
              :key="material.id"
              :label="material.filename"
              :value="material.id"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="本次發送帳號">
          <el-checkbox-group v-model="generateForm.selectedAccountIds" class="generate-account-list">
            <el-checkbox
              v-for="account in currentProfileAccounts"
              :key="account.id"
              :label="account.id"
            >
              {{ account.name }}（{{ account.platform }}）
            </el-checkbox>
          </el-checkbox-group>
          <div v-if="currentProfileAccounts.length === 0" class="muted-text">
            這個 Profile 目前沒有綁定任何帳號。
          </div>
        </el-form-item>

        <el-form-item label="本次內容帳號">
          <el-checkbox-group v-model="generateForm.selectedContentAccountIds" class="generate-account-list">
            <el-checkbox
              v-for="contentAccount in currentProfileContentAccounts"
              :key="contentAccount.id"
              :label="contentAccount.id"
            >
              {{ getContentAccountDisplayName(contentAccount) }}
            </el-checkbox>
          </el-checkbox-group>
          <div v-if="currentProfileContentAccounts.length === 0" class="muted-text">
            這個 Profile 目前沒有設定內容帳號，將退回使用 Profile 的共用提示詞產生一組通用文案。
          </div>
        </el-form-item>

        <el-form-item label="導流連結">
          <el-input v-model="generateForm.link" placeholder="選填，可覆蓋 Profile 預設連結" />
        </el-form-item>

        <el-form-item label="排程時間">
          <el-date-picker
            v-model="generateForm.scheduleAt"
            type="datetime"
            value-format="YYYY-MM-DDTHH:mm:ss"
            placeholder="選填"
            style="width: 100%"
          />
        </el-form-item>

        <el-form-item label="寫入 Google 試算表">
          <el-switch v-model="generateForm.writeToSheet" />
        </el-form-item>
      </el-form>

      <div v-if="generationBatchResult" class="generation-result">
        <el-alert
          :title="buildBatchSummaryTitle()"
          type="success"
          :closable="false"
          show-icon
        />

        <div class="result-block">
          <h3>本次帳號</h3>
          <div class="account-tags">
            <el-tag
              v-for="account in selectedGenerationAccounts"
              :key="account.id"
              class="account-tag"
            >
              {{ account.name }}（{{ account.platform }}）
            </el-tag>
            <span v-if="selectedGenerationAccounts.length === 0" class="muted-text">未挑選帳號</span>
          </div>
        </div>

        <div class="result-block">
          <h3>本次內容帳號</h3>
          <div class="account-tags">
            <el-tag
              v-for="contentAccount in selectedGenerationContentAccounts"
              :key="contentAccount.id"
              class="account-tag"
              type="success"
            >
              {{ getContentAccountDisplayName(contentAccount) }}
            </el-tag>
            <span v-if="selectedGenerationContentAccounts.length === 0" class="muted-text">未挑選內容帳號</span>
          </div>
        </div>

        <div class="batch-result-list">
          <div
            v-for="item in generationBatchResult.results"
            :key="item.material.id"
            class="batch-result-card"
          >
            <div class="result-block">
              <h3>{{ item.material.filename }}</h3>
              <el-link :href="item.storage?.publicUrl" target="_blank" type="primary">
                {{ item.storage?.publicUrl }}
              </el-link>
              <div class="handoff-actions">
                <el-button size="small" type="primary" @click="openPublishHandoffDialog(item)">
                  匯入發佈中心
                </el-button>
              </div>
            </div>

            <div class="result-block">
              <h3>轉錄內容</h3>
              <el-input :model-value="item.transcript" type="textarea" :rows="6" readonly />
            </div>

            <div v-if="item.contentAccountResults?.length" class="content-account-result-list">
              <h3>內容帳號文案</h3>
              <el-tabs v-model="contentResultTabMap[item.material.id]" class="content-result-tabs">
                <el-tab-pane
                  v-for="group in groupContentAccountResultsByPlatform(item.contentAccountResults)"
                  :key="group.platform"
                  :label="`${group.label}（${group.items.length}）`"
                  :name="group.platform"
                >
                  <div class="platform-result-list">
                    <div
                      v-for="contentResult in group.items"
                      :key="contentResult.account?.id"
                      class="post-card"
                    >
                      <h4>{{ getContentAccountDisplayName(contentResult.account) }}</h4>
                      <div v-if="contentResult.account?.postPreset" class="muted-text">
                        Post Preset：{{ contentResult.account.postPreset }}
                      </div>
                      <el-input :model-value="contentResult.content || ''" type="textarea" :rows="6" readonly />
                    </div>
                  </div>
                </el-tab-pane>
              </el-tabs>
            </div>

            <div v-if="item.sheetRowMappings?.length" class="result-block">
              <h3>Google Sheet 列對應</h3>
              <el-table :data="item.sheetRowMappings" size="small" style="width: 100%">
                <el-table-column prop="rowNumber" label="列號" width="80" />
                <el-table-column label="內容帳號" min-width="220">
                  <template #default="scope">
                    <span v-if="scope.row.accountName">
                      {{ scope.row.accountName }}（{{ getContentAccountPlatformLabel(scope.row.platform) }}）
                    </span>
                    <span v-else class="muted-text">
                      {{ getContentAccountPlatformLabel(scope.row.platform) }}（共用文案）
                    </span>
                  </template>
                </el-table-column>
                <el-table-column prop="postPreset" label="Post Preset" min-width="180" />
                <el-table-column label="訊息預覽" min-width="260">
                  <template #default="scope">
                    <span>{{ truncateText(scope.row.message, 120) }}</span>
                  </template>
                </el-table-column>
              </el-table>
            </div>

            <div v-else-if="item.contentAccountResults?.length" class="result-block">
              <h3>Google Sheet 列對應</h3>
              <div class="muted-text">這次內容帳號沒有對應可匯出的 Google Sheet 平台列。</div>
            </div>

            <div v-if="!item.contentAccountResults?.length" class="post-grid">
              <div v-for="(label, key) in postLabels" :key="key" class="post-card">
                <h4>{{ label }}</h4>
                <el-input :model-value="item.posts?.[key] || ''" type="textarea" :rows="6" readonly />
              </div>
            </div>
          </div>
        </div>
      </div>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="generateDialogVisible = false">關閉</el-button>
          <el-button type="primary" @click="submitGeneration" :loading="isGenerating">
            {{ isGenerating ? '產生中' : '開始批次產生' }}
          </el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog
      v-model="publishHandoffDialogVisible"
      title="匯入發佈中心"
      width="720px"
    >
      <div class="cell-lines">
        <div>素材：{{ handoffTargetItem?.material?.filename || '未選擇' }}</div>
        <div class="muted-text">你可以為每個國內平台指定要套用哪一種已生成文案來源。</div>
      </div>

      <el-form :model="publishHandoffForm" label-width="160px" class="google-sheet-form">
        <el-form-item
          v-for="platform in availablePublishHandoffPlatforms"
          :key="platform.key"
          :label="`${platform.label} 文案來源`"
        >
          <el-select v-model="publishHandoffForm[platform.key]" style="width: 100%">
            <el-option
              v-for="option in publishSourceOptions"
              :key="option.key"
              :label="option.label"
              :value="option.key"
            />
          </el-select>
        </el-form-item>
      </el-form>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="publishHandoffDialogVisible = false">取消</el-button>
          <el-button type="primary" @click="submitPublishHandoff">建立發佈草稿</el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog
      v-model="importPreviewDialogVisible"
      title="YAML 匯入預覽"
      width="860px"
    >
      <div class="import-preview-summary">
        <el-alert
          title="匯入會以 Profile 名稱為主鍵：同名更新，不同名新增，不會自動刪除既有 Profile。"
          type="info"
          :closable="false"
          show-icon
        />
        <div class="summary-cards">
          <div class="summary-card">
            <strong>{{ importPreview.summary.total }}</strong>
            <span>總筆數</span>
          </div>
          <div class="summary-card">
            <strong>{{ importPreview.summary.create }}</strong>
            <span>新增</span>
          </div>
          <div class="summary-card">
            <strong>{{ importPreview.summary.update }}</strong>
            <span>更新</span>
          </div>
          <div class="summary-card">
            <strong>{{ importPreview.summary.unchanged }}</strong>
            <span>不變</span>
          </div>
        </div>
      </div>

      <el-table :data="importPreview.items" style="width: 100%">
        <el-table-column prop="name" label="Profile" min-width="220" />
        <el-table-column label="動作" width="120">
          <template #default="scope">
            <el-tag :type="getImportPreviewActionTagType(scope.row.action)">
              {{ getImportPreviewActionLabel(scope.row.action) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="變更欄位" min-width="280">
          <template #default="scope">
            <span v-if="scope.row.changedFields?.length">{{ scope.row.changedFields.join(', ') }}</span>
            <span v-else class="muted-text">無變更</span>
          </template>
        </el-table-column>
        <el-table-column label="帳號數" width="100">
          <template #default="scope">
            {{ scope.row.accountCount }}
          </template>
        </el-table-column>
        <el-table-column label="內容帳號數" width="120">
          <template #default="scope">
            {{ scope.row.contentAccountCount }}
          </template>
        </el-table-column>
      </el-table>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="closeImportPreviewDialog">取消</el-button>
          <el-button type="primary" @click="confirmImportProfilesYaml" :loading="isImportingProfiles">
            {{ isImportingProfiles ? '匯入中' : '確認匯入' }}
          </el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog
      v-model="backupDialogVisible"
      title="Profile YAML 備份"
      width="720px"
    >
      <el-alert
        title="備份內容包含 Profile 匯出 YAML、資料庫與直發/Google 設定檔，會打包後上傳到你設定的 Rclone remote 目錄。"
        type="info"
        :closable="false"
        show-icon
      />

      <el-form :model="backupForm" label-width="150px" class="google-sheet-form backup-form">
        <el-form-item label="啟用每日備份">
          <el-switch v-model="backupForm.enabled" />
        </el-form-item>
        <el-form-item label="Rclone Remote">
          <el-input v-model="backupForm.remoteName" placeholder="Onedrive-Yahooforsub-Tao" />
        </el-form-item>
        <el-form-item label="備份資料夾">
          <el-input v-model="backupForm.remotePath" placeholder="Scripts-ssh-ssl-keys/SocialUpload/backups/profile-configs" />
        </el-form-item>
        <el-form-item label="每日備份時間">
          <el-input v-model="backupForm.scheduleTime" placeholder="03:00" />
        </el-form-item>
        <el-form-item label="保留份數">
          <el-input-number v-model="backupForm.keepCopies" :min="1" :max="30" />
        </el-form-item>
      </el-form>

      <div class="backup-status-block">
        <div><strong>上次備份：</strong>{{ backupForm.lastBackupAt || '尚未執行' }}</div>
        <div><strong>最後狀態：</strong>{{ buildBackupStatusLabel(backupForm.lastBackupStatus) }}</div>
        <div class="cell-lines"><strong>遠端位置：</strong>{{ backupForm.lastBackupRemoteSpec || '尚未產生' }}</div>
      </div>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="backupDialogVisible = false">取消</el-button>
          <el-button @click="submitRunProfileBackup" :loading="isRunningBackup">立即備份一次</el-button>
          <el-button type="primary" @click="submitSaveBackupConfig" :loading="isSavingBackupConfig">
            {{ isSavingBackupConfig ? '儲存中' : '儲存設定' }}
          </el-button>
        </div>
      </template>
    </el-dialog>

    <input
      ref="profileYamlInputRef"
      type="file"
      accept=".yaml,.yml"
      class="hidden-input"
      @change="handleImportProfilesYaml"
    >
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'
import { profileApi } from '@/api/profile'
import { accountApi } from '@/api/account'
import { materialApi } from '@/api/material'
import { useAccountStore } from '@/stores/account'
import { useAppStore } from '@/stores/app'

const accountStore = useAccountStore()
const appStore = useAppStore()
const router = useRouter()

const searchKeyword = ref('')
const isRefreshing = ref(false)
const isSubmitting = ref(false)
const isGenerating = ref(false)
const isSavingGoogleSheet = ref(false)
const isValidatingGoogleSheet = ref(false)
const isImportingProfiles = ref(false)
const isSavingBackupConfig = ref(false)
const isRunningBackup = ref(false)
const isSavingDirectPublishers = ref(false)

const profiles = ref([])
const dialogVisible = ref(false)
const dialogType = ref('create')
const googleSheetDialogVisible = ref(false)
const directPublishersDialogVisible = ref(false)
const backupDialogVisible = ref(false)
const generateDialogVisible = ref(false)
const importPreviewDialogVisible = ref(false)
const publishHandoffDialogVisible = ref(false)
const currentProfile = ref(null)
const generationBatchResult = ref(null)
const handoffTargetItem = ref(null)
const googleSheetValidationResult = ref(null)
const googleSheetConfig = ref({
  configured: false,
  source: null,
  clientEmail: '',
  projectId: '',
  filePath: ''
})
const contentResultTabMap = ref({})
const profileYamlInputRef = ref(null)
const directPublishersForm = ref({
  targets: []
})
const pendingImportYamlContent = ref('')
const importPreview = ref({
  summary: {
    total: 0,
    create: 0,
    update: 0,
    unchanged: 0
  },
  items: []
})
const backupForm = ref({
  enabled: true,
  remoteName: '',
  remotePath: 'profile-backups',
  scheduleTime: '03:00',
  keepCopies: 3,
  lastBackupAt: '',
  lastBackupRemoteSpec: '',
  lastBackupStatus: ''
})

const postLabels = {
  twitter: 'X / Twitter 貼文',
  threads: 'Threads',
  instagram: 'Instagram 長文',
  facebook: 'Facebook 長文',
  youtube: 'YouTube 貼文與說明',
  tiktok: 'TikTok 貼文與說明',
  telegram: 'Telegram',
  discord: 'Discord',
  patreon: 'Patreon'
}

const PUBLISH_HANDOFF_STORAGE_KEY = 'sau-publish-handoff-drafts'
const contentAccountPlatformOptions = [
  { label: 'X / Twitter', value: 'twitter' },
  { label: 'Threads', value: 'threads' },
  { label: 'Instagram', value: 'instagram' },
  { label: 'Facebook', value: 'facebook' },
  { label: 'YouTube', value: 'youtube' },
  { label: 'TikTok', value: 'tiktok' },
  { label: 'Telegram', value: 'telegram' },
  { label: 'Discord', value: 'discord' },
  { label: 'Patreon', value: 'patreon' },
  { label: 'Reddit', value: 'reddit' }
]
const publishSourceOptions = [
  { key: 'twitter', label: 'X / Twitter' },
  { key: 'threads', label: 'Threads' },
  { key: 'instagram', label: 'Instagram 長文' },
  { key: 'facebook', label: 'Facebook 長文' },
  { key: 'youtube', label: 'YouTube 貼文' },
  { key: 'tiktok', label: 'TikTok 貼文' },
  { key: 'telegram', label: 'Telegram' },
  { key: 'patreon', label: 'Patreon' }
]
const publishHandoffPlatforms = [
  { key: 'douyin', label: '抖音', publishType: 3, accountType: 3, defaultSource: 'tiktok', titleLimit: 30 },
  { key: 'kuaishou', label: '快手', publishType: 4, accountType: 4, defaultSource: 'tiktok', titleLimit: 30 },
  { key: 'videohao', label: '影片號', publishType: 2, accountType: 2, defaultSource: 'facebook', titleLimit: 100 },
  { key: 'xiaohongshu', label: '小紅書', publishType: 1, accountType: 1, defaultSource: 'instagram', titleLimit: 20 }
]

const createContentAccount = () => ({
  id: `content-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
  platform: 'twitter',
  name: '',
  prompt: '',
  contactDetails: '',
  cta: '',
  postPreset: '',
  publisherTargetId: ''
})

const normalizeDirectPublisherConfig = (platform, config = {}) => {
  if (platform === 'telegram') {
    return {
      botToken: config.botToken || '',
      chatId: config.chatId || '',
      parseMode: config.parseMode || 'HTML',
      disableWebPagePreview: Boolean(config.disableWebPagePreview)
    }
  }
  if (platform === 'discord') {
    return {
      webhookUrl: config.webhookUrl || '',
      username: config.username || ''
    }
  }
  if (platform === 'reddit') {
    return {
      clientId: config.clientId || '',
      clientSecret: config.clientSecret || '',
      refreshToken: config.refreshToken || '',
      subreddit: config.subreddit || ''
    }
  }
  return {
    apiKey: config.apiKey || '',
    apiKeySecret: config.apiKeySecret || '',
    accessToken: config.accessToken || '',
    accessTokenSecret: config.accessTokenSecret || ''
  }
}

const createDirectPublisherTarget = (platform = 'telegram') => ({
  id: `publisher-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
  platform,
  name: '',
  enabled: true,
  config: normalizeDirectPublisherConfig(platform, {})
})

const normalizeDirectPublisherTargets = (targets = []) => {
  if (!Array.isArray(targets)) {
    return []
  }
  return targets
    .filter(item => item && typeof item === 'object')
    .map(item => ({
      id: item.id || createDirectPublisherTarget(item.platform || 'telegram').id,
      platform: item.platform || 'telegram',
      name: item.name || '',
      enabled: item.enabled !== false,
      config: normalizeDirectPublisherConfig(item.platform || 'telegram', item.config || {})
    }))
}

const normalizeWatermarkFormSettings = (watermark = {}) => ({
  enabled: Boolean(watermark.enabled),
  type: watermark.type === 'image' ? 'image' : 'text',
  mode: watermark.mode === 'dynamic' ? 'dynamic' : 'static',
  templateName: watermark.templateName || '',
  pattern: watermark.pattern === 'repeat-slanted' ? 'repeat-slanted' : 'single',
  repeatLines: Math.min(Math.max(Number(watermark.repeatLines) || 3, 2), 5),
  angle: Math.min(Math.max(Number(watermark.angle) || -30, -80), 80),
  spacing: Math.min(Math.max(Number(watermark.spacing) || 220, 40), 600),
  fontSize: Math.min(Math.max(Number(watermark.fontSize) || 28, 12), 80),
  color: /^#[0-9A-Fa-f]{6}$/.test(watermark.color || '') ? watermark.color : '#FFFFFF',
  text: watermark.text || '',
  imagePath: watermark.imagePath || '',
  position: watermark.position || 'bottom-right',
  opacity: Math.min(Math.max(Number(watermark.opacity) || 0.45, 0.1), 1)
})

const normalizeContentAccounts = (values = []) => {
  if (!Array.isArray(values)) {
    return []
  }
  return values
    .filter(item => item && typeof item === 'object')
    .map(item => ({
      id: item.id || createContentAccount().id,
      platform: item.platform || 'twitter',
      name: item.name || '',
      prompt: item.prompt || '',
      contactDetails: item.contactDetails || '',
      cta: item.cta || '',
      postPreset: item.postPreset || '',
      publisherTargetId: item.publisherTargetId || ''
    }))
}

const normalizeProfileForm = (profile = {}) => {
  const base = makeDefaultProfile()
  const merged = {
    ...base,
    ...profile,
    settings: {
      ...base.settings,
      ...(profile.settings || {}),
      llm: {
        ...base.settings.llm,
        ...((profile.settings || {}).llm || {})
      },
      storage: {
        ...base.settings.storage,
        ...((profile.settings || {}).storage || {})
      },
      watermark: normalizeWatermarkFormSettings({
        ...base.settings.watermark,
        ...((profile.settings || {}).watermark || {})
      }),
      googleSheet: {
        ...base.settings.googleSheet,
        ...((profile.settings || {}).googleSheet || {})
      },
      socialImport: {
        ...base.settings.socialImport,
        ...((profile.settings || {}).socialImport || {})
      },
      postPresets: {
        ...base.settings.postPresets,
        ...((profile.settings || {}).postPresets || {})
      },
      contentAccounts: normalizeContentAccounts((profile.settings || {}).contentAccounts || [])
    },
    accountIds: Array.isArray(profile.accountIds) ? [...profile.accountIds] : []
  }
  return merged
}

const makeDefaultProfile = () => ({
  id: null,
  name: '',
  systemPrompt: '',
  contactDetails: '',
  cta: '',
  accountIds: [],
  settings: {
    llm: {
      apiBaseUrl: 'https://llmapi.iamwillywang.com/',
      transcriptionModel: '',
      generationModel: ''
    },
    storage: {
      remoteName: '',
      remotePath: 'Scripts-ssh-ssl-keys/SocialUpload',
      publicUrlTemplate: ''
    },
    watermark: {
      enabled: false,
      type: 'text',
      mode: 'static',
      templateName: '',
      pattern: 'single',
      repeatLines: 3,
      angle: -30,
      spacing: 220,
      fontSize: 28,
      color: '#FFFFFF',
      text: '',
      imagePath: '',
      position: 'bottom-right',
      opacity: 0.45
    },
    googleSheet: {
      spreadsheetId: '',
      worksheetName: 'Sheet1'
    },
    socialImport: {
      defaultLink: '',
      category: '',
      watermarkName: '',
      hashtagGroup: '',
      videoThumbnailUrl: '',
      ctaGroup: '',
      firstComment: '',
      story: false,
      pinterestBoard: '',
      altText: '',
      pinTitle: ''
    },
    postPresets: {
      twitter: '',
      threads: '',
      instagram: '',
      facebook: '',
      youtube: '',
      tiktok: ''
    },
    contentAccounts: []
  }
})

const profileForm = ref(makeDefaultProfile())
const googleSheetForm = ref({
  serviceAccountJson: '',
  spreadsheetId: ''
})
const publishHandoffForm = ref({
  douyin: 'tiktok',
  kuaishou: 'tiktok',
  videohao: 'facebook',
  xiaohongshu: 'instagram'
})
const generateForm = ref({
  materialIds: [],
  selectedAccountIds: [],
  selectedContentAccountIds: [],
  link: '',
  scheduleAt: '',
  writeToSheet: true
})

const filteredProfiles = computed(() => {
  if (!searchKeyword.value.trim()) {
    return profiles.value
  }

  const keyword = searchKeyword.value.trim().toLowerCase()
  return profiles.value.filter(profile => profile.name.toLowerCase().includes(keyword))
})

const materials = computed(() => appStore.materials)
const watermarkPreviewText = computed(() => (
  profileForm.value.settings.watermark.text || '@brandname'
))
const watermarkPreviewLineArray = computed(() => (
  Array.from({ length: Number(profileForm.value.settings.watermark.repeatLines || 3) }, (_, index) => index)
))
const watermarkPreviewTileArray = computed(() => (
  Array.from({ length: 8 }, (_, index) => index)
))
const watermarkPreviewLayerStyle = computed(() => ({
  opacity: profileForm.value.settings.watermark.opacity,
  transform: `rotate(${profileForm.value.settings.watermark.angle || -30}deg)`,
  color: profileForm.value.settings.watermark.color || '#FFFFFF',
  fontSize: `${Math.max(Number(profileForm.value.settings.watermark.fontSize) || 28, 12)}px`,
  gap: `${Math.max(Math.round((Number(profileForm.value.settings.watermark.spacing) || 220) / 6), 24)}px`
}))
const watermarkSinglePreviewStyle = computed(() => {
  const watermark = profileForm.value.settings.watermark
  const positionMap = {
    'top-left': { top: '24px', left: '24px' },
    'top-right': { top: '24px', right: '24px' },
    'bottom-left': { bottom: '24px', left: '24px' },
    'bottom-right': { bottom: '24px', right: '24px' },
    center: { top: '50%', left: '50%', transform: 'translate(-50%, -50%)' }
  }
  return {
    ...positionMap[watermark.position || 'bottom-right'],
    opacity: watermark.opacity,
    color: watermark.color || '#FFFFFF',
    fontSize: `${Math.max(Number(watermark.fontSize) || 28, 12)}px`
  }
})
const currentProfileAccounts = computed(() => {
  if (!currentProfile.value) {
    return []
  }
  const selectedIds = new Set(currentProfile.value.accountIds || [])
  return accountStore.accounts.filter(item => selectedIds.has(item.id))
})
const currentProfileContentAccounts = computed(() => (
  normalizeContentAccounts(currentProfile.value?.settings?.contentAccounts || [])
))
const selectedGenerationAccounts = computed(() => {
  const selectedIds = new Set(generationBatchResult.value?.selectedAccountIds || [])
  return accountStore.accounts.filter(item => selectedIds.has(item.id))
})
const selectedGenerationContentAccounts = computed(() => {
  const allContentAccounts = normalizeContentAccounts(generationBatchResult.value?.profile?.settings?.contentAccounts || [])
  const selectedIds = new Set(generationBatchResult.value?.selectedContentAccountIds || [])
  return allContentAccounts.filter(item => selectedIds.has(item.id))
})
const directPublisherTargetOptions = computed(() => normalizeDirectPublisherTargets(directPublishersForm.value.targets || []))
const availablePublishHandoffPlatforms = computed(() => (
  publishHandoffPlatforms.filter(platform => selectedGenerationAccounts.value.some(account => account.type === platform.accountType))
))

const getAccountName = (accountId) => {
  const account = accountStore.accounts.find(item => item.id === accountId)
  return account ? `${account.name}（${account.platform}）` : accountId
}

const getContentAccountPlatformLabel = (platform) => {
  return contentAccountPlatformOptions.find(item => item.value === platform)?.label || platform
}

const getContentAccountDisplayName = (contentAccount) => {
  if (!contentAccount) {
    return ''
  }
  const name = (contentAccount.name || '').trim()
  const platformLabel = getContentAccountPlatformLabel(contentAccount.platform)
  return name ? `${name}（${platformLabel}）` : platformLabel
}

const supportsDirectPublisherTarget = (platform) => ['telegram', 'discord', 'reddit', 'twitter'].includes(platform)

const getAvailableDirectPublisherTargets = (platform) => (
  directPublisherTargetOptions.value.filter(item => item.platform === platform)
)

const truncateText = (value, limit = 120) => {
  const text = (value || '').trim()
  if (!text || text.length <= limit) {
    return text
  }
  return `${text.slice(0, Math.max(limit - 1, 1)).trim()}…`
}

const groupContentAccountResultsByPlatform = (results = []) => {
  const groups = new Map()
  results.forEach((result) => {
    const platform = result?.account?.platform || 'unknown'
    if (!groups.has(platform)) {
      groups.set(platform, {
        platform,
        label: getContentAccountPlatformLabel(platform),
        items: []
      })
    }
    groups.get(platform).items.push(result)
  })
  return [...groups.values()]
}

const ensureContentResultTabs = (batchResult) => {
  const nextMap = {}
  ;(batchResult?.results || []).forEach((item) => {
    const groups = groupContentAccountResultsByPlatform(item.contentAccountResults || [])
    if (groups.length > 0) {
      nextMap[item.material.id] = groups[0].platform
    }
  })
  contentResultTabMap.value = nextMap
}

const addContentAccount = () => {
  profileForm.value.settings.contentAccounts.push(createContentAccount())
}

const removeContentAccount = (index) => {
  profileForm.value.settings.contentAccounts.splice(index, 1)
}

const addDirectPublisherTarget = () => {
  directPublishersForm.value.targets.push(createDirectPublisherTarget())
}

const removeDirectPublisherTarget = (index) => {
  directPublishersForm.value.targets.splice(index, 1)
}

const handleDirectPublisherPlatformChange = (target) => {
  if (!target) {
    return
  }
  target.config = normalizeDirectPublisherConfig(target.platform, {})
}

const fetchProfiles = async () => {
  isRefreshing.value = true
  try {
    const response = await profileApi.getProfiles()
    profiles.value = (response.data || []).map(item => normalizeProfileForm(item))
  } catch (error) {
    ElMessage.error('取得 Profile 清單失敗')
  } finally {
    isRefreshing.value = false
  }
}

const fetchDirectPublishersConfig = async () => {
  try {
    const response = await profileApi.getDirectPublishersConfig()
    directPublishersForm.value = {
      targets: normalizeDirectPublisherTargets(response.data?.targets || [])
    }
  } catch (error) {
    directPublishersForm.value = { targets: [] }
    ElMessage.error(error.message || '取得 Direct Publishers 設定失敗')
  }
}

const ensureAccounts = async () => {
  if (accountStore.accounts.length > 0) {
    return
  }

  try {
    const response = await accountApi.getAccounts()
    if (response.code === 200 && response.data) {
      accountStore.setAccounts(response.data)
    }
  } catch (error) {
    ElMessage.error('取得帳號清單失敗')
  }
}

const ensureMaterials = async () => {
  if (appStore.materials.length > 0) {
    return
  }

  try {
    const response = await materialApi.getAllMaterials()
    if (response.code === 200) {
      appStore.setMaterials(response.data || [])
    }
  } catch (error) {
    ElMessage.error('取得素材清單失敗')
  }
}

const openCreateDialog = () => {
  dialogType.value = 'create'
  profileForm.value = normalizeProfileForm(makeDefaultProfile())
  dialogVisible.value = true
}

const normalizeBackupForm = (value = {}) => ({
  enabled: value.enabled !== false,
  remoteName: value.remoteName || '',
  remotePath: value.remotePath || 'profile-backups',
  scheduleTime: value.scheduleTime || '03:00',
  keepCopies: Number.isFinite(Number(value.keepCopies)) ? Number(value.keepCopies) : 3,
  lastBackupAt: value.lastBackupAt || '',
  lastBackupRemoteSpec: value.lastBackupRemoteSpec || '',
  lastBackupStatus: value.lastBackupStatus || ''
})

const getImportPreviewActionLabel = (action) => {
  if (action === 'create') {
    return '新增'
  }
  if (action === 'update') {
    return '更新'
  }
  return '不變'
}

const getImportPreviewActionTagType = (action) => {
  if (action === 'create') {
    return 'success'
  }
  if (action === 'update') {
    return 'warning'
  }
  return 'info'
}

const buildBackupStatusLabel = (status) => {
  if (status === 'success') {
    return '成功'
  }
  if (status === 'failed') {
    return '失敗'
  }
  return '尚未執行'
}

const downloadExampleProfilesYaml = () => {
  const link = document.createElement('a')
  link.href = profileApi.getExampleProfilesYamlUrl()
  link.download = ''
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
}

const triggerImportProfilesYaml = () => {
  if (!profileYamlInputRef.value) {
    return
  }
  profileYamlInputRef.value.value = ''
  profileYamlInputRef.value.click()
}

const handleImportProfilesYaml = async (event) => {
  const file = event.target.files?.[0]
  if (!file) {
    return
  }

  isImportingProfiles.value = true
  try {
    const yamlContent = await file.text()
    const response = await profileApi.previewProfilesYaml(yamlContent)
    pendingImportYamlContent.value = yamlContent
    importPreview.value = response.data || {
      summary: {
        total: 0,
        create: 0,
        update: 0,
        unchanged: 0
      },
      items: []
    }
    importPreviewDialogVisible.value = true
  } catch (error) {
    ElMessage.error(error.message || '預覽 Profile YAML 失敗')
  } finally {
    isImportingProfiles.value = false
    if (profileYamlInputRef.value) {
      profileYamlInputRef.value.value = ''
    }
  }
}

const closeImportPreviewDialog = () => {
  importPreviewDialogVisible.value = false
  pendingImportYamlContent.value = ''
  importPreview.value = {
    summary: {
      total: 0,
      create: 0,
      update: 0,
      unchanged: 0
    },
    items: []
  }
}

const confirmImportProfilesYaml = async () => {
  if (!pendingImportYamlContent.value) {
    ElMessage.warning('沒有可匯入的 YAML 內容')
    return
  }

  isImportingProfiles.value = true
  try {
    const response = await profileApi.importProfilesYaml(pendingImportYamlContent.value)
    await fetchProfiles()
    const summary = response.data || {}
    closeImportPreviewDialog()
    ElMessage.success(`匯入完成：新增 ${summary.created || 0} 筆，更新 ${summary.updated || 0} 筆`)
  } catch (error) {
    ElMessage.error(error.message || '匯入 Profile YAML 失敗')
  } finally {
    isImportingProfiles.value = false
  }
}

const exportProfilesYaml = () => {
  const link = document.createElement('a')
  link.href = profileApi.getExportProfilesYamlUrl()
  link.download = ''
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
}

const fetchBackupConfig = async () => {
  try {
    const response = await profileApi.getProfileBackupConfig()
    backupForm.value = normalizeBackupForm(response.data || {})
  } catch (error) {
    backupForm.value = normalizeBackupForm()
    ElMessage.error('取得備份設定失敗')
  }
}

const openBackupDialog = async () => {
  await fetchBackupConfig()
  backupDialogVisible.value = true
}

const submitSaveBackupConfig = async () => {
  isSavingBackupConfig.value = true
  try {
    const response = await profileApi.saveProfileBackupConfig(backupForm.value)
    backupForm.value = normalizeBackupForm(response.data || {})
    ElMessage.success('備份設定已儲存')
  } catch (error) {
    ElMessage.error(error.message || '儲存備份設定失敗')
  } finally {
    isSavingBackupConfig.value = false
  }
}

const submitRunProfileBackup = async () => {
  isRunningBackup.value = true
  try {
    const response = await profileApi.runProfileBackup()
    backupForm.value = normalizeBackupForm(response.data?.backup || {})
    ElMessage.success(`備份完成：${response.data?.remoteSpec || ''}`)
  } catch (error) {
    ElMessage.error(error.message || '執行備份失敗')
  } finally {
    isRunningBackup.value = false
  }
}

const fetchGoogleSheetConfig = async () => {
  try {
    const response = await profileApi.getGoogleSheetConfig()
    googleSheetConfig.value = response.data || {
      configured: false,
      source: null,
      clientEmail: '',
      projectId: '',
      filePath: ''
    }
  } catch (error) {
    googleSheetConfig.value = {
      configured: false,
      source: null,
      clientEmail: '',
      projectId: '',
      filePath: ''
    }
    ElMessage.error(error.message || '取得 Google 試算表設定失敗')
  }
}

const openGoogleSheetDialog = async () => {
  googleSheetValidationResult.value = null
  googleSheetForm.value = {
    serviceAccountJson: '',
    spreadsheetId: ''
  }
  await fetchGoogleSheetConfig()
  googleSheetDialogVisible.value = true
}

const openDirectPublishersDialog = async () => {
  await fetchDirectPublishersConfig()
  directPublishersDialogVisible.value = true
}

const saveGoogleSheetConfig = async () => {
  if (!googleSheetForm.value.serviceAccountJson.trim()) {
    ElMessage.warning('請貼上 Google service account JSON')
    return
  }

  isSavingGoogleSheet.value = true
  try {
    const response = await profileApi.saveGoogleSheetConfig({
      serviceAccountJson: googleSheetForm.value.serviceAccountJson
    })
    googleSheetConfig.value = response.data
    googleSheetForm.value.serviceAccountJson = ''
    ElMessage.success('Google 試算表設定已儲存')
  } catch (error) {
    ElMessage.error(error.message || 'Google 試算表設定儲存失敗')
  } finally {
    isSavingGoogleSheet.value = false
  }
}

const validateGoogleSheetConfig = async () => {
  const spreadsheetId = googleSheetForm.value.spreadsheetId.trim() || profileForm.value.settings.googleSheet.spreadsheetId.trim()
  if (!spreadsheetId) {
    ElMessage.warning('請輸入要測試的 Spreadsheet ID')
    return
  }

  isValidatingGoogleSheet.value = true
  try {
    const response = await profileApi.validateGoogleSheetConfig({
      spreadsheetId
    })
    googleSheetValidationResult.value = response.data
    ElMessage.success('Google 試算表連線成功')
  } catch (error) {
    googleSheetValidationResult.value = null
    ElMessage.error(error.message || 'Google 試算表連線失敗')
  } finally {
    isValidatingGoogleSheet.value = false
  }
}

const submitSaveDirectPublishersConfig = async () => {
  isSavingDirectPublishers.value = true
  try {
    const payload = {
      targets: normalizeDirectPublisherTargets(directPublishersForm.value.targets || []).map(target => ({
        ...target,
        config: normalizeDirectPublisherConfig(target.platform, target.config || {})
      }))
    }
    const response = await profileApi.saveDirectPublishersConfig(payload)
    directPublishersForm.value = {
      targets: normalizeDirectPublisherTargets(response.data?.targets || [])
    }
    directPublishersDialogVisible.value = false
    ElMessage.success('Direct Publishers 設定已儲存')
  } catch (error) {
    ElMessage.error(error.message || '儲存 Direct Publishers 設定失敗')
  } finally {
    isSavingDirectPublishers.value = false
  }
}

const openEditDialog = (profile) => {
  dialogType.value = 'edit'
  profileForm.value = normalizeProfileForm(JSON.parse(JSON.stringify(profile)))
  dialogVisible.value = true
}

const submitProfile = async () => {
  if (!profileForm.value.name.trim()) {
    ElMessage.warning('請輸入 Profile 名稱')
    return
  }

  isSubmitting.value = true
  try {
    const response = await profileApi.saveProfile(normalizeProfileForm(profileForm.value))
    const savedProfile = normalizeProfileForm(response.data)
    const index = profiles.value.findIndex(item => item.id === savedProfile.id)

    if (index > -1) {
      profiles.value[index] = savedProfile
    } else {
      profiles.value.unshift(savedProfile)
    }

    dialogVisible.value = false
    ElMessage.success('Profile 儲存成功')
  } catch (error) {
    ElMessage.error(error.message || 'Profile 儲存失敗')
  } finally {
    isSubmitting.value = false
  }
}

const handleDelete = (profile) => {
  ElMessageBox.confirm(
    `確定要刪除 Profile「${profile.name}」嗎？`,
    '提醒',
    {
      confirmButtonText: '確定',
      cancelButtonText: '取消',
      type: 'warning'
    }
  ).then(async () => {
    try {
      await profileApi.deleteProfile(profile.id)
      profiles.value = profiles.value.filter(item => item.id !== profile.id)
      ElMessage.success('刪除成功')
    } catch (error) {
      ElMessage.error('刪除失敗')
    }
  }).catch(() => {})
}

const openGenerateDialog = async (profile) => {
  currentProfile.value = profile
  generationBatchResult.value = null
  generateForm.value = {
    materialIds: [],
    selectedAccountIds: [...(profile.accountIds || [])],
    selectedContentAccountIds: currentProfileContentAccounts.value.map(item => item.id),
    link: profile.settings?.socialImport?.defaultLink || '',
    scheduleAt: '',
    writeToSheet: true
  }
  await ensureMaterials()
  generateDialogVisible.value = true
}

const submitGeneration = async () => {
  if (!currentProfile.value) {
    return
  }

  if (generateForm.value.materialIds.length === 0) {
    ElMessage.warning('請至少選擇一份素材')
    return
  }

  if (currentProfileContentAccounts.value.length > 0 && generateForm.value.selectedContentAccountIds.length === 0) {
    ElMessage.warning('請至少選擇一個內容帳號')
    return
  }

  isGenerating.value = true
  try {
    const response = await profileApi.generateBatchContent({
      profileId: currentProfile.value.id,
      materialIds: generateForm.value.materialIds,
      selectedAccountIds: generateForm.value.selectedAccountIds,
      selectedContentAccountIds: generateForm.value.selectedContentAccountIds,
      link: generateForm.value.link,
      scheduleAt: generateForm.value.scheduleAt,
      writeToSheet: generateForm.value.writeToSheet
    })
    generationBatchResult.value = response.data
    ensureContentResultTabs(response.data)
    ElMessage.success('批次文案產生完成')
  } catch (error) {
    ElMessage.error(error.message || '批次產生失敗')
  } finally {
    isGenerating.value = false
  }
}

const openPublishHandoffDialog = (item) => {
  if (!selectedGenerationAccounts.value.length) {
    ElMessage.warning('這次批次結果沒有可匯入的帳號')
    return
  }

  handoffTargetItem.value = item
  publishHandoffForm.value = publishHandoffPlatforms.reduce((acc, platform) => {
    acc[platform.key] = platform.defaultSource
    return acc
  }, {})
  publishHandoffDialogVisible.value = true
}

const normalizeWhitespace = (value) => (value || '').replace(/\s+/g, ' ').trim()

const extractHashtags = (value) => {
  const matches = [...(value || '').matchAll(/#([^\s#]+)/g)]
  return [...new Set(matches.map(match => match[1].trim()).filter(Boolean))].slice(0, 5)
}

const stripHashtags = (value) => normalizeWhitespace((value || '').replace(/#[^\s#]+/g, ' '))

const buildPublishTitle = (value, limit) => {
  const text = stripHashtags(value)
  if (!text) {
    return ''
  }
  return text.slice(0, limit).trim()
}

const submitPublishHandoff = async () => {
  if (!handoffTargetItem.value) {
    return
  }

  const item = handoffTargetItem.value
  const drafts = []

  for (const platform of availablePublishHandoffPlatforms.value) {
    const sourceKey = publishHandoffForm.value[platform.key]
    const sourceText = normalizeWhitespace(item.posts?.[sourceKey] || '')
    const accountIds = selectedGenerationAccounts.value
      .filter(account => account.type === platform.accountType)
      .map(account => account.id)

    if (!sourceText || accountIds.length === 0) {
      continue
    }

    drafts.push({
      label: `${platform.label}-${item.material.filename}`,
      fileList: [
        {
          name: item.material.filename,
          url: item.storage?.publicUrl || '',
          path: item.processedMediaPath || item.material.file_path,
          size: Number(item.material.filesize || 0) * 1024 * 1024,
          type: item.storage?.mediaKind === 'image' ? 'image/*' : 'video/mp4'
        }
      ],
      selectedAccounts: accountIds,
      selectedPlatform: platform.publishType,
      title: buildPublishTitle(sourceText, platform.titleLimit),
      description: sourceText,
      productLink: '',
      productTitle: '',
      selectedTopics: extractHashtags(sourceText),
      scheduleEnabled: false,
      videosPerDay: 1,
      dailyTimes: ['10:00'],
      startDays: 0,
      publishStatus: null,
      publishing: false,
      isDraft: false,
      isOriginal: false
    })
  }

  if (!drafts.length) {
    ElMessage.warning('找不到可匯入發佈中心的內容或帳號')
    return
  }

  const existingDrafts = JSON.parse(localStorage.getItem(PUBLISH_HANDOFF_STORAGE_KEY) || '[]')
  localStorage.setItem(PUBLISH_HANDOFF_STORAGE_KEY, JSON.stringify([...existingDrafts, ...drafts]))
  publishHandoffDialogVisible.value = false
  ElMessage.success(`已建立 ${drafts.length} 個發佈草稿，正在前往發佈中心`)
  await router.push('/publish-center')
}

const buildBatchSummaryTitle = () => {
  if (!generationBatchResult.value) {
    return ''
  }
  const summary = generationBatchResult.value.summary || {}
  const rowCount = summary.sheetRows || 0
  const materialCount = summary.materials || 0
  const worksheetText = (summary.worksheets || []).join('、')
  if (generateForm.value.writeToSheet) {
    return `已完成 ${materialCount} 份素材，匯出 ${rowCount} 列到 ${worksheetText || 'Google 試算表'}`
  }
  return `已完成 ${materialCount} 份素材，僅產生文案，尚未寫入 Google 試算表`
}

const buildGoogleSheetSourceLabel = (source) => {
  if (source === 'env_json') {
    return '環境變數 JSON'
  }
  if (source === 'env_file') {
    return '環境變數檔案路徑'
  }
  if (source === 'stored_file') {
    return '後台儲存檔案'
  }
  return '尚未設定'
}

onMounted(async () => {
  await Promise.all([
    fetchProfiles(),
    ensureAccounts(),
    ensureMaterials(),
    fetchGoogleSheetConfig(),
    fetchDirectPublishersConfig()
  ])
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

.profile-management {
  .page-header {
    margin-bottom: 20px;

    h1 {
      font-size: 24px;
      color: $text-primary;
      margin: 0;
    }
  }

  .profile-list-container {
    background-color: #fff;
    border-radius: 4px;
    box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.1);
    padding: 20px;
  }

  .profile-toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 20px;

    .el-input {
      width: 320px;
    }

    .action-buttons {
      display: flex;
      gap: 10px;
    }

    .is-loading {
      animation: rotate 1s linear infinite;
    }
  }

  .empty-data {
    padding: 40px 0;
  }

  .hidden-input {
    display: none;
  }

  .import-preview-summary {
    display: flex;
    flex-direction: column;
    gap: 16px;
    margin-bottom: 16px;
  }

  .summary-cards {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 12px;
  }

  .summary-card {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 12px;
    border: 1px solid $border-light;
    border-radius: 8px;
    background: $bg-color-page;

    strong {
      font-size: 22px;
      line-height: 1.2;
    }

    span {
      color: $text-secondary;
      font-size: 13px;
    }
  }

  .backup-form {
    margin-top: 20px;
  }

  .backup-status-block {
    margin-top: 8px;
    padding: 12px 16px;
    border-radius: 8px;
    background: $bg-color-page;
    line-height: 1.8;
  }

  .account-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .account-tag {
    margin-right: 0;
  }

  .muted-text {
    color: #909399;
    font-size: 13px;
  }

  .cell-lines {
    line-height: 1.6;
  }

  .content-account-config {
    width: 100%;
  }

  .content-account-toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
    margin-bottom: 16px;
  }

  .content-account-list {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .content-account-card {
    border: 1px solid #e5eaf3;
    border-radius: 10px;
    padding: 16px;
    background-color: #f8fafc;
  }

  .content-account-card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
  }

  .watermark-preview {
    width: 100%;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .preview-canvas {
    position: relative;
    overflow: hidden;
    width: 100%;
    max-width: 520px;
    aspect-ratio: 16 / 9;
    border-radius: 12px;
    border: 1px solid #e5eaf3;
    background: linear-gradient(135deg, #1f2937, #334155 55%, #475569);
  }

  .preview-repeat-layer {
    position: absolute;
    inset: -30%;
    display: grid;
    grid-template-columns: repeat(4, minmax(120px, 1fr));
    align-content: center;
    pointer-events: none;
  }

  .preview-repeat-tile {
    display: flex;
    flex-direction: column;
    gap: 10px;
    text-align: center;
    font-weight: 600;
    white-space: nowrap;
  }

  .preview-repeat-line,
  .preview-single-mark {
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.35);
    font-weight: 600;
  }

  .preview-single-mark {
    position: absolute;
    padding: 8px 14px;
    border-radius: 999px;
    background: rgba(0, 0, 0, 0.18);
    pointer-events: none;
  }

  .generate-account-list {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 16px;
  }

  .generation-result {
    margin-top: 20px;
  }

  .google-sheet-form {
    margin-top: 20px;
  }

  .result-block {
    margin-top: 20px;

    h3 {
      margin-bottom: 10px;
      font-size: 16px;
      color: $text-primary;
    }
  }

  .batch-result-list {
    display: flex;
    flex-direction: column;
    gap: 20px;
    margin-top: 20px;
  }

  .content-account-result-list {
    display: flex;
    flex-direction: column;
    gap: 16px;
    margin-top: 20px;
  }

  .content-result-tabs {
    width: 100%;
  }

  .platform-result-list {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .batch-result-card {
    background-color: #f8fafc;
    border: 1px solid #e5eaf3;
    border-radius: 10px;
    padding: 20px;
  }

  .handoff-actions {
    margin-top: 12px;
  }

  .validation-details {
    margin-top: 12px;
  }

  .post-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 16px;
    margin-top: 20px;
  }

  .post-card {
    background-color: #f5f7fa;
    border-radius: 6px;
    padding: 16px;

    h4 {
      margin: 0 0 10px 0;
      font-size: 14px;
      color: $text-primary;
    }
  }
}

.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}
</style>
