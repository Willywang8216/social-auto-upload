<template>
  <div class="fade-in">
    <!-- Toolbar -->
    <div class="toolbar">
      <div class="seg">
        <button :class="{ on: filter === 'all' }" @click="filter = 'all'">All</button>
        <button :class="{ on: filter === 'ready' }" @click="filter = 'ready'">Ready</button>
        <button :class="{ on: filter === 'attn' }" @click="filter = 'attn'">Needs auth</button>
      </div>
      <span class="count">{{ filteredAccounts.length }} accounts · {{ platformCount }} platforms</span>
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

    <!-- Account grid -->
    <div class="acct-grid">
      <div v-for="acct in filteredAccounts" :key="acct.id" class="acct">
        <div class="acct-top">
          <div class="acct-logo" :style="{ background: platformBg(acct.platformSlug) }">
            {{ platformShort(acct.platformSlug) }}
          </div>
          <div style="flex:1;min-width:0">
            <div class="acct-name">{{ acct.accountName }}</div>
            <div class="acct-handle">{{ acct.connectionDetail || acct.platform }}</div>
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

const allPlatforms = computed(() =>
  Object.entries(PLATFORM_META).map(([slug, meta]) => ({ slug, ...meta }))
)

/* Filter state */
const filter = ref('all')
const filteredAccounts = computed(() => {
  if (filter.value === 'ready') return accounts.value.filter(a => a.connectionLabel === 'Ready')
  if (filter.value === 'attn') return accounts.value.filter(a => a.connectionLabel !== 'Ready')
  return accounts.value
})
const platformCount = computed(() => new Set(filteredAccounts.value.map(a => a.platformSlug)).size)

/* Helpers */
const platformBg = (slug) => PLATFORM_META[slug]?.color || '#888'
const platformShort = (slug) => PLATFORM_META[slug]?.short || '?'
const platformLabel = (slug) => PLATFORM_META[slug]?.label || slug

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
const connectData = ref({ platform: null, account: '', profile: 'default', paste: '' })
const importBusy = ref(false)
const oauthBusy = ref(false)
const refreshAllBusy = ref(false)
const loginStatus = ref('pending')
const toast = ref(null)

const isOAuthPlatform = computed(() =>
  connectData.value.platform && OAUTH_PLATFORMS.includes(connectData.value.platform)
)

const loginStatusLabel = computed(() => {
  const map = { pending: 'Waiting for scan…', scanned: 'Scanned — confirm on your phone', confirmed: 'Confirmed — saving session', saved: 'Saved', error: 'Login failed' }
  return map[loginStatus.value] || 'Waiting for scan…'
})

const openConnect = (initial = {}) => {
  connectData.value = { platform: initial.platform || null, account: initial.account || '', profile: initial.profile || 'default', paste: '' }
  // Default to OAuth for platforms that support it
  connectMethod.value = initial.platform && OAUTH_PLATFORMS.includes(initial.platform) ? 'oauth' : 'qr'
  loginStatus.value = 'pending'
  showConnect.value = true
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
        id: a.id,
        platformSlug: a.platform,
        accountName: a.name,
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

// Local reactive accounts (not using the store's normalization since we
// get pre-enriched data from /api/accounts)
const accounts = ref([])

onMounted(loadAccounts)
</script>
