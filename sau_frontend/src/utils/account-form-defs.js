export const redditFieldDefs = [
  { key: 'subreddits', label: 'Subreddits', placeholder: 'e.g. videos, funny, gaming (comma-separated)', type: 'tags' },
  { key: 'clientIdEnv', label: 'Client ID Env', placeholder: '例如：REDDIT_CLIENT_ID' },
  { key: 'clientSecretEnv', label: 'Client Secret Env', placeholder: '例如：REDDIT_CLIENT_SECRET' },
  { key: 'refreshTokenEnv', label: 'Refresh Token Env', placeholder: '例如：REDDIT_REFRESH_TOKEN' }
]

export const youtubeIdentityFieldDefs = [
  { key: 'channelId', label: 'Channel ID', placeholder: '例如：UCxxxx' }
]

export const youtubeFieldDefs = [
  { key: 'playlistId', label: 'Playlist ID', placeholder: '可選，自動加入播放清單' },
  { key: 'categoryId', label: 'Category ID', placeholder: '預設 22' },
  { key: 'clientIdEnv', label: 'Client ID Env', placeholder: '例如：YT_CLIENT_ID' },
  { key: 'clientSecretEnv', label: 'Client Secret Env', placeholder: '例如：YT_CLIENT_SECRET' },
  { key: 'refreshTokenEnv', label: 'Refresh Token Env', placeholder: '例如：YT_REFRESH_TOKEN' }
]

export const facebookFieldDefs = [
  { key: 'pageId', label: 'Page ID' },
  { key: 'accessTokenEnv', label: 'Access Token Env', placeholder: '例如：FB_PAGE_TOKEN' }
]

export const instagramFieldDefs = [
  { key: 'igUserId', label: 'IG User ID' },
  { key: 'accessTokenEnv', label: 'Access Token Env', placeholder: '例如：IG_ACCESS_TOKEN' }
]

export const threadsFieldDefs = [
  { key: 'threadUserId', label: 'User ID' },
  { key: 'accessTokenEnv', label: 'Access Token Env', placeholder: '例如：THREADS_ACCESS_TOKEN' }
]

export const telegramFieldDefs = [
  { key: 'chatId', label: 'Chat ID (legacy)', placeholder: '例如：@channel_name 或 -100123456（單一目標）', hint: '單一目標時使用。需多個請改填下方的 Chat IDs（多個）。' },
  { key: 'chatIds', label: 'Chat IDs（多個，逗號分隔）', placeholder: '例如：@channel_a, -100123456, @group_b', hint: '同一支 bot 會依序送出至每個 chat id。多 channel / group 場景推薦使用。' },
  { key: 'botTokenEnv', label: 'Bot Token Env', placeholder: '例如：TELEGRAM_BOT_TOKEN', hint: '對應 .env 裡的環境變數名稱。Bot token 由 @BotFather 取得，僅存於 .env 不入庫。' }
]

export const tiktokTokenFieldDefs = [
  { key: 'accessToken', label: 'Access Token', placeholder: '由 TikTok Connect 自動填入，或手動貼上', type: 'textarea', rows: 2 },
  { key: 'refreshToken', label: 'Refresh Token', placeholder: '由 TikTok Connect 自動填入', type: 'textarea', rows: 2 },
  { key: 'accessTokenEnv', label: 'Access Token Env', placeholder: '例如：TIKTOK_ACCESS_TOKEN；若已直連可留空' },
  { key: 'videoCoverTimestampMs', label: '封面時間 ms', placeholder: '例如：1000' }
]

export const discordFieldDefs = [
  { key: 'webhookUrlEnv', label: 'Webhook URL Env', placeholder: '例如：DISCORD_WEBHOOK_URL' }
]

export const twitterFieldDefs = [
  { key: 'apiKeyEnv', label: 'API Key Env', placeholder: '可留空，預設讀取 X_API_KEY' },
  { key: 'apiKeySecretEnv', label: 'API Key Secret Env', placeholder: '可留空，預設讀取 X_API_KEY_SECRET' },
  { key: 'accessTokenEnv', label: 'Access Token Env', placeholder: '可留空，預設讀取 X_ACCESS_TOKEN' },
  { key: 'accessTokenSecretEnv', label: 'Access Token Secret Env', placeholder: '可留空，預設讀取 X_ACCESS_TOKEN_SECRET' },
]
