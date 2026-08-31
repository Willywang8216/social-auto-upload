<template>
  <div class="fade-in">
    <!-- Toolbar -->
    <div class="toolbar">
      <div class="seg" role="tablist" aria-label="Connection filter">
        <button :class="{ on: filter === 'all' }" @click="filter = 'all'" role="tab">All</button>
        <button :class="{ on: filter === 'ready' }" @click="filter = 'ready'" role="tab">Ready</button>
        <button :class="{ on: filter === 'attn' }" @click="filter = 'attn'" role="tab">Needs auth</button>
      </div>
      <div class="filter-field" title="Filter by nickname, account name, or platform">
        <component :is="icons.search" :width="14" :height="14" class="filter-icon" />
        <input
          class="filter-input filter-input-padded"
          type="search"
          v-model="searchFilter"
          placeholder="Search nickname / handle"
          aria-label="Search accounts"
        />
      </div>
      <div class="filter-field" title="Filter by platform">
        <component :is="icons.accounts" :width="14" :height="14" class="filter-icon" />
        <select
          class="filter-select filter-select-padded"
          :value="platformFilter"
          @change="onPlatformFilterChange"
          aria-label="Filter by platform"
        >
          <option value="">All platforms</option>
          <option v-for="p in platformsPresent" :key="p.slug" :value="p.slug">{{ p.label }}</option>
        </select>
      </div>
      <div class="filter-field" title="Filter by group">
        <component :is="icons.tag" :width="14" :height="14" class="filter-icon" />
        <select
          class="filter-select filter-select-padded"
          :value="groupFilter"
          @change="onGroupFilterChange"
          aria-label="Filter by group"
        >
          <option value="">All groups</option>
          <option value="__none__">Ungrouped</option>
          <option v-for="g in groupsWithPlatforms" :key="g.name" :value="g.name">
            {{ g.name }} · {{ g.platformsLabel }}
          </option>
        </select>
      </div>
      <div class="filter-field" title="Filter by profile">
        <component :is="icons.users" :width="14" :height="14" class="filter-icon" />
        <select
          class="filter-select filter-select-padded"
          :value="profileFilter"
          @change="onProfileFilterChange"
          aria-label="Filter by profile"
        >
          <option value="">All profiles</option>
          <option v-for="p in profiles" :key="p.id" :value="String(p.id)">
            {{ p.name }} ({{ p.count }})
          </option>
        </select>
      </div>
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

    <!-- Profile summary strip — makes it obvious what's actually inside
         each profile (and per-group bucket) at a glance, instead of forcing
         the operator to mentally JOIN profile/group/platform from the card
         chips below. Collapsed by default; click [▾] to expand a profile and
         see its group × platform breakdown. -->
    <div v-if="profileSummary.length > 0" class="profile-summary">
      <div class="profile-summary-head">
        <component :is="icons.users" :width="13" :height="13" />
        <span class="profile-summary-title">Profiles</span>
        <span class="profile-summary-meta">{{ profileSummary.length }} profile{{ profileSummary.length === 1 ? '' : 's' }}</span>
      </div>
      <ul class="profile-summary-list">
        <li
          v-for="p in profileSummary"
          :key="p.id"
          class="profile-summary-row"
          :class="{ on: profileFilter === String(p.id) }"
        >
          <button
            class="profile-summary-name"
            type="button"
            :title="`Filter to ${p.name}`"
            @click="setProfileFilter(p.id)"
          >
            {{ p.name }}
            <span class="profile-summary-count">{{ p.count }} acct{{ p.count === 1 ? '' : 's' }}</span>
          </button>
          <span class="profile-summary-platforms" :title="p.platformsLabel">
            <span
              v-for="slug in p.visiblePlatformSlugs"
              :key="slug"
              class="platform-chip"
              :style="{ background: platformBg(slug) }"
              :title="platformLabel(slug)"
            >
              {{ platformShort(slug) }}
            </span>
            <span v-if="p.overflowPlatformCount > 0" class="platform-chip platform-chip-more" :title="p.overflowPlatformLabel">
              +{{ p.overflowPlatformCount }}
            </span>
          </span>
          <button
            type="button"
            class="profile-summary-toggle"
            :aria-expanded="expandedProfiles.has(p.id)"
            :aria-label="`Toggle ${p.name} group breakdown`"
            @click="toggleProfileExpanded(p.id)"
          >
            <span class="profile-summary-toggle-arrow" :class="{ open: expandedProfiles.has(p.id) }">▾</span>
          </button>
          <ul v-if="expandedProfiles.has(p.id)" class="profile-summary-groups">
            <li
              v-for="g in p.groups"
              :key="`${p.id}:${g.name}`"
              class="profile-summary-group"
              :class="{ on: groupFilter === g.name }"
            >
              <button
                type="button"
                class="profile-summary-group-name"
                @click="setGroupFilter(g.name)"
                :title="`Filter to group ${g.name}`"
              >
                {{ g.displayName }}
              </button>
              <span class="profile-summary-group-count">{{ g.count }}</span>
              <span class="profile-summary-platforms" :title="g.platformsLabel">
                <span
                  v-for="slug in g.platformSlugs"
                  :key="`${p.id}:${g.name}:${slug}`"
                  class="platform-chip platform-chip-mini"
                  :style="{ background: platformBg(slug) }"
                  :title="platformLabel(slug)"
                >
                  {{ platformShort(slug) }}
                </span>
              </span>
            </li>
          </ul>
        </li>
      </ul>
    </div>

    <!-- Datalist shared by all inline group editors — typing into the input
         suggests existing groups, and free-text values create a brand-new
         one on save. -->
    <datalist id="account-group-options">
      <option v-for="g in groups" :key="g" :value="g"></option>
    </datalist>

    <!-- Account grid -->
    <div class="acct-grid">
      <div
        v-for="acct in filteredAccounts"
        :key="acct.id"
        class="acct"
        :class="{ 'acct-editing': isEditing(acct), 'acct-flash': flashId === acct.id }"
        @click="onCardClick(acct)"
      >
        <div class="acct-top">
          <div class="acct-logo-wrap">
            <img :src="acct.avatarUrl || defaultAvatar(acct.accountName)" class="acct-avatar" @error="e => e.target.src = defaultAvatar(acct.accountName)" @click.stop />
            <span class="acct-platform-badge" :style="{ background: platformBg(acct.platformSlug) }">
              {{ platformShort(acct.platformSlug) }}
            </span>
          </div>
          <div style="flex:1;min-width:0" @click.stop>
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
                @click.stop
              />
            </div>
            <div class="acct-handle">{{ acct.connectionDetail || acct.platform }}</div>
            <div class="acct-tags">
              <span
                v-if="acct.profileName && !isEditing(acct)"
                class="acct-tag acct-tag-profile"
                :title="acct.profileSlug ? `slug: ${acct.profileSlug}` : ''"
              >
                <component :is="icons.users" :width="10" :height="10" />
                {{ acct.profileName }}
              </span>
              <span v-if="acct.accountGroup && !isEditing(acct)" class="acct-tag">{{ acct.accountGroup }}</span>
              <select
                v-if="isEditing(acct)"
                class="input inline-input profile-select"
                v-model="editDraft.profileId"
                @click.stop
                @keyup.enter="saveEdit(acct)"
                @keyup.esc="cancelEdit"
              >
                <option value="">— pick profile —</option>
                <option v-for="p in profiles" :key="p.id" :value="String(p.id)">{{ p.name }}</option>
              </select>
              <input
                v-if="isEditing(acct)"
                class="input inline-input group-input"
                list="account-group-options"
                v-model="editDraft.accountGroup"
                placeholder="No group — type to create"
                @keyup.enter="saveEdit(acct)"
                @keyup.esc="cancelEdit"
                @click.stop
              />
              <template v-if="!isEditing(acct)">
                <button
                  class="link-btn edit-btn"
                  @click.stop="startEdit(acct)"
                  title="Edit nickname, profile, and group"
                  aria-label="Edit nickname, profile, and group"
                >
                  <component :is="icons.pencil" :width="12" :height="12" />
                  Edit
                </button>
              </template>
              <template v-else>
                <button class="link-btn save" @click.stop="saveEdit(acct)" :disabled="editSaving" title="Save changes">Save</button>
                <button class="link-btn" @click.stop="cancelEdit" title="Discard changes">Cancel</button>
              </template>
            </div>
          </div>
          <span class="cookie-pill" :class="cookieStatusClass(acct)" @click.stop>
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
        <div class="acct-actions" @click.stop>
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
        <div v-if="isEditing(acct)" class="acct-edit-hint" @click.stop>
          <span>Editing — press <kbd>Enter</kbd> to save, <kbd>Esc</kbd> to cancel</span>
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
                <select class="input" v-model="connectData.profileId">
                  <option :value="null">— pick profile —</option>
                  <option v-for="p in profiles" :key="p.id" :value="p.id">{{ p.name }}</option>
                </select>
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
import { ref, computed, onMounted, nextTick } from 'vue'
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
const profileFilter = ref('')
const searchFilter = ref('')
const groups = ref([])
const profiles = ref([])
// Profile summary strip state — keep IDs that the operator has expanded so
// they survive a filter change (but reset when accounts reload).
const expandedProfiles = ref(new Set())
const editingId = ref(null)
const editDraft = ref({ nickname: '', accountGroup: '', profileId: '' })
const editSaving = ref(false)
const flashId = ref(null)
const editNameInput = ref(null)

const isEditing = (acct) => editingId.value === acct.id

function startEdit(acct) {
  editingId.value = acct.id
  editDraft.value = {
    nickname: acct.nickname || '',
    accountGroup: acct.accountGroup || '',
    // Profile id is a number on the backend; the <select> binds to strings.
    profileId: acct.profileId != null ? String(acct.profileId) : '',
  }
  // Focus the nickname input after Vue flushes the DOM
  nextTick(() => {
    const inputs = editNameInput.value
    if (inputs) {
      const el = Array.isArray(inputs) ? inputs[0] : inputs
      el?.focus?.()
      el?.select?.()
    }
  })
}
function cancelEdit() {
  editingId.value = null
  editDraft.value = { nickname: '', accountGroup: '', profileId: '' }
}
function onCardClick(acct) {
  // Click on the card itself toggles edit mode (excluding inner controls,
  // which stop propagation before this handler fires).
  if (isEditing(acct)) return
  startEdit(acct)
}
async function saveEdit(acct) {
  if (editSaving.value) return
  const nextNick = (editDraft.value.nickname || '').trim()
  const nextGroup = (editDraft.value.accountGroup || '').trim()
  const nextProfileId = editDraft.value.profileId
    ? parseInt(editDraft.value.profileId, 10) || null
    : acct.profileId
  const profileChanged = nextProfileId != null && nextProfileId !== acct.profileId
  if (
    nextNick === (acct.nickname || '') &&
    nextGroup === (acct.accountGroup || '') &&
    !profileChanged
  ) {
    cancelEdit()
    return
  }
  editSaving.value = true
  try {
    const payload = { nickname: nextNick, accountGroup: nextGroup }
    if (profileChanged) payload.profileId = nextProfileId
    await accountApi.updateAccountMeta(acct.id, payload)
    acct.nickname = nextNick
    acct.accountGroup = nextGroup
    if (profileChanged) {
      acct.profileId = nextProfileId
      const newProfile = profiles.value.find(p => p.id === nextProfileId)
      if (newProfile) {
        acct.profileName = newProfile.name
        acct.profileSlug = newProfile.slug
      }
      // Profile assignments affect the per-profile count, so reload.
      await loadProfiles()
    }
    // Refresh groups list if a new group name was added (including clearing a
    // group — reloading from the server keeps the dropdown in sync).
    await loadGroups()
    // Brief highlight so the user sees their save took effect
    flashId.value = acct.id
    setTimeout(() => { if (flashId.value === acct.id) flashId.value = null }, 1200)
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
function onProfileFilterChange(e) {
  profileFilter.value = e.target.value
}

function resetFilters() {
  filter.value = 'all'
  platformFilter.value = ''
  groupFilter.value = ''
  profileFilter.value = ''
  searchFilter.value = ''
}

function setProfileFilter(profileId) {
  // Toggle off when clicking the currently-active profile so the strip
  // behaves like a quick-jump rather than a sticky state.
  const wanted = String(profileId)
  profileFilter.value = profileFilter.value === wanted ? '' : wanted
}

function setGroupFilter(name) {
  groupFilter.value = groupFilter.value === name ? '' : name
}

function toggleProfileExpanded(profileId) {
  // Use a new Set so Vue's reactivity picks up add/delete; a plain Set
  // mutation via .add/.delete won't trigger re-render of the conditional
  // v-for bindings inside the row.
  const next = new Set(expandedProfiles.value)
  if (next.has(profileId)) {
    next.delete(profileId)
  } else {
    next.add(profileId)
  }
  expandedProfiles.value = next
}

const PLATFORM_CHIP_LIMIT = 4

function summarisePlatforms(slugSet) {
  // Stable, label-ordered platform list — used in the chip strip and in the
  // group dropdown suffix so operators can spot duplicates (e.g. "sexualwill"
  // exists in multiple profiles with different platform sets) at a glance.
  const slugs = [...slugSet]
  const labelBySlug = new Map(allPlatforms.value.map(p => [p.slug, p.label]))
  slugs.sort((a, b) => (labelBySlug.get(a) || a).localeCompare(labelBySlug.get(b) || b))
  const visible = slugs.slice(0, PLATFORM_CHIP_LIMIT)
  const overflow = slugs.length - visible.length
  return {
    slugs,
    visibleSlugs: visible,
    overflowCount: overflow,
    overflowLabel: overflow > 0 ? slugs.slice(PLATFORM_CHIP_LIMIT).map(s => platformLabel(s)).join(' · ') : '',
    label: slugs.map(s => platformLabel(s)).join(' · '),
    shortLabel: visible.map(s => platformShort(s)).join(' · '),
  }
}

const hasActiveFilters = computed(() =>
  filter.value !== 'all' ||
  Boolean(platformFilter.value) ||
  Boolean(groupFilter.value) ||
  Boolean(profileFilter.value) ||
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
  if (profileFilter.value) {
    const wanted = parseInt(profileFilter.value, 10)
    if (!Number.isNaN(wanted)) list = list.filter(a => a.profileId === wanted)
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

/* Profile summary strip — per-profile and per-(profile × group) breakdown of
   platforms + counts. Derived client-side from the already-fetched accounts so
   we don't need an extra round-trip. */
const profileSummary = computed(() => {
  const byId = new Map()
  for (const p of profiles.value) {
    byId.set(p.id, {
      id: p.id,
      name: p.name,
      count: 0,
      platformSet: new Set(),
      groupMap: new Map(),
    })
  }
  for (const acct of accounts.value) {
    const bucket = byId.get(acct.profileId)
    if (!bucket) continue
    bucket.count += 1
    if (acct.platformSlug) bucket.platformSet.add(acct.platformSlug)
    const groupKey = acct.accountGroup || '__none__'
    if (!bucket.groupMap.has(groupKey)) {
      bucket.groupMap.set(groupKey, { name: groupKey, count: 0, platformSet: new Set() })
    }
    const g = bucket.groupMap.get(groupKey)
    g.count += 1
    if (acct.platformSlug) g.platformSet.add(acct.platformSlug)
  }
  return [...byId.values()]
    .filter((bucket) => bucket.count > 0)
    .map((bucket) => {
      const platformSummary = summarisePlatforms(bucket.platformSet)
      const groups = [...bucket.groupMap.values()]
        .map((g) => {
          const summary = summarisePlatforms(g.platformSet)
          return {
            name: g.name,
            displayName: g.name === '__none__' ? 'Ungrouped' : g.name,
            count: g.count,
            platformSlugs: summary.slugs,
            platformsLabel: summary.label,
          }
        })
        .sort((a, b) => a.displayName.localeCompare(b.displayName))
      return {
        id: bucket.id,
        name: bucket.name,
        count: bucket.count,
        platformSlugs: platformSummary.slugs,
        visiblePlatformSlugs: platformSummary.visibleSlugs,
        overflowPlatformCount: platformSummary.overflowCount,
        overflowPlatformLabel: platformSummary.overflowLabel,
        platformsLabel: platformSummary.label,
        groups,
      }
    })
    .sort((a, b) => a.name.localeCompare(b.name))
})

/* Group dropdown suffix — surface the platforms under each group name so the
   dropdown tells the operator where each group actually appears. Ungrouped is
   a UI-only bucket (accountGroup === '') and therefore has its own entry. */
const groupsWithPlatforms = computed(() => {
  const groupsMap = new Map()
  for (const acct of accounts.value) {
    const key = acct.accountGroup || ''
    if (!key) continue  // The dropdown has its own "Ungrouped" option.
    if (!groupsMap.has(key)) groupsMap.set(key, new Set())
    if (acct.platformSlug) groupsMap.get(key).add(acct.platformSlug)
  }
  // Also surface the explicit groups list (which is workspace-scoped via the
  // /api/accounts/groups endpoint), so newly-created groups appear in the
  // dropdown even before they have any matching account loaded.
  for (const g of groups.value) {
    if (!groupsMap.has(g)) groupsMap.set(g, new Set())
  }
  return [...groupsMap.entries()]
    .map(([name, set]) => ({
      name,
      platformsLabel: summarisePlatforms(set).label,
    }))
    .sort((a, b) => a.name.localeCompare(b.name))
})

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
  // Pass the actual profile (name + id) so the connect modal pre-selects the
  // right profile instead of "default". Previously this always sent
  // `profile: acct.profileName` which was the literal string "default"
  // because /api/accounts never JOINed the profiles table.
  openConnect({
    platform: acct.platformSlug,
    account: acct.accountName,
    profile: acct.profileName || acct.profileNameLegacy || '',
    profileId: acct.profileId,
  })
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
const connectData = ref({ platform: null, account: '', profile: '', profileId: null, paste: '', config: {} })
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
  connectData.value = {
    platform: initial.platform || null,
    account: initial.account || '',
    // Prefer the explicit profile id when the caller has one (e.g. onReauth
    // from an existing card) — that way the modal lands on the right profile
    // immediately, not the first profile in the list.
    profileId: initial.profileId ?? null,
    profile: initial.profile || '',
    paste: '',
    config: {},
  }
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
    const profileName = connectData.value.profile || ''

    // Step 1: Get profiles. If the modal already has a profileId (e.g.
    // reauth from an existing card), prefer that exact id — otherwise fall
    // back to matching by name, and finally the first profile in the list.
    const profilesRes = await profilesApi.list()
    const allProfiles = profilesRes?.data || profilesRes || []
    let profileId = null
    if (connectData.value.profileId != null) {
      const matched = allProfiles.find(p => p.id === connectData.value.profileId)
      if (matched) profileId = matched.id
    }
    if (profileId == null && profileName) {
      const matched = allProfiles.find(p => p.name === profileName || p.slug === profileName)
      if (matched) profileId = matched.id
    }
    if (profileId == null && allProfiles.length > 0) profileId = allProfiles[0].id
    if (!profileId) throw new Error('No profiles available — create a profile first')

    // Step 2: Find existing account or create new one
    let accountId = null
    const existingAccount = accounts.value.find(
      a => a.platformSlug === platform && a.accountName === accountName
    )

    if (existingAccount) {
      // Reuse existing account for reauth
      accountId = existingAccount.id
    } else {
      // Create new account — backend uses the profile id as the parent.
      const createRes = await profilesApi.createAccount(profileId, {
        accountName,
        platform,
        authType: 'oauth',
        profile: profileName,
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
    const profileName = connectData.value.profile || ''

    const profilesRes = await profilesApi.list()
    const allProfiles = profilesRes?.data || profilesRes || []
    let profileId = null
    if (connectData.value.profileId != null) {
      const matched = allProfiles.find(p => p.id === connectData.value.profileId)
      if (matched) profileId = matched.id
    }
    if (profileId == null && profileName) {
      const matched = allProfiles.find(p => p.name === profileName || p.slug === profileName)
      if (matched) profileId = matched.id
    }
    if (profileId == null && allProfiles.length > 0) profileId = allProfiles[0].id
    if (!profileId) throw new Error('No profiles available — create a profile first')

    // Filter empty values so we don't push blanks into config_json.
    const config = Object.fromEntries(
      Object.entries(connectData.value.config || {}).filter(([, v]) => String(v || '').trim() !== '')
    )

    await profilesApi.createAccount(profileId, {
      accountName,
      platform,
      authType: 'manual',
      profile: profileName,
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
        // Profile metadata — id is the join key, name/slug come straight from
        // the profiles table so the UI can show "Profile: NW" instead of the
        // hard-coded "default" placeholder we used before the JOIN landed.
        profileId: a.profileId ?? null,
        profileName: a.profileName || '',
        profileSlug: a.profileSlug || '',
        profileNameLegacy: a.profile || '',
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

const loadProfiles = async () => {
  try {
    const res = await accountApi.getAccountProfiles()
    profiles.value = (res?.data || res || []).slice().sort((a, b) => a.name.localeCompare(b.name))
  } catch (e) { console.warn('Failed to load profiles:', e) }
}

// Local reactive accounts (not using the store's normalization since we
// get pre-enriched data from /api/accounts)
const accounts = ref([])

onMounted(async () => {
  await Promise.all([loadAccounts(), loadGroups(), loadProfiles()])
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

/* Edit button — give it a visible affordance (border + accent on hover) so
   it isn't lost among the grey text. */
.edit-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  border: 1px solid var(--border, transparent);
  background: var(--bg-2, transparent);
  color: var(--text-2);
  font-size: 11.5px;
  font-weight: 600;
  border-radius: var(--r-full, 999px);
  transition: background 0.15s, color 0.15s, border-color 0.15s;
}
.edit-btn:hover {
  background: var(--accent-soft);
  color: var(--accent);
  border-color: var(--accent);
}

/* Card-level click-to-edit affordance */
.acct {
  cursor: pointer;
  transition: outline-color 0.15s, background 0.15s, box-shadow 0.4s;
}
.acct:hover {
  outline: 1px solid var(--border, rgba(255, 255, 255, 0.08));
}
.acct.editing {
  cursor: default;
}
.acct.acct-flash {
  box-shadow: 0 0 0 2px var(--accent), 0 0 12px var(--accent-soft);
}

/* Filter field wrapper (icon + control) */
.filter-field {
  position: relative;
  display: inline-flex;
  align-items: center;
}
.filter-icon {
  position: absolute;
  left: 9px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--text-3);
  pointer-events: none;
}
.filter-input-padded { padding-left: 26px; }
.filter-select-padded { padding-left: 26px; }

/* Inline editing hint */
.acct-edit-hint {
  margin-top: 8px;
  padding: 6px 10px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 11.5px;
  border-radius: 6px;
  display: flex;
  align-items: center;
}
.acct-edit-hint kbd {
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0 4px;
  font-family: var(--font-mono);
  font-size: 11px;
  margin: 0 2px;
}
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

/* Profile summary strip — sits directly under the toolbar so operators can
   see, at a glance, which profiles own accounts on which platforms (and how
   those accounts are bucketed into groups) before they dive into the cards. */
.profile-summary {
  margin: -6px 0 18px;
  padding: 12px 14px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  box-shadow: var(--shadow);
}
.profile-summary-head {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-2);
  font-size: 11.5px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin-bottom: 10px;
}
.profile-summary-title {
  color: var(--text);
}
.profile-summary-meta {
  margin-left: 4px;
  color: var(--text-3);
  font-weight: 500;
  text-transform: none;
  letter-spacing: 0;
}
.profile-summary-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.profile-summary-row {
  display: grid;
  grid-template-columns: minmax(140px, 200px) 1fr auto;
  align-items: center;
  gap: 12px;
  padding: 8px 10px;
  border-radius: var(--r-md);
  border: 1px solid transparent;
  background: var(--panel-2, rgba(255, 255, 255, 0.02));
  transition: background 0.15s, border-color 0.15s;
}
.profile-summary-row.on {
  border-color: var(--accent);
  background: var(--accent-soft);
}
.profile-summary-name {
  display: flex;
  align-items: center;
  gap: 6px;
  background: none;
  border: none;
  padding: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  cursor: pointer;
  text-align: left;
}
.profile-summary-name:hover { color: var(--accent); }
.profile-summary-count {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-3);
  font-weight: 500;
}
.profile-summary-platforms {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px;
}
.profile-summary-toggle {
  background: none;
  border: 1px solid var(--line);
  color: var(--text-2);
  border-radius: var(--r-md);
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  cursor: pointer;
  transition: background 0.15s, color 0.15s, border-color 0.15s;
}
.profile-summary-toggle:hover {
  border-color: var(--accent);
  color: var(--accent);
}
.profile-summary-toggle-arrow {
  font-size: 13px;
  line-height: 1;
  transition: transform 0.18s var(--ease, ease-out);
  display: inline-block;
}
.profile-summary-toggle-arrow.open { transform: rotate(180deg); }
.platform-chip {
  display: inline-grid;
  place-items: center;
  min-width: 22px;
  height: 22px;
  padding: 0 6px;
  border-radius: var(--r-full);
  color: #fff;
  font-size: 10.5px;
  font-weight: 700;
  font-family: var(--font-mono);
  letter-spacing: 0.02em;
}
.platform-chip-mini {
  min-width: 18px;
  height: 18px;
  font-size: 9.5px;
  padding: 0 4px;
}
.platform-chip-more {
  background: var(--panel-2, rgba(255, 255, 255, 0.06));
  color: var(--text-2);
  border: 1px dashed var(--line-2);
  letter-spacing: 0;
}
.profile-summary-groups {
  grid-column: 1 / -1;
  list-style: none;
  margin: 4px 0 -2px;
  padding: 6px 0 0 14px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  border-top: 1px dashed var(--line);
}
.profile-summary-group {
  display: grid;
  grid-template-columns: minmax(120px, 1fr) auto 2fr;
  align-items: center;
  gap: 10px;
  padding: 4px 8px;
  border-radius: var(--r-md);
  font-size: 12px;
  color: var(--text-2);
}
.profile-summary-group.on {
  background: var(--accent-soft);
  color: var(--accent);
}
.profile-summary-group-name {
  background: none;
  border: none;
  padding: 0;
  font: inherit;
  color: inherit;
  cursor: pointer;
  text-align: left;
}
.profile-summary-group-name:hover { color: var(--accent); }
.profile-summary-group-count {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-3);
}
@media (max-width: 760px) {
  .profile-summary-row {
    grid-template-columns: 1fr auto;
  }
  .profile-summary-platforms {
    grid-column: 1 / -1;
  }
  .profile-summary-group {
    grid-template-columns: 1fr auto;
  }
  .profile-summary-group .profile-summary-platforms {
    grid-column: 1 / -1;
  }
}
</style>
