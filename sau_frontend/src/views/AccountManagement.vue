<template>
  <div class="fade-in">
    <!-- Toolbar -->
    <div class="toolbar">
      <div class="seg">
        <button :class="{ on: filter === 'all' }" @click="filter = 'all'">All</button>
        <button :class="{ on: filter === 'ready' }" @click="filter = 'ready'">Ready</button>
        <button :class="{ on: filter === 'attn' }" @click="filter = 'attn'">Needs auth</button>
      </div>
      <input
        class="filter-input"
        type="search"
        v-model="searchFilter"
        placeholder="Search nickname / handle"
        title="Filter by nickname, account name, or platform"
      />
      <select
        class="filter-select"
        :value="platformFilter"
        @change="onPlatformFilterChange"
        title="Filter by platform"
      >
        <option value="">All platforms</option>
        <option v-for="p in platformsPresent" :key="p.slug" :value="p.slug">{{ p.label }}</option>
      </select>
      <select
        class="filter-select"
        :value="groupFilter"
        @change="onGroupFilterChange"
        title="Filter by group"
      >
        <option value="">All groups</option>
        <option value="__none__">Ungrouped</option>
        <option v-for="g in groups" :key="g" :value="g">{{ g }}</option>
      </select>
      <button
        class="btn-ghost btn-tiny"
        :disabled="!hasActiveFilters"
        @click="resetFilters"
        title="Reset all filters"
      >
        Clear filters
      </button>
      <span class="count">
        <template v-if="hasActiveFilters">
          {{ filteredAccounts.length }} of {{ accounts.length }} accounts
        </template>
        <template v-else>
          {{ filteredAccounts.length }} accounts
        </template>
        · {{ platformCount }} platforms
      </span>
      <div class="spacer"></div>
      <button class="btn-ghost" @click="runHealthCheck">
        <component :is="icons.spark" :width="15" :height="15" /> Run health check
      </button>
      <button class="btn-ghost" @click="onRefreshAll" :disabled="refreshAllBusy">
        <component :is="icons.oauth" :width="15" :height="15" /> {{ refreshAllBusy ? 'Refreshing…' : 'Refresh tokens' }}
      </button>
      <button class="btn-primary" @click="openConnect()">
        <component :is="icons.plus" /> Connect Account
      </button>
    </div>

    <!-- Datalist shared by all inline group editors — typing into the input
         suggests existing groups, and free-text values create a brand-new
         one on save. -->
    <datalist id="account-group-options">
      <option v-for="g in groups" :key="g" :value="g"></option>
    </datalist>

    <!-- Account grid -->
    <div class="acct-grid">
      <div v-for="acct in filteredAccounts" :key="acct.id" class="acct">
        <div class="acct-top">
          <div class="acct-logo-wrap">
            <img :src="acct.avatarUrl || defaultAvatar(acct.accountName)" class="acct-avatar" @error="e => e.target.src = defaultAvatar(acct.accountName)" />
            <span class="acct-platform-badge" :style="{ background: platformBg(acct.platformSlug) }">
              {{ platformShort(acct.platformSlug) }}
            </span>
          </div>
          <div style="flex:1;min-width:0">
            <div class="acct-name">
              <template v-if="!isEditing(acct)">
                <span>{{ acct.nickname || acct.accountName }}</span>
                <span v-if="acct.nickname" class="acct-name-sub">@{{ acct.accountName }}</span>
              </template>
              <input
                v-else
                ref="editNameInput"
                class="input inline-input"
                v-model="editDraft.nickname"
                :placeholder="acct.accountName"
                @keyup.enter="saveEdit(acct)"
                @keyup.esc="cancelEdit"
              />
            </div>
            <div class="acct-handle">{{ acct.connectionDetail || acct.platform }}</div>
            <div class="acct-tags">
              <span v-if="acct.accountGroup && !isEditing(acct)" class="acct-tag">{{ acct.accountGroup }}</span>
              <input
                v-if="isEditing(acct)"
                class="input inline-input group-input"
                list="account-group-options"
                v-model="editDraft.accountGroup"
                placeholder="No group — type to create"
                @keyup.enter="saveEdit(acct)"
                @keyup.esc="cancelEdit"
              />
              <button
                v-if="!isEditing(acct)"
                class="link-btn"
                @click="startEdit(acct)"
                title="Edit nickname and group"
              >Edit</button>
              <template v-else>
                <button class="link-btn save" @click="saveEdit(acct)" :disabled="editSaving">Save</button>
                <button class="link-btn" @click="cancelEdit">Cancel</button>
              </template>
            </div>
          </div>
          <span class="cookie-pill" :class="cookieStatusClass(acct)">
            <span class="d"></span>{{ cookieStatusLabel(acct) }}
          </span>
        </div>
        <div class="acct-body">
          <div class="acct-stat">
            <div class="n" style="font-size:13px">{{ acct.connectionLabel }}</div>
            <div class="l">Status</div>
          </div>
          <div class="acct-stat">
            <div class="n" style="font-size:13px">{{ acct.platform }}</div>
            <div class="l">Platform</div>
          </div>
          <div class="acct-stat">
            <div class="n" style="font-size:13px">{{ expiryLabel(acct) }}</div>
            <div class="l">Cookie</div>
          </div>
        </div>
        <div class="acct-actions">
          <button class="mini-btn" :class="{ accent: cookieStatusClass(acct) !== 'ck-valid' }" @click="onReauth(acct)">
            <component :is="icons.oauth" :width="13" :height="13" />
            {{ acct.connectionLabel === 'Ready' ? 'Re-auth' : 'Reconnect' }}
          </button>
          <button v-if="acct.authType === 'oauth' && (acct.isExpiringWithin24h || acct.isExpiringWithin7d)" class="mini-btn" @click="onRefreshToken(acct)">
            <component :is="icons.spark" :width="13" :height="13" /> Refresh
          </button>
          <button class="mini-btn" @click="onExport(acct)">
            <component :is="icons.upload" :width="13" :height="13" /> Export
          </button>
          <button class="mini-btn" title="Remove" style="flex:0 0 38px" @click="onRemove(acct)">✕</button>
        </div>
      </div>
    </div>

    <!-- Empty state -->
    <div v-if="filteredAccounts.length === 0" class="empty-state">
      <div class="empty-icon">📭</div>
      <h3>No accounts found</h3>
      <p>Connect your first social media account to get started.</p>
    </div>

    <!-- Connect modal -->
    <div v-if="showConnect" class="overlay" @click="showConnect = false">
      <div class="modal" @click.stop>
        <div class="modal-head">
          <div class="dz-ic" style="width:38px;height:38px;border-radius:10px">
            <component :is="icons.oauth" :width="18" :height="18" />
          </div>
          <div>
            <h2>{{ connectData.account ? 'Re-authorize account' : 'Connect an account' }}</h2>
            <div class="ms">Capture &amp; encrypt a platform session</div>
          </div>
          <button class="modal-x" @click="showConnect = false">✕</button>
        </div>

        <div class="modal-body">
          <!-- Platform picker -->
          <label style="display:block;font-size:12px;font-weight:600;color:var(--text-2);margin-bottom:8px">Platform</label>
          <div class="plat-grid">
            <div
              v-for="p in allPlatforms"
              :key="p.slug"
              class="plat-pick"
              :class="{ on: connectData.platform === p.slug }"
              @click="connectData.platform = p.slug"
            >
              <div class="acct-logo" :style="{ background: p.color, width: '32px', height: '32px', borderRadius: '9px', fontSize: '13px' }">
                {{ p.short }}
              </div>
              <span class="pn">{{ p.label }}</span>
            </div>
          </div>

          <template v-if="connectData.platform">
            <div style="display:flex;gap:12px;margin-top:18px">
              <div class="field" style="flex:1;margin-top:0">
                <label>Account name</label>
                <input class="input" placeholder="e.g. acme.official" v-model="connectData.account" />
              </div>
              <div class="field" style="flex:1;margin-top:0">
                <label>Profile</label>
                <input class="input" v-model="connectData.profile" />
              </div>
            </div>

            <!-- Method tabs -->
            <div class="method-tabs" style="margin-top:18px">
              <div v-if="isOAuthPlatform" class="method-tab" :class="{ on: connectMethod === 'oauth' }" @click="connectMethod = 'oauth'">
                <div class="mi"><component :is="icons.oauth" :width="16" :height="16" /></div>
                <div>
                  <div class="mt">OAuth Connect</div>
                  <div class="md">Authorize via {{ platformLabel(connectData.platform) }}</div>
                </div>
              </div>
              <div v-if="supportsManual" class="method-tab" :class="{ on: connectMethod === 'manual' }" @click="connectMethod = 'manual'">
                <div class="mi"><component :is="icons.settings" :width="16" :height="16" /></div>
                <div>
                  <div class="mt">Configure manually</div>
                  <div class="md">
                    <template v-if="connectData.platform === 'telegram'">Bot token env + chat ids</template>
                    <template v-else-if="connectData.platform === 'discord'">Webhook URL env</template>
                    <template v-else>API token + IDs</template>
                  </div>
                </div>
              </div>
              <div class="method-tab" :class="{ on: connectMethod === 'qr' }" @click="connectMethod = 'qr'">
                <div class="mi"><component :is="icons.oauth" :width="16" :height="16" /></div>
                <div>
                  <div class="mt">Scan to log in</div>
                  <div class="md">QR / browser login</div>
                </div>
              </div>
              <div class="method-tab" :class="{ on: connectMethod === 'import' }" @click="connectMethod = 'import'">
                <div class="mi"><component :is="icons.upload" :width="16" :height="16" /></div>
                <div>
                  <div class="mt">Import cookies</div>
                  <div class="md">Paste / upload JSON</div>
                </div>
              </div>
            </div>

            <!-- OAuth method -->
            <div v-if="connectMethod === 'oauth'" class="qr-wrap">
              <div class="qr-box" style="display:flex;align-items:center;justify-content:center">
                <component :is="icons.oauth" :width="36" :height="36" />
              </div>
              <div class="qr-info">
                <span class="qs"><span class="d"></span>OAuth Authorization</span>
                <p>Click "Connect with OAuth" to open {{ platformLabel(connectData.platform) }}'s authorization page. You'll grant API access — no cookies or passwords needed. Tokens are stored encrypted and auto-refreshed.</p>
                <div class="note">
                  <component :is="icons.about" />
                  <p>OAuth is the recommended auth method. Your credentials never pass through this app — only the platform's official authorization flow is used.</p>
                </div>
              </div>
            </div>

            <!-- Manual configure method -->
            <div v-else-if="connectMethod === 'manual'">
              <div class="steps" v-if="connectData.platform === 'telegram'">
                <div class="step"><span class="num">1</span><span>Set <code>{{ connectData.config.botTokenEnv || 'TELEGRAM_BOT_TOKEN' }}</code> in the worker's <code>.env</code> with the bot token from <code>@BotFather</code>.</span></div>
                <div class="step"><span class="num">2</span><span>Add one or more chat ids — channels (<code>@channel_name</code>), groups, or numeric ids (<code>-100123…</code>). One bot → many targets.</span></div>
                <div class="step"><span class="num">3</span><span>Save the account, then "Run health check" to confirm each chat is reachable.</span></div>
              </div>
              <div class="steps" v-else-if="connectData.platform === 'discord'">
                <div class="step"><span class="num">1</span><span>Set <code>{{ connectData.config.webhookUrlEnv || 'DISCORD_WEBHOOK_URL' }}</code> in <code>.env</code>.</span></div>
              </div>
              <div class="steps" v-else>
                <div class="step"><span class="num">1</span><span>Paste the env var names holding the credentials in <code>.env</code>.</span></div>
                <div class="step"><span class="num">2</span><span>Save the account — credentials stay in <code>.env</code>, never in the DB.</span></div>
              </div>
              <AccountTextFieldList
                :fields="currentFieldDefs"
                :model-value="connectData.config"
                @update-field="onConfigFieldUpdate"
              />
            </div>

            <!-- QR method -->
            <div v-else-if="connectMethod === 'qr'" class="qr-wrap">
              <div class="qr-box">
                <svg viewBox="0 0 25 25" shape-rendering="crispEdges">
                  <rect x="0" y="0" width="25" height="25" fill="#fff" />
                  <rect x="0" y="0" width="7" height="7" fill="#0a0a0d" />
                  <rect x="1" y="1" width="5" height="5" fill="#fff" />
                  <rect x="2" y="2" width="3" height="3" fill="#0a0a0d" />
                  <rect x="18" y="0" width="7" height="7" fill="#0a0a0d" />
                  <rect x="19" y="1" width="5" height="5" fill="#fff" />
                  <rect x="20" y="2" width="3" height="3" fill="#0a0a0d" />
                  <rect x="0" y="18" width="7" height="7" fill="#0a0a0d" />
                  <rect x="1" y="19" width="5" height="5" fill="#fff" />
                  <rect x="2" y="20" width="3" height="3" fill="#0a0a0d" />
                </svg>
              </div>
              <div class="qr-info">
                <span class="qs"><span class="d"></span>{{ loginStatusLabel }}</span>
                <p>The backend opens a {{ platformLabel(connectData.platform) }} login session in a (headless) browser and renders its QR here. Once you confirm on your phone, it captures the session cookies and writes them <b>AES-GCM encrypted</b> to the cookie store.</p>
                <div class="note">
                  <component :is="icons.about" />
                  <p>No password ever passes through Socialupload — only the resulting session cookie, encrypted at rest.</p>
                </div>
              </div>
            </div>

            <!-- Import method -->
            <div v-else>
              <div class="steps">
                <div class="step"><span class="num">1</span><span>Open {{ platformLabel(connectData.platform) }} in your browser and log in.</span></div>
                <div class="step"><span class="num">2</span><span>Export cookies with EditThisCookie / Cookie-Editor (JSON), or paste a Netscape <code>cookies.txt</code>.</span></div>
                <div class="step"><span class="num">3</span><span>Paste below — the backend validates, normalizes and encrypts them.</span></div>
              </div>
              <div class="field">
                <label>Cookie payload (JSON or Netscape)</label>
                <textarea class="textarea" rows="5" placeholder='[{"name":"sessionid","value":"…","domain":".douyin.com"}]' v-model="connectData.paste"></textarea>
              </div>
            </div>
          </template>
        </div>

        <div class="modal-foot">
          <span class="ms" style="align-self:center;font-size:12px;color:var(--text-3)">
            {{ connectData.platform ? platformLabel(connectData.platform) : 'Select a platform' }}
          </span>
          <div class="spacer"></div>
          <button class="btn-sec" @click="showConnect = false">Cancel</button>
          <button
            v-if="connectMethod === 'oauth'"
            class="btn-primary"
            :disabled="!connectData.platform || !connectData.account || oauthBusy"
            :style="{ opacity: (!connectData.platform || !connectData.account || oauthBusy) ? 0.5 : 1 }"
            @click="doOAuthConnect"
          >
            {{ oauthBusy ? 'Connecting…' : 'Connect with OAuth' }}
          </button>
          <button
            v-else-if="connectMethod === 'import'"
            class="btn-primary"
            :disabled="!connectData.platform || !connectData.paste"
            :style="{ opacity: (!connectData.platform || !connectData.paste) ? 0.5 : 1 }"
            @click="doImport"
          >
            {{ importBusy ? 'Importing…' : 'Import & encrypt' }}
          </button>
          <button
            v-else-if="connectMethod === 'manual'"
            class="btn-primary"
            :disabled="!manualFormValid || manualBusy"
            :style="{ opacity: (!manualFormValid || manualBusy) ? 0.5 : 1 }"
            @click="doManualConnect"
          >
            {{ manualBusy ? 'Saving…' : 'Save account' }}
          </button>
          <button v-else class="btn-sec" disabled style="opacity:0.6">
            {{ loginStatusLabel }}
          </button>
        </div>
      </div>
    </div>

    <!-- Toast -->
    <div v-if="toast" class="toast">
      <component :is="icons.check" :width="15" :height="15" /> {{ toast }}
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useAccountStore } from '@/stores/account'
import { accountApi } from '@/api/account'
import { profilesApi } from '@/api/profiles'
import { tiktokApi } from '@/api/tiktok'
import { metaApi } from '@/api/meta'
import { threadsApi } from '@/api/threads'
import { youtubeApi } from '@/api/youtube'
import { twitterApi } from '@/api/twitter'
import { icons } from '@/utils/icons'
import {
  telegramFieldDefs,
  redditFieldDefs,
  youtubeFieldDefs,
  facebookFieldDefs,
  instagramFieldDefs,
  threadsFieldDefs,
  tiktokTokenFieldDefs,
  discordFieldDefs,
  twitterFieldDefs,
} from '@/utils/account-form-defs'
import AccountTextFieldList from '@/components/AccountTextFieldList.vue'

const accountStore = useAccountStore()

/* Platform metadata (colors + short labels for the card design) */
const PLATFORM_META = {
  douyin:      { label: '抖音 Douyin',      short: '抖', color: '#fe2c55' },
  tiktok:      { label: 'TikTok',           short: 'TT', color: 'linear-gradient(135deg,#0b0b0b,#25f4ee)' },
  bilibili:    { label: 'Bilibili',         short: 'B',  color: '#00aeec' },
  xiaohongshu: { label: '小红书 RED',       short: '红', color: '#ff2442' },
  kuaishou:    { label: '快手 Kuaishou',    short: '快', color: '#ff7a00' },
  tencent:     { label: '视频号 Channels',  short: '视', color: '#07c160' },
  channels:    { label: '视频号 Channels',  short: '视', color: '#07c160' },
  baijiahao:   { label: '百家号 Baijia',    short: '百', color: '#3c4ce4' },
  youtube:     { label: 'YouTube',          short: 'YT', color: '#ff0033' },
  reddit:      { label: 'Reddit',           short: 'R',  color: '#ff4500' },
  facebook:    { label: 'Facebook',         short: 'FB', color: '#1877f2' },
  instagram:   { label: 'Instagram',        short: 'IG', color: 'linear-gradient(135deg,#833ab4,#fd1d1d,#fcb045)' },
  threads:     { label: 'Threads',          short: 'TH', color: '#000' },
  twitter:     { label: 'X / Twitter',      short: 'X',  color: '#000' },
  telegram:    { label: 'Telegram',         short: 'TG', color: '#0088cc' },
  discord:     { label: 'Discord',          short: 'DC', color: '#5865f2' },
  medium:      { label: 'Medium',           short: 'M',  color: '#1a1a1a' },
  substack:    { label: 'Substack',         short: 'S',  color: '#ff6719' },
  patreon:     { label: 'Patreon',          short: 'P',  color: '#ff424d' },
}

/* Platforms that support OAuth (default auth method) */
const OAUTH_PLATFORMS = ['tiktok', 'facebook', 'instagram', 'threads', 'youtube', 'twitter']

/* Platform-specific manual config field definitions, sourced from
   account-form-defs.js. Drives the "Configure manually" connect tab so
   operators can paste bot tokens / chat ids / webhook URLs at account
   creation time (especially for Telegram's multi-target chatIds). */
const platformManualFieldDefs = {
  telegram: telegramFieldDefs,
  reddit: redditFieldDefs,
  youtube: youtubeFieldDefs,
  facebook: facebookFieldDefs,
  instagram: instagramFieldDefs,
  threads: threadsFieldDefs,
  tiktok: tiktokTokenFieldDefs,
  discord: discordFieldDefs,
  twitter: twitterFieldDefs,
}

const allPlatforms = computed(() =>
  Object.entries(PLATFORM_META).map(([slug, meta]) => ({ slug, ...meta }))
)

/* Filter state */
const filter = ref('all')
const platformFilter = ref('')
const groupFilter = ref('')
const searchFilter = ref('')
const groups = ref([])
const editingId = ref(null)
const editDraft = ref({ nickname: '', accountGroup: '' })
const editSaving = ref(false)

const isEditing = (acct) => editingId.value === acct.id

function startEdit(acct) {
  editingId.value = acct.id
  editDraft.value = { nickname: acct.nickname || '', accountGroup: acct.accountGroup || '' }
}
function cancelEdit() {
  editingId.value = null
  editDraft.value = { nickname: '', accountGroup: '' }
}
async function saveEdit(acct) {
  if (editSaving.value) return
  const nextNick = (editDraft.value.nickname || '').trim()
  const nextGroup = (editDraft.value.accountGroup || '').trim()
  if (nextNick === (acct.nickname || '') && nextGroup === (acct.accountGroup || '')) {
    cancelEdit()
    return
  }
  editSaving.value = true
  try {
    await accountApi.updateAccountMeta(acct.id, { nickname: nextNick, accountGroup: nextGroup })
    acct.nickname = nextNick
    acct.accountGroup = nextGroup
    // Refresh groups list if a new group name was added (including clearing a
    // group — reloading from the server keeps the dropdown in sync).
    await loadGroups()
    flash(`Saved ${acct.accountName}`)
    cancelEdit()
  } catch (e) { flash('Save failed: ' + e.message) }
  finally { editSaving.value = false }
}

function onPlatformFilterChange(e) { platformFilter.value = e.target.value }
function onGroupFilterChange(e) {
  const v = e.target.value
  groupFilter.value = v === '__none__' ? '__none__' : v
}

function resetFilters() {
  filter.value = 'all'
  platformFilter.value = ''
  groupFilter.value = ''
  searchFilter.value = ''
}

const hasActiveFilters = computed(() =>
  filter.value !== 'all' ||
  Boolean(platformFilter.value) ||
  Boolean(groupFilter.value) ||
  Boolean((searchFilter.value || '').trim())
)

const platformsPresent = computed(() => {
  const slugs = new Set(accounts.value.map(a => a.platformSlug))
  return allPlatforms.value
    .filter(p => slugs.has(p.slug))
    .sort((a, b) => a.label.localeCompare(b.label))
})

const filteredAccounts = computed(() => {
  let list = accounts.value
  if (filter.value === 'ready') list = list.filter(a => a.connectionLabel === 'Ready')
  else if (filter.value === 'attn') list = list.filter(a => a.connectionLabel !== 'Ready')
  if (platformFilter.value) list = list.filter(a => a.platformSlug === platformFilter.value)
  if (groupFilter.value) {
    if (groupFilter.value === '__none__') {
      list = list.filter(a => !a.accountGroup)
    } else {
      list = list.filter(a => a.accountGroup === groupFilter.value)
    }
  }
  const q = (searchFilter.value || '').trim().toLowerCase()
  if (q) {
    list = list.filter(a => {
      const hay = [
        a.nickname,
        a.accountName,
        a.platformSlug,
        a.platform,
        PLATFORM_META[a.platformSlug]?.label || '',
      ]
      return hay.some(s => s && String(s).toLowerCase().includes(q))
    })
  }
  return list
})
const platformCount = computed(() => new Set(filteredAccounts.value.map(a => a.platformSlug)).size)

/* Helpers */
const platformBg = (slug) => PLATFORM_META[slug]?.color || '#888'
const platformShort = (slug) => PLATFORM_META[slug]?.short || '?'
const platformLabel = (slug) => PLATFORM_META[slug]?.label || slug
const defaultAvatar = (name) => `https://ui-avatars.com/api/?name=${encodeURIComponent(name)}&background=random&size=84&bold=true`

const cookieStatusClass = (acct) => {
  if (acct.isOverdue || acct.reconnectRequired || acct.connectionLabel === 'Missing' || acct.connectionLabel === 'Token expired') return 'ck-exp'
  if (acct.isExpiringWithin24h || acct.isExpiringWithin7d) return 'ck-soon'
  return 'ck-valid'
}
const cookieStatusLabel = (acct) => {
  const cls = cookieStatusClass(acct)
  const isOAuth = acct.authType === 'oauth'
  if (cls === 'ck-exp') return isOAuth ? 'Token expired' : 'Expired'
  if (cls === 'ck-soon') return isOAuth ? 'Token expiring' : 'Expiring'
  return isOAuth ? 'Connected' : 'Valid'
}
const expiryLabel = (acct) => {
  if (acct.isOverdue) return acct.authType === 'oauth' ? 'token expired' : 'expired'
  if (acct.secondsRemaining != null) {
    const d = Math.floor(acct.secondsRemaining / 86400)
    return d > 0 ? `in ${d}d` : `in ${Math.floor(acct.secondsRemaining / 3600)}h`
  }
  return acct.connectionLabel === 'Ready' ? (acct.authType === 'oauth' ? 'token' : 'session') : '—'
}

/* Actions */
const flash = (msg) => { toast.value = msg; setTimeout(() => { toast.value = null }, 3000) }

const onReauth = (acct) => {
  openConnect({ platform: acct.platformSlug, account: acct.accountName, profile: acct.profileName })
}
const onRefreshToken = async (acct) => {
  try {
    await profilesApi.refreshAccountToken(acct.id)
    flash(`Token refreshed for ${acct.accountName}`)
    await loadAccounts()
  } catch (e) { flash('Refresh failed: ' + e.message) }
}
const onRefreshAll = async () => {
  refreshAllBusy.value = true
  try {
    const oauthAccounts = accounts.value.filter(a => a.authType === 'oauth')
    if (oauthAccounts.length === 0) {
      flash('No OAuth accounts to refresh')
      return
    }
    const ids = oauthAccounts.map(a => a.id)
    await profilesApi.batchRefreshTokens(ids)
    flash(`Refreshed ${ids.length} OAuth account(s)`)
    await loadAccounts()
  } catch (e) { flash('Refresh all failed: ' + e.message) }
  finally { refreshAllBusy.value = false }
}
const onExport = async (acct) => {
  try {
    const res = await accountApi.exportCookies(acct.id)
    if (res?.data) {
      const blob = new Blob([JSON.stringify(res.data, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url; a.download = `${acct.platformSlug}_${acct.accountName}.cookies.json`; a.click()
      URL.revokeObjectURL(url)
      flash(`Exported cookies for ${acct.accountName}`)
    }
  } catch (e) { flash('Export failed: ' + e.message) }
}
const onRemove = async (acct) => {
  if (!confirm(`Remove ${acct.accountName} (${acct.platform})?`)) return
  try {
    await accountApi.deleteAccount(acct.id)
    accounts.value = accounts.value.filter(a => a.id !== acct.id)
    flash(`Removed ${acct.accountName}`)
  } catch (e) { flash('Remove failed: ' + e.message) }
}
const runHealthCheck = async () => {
  try {
    await accountApi.getHealthSummary()
    flash('Health check complete')
  } catch (e) { flash('Health check failed: ' + e.message) }
}

/* Connect modal */
const showConnect = ref(false)
const connectMethod = ref('qr')
const connectData = ref({ platform: null, account: '', profile: 'default', paste: '', config: {} })
const importBusy = ref(false)
const oauthBusy = ref(false)
const manualBusy = ref(false)
const refreshAllBusy = ref(false)
const loginStatus = ref('pending')
const toast = ref(null)

const isOAuthPlatform = computed(() =>
  connectData.value.platform && OAUTH_PLATFORMS.includes(connectData.value.platform)
)

const currentFieldDefs = computed(() =>
  connectData.value.platform ? (platformManualFieldDefs[connectData.value.platform] || []) : []
)
const supportsManual = computed(() => currentFieldDefs.value.length > 0)

const manualFormValid = computed(() => {
  if (!connectData.value.platform || !supportsManual.value) return false
  const cfg = connectData.value.config || {}
  const platform = connectData.value.platform
  // Telegram requires botTokenEnv + chatId(s); other platforms require at
  // least one non-empty field so the backend doesn't reject with "missing
  // field" errors.
  if (platform === 'telegram') {
    const hasToken = Boolean((cfg.botTokenEnv || '').trim())
    const hasTargets = Boolean((cfg.chatId || '').trim()) || Boolean((cfg.chatIds || '').trim())
    return hasToken && hasTargets
  }
  if (platform === 'discord') {
    return Boolean((cfg.webhookUrlEnv || '').trim())
  }
  // Generic: require any of the provided fields to be non-empty.
  return Object.values(cfg).some((v) => Boolean(String(v || '').trim()))
})

const loginStatusLabel = computed(() => {
  const map = { pending: 'Waiting for scan…', scanned: 'Scanned — confirm on your phone', confirmed: 'Confirmed — saving session', saved: 'Saved', error: 'Login failed' }
  return map[loginStatus.value] || 'Waiting for scan…'
})

const openConnect = (initial = {}) => {
  connectData.value = { platform: initial.platform || null, account: initial.account || '', profile: initial.profile || 'default', paste: '', config: {} }
  // Default to OAuth for platforms that support it
  connectMethod.value = initial.platform && OAUTH_PLATFORMS.includes(initial.platform) ? 'oauth' : 'qr'
  loginStatus.value = 'pending'
  showConnect.value = true
}

const onConfigFieldUpdate = ({ key, value }) => {
  connectData.value.config = { ...(connectData.value.config || {}), [key]: value }
}

/* Start OAuth flow for supported platforms */
const doOAuthConnect = async () => {
  if (!connectData.value.platform) return
  oauthBusy.value = true
  try {
    const platform = connectData.value.platform
    const accountName = connectData.value.account || `${platform}-oauth`
    const profile = connectData.value.profile || 'default'

    // Step 1: Get profiles
    const profilesRes = await profilesApi.list()
    const profiles = profilesRes?.data || profilesRes || []
    const profileId = profiles[0]?.id || 1

    // Step 2: Find existing account or create new one
    let accountId = null
    const existingAccount = accounts.value.find(
      a => a.platformSlug === platform && a.accountName === accountName
    )

    if (existingAccount) {
      // Reuse existing account for reauth
      accountId = existingAccount.id
    } else {
      // Create new account
      const createRes = await profilesApi.createAccount(profileId, {
        accountName,
        platform,
        authType: 'oauth',
        profile,
      })
      const newAccount = createRes?.data || createRes
      accountId = newAccount?.id
      if (!accountId) throw new Error('Failed to create account')
    }

    // Step 3: Start OAuth flow
    let oauthRes
    const payload = { accountId, accountName, profileId }
    if (platform === 'tiktok') {
      oauthRes = await tiktokApi.startOAuth(payload)
    } else if (platform === 'facebook' || platform === 'instagram') {
      oauthRes = await metaApi.startOAuth(payload)
    } else if (platform === 'threads') {
      oauthRes = await threadsApi.startOAuth(payload)
    } else if (platform === 'youtube') {
      oauthRes = await youtubeApi.startOAuth(payload)
    } else if (platform === 'twitter') {
      oauthRes = await twitterApi.startOAuth(payload)
    }

    const authorizeUrl = oauthRes?.data?.authorizeUrl
    if (!authorizeUrl) throw new Error('No authorize URL returned')

    // Step 4: Open OAuth popup
    const popup = window.open(authorizeUrl, 'oauth-popup', 'width=600,height=700,scrollbars=yes')
    if (!popup) throw new Error('Popup blocked — please allow popups for this site')

    // Step 5: Listen for the OAuth callback postMessage
    // Backend sends: { type: 'sau:{platform}-oauth', ok: true/false, data/error }
    const expectedType = `sau:${platform === 'facebook' || platform === 'instagram' ? 'meta' : platform}-oauth`
    const handler = (event) => {
      if (event.data?.type === expectedType) {
        window.removeEventListener('message', handler)
        popup.close()
        if (event.data.ok) {
          flash(existingAccount ? `Re-authenticated ${accountName}` : `Connected ${accountName} via OAuth`)
          showConnect.value = false
        } else {
          flash('OAuth failed: ' + (event.data.error || 'Unknown error'))
        }
        loadAccounts()
      }
    }
    window.addEventListener('message', handler)

    // Cleanup if popup is closed manually
    const checkClosed = setInterval(() => {
      if (popup.closed) {
        clearInterval(checkClosed)
        window.removeEventListener('message', handler)
        // Refresh accounts in case OAuth completed
        loadAccounts()
      }
    }, 1000)
  } catch (e) { flash('OAuth failed: ' + e.message) }
  finally { oauthBusy.value = false }
}

const doImport = async () => {
  if (!connectData.value.platform || !connectData.value.paste) return
  importBusy.value = true
  try {
    const fmt = connectData.value.paste.trim().startsWith('[') || connectData.value.paste.trim().startsWith('{') ? 'json' : 'netscape'
    await accountApi.importCookies(connectData.value.platform, connectData.value.account, connectData.value.profile, fmt, connectData.value.paste)
    flash(`Connected ${connectData.value.account || 'account'} · cookies stored (encrypted)`)
    showConnect.value = false
    await loadAccounts()
  } catch (e) { flash('Import failed: ' + e.message) }
  finally { importBusy.value = false }
}

const doManualConnect = async () => {
  if (!connectData.value.platform || !supportsManual.value) return
  manualBusy.value = true
  try {
    const platform = connectData.value.platform
    const accountName = connectData.value.account || `${platform}-manual`

    const profilesRes = await profilesApi.list()
    const profiles = profilesRes?.data || profilesRes || []
    const profileId = profiles[0]?.id
    if (!profileId) throw new Error('No profiles available — create a profile first')

    // Filter empty values so we don't push blanks into config_json.
    const config = Object.fromEntries(
      Object.entries(connectData.value.config || {}).filter(([, v]) => String(v || '').trim() !== '')
    )

    await profilesApi.createAccount(profileId, {
      accountName,
      platform,
      authType: 'manual',
      profile: connectData.value.profile || 'default',
      config,
    })
    flash(`Saved ${accountName} · ${platformLabel(platform)} (manual)`)
    showConnect.value = false
    await loadAccounts()
  } catch (e) { flash('Save failed: ' + e.message) }
  finally { manualBusy.value = false }
}

/* Load accounts on mount — uses /api/accounts which returns the enriched
   shape directly (with cookieStatus, expiresAt, handle, profile). */
const loadAccounts = async () => {
  try {
    const res = await accountApi.getAccountsApi()
    const list = res?.data || res || []
    // Map to the shape the component expects (merge with PLATFORM_META)
    accounts.value = list.map(a => {
      const isOAuth = a.authType === 'oauth'
      const isExpired = a.cookieStatus === 'expired'
      const isSoon = a.cookieStatus === 'soon'
      return {
        avatarUrl: a.avatarUrl || '',
        id: a.id,
        platformSlug: a.platform,
        accountName: a.name,
        nickname: a.nickname || '',
        accountGroup: a.accountGroup || '',
        platform: PLATFORM_META[a.platform]?.label || a.platform,
        authType: a.authType || 'cookie',
        // For OAuth accounts, show "Token expired" instead of "Missing"
        connectionLabel: isExpired ? (isOAuth ? 'Token expired' : 'Missing') : 'Ready',
        connectionDetail: a.handle || '',
        profileName: a.profile || 'default',
        isOverdue: isExpired,
        isExpiringWithin24h: isSoon,
        isExpiringWithin7d: isSoon,
        reconnectRequired: isExpired,
        secondsRemaining: null,
        cookieStatus: a.cookieStatus,
        expiresAt: a.expiresAt,
      }
    })
  } catch (e) { console.warn('Failed to load accounts:', e) }
}

const loadGroups = async () => {
  try {
    const res = await accountApi.getAccountGroups()
    groups.value = (res?.data || res || []).slice().sort((a, b) => a.localeCompare(b))
  } catch (e) { console.warn('Failed to load groups:', e) }
}

// Local reactive accounts (not using the store's normalization since we
// get pre-enriched data from /api/accounts)
const accounts = ref([])

onMounted(async () => {
  await Promise.all([loadAccounts(), loadGroups()])
})
</script>

<style scoped>
.acct-logo-wrap {
  position: relative;
  width: 42px;
  height: 42px;
  flex-shrink: 0;
}
.acct-avatar {
  width: 42px;
  height: 42px;
  border-radius: 50%;
  object-fit: cover;
  display: block;
}
.acct-platform-badge {
  position: absolute;
  bottom: -2px;
  right: -2px;
  width: 18px;
  height: 18px;
  border-radius: 6px;
  display: grid;
  place-items: center;
  color: #fff;
  font-size: 8px;
  font-weight: 700;
  font-family: var(--font-display);
  border: 2px solid var(--bg-1, #0a0a0d);
  line-height: 1;
}
.acct-name {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.acct-name-sub {
  font-size: 11px;
  color: var(--text-3);
  font-weight: 400;
}
.acct-tags {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 4px;
}
.acct-tag {
  font-size: 10.5px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: var(--r-full);
  background: var(--accent-soft);
  color: var(--accent);
  font-family: var(--font-mono);
}
.link-btn {
  background: none;
  border: none;
  color: var(--text-3);
  font-size: 11.5px;
  font-weight: 600;
  cursor: pointer;
  padding: 1px 4px;
  border-radius: 6px;
  transition: color 0.15s;
}
.link-btn:hover { color: var(--text); }
.link-btn.save { color: var(--accent); }
.inline-input {
  width: 100%;
  padding: 4px 8px;
  font-size: 13px;
  margin-top: 2px;
}
.inline-select {
  font-size: 11.5px;
  padding: 3px 6px;
}
.filter-select {
  height: 32px;
  padding: 0 10px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  color: var(--text);
  font-size: 12.5px;
  font-family: inherit;
  cursor: pointer;
  transition: border-color 0.15s;
}
.filter-select:focus { outline: none; border-color: var(--accent); }
.filter-input {
  height: 32px;
  padding: 0 10px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  color: var(--text);
  font-size: 12.5px;
  font-family: inherit;
  min-width: 200px;
  transition: border-color 0.15s;
}
.filter-input:focus { outline: none; border-color: var(--accent); }
.filter-input::placeholder { color: var(--text-3, #888); }
.group-input {
  font-size: 11.5px;
  padding: 3px 6px;
  margin-top: 0;
  width: auto;
  min-width: 140px;
}
.btn-tiny {
  font-size: 11px;
  padding: 4px 10px;
  height: 32px;
}
.btn-ghost:disabled { opacity: 0.45; cursor: not-allowed; }
</style>
