<template>
  <main class="landing-page">
    <!-- Navigation -->
    <nav class="nav-bar">
      <div class="nav-inner">
        <router-link class="nav-logo" to="/">
          <img src="/socialupload-app-icon.png" alt="Socialupload" class="nav-icon" />
          <span class="nav-brand">Socialupload</span>
        </router-link>
        <div class="nav-links">
          <a href="#" @click.prevent="scrollTo('features')">Features</a>
          <a href="#" @click.prevent="scrollTo('platforms')">Platforms</a>
          <a href="#" @click.prevent="scrollTo('how-it-works')">How It Works</a>
          <a href="#" @click.prevent="scrollTo('quick-start')">Quick Start</a>
          <a href="#" @click.prevent="scrollTo('api')">API</a>
          <router-link to="/privacy">Privacy</router-link>
          <router-link to="/terms">Terms</router-link>
          <router-link class="nav-cta" to="/login">Sign In</router-link>
        </div>
      </div>
    </nav>

    <!-- Hero -->
    <section class="hero">
      <div class="hero-inner">
        <p class="eyebrow">The all-in-one publishing command center for creators and operators</p>
        <h1>
          Create, schedule, publish, and engage
          <span class="highlight">across every platform</span>
        </h1>
        <p class="subheadline">
          Socialupload unifies your entire social media workflow &mdash; from content drafting and
          cross-platform scheduling to audience engagement, comment management, and real-time
          performance analytics. One dashboard. Every channel. Total control.
        </p>
        <div class="hero-actions">
          <router-link class="btn btn-primary" to="/login">Get Started</router-link>
          <a class="btn btn-secondary" href="#" @click.prevent="scrollTo('features')">Explore Features</a>
        </div>
        <div class="platform-strip">
          <span class="platform-label">Works with</span>
          <div class="platform-icons">
            <span class="platform-chip">TikTok</span>
            <span class="platform-chip">YouTube</span>
            <span class="platform-chip">Instagram</span>
            <span class="platform-chip">Facebook</span>
            <span class="platform-chip">Threads</span>
            <span class="platform-chip">Reddit</span>
            <span class="platform-chip">X / Twitter</span>
            <span class="platform-chip">Bilibili</span>
            <span class="platform-chip">Douyin</span>
            <span class="platform-chip">Xiaohongshu</span>
            <span class="platform-chip">Kuaishou</span>
            <span class="platform-chip">Telegram</span>
            <span class="platform-chip">Discord</span>
          </div>
        </div>
      </div>
    </section>

    <!-- Features -->
    <section id="features" class="features">
      <div class="section-inner">
        <h2 class="section-title">Everything you need to run your social presence at scale</h2>
        <p class="section-subtitle">
          No more jumping between browser tabs, native apps, and analytics dashboards.
          Socialupload brings publishing, engagement, and insights together in one place.
        </p>

        <div class="feature-category">
          <h3 class="category-title">Content &amp; Publishing</h3>
          <div class="feature-grid">
            <div class="feature-card">
              <div class="feature-icon">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>
              </div>
              <h3>Multi-Platform Publishing</h3>
              <p>
                Upload once, publish everywhere. Support for TikTok, YouTube, Instagram,
                Facebook, Threads, Reddit, X, Bilibili, Douyin, Xiaohongshu, Kuaishou,
                Telegram, Discord, and more.
              </p>
            </div>

            <div class="feature-card">
              <div class="feature-icon">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
              </div>
              <h3>Smart Scheduling</h3>
              <p>
                Queue posts for the optimal time on each platform. Set publish dates,
                stagger releases across time zones, and let the worker drain the queue
                automatically with retry logic and exponential backoff.
              </p>
            </div>

            <div class="feature-card">
              <div class="feature-icon">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
              </div>
              <h3>Rich Metadata Editor</h3>
              <p>
                Craft captions, hashtags, and titles per platform. Set privacy levels,
                age restrictions, category tags, thumbnail covers, and
                platform-specific options before any content goes live.
              </p>
            </div>
          </div>
        </div>

        <div class="feature-category">
          <h3 class="category-title">Audience Engagement</h3>
          <div class="feature-grid">
            <div class="feature-card">
              <div class="feature-icon">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
              </div>
              <h3>Comment Management</h3>
              <p>
                View, reply to, hide, and delete comments across Instagram, Facebook,
                Threads, and TikTok from a unified inbox. Never miss a conversation
                with your audience.
              </p>
            </div>

            <div class="feature-card">
              <div class="feature-icon">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
              </div>
              <h3>Direct Messaging</h3>
              <p>
                Manage Instagram and Messenger DMs directly from Socialupload.
                Human-agent handoff support lets you respond personally within
                the 7-day messaging window.
              </p>
            </div>

            <div class="feature-card">
              <div class="feature-icon">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>
              </div>
              <h3>Engagement Tracking</h3>
              <p>
                Monitor likes, reactions, shares, and saves across platforms.
                Understand which content resonates with your audience and
                double down on what works.
              </p>
            </div>
          </div>
        </div>

        <div class="feature-category">
          <h3 class="category-title">Analytics &amp; Insights</h3>
          <div class="feature-grid">
            <div class="feature-card">
              <div class="feature-icon">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>
              </div>
              <h3>Performance Analytics</h3>
              <p>
                Track views, reach, impressions, watch time, and engagement rates.
                Platform-level breakdowns let you compare performance across TikTok,
                YouTube, Instagram, and more in a single view.
              </p>
            </div>

            <div class="feature-card">
              <div class="feature-icon">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
              </div>
              <h3>Audience Insights</h3>
              <p>
                Understand follower demographics, growth trends, and peak
                activity windows. Use data to inform your content strategy
                and posting schedule.
              </p>
            </div>

            <div class="feature-card">
              <div class="feature-icon">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
              </div>
              <h3>Content Performance</h3>
              <p>
                Per-post metrics including reach, engagement rate, shares,
                saves, and comment sentiment. Identify top-performing content
                and replicate what works across platforms.
              </p>
            </div>
          </div>
        </div>

        <div class="feature-category">
          <h3 class="category-title">Infrastructure &amp; Security</h3>
          <div class="feature-grid">
            <div class="feature-card">
              <div class="feature-icon">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
              </div>
              <h3>Enterprise-Grade Security</h3>
              <p>
                OAuth-based authentication for every platform. AES-GCM encryption
                protects credentials at rest. API token enforcement with constant-time
                comparison. You control who accesses your deployment.
              </p>
            </div>

            <div class="feature-card">
              <div class="feature-icon">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>
              </div>
              <h3>Self-Hosted</h3>
              <p>
                Deploy on your own infrastructure with a single Docker command.
                Keep full ownership of your data, media files, cookies, and
                publishing history. No third-party access to your accounts.
              </p>
            </div>

            <div class="feature-card">
              <div class="feature-icon">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>
              </div>
              <h3>Reliable Job Queue</h3>
              <p>
                Background workers process publishing jobs with bounded concurrency,
                per-platform retry with exponential backoff, and structured JSON
                logging. Every job gets a detailed audit trail.
              </p>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- Platforms -->
    <section id="platforms" class="platforms-section">
      <div class="section-inner">
        <h2 class="section-title">Every platform. Every capability.</h2>
        <p class="section-subtitle">
          Socialupload connects to the platforms that matter, with deep integration
          for publishing, engagement, and analytics on each one.
        </p>
        <div class="platform-grid">
          <div class="platform-item" v-for="p in platforms" :key="p.name">
            <div class="platform-header">
              <span class="platform-name">{{ p.name }}</span>
              <span v-if="p.badge" class="platform-badge">{{ p.badge }}</span>
            </div>
            <p class="platform-desc">{{ p.desc }}</p>
            <div class="platform-capabilities">
              <span v-for="cap in p.capabilities" :key="cap" class="cap-tag">{{ cap }}</span>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- How it works -->
    <section id="how-it-works" class="how-it-works">
      <div class="section-inner">
        <h2 class="section-title">How it works</h2>
        <div class="steps">
          <div class="step">
            <div class="step-number">1</div>
            <h3>Connect your accounts</h3>
            <p>
              Link your social media accounts using secure OAuth flows &mdash; TikTok Login Kit,
              YouTube OAuth, Meta Login, and more. Each connection is verified before use.
            </p>
          </div>
          <div class="step">
            <div class="step-number">2</div>
            <h3>Upload and prepare</h3>
            <p>
              Upload videos and images, write captions, add hashtags, configure
              platform-specific settings, and preview your content exactly as it will appear.
            </p>
          </div>
          <div class="step">
            <div class="step-number">3</div>
            <h3>Schedule or publish now</h3>
            <p>
              Push content live immediately or queue it for a specific date and time.
              The worker handles the rest with automatic retry and detailed job logs.
            </p>
          </div>
          <div class="step">
            <div class="step-number">4</div>
            <h3>Engage and analyze</h3>
            <p>
              Monitor comments, DMs, reactions, and performance metrics. Respond to
              your audience and use data to refine your content strategy.
            </p>
          </div>
        </div>
      </div>
    </section>

    <!-- Quick Start -->
    <section id="quick-start" class="quick-start">
      <div class="section-inner">
        <h2 class="section-title">Quick Start</h2>
        <div class="quick-start-grid">
          <div class="qs-card">
            <h3>1. Create a Profile</h3>
            <pre class="code-block">curl -X POST /profiles \
  -H "Authorization: Bearer TOKEN" \
  -d '{"name":"My Brand",
       "system_prompt":"You are a social media manager.",
       "default_cta":"Follow us!"}'</pre>
          </div>
          <div class="qs-card">
            <h3>2. Upload Media</h3>
            <pre class="code-block">curl -X POST /api/media/upload/batch \
  -H "Authorization: Bearer TOKEN" \
  -F "files=@video.mp4" \
  -F "files=@photo.jpg"</pre>
          </div>
          <div class="qs-card">
            <h3>3. Generate Content</h3>
            <pre class="code-block">curl -X POST /api/campaigns/1/generate \
  -H "Authorization: Bearer TOKEN" \
  -d '{"platforms":["twitter",
       "instagram","facebook"]}'</pre>
          </div>
          <div class="qs-card">
            <h3>4. Export or Publish</h3>
            <pre class="code-block"># Export to Google Sheet
curl -X POST /api/campaigns/1/export/google-sheet \
  -d '{"profile_slug":"my-brand"}'

# Or download CSV
curl /api/campaigns/1/export/csv -o export.csv</pre>
          </div>
        </div>
        <div class="qs-actions">
          <router-link class="btn btn-primary" to="/api-docs">View Full API Docs</router-link>
          <a class="btn btn-secondary" href="https://github.com/Willywang8216/social-auto-upload" target="_blank">GitHub</a>
        </div>
      </div>
    </section>

    <!-- API Reference -->
    <section id="api" class="api-section">
      <div class="section-inner">
        <h2 class="section-title">API Reference</h2>
        <p class="section-desc">Full REST API for automation and integration. All endpoints support JSON request/response.</p>
        <div class="api-grid">
          <div class="api-card">
            <h3>📋 Profiles</h3>
            <p>CRUD for brand profiles with system prompts, writing styles, and default settings.</p>
            <code>GET/POST /profiles</code>
          </div>
          <div class="api-card">
            <h3>🎬 Media Assets</h3>
            <p>Batch upload, process (watermark/thumbnail), and manage media files.</p>
            <code>POST /api/media/upload/batch</code>
          </div>
          <div class="api-card">
            <h3>💧 Watermarks</h3>
            <p>Configure text/image watermarks with dynamic positioning for videos.</p>
            <code>POST /api/watermark-configs</code>
          </div>
          <div class="api-card">
            <h3>📢 Campaigns</h3>
            <p>Generate, validate, approve, and export platform-specific content.</p>
            <code>POST /api/campaigns/:id/generate</code>
          </div>
          <div class="api-card">
            <h3>📊 Sheet Export</h3>
            <p>Export to Google Sheets (20-column format) or download as CSV.</p>
            <code>POST /api/campaigns/:id/export/google-sheet</code>
          </div>
          <div class="api-card">
            <h3>⚙️ Jobs</h3>
            <p>Async job queue with progress tracking, retry, and cancellation.</p>
            <code>GET /jobs</code>
          </div>
        </div>
      </div>
    </section>

    <!-- CTA -->
    <section class="cta-section">
      <div class="section-inner">
        <h2>Ready to take control of your social presence?</h2>
        <p>
          Deploy Socialupload on your own server, connect your accounts, and start
          publishing across every platform from one unified dashboard.
        </p>
        <router-link class="btn btn-primary btn-large" to="/login">Get Started</router-link>
        <p class="cta-note">Self-hosted. Self-contained. Your data, your rules.</p>
      </div>
    </section>

    <!-- Footer -->
    <footer class="site-footer">
      <div class="footer-inner">
        <div class="footer-brand">
          <img src="/socialupload-app-icon.png" alt="Socialupload" class="footer-icon" />
          <span class="footer-name">Socialupload</span>
          <p class="footer-tagline">The all-in-one publishing command center for creators and operators.</p>
        </div>
        <div class="footer-links">
          <div class="footer-col">
            <h4>Product</h4>
            <a href="#features">Features</a>
            <a href="#platforms">Platforms</a>
            <a href="#how-it-works">How It Works</a>
            <a href="#quick-start">Quick Start</a>
            <a href="#api">API</a>
            <router-link to="/login">Sign In</router-link>
          </div>
          <div class="footer-col">
            <h4>Legal</h4>
            <router-link to="/privacy">Privacy Policy</router-link>
            <router-link to="/terms">Terms of Service</router-link>
          </div>
        </div>
        <div class="footer-bottom">
          <p>&copy; {{ new Date().getFullYear() }} Socialupload. All rights reserved.</p>
        </div>
      </div>
    </footer>
  </main>
</template>

<script setup>
function scrollTo(id) {
  const el = document.getElementById(id)
  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
}

const platforms = [
  {
    name: 'TikTok',
    badge: 'Direct API',
    desc: 'Full Content Posting API integration with draft creation and direct publish modes.',
    capabilities: ['Video Upload', 'Draft Mode', 'Scheduling', 'Captions', 'Privacy Controls']
  },
  {
    name: 'YouTube',
    badge: 'OAuth',
    desc: 'Video uploads via YouTube Data API v3 with full metadata, thumbnails, and playlist support.',
    capabilities: ['Video Upload', 'Thumbnails', 'Playlists', 'Titles & Descriptions', 'Privacy Levels']
  },
  {
    name: 'Instagram',
    badge: 'Meta API',
    desc: 'Publish feed posts, reels, and stories. Manage comments, likes, and DMs through the Meta Graph API.',
    capabilities: ['Feed Posts', 'Reels', 'Stories', 'Comments', 'DM Management', 'Insights']
  },
  {
    name: 'Facebook',
    badge: 'Meta API',
    desc: 'Page content publishing, comment moderation, and Messenger DM management.',
    capabilities: ['Page Posts', 'Scheduling', 'Comment Moderation', 'Messenger DMs', 'Reactions']
  },
  {
    name: 'Threads',
    badge: 'Meta API',
    desc: 'Text and media posts to Threads with reply management and engagement tracking.',
    capabilities: ['Text Posts', 'Media Posts', 'Reply Management', 'Engagement Tracking']
  },
  {
    name: 'Reddit',
    badge: 'OAuth',
    desc: 'Video and media posts to subreddits with flair selection and scheduled posting.',
    capabilities: ['Video Posts', 'Subreddit Targeting', 'Flairs', 'Scheduling', 'Comment Replies']
  },
  {
    name: 'X / Twitter',
    badge: 'API v2',
    desc: 'Tweet posting with media attachments, thread support, and engagement monitoring.',
    capabilities: ['Tweets', 'Threads', 'Media', 'Likes & Retweets', 'Reply Management']
  },
  {
    name: 'Bilibili',
    desc: 'Video uploads with full metadata for the Chinese creator ecosystem.',
    capabilities: ['Video Upload', 'Tags & Categories', 'Cover Images', 'Scheduling']
  },
  {
    name: 'Douyin',
    desc: 'Chinese TikTok publishing with account management and scheduled uploads.',
    capabilities: ['Video Upload', 'Draft Mode', 'Captions', 'Scheduling', 'Account Mgmt']
  },
  {
    name: 'Xiaohongshu',
    desc: 'Short video and image posts with rich product tagging for the RED community.',
    capabilities: ['Video Posts', 'Image Posts', 'Product Tags', 'Captions', 'Scheduling']
  },
  {
    name: 'Kuaishou',
    desc: 'Short video publishing for the Kuaishou platform with metadata management.',
    capabilities: ['Video Upload', 'Captions', 'Tags', 'Scheduling']
  },
  {
    name: 'Telegram',
    desc: 'Channel and group messaging with rich media and scheduled delivery.',
    capabilities: ['Channel Posts', 'Media Upload', 'Rich Text', 'Scheduling']
  },
  {
    name: 'Discord',
    desc: 'Webhook-based message delivery to Discord channels with embed support.',
    capabilities: ['Channel Posts', 'Embeds', 'Media', 'Webhook Integration']
  },
  {
    name: 'Patreon',
    desc: 'Exclusive content publishing for patron tiers with scheduled delivery.',
    capabilities: ['Tiered Posts', 'Media', 'Scheduling', 'Subscriber Management']
  }
]
</script>

<style lang="scss" scoped>
// Variables
$primary: #3b82f6;
$primary-dark: #2563eb;
$text: #1e293b;
$text-secondary: #64748b;
$bg: #f8fafc;
$surface: #ffffff;
$border: #e2e8f0;
$radius: 12px;
$max-width: 1120px;

.landing-page {
  min-height: 100vh;
  background: $bg;
  color: $text;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  line-height: 1.6;
}

// Navigation
.nav-bar {
  position: sticky;
  top: 0;
  z-index: 100;
  background: rgba($surface, 0.92);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid $border;
}

.nav-inner {
  max-width: $max-width;
  margin: 0 auto;
  padding: 0 24px;
  height: 64px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.nav-logo {
  display: flex;
  align-items: center;
  gap: 10px;
  text-decoration: none;
  color: $text;
}

.nav-icon {
  width: 32px;
  height: 32px;
  border-radius: 8px;
}

.nav-brand {
  font-weight: 700;
  font-size: 18px;
  letter-spacing: -0.02em;
}

.nav-links {
  display: flex;
  align-items: center;
  gap: 28px;

  a {
    text-decoration: none;
    color: $text-secondary;
    font-size: 14px;
    font-weight: 500;
    transition: color 0.15s;

    &:hover {
      color: $text;
    }
  }
}

.nav-cta {
  background: $primary !important;
  color: #fff !important;
  padding: 8px 18px;
  border-radius: 8px;
  font-weight: 600 !important;

  &:hover {
    background: $primary-dark !important;
  }
}

// Hero
.hero {
  padding: 80px 24px 60px;
  text-align: center;
}

.hero-inner {
  max-width: 820px;
  margin: 0 auto;
}

.eyebrow {
  font-size: 14px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: $primary;
  margin: 0 0 16px;
}

.hero h1 {
  font-size: 48px;
  font-weight: 800;
  line-height: 1.15;
  letter-spacing: -0.03em;
  margin: 0 0 20px;
  color: $text;
}

.highlight {
  color: $primary;
}

.subheadline {
  font-size: 18px;
  color: $text-secondary;
  line-height: 1.7;
  max-width: 680px;
  margin: 0 auto 32px;
}

.hero-actions {
  display: flex;
  justify-content: center;
  gap: 12px;
  margin-bottom: 40px;
}

// Buttons
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 12px 28px;
  border-radius: 8px;
  font-size: 15px;
  font-weight: 600;
  text-decoration: none;
  transition: all 0.15s;
  cursor: pointer;
}

.btn-primary {
  background: $primary;
  color: #fff;

  &:hover {
    background: $primary-dark;
    transform: translateY(-1px);
    box-shadow: 0 4px 12px rgba($primary, 0.35);
  }
}

.btn-secondary {
  background: $surface;
  color: $text;
  border: 1px solid $border;

  &:hover {
    background: #f1f5f9;
    border-color: #cbd5e1;
  }
}

.btn-large {
  padding: 16px 36px;
  font-size: 17px;
}

// Platform strip
.platform-strip {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  flex-wrap: wrap;
}

.platform-label {
  font-size: 13px;
  color: $text-secondary;
  font-weight: 500;
}

.platform-icons {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: center;
}

.platform-chip {
  background: $surface;
  border: 1px solid $border;
  border-radius: 6px;
  padding: 4px 12px;
  font-size: 13px;
  font-weight: 500;
  color: $text-secondary;
}

// Section shared
.section-inner {
  max-width: $max-width;
  margin: 0 auto;
  padding: 0 24px;
}

.section-title {
  font-size: 32px;
  font-weight: 700;
  letter-spacing: -0.02em;
  text-align: center;
  margin: 0 0 12px;
}

.section-subtitle {
  font-size: 16px;
  color: $text-secondary;
  text-align: center;
  max-width: 640px;
  margin: 0 auto 48px;
  line-height: 1.7;
}

// Features
.features {
  padding: 80px 24px;
  background: $surface;
  border-top: 1px solid $border;
  border-bottom: 1px solid $border;
}

.feature-category {
  margin-bottom: 48px;

  &:last-child {
    margin-bottom: 0;
  }
}

.category-title {
  font-size: 13px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: $primary;
  margin: 0 0 20px;
  text-align: center;
}

.feature-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 24px;
}

.feature-card {
  background: $bg;
  border: 1px solid $border;
  border-radius: $radius;
  padding: 28px;

  h3 {
    font-size: 17px;
    font-weight: 600;
    margin: 16px 0 8px;
  }

  p {
    font-size: 14px;
    color: $text-secondary;
    line-height: 1.7;
    margin: 0;
  }
}

.feature-icon {
  width: 48px;
  height: 48px;
  border-radius: 10px;
  background: rgba($primary, 0.1);
  color: $primary;
  display: flex;
  align-items: center;
  justify-content: center;
}

// Platforms section
.platforms-section {
  padding: 80px 24px;
}

.platform-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
}

.platform-item {
  background: $surface;
  border: 1px solid $border;
  border-radius: $radius;
  padding: 24px;

  .platform-header {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 8px;
  }

  .platform-name {
    font-size: 17px;
    font-weight: 700;
  }

  .platform-badge {
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    background: rgba($primary, 0.1);
    color: $primary;
    padding: 2px 8px;
    border-radius: 4px;
  }

  .platform-desc {
    font-size: 14px;
    color: $text-secondary;
    line-height: 1.6;
    margin: 0 0 12px;
  }
}

.platform-capabilities {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.cap-tag {
  font-size: 11px;
  font-weight: 500;
  color: $text-secondary;
  background: $bg;
  border: 1px solid $border;
  border-radius: 4px;
  padding: 2px 8px;
}

// How it works
.how-it-works {
  padding: 80px 24px;
  background: $surface;
  border-top: 1px solid $border;
  border-bottom: 1px solid $border;
}

.steps {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 32px;
  text-align: center;
}

.step {
  .step-number {
    width: 48px;
    height: 48px;
    border-radius: 50%;
    background: $primary;
    color: #fff;
    font-size: 20px;
    font-weight: 700;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    margin-bottom: 16px;
  }

  h3 {
    font-size: 17px;
    font-weight: 600;
    margin: 0 0 8px;
  }

  p {
    font-size: 14px;
    color: $text-secondary;
    line-height: 1.7;
    margin: 0;
  }
}

// Quick Start
.quick-start {
  padding: 80px 24px;
  background: $bg;

  .quick-start-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-bottom: 32px;
  }

  .qs-card {
    background: white;
    border-radius: 12px;
    padding: 24px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);

    h3 {
      font-size: 16px;
      font-weight: 600;
      margin: 0 0 12px;
    }
  }

  .code-block {
    background: #1e1e1e;
    color: #d4d4d4;
    padding: 16px;
    border-radius: 8px;
    font-size: 12px;
    line-height: 1.6;
    overflow-x: auto;
    white-space: pre;
    margin: 0;
  }

  .qs-actions {
    text-align: center;
    display: flex;
    gap: 16px;
    justify-content: center;
  }
}

// API Reference
.api-section {
  padding: 80px 24px;

  .section-desc {
    text-align: center;
    color: $text-secondary;
    margin: 0 0 40px;
    font-size: 16px;
  }

  .api-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 20px;
  }

  .api-card {
    background: $bg;
    border-radius: 12px;
    padding: 24px;
    transition: transform 0.2s, box-shadow 0.2s;

    &:hover {
      transform: translateY(-2px);
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
    }

    h3 {
      font-size: 18px;
      font-weight: 600;
      margin: 0 0 8px;
    }

    p {
      font-size: 14px;
      color: $text-secondary;
      margin: 0 0 12px;
      line-height: 1.5;
    }

    code {
      background: #e8e8e8;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 12px;
      color: #333;
    }
  }
}

// CTA
.cta-section {
  padding: 80px 24px;
  text-align: center;

  h2 {
    font-size: 32px;
    font-weight: 700;
    letter-spacing: -0.02em;
    margin: 0 0 12px;
  }

  p {
    font-size: 16px;
    color: $text-secondary;
    max-width: 540px;
    margin: 0 auto 28px;
    line-height: 1.7;
  }
}

.cta-note {
  font-size: 13px;
  color: $text-secondary;
  opacity: 0.6;
  margin-top: 16px;
}

// Footer
.site-footer {
  background: $text;
  color: #cbd5e1;
  padding: 48px 24px 32px;
}

.footer-inner {
  max-width: $max-width;
  margin: 0 auto;
}

.footer-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 32px;
}

.footer-icon {
  width: 28px;
  height: 28px;
  border-radius: 6px;
}

.footer-name {
  font-size: 16px;
  font-weight: 700;
  color: #fff;
}

.footer-tagline {
  width: 100%;
  font-size: 14px;
  margin: 4px 0 0;
  opacity: 0.7;
}

.footer-links {
  display: flex;
  gap: 64px;
  margin-bottom: 32px;
}

.footer-col {
  h4 {
    font-size: 13px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #94a3b8;
    margin: 0 0 12px;
  }

  a {
    display: block;
    font-size: 14px;
    color: #cbd5e1;
    text-decoration: none;
    margin-bottom: 8px;

    &:hover {
      color: #fff;
    }
  }
}

.footer-bottom {
  border-top: 1px solid rgba(#fff, 0.1);
  padding-top: 20px;

  p {
    font-size: 13px;
    margin: 0;
    opacity: 0.5;
  }
}

// Responsive
@media (max-width: 900px) {
  .feature-grid {
    grid-template-columns: repeat(2, 1fr);
  }

  .platform-grid {
    grid-template-columns: 1fr;
  }

  .steps {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 768px) {
  .nav-links {
    gap: 16px;

    a:not(.nav-cta) {
      display: none;
    }
  }

  .hero {
    padding: 60px 16px 40px;

    h1 {
      font-size: 32px;
    }
  }

  .subheadline {
    font-size: 16px;
  }

  .feature-grid,
  .steps {
    grid-template-columns: 1fr;
  }

  .section-title {
    font-size: 26px;
  }

  .footer-links {
    gap: 32px;
  }
}
</style>