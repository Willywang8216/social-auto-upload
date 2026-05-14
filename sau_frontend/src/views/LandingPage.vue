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
          <router-link to="/privacy">Privacy</router-link>
          <router-link to="/terms">Terms</router-link>
          <router-link class="nav-cta" to="/login">Sign In</router-link>
        </div>
      </div>
    </nav>

    <!-- Hero -->
    <section class="hero">
      <div class="hero-inner">
        <p class="eyebrow">Publishing workspace for creators and operators</p>
        <h1>
          Manage, schedule, and publish your social&nbsp;content
          <span class="highlight">from one dashboard</span>
        </h1>
        <p class="subheadline">
          Socialupload connects your social accounts, prepares captions and media,
          and publishes or queues content across TikTok, YouTube, Reddit, and more &mdash;
          all from a single web app.
        </p>
        <div class="hero-actions">
          <router-link class="btn btn-primary" to="/login">Get Started</router-link>
          <a class="btn btn-secondary" href="#" @click.prevent="scrollTo('features')">Learn More</a>
        </div>
        <div class="platform-strip">
          <span class="platform-label">Works with</span>
          <div class="platform-icons">
            <span class="platform-chip">TikTok</span>
            <span class="platform-chip">YouTube</span>
            <span class="platform-chip">Reddit</span>
            <span class="platform-chip">Meta</span>
            <span class="platform-chip">Threads</span>
            <span class="platform-chip">Bilibili</span>
            <span class="platform-chip">Douyin</span>
            <span class="platform-chip">Xiaohongshu</span>
          </div>
        </div>
      </div>
    </section>

    <!-- Features -->
    <section id="features" class="features">
      <div class="section-inner">
        <h2 class="section-title">Everything you need to publish at scale</h2>
        <p class="section-subtitle">
          Stop switching between platforms. Socialupload gives you one place to prepare,
          review, and send content to every account you manage.
        </p>

        <div class="feature-grid">
          <div class="feature-card">
            <div class="feature-icon">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
            </div>
            <h3>Connect your accounts</h3>
            <p>
              Use OAuth-based connections such as TikTok Login Kit to securely link
              the accounts you manage. Confirm the correct profile before any content is published.
            </p>
          </div>

          <div class="feature-card">
            <div class="feature-icon">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><line x1="10" y1="9" x2="8" y2="9"/></svg>
            </div>
            <h3>Prepare content and metadata</h3>
            <p>
              Upload media, write captions and hashtags, choose privacy settings,
              and review every detail before starting a publishing job.
            </p>
          </div>

          <div class="feature-card">
            <div class="feature-icon">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
            </div>
            <h3>Schedule and publish</h3>
            <p>
              Queue posts for later or publish immediately. TikTok draft uploads and
              direct posts are initiated only when you confirm the action.
            </p>
          </div>

          <div class="feature-card">
            <div class="feature-icon">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
            </div>
            <h3>Multi-platform support</h3>
            <p>
              Manage TikTok, YouTube, Reddit, Meta, Threads, Bilibili, Douyin,
              Xiaohongshu, and more from a single unified interface.
            </p>
          </div>

          <div class="feature-card">
            <div class="feature-icon">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
            </div>
            <h3>Secure by design</h3>
            <p>
              OAuth tokens and cookies are stored securely. Optional AES-GCM encryption
              protects credentials at rest. You control your own deployment.
            </p>
          </div>

          <div class="feature-card">
            <div class="feature-icon">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>
            </div>
            <h3>Self-hosted control</h3>
            <p>
              Deploy on your own infrastructure with Docker. Keep full ownership of your data,
              accounts, and publishing workflows.
            </p>
          </div>
        </div>
      </div>
    </section>

    <!-- Platforms -->
    <section id="platforms" class="platforms-section">
      <div class="section-inner">
        <h2 class="section-title">Connect every platform you use</h2>
        <p class="section-subtitle">
          Socialupload supports the platforms that matter to creators and operators in Asia and worldwide.
        </p>
        <div class="platform-grid">
          <div class="platform-item" v-for="p in platforms" :key="p.name">
            <span class="platform-name">{{ p.name }}</span>
            <span class="platform-desc">{{ p.desc }}</span>
          </div>
        </div>
      </div>
    </section>

    <!-- How it works -->
    <section class="how-it-works">
      <div class="section-inner">
        <h2 class="section-title">How it works</h2>
        <div class="steps">
          <div class="step">
            <div class="step-number">1</div>
            <h3>Connect your accounts</h3>
            <p>Link your social media accounts using secure OAuth flows. Socialupload never stores raw passwords.</p>
          </div>
          <div class="step">
            <div class="step-number">2</div>
            <h3>Upload and prepare</h3>
            <p>Upload videos, write captions, add hashtags, and configure platform-specific settings.</p>
          </div>
          <div class="step">
            <div class="step-number">3</div>
            <h3>Review and publish</h3>
            <p>Double-check everything, then publish immediately or schedule for later. You stay in control.</p>
          </div>
        </div>
      </div>
    </section>

    <!-- CTA -->
    <section class="cta-section">
      <div class="section-inner">
        <h2>Ready to streamline your publishing workflow?</h2>
        <p>Get started with Socialupload today. Deploy it on your own server and connect your accounts in minutes.</p>
        <router-link class="btn btn-primary btn-large" to="/login">Get Started</router-link>
      </div>
    </section>

    <!-- Footer -->
    <footer class="site-footer">
      <div class="footer-inner">
        <div class="footer-brand">
          <img src="/socialupload-app-icon.png" alt="Socialupload" class="footer-icon" />
          <span class="footer-name">Socialupload</span>
          <p class="footer-tagline">Publishing workspace for creators and operators.</p>
        </div>
        <div class="footer-links">
          <div class="footer-col">
            <h4>Product</h4>
            <a href="#features">Features</a>
            <a href="#platforms">Platforms</a>
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
  { name: 'TikTok', desc: 'Content Posting API with draft and direct modes' },
  { name: 'YouTube', desc: 'Video uploads via YouTube Data API' },
  { name: 'Reddit', desc: 'Video and media posts to subreddits' },
  { name: 'Meta', desc: 'Facebook and Instagram publishing' },
  { name: 'Threads', desc: 'Text and media posts' },
  { name: 'Bilibili', desc: 'Video uploads for Chinese creators' },
  { name: 'Douyin', desc: 'Chinese TikTok publishing' },
  { name: 'Xiaohongshu', desc: 'Short video and image posts' },
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
  max-width: 780px;
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
  max-width: 640px;
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
    background: darken($bg, 2%);
    border-color: darken($border, 5%);
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
  max-width: 600px;
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
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}

.platform-item {
  background: $surface;
  border: 1px solid $border;
  border-radius: $radius;
  padding: 20px;
  text-align: center;

  .platform-name {
    display: block;
    font-size: 16px;
    font-weight: 600;
    margin-bottom: 4px;
  }

  .platform-desc {
    font-size: 13px;
    color: $text-secondary;
  }
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
  grid-template-columns: repeat(3, 1fr);
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
    max-width: 520px;
    margin: 0 auto 28px;
    line-height: 1.7;
  }
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

  .platform-grid {
    grid-template-columns: repeat(2, 1fr);
  }

  .section-title {
    font-size: 26px;
  }

  .footer-links {
    gap: 32px;
  }
}
</style>
