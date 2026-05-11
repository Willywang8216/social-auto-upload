<template>
  <main class="landing-page">
    <section class="hero-card" aria-labelledby="socialupload-title">
      <p class="eyebrow">Creator publishing workspace</p>
      <h1 id="socialupload-title">Socialupload</h1>
      <p class="summary">
        Socialupload helps creators and operators connect their own social accounts, upload
        first-party videos, prepare captions and TikTok settings, and publish or queue content from a
        single web dashboard.
      </p>
      <div class="actions">
        <router-link class="primary-link" to="/login">Sign in to Socialupload</router-link>
        <router-link class="secondary-link" to="/privacy">Privacy Policy</router-link>
        <router-link class="secondary-link" to="/terms">Terms of Service</router-link>
      </div>
    </section>

    <section class="info-grid" aria-label="Socialupload capabilities">
      <article>
        <h2>Connect accounts you manage</h2>
        <p>
          Use OAuth-based connections such as TikTok Login Kit to confirm the authorized account
          before publishing workflows are used.
        </p>
      </article>
      <article>
        <h2>Prepare publishing metadata</h2>
        <p>
          Upload media, write captions and hashtags, choose privacy settings, and review the selected
          destination account before starting a job.
        </p>
      </article>
      <article>
        <h2>Post only after user action</h2>
        <p>
          TikTok draft uploads and direct posts are initiated from the app by an authenticated user;
          Socialupload does not publish without user action.
        </p>
      </article>
    </section>
  </main>
</template>


<style lang="scss" scoped>
.landing-page {
  min-height: 100vh;
  background: #f5f7fa;
  color: #303133;
  padding: 48px 16px;
}

.hero-card,
.info-grid {
  max-width: 960px;
  margin: 0 auto;
}

.hero-card {
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 16px;
  padding: 48px;

  .eyebrow {
    margin: 0 0 12px;
    color: #606266;
    font-size: 14px;
    letter-spacing: 0.02em;
  }

  h1 {
    margin: 0;
    font-size: 44px;
    line-height: 1.1;
  }

  .summary {
    max-width: 720px;
    margin: 20px 0 0;
    color: #606266;
    font-size: 17px;
    line-height: 1.8;
  }
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 28px;

  a {
    border-radius: 8px;
    padding: 10px 16px;
    text-decoration: none;
    font-weight: 500;
  }

  .primary-link {
    background: #409eff;
    color: #fff;
  }

  .secondary-link {
    background: #f5f7fa;
    color: #409eff;
    border: 1px solid #dcdfe6;
  }
}

.info-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
  margin-top: 16px;

  article {
    background: #fff;
    border: 1px solid #ebeef5;
    border-radius: 12px;
    padding: 20px;

    h2 {
      margin: 0 0 10px;
      font-size: 18px;
    }

    p {
      margin: 0;
      color: #606266;
      line-height: 1.7;
    }
  }
}

@media (max-width: 760px) {
  .hero-card {
    padding: 32px 24px;

    h1 {
      font-size: 36px;
    }
  }

  .info-grid {
    grid-template-columns: 1fr;
  }
}
</style>
