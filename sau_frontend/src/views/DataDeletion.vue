<template>
  <div class="legal-page">
    <div class="legal-card">
      <div class="legal-header">
        <h1>Data Deletion Instructions</h1>
        <p class="meta">Socialupload — Last updated: 2026-06-28</p>
        <p class="summary">
          If you want to delete your data from Socialupload, follow the instructions below.
          You can also submit a deletion request through the form at the bottom of this page.
        </p>
      </div>

      <section>
        <h2>Delete your data via the dashboard</h2>
        <ol>
          <li>Log in to your Socialupload dashboard at <strong>https://up.iamwillywang.com</strong>.</li>
          <li>Navigate to <strong>Accounts</strong> from the sidebar.</li>
          <li>For each connected account you want to remove, click the <strong>✕</strong> (Remove) button on the account card. This deletes the stored OAuth tokens, cookies, profile data, and account configuration from our servers.</li>
          <li>Navigate to <strong>Library</strong> or <strong>Publish Center</strong> and delete any uploaded media, drafts, campaigns, or scheduled jobs you want removed.</li>
          <li>Go to <strong>Settings</strong> and delete your profile or workspace if the option is available.</li>
        </ol>
        <p>
          Once you remove a connected account, all associated OAuth tokens, refresh tokens,
          cookies, avatar URLs, display names, platform user IDs, and publishing history for
          that account are permanently deleted from our active database.
        </p>
      </section>

      <section>
        <h2>Revoke platform access</h2>
        <p>
          In addition to removing accounts from Socialupload, you should revoke the app's
          access directly on each platform:
        </p>
        <ul>
          <li><strong>Facebook / Instagram:</strong> Go to <a href="https://www.facebook.com/settings/?tab=applications" target="_blank">Facebook App Settings</a> and remove Socialupload.</li>
          <li><strong>TikTok:</strong> Go to your TikTok Settings &gt; Manage account &gt; Authorized apps and revoke access.</li>
          <li><strong>YouTube / Google:</strong> Go to <a href="https://myaccount.google.com/permissions" target="_blank">Google Account Permissions</a> and remove access.</li>
          <li><strong>Twitter / X:</strong> Go to Settings &gt; Connected apps and revoke access.</li>
          <li><strong>Threads:</strong> Go to your Threads settings and manage connected apps.</li>
          <li><strong>Reddit:</strong> Go to <a href="https://www.reddit.com/settings/apps" target="_blank">Reddit App Preferences</a> and revoke access.</li>
        </ul>
      </section>

      <section>
        <h2>Submit a deletion request</h2>
        <p>
          If you are unable to delete your data through the dashboard, or if you want to
          request deletion of all data associated with your social media account, fill out
          the form below. We will process your request within 30 days.
        </p>
        <div class="deletion-form">
          <div class="field">
            <label>Your name</label>
            <input class="input" v-model="form.name" placeholder="Your name" />
          </div>
          <div class="field">
            <label>Email address</label>
            <input class="input" v-model="form.email" type="email" placeholder="you@example.com" />
          </div>
          <div class="field">
            <label>Platform account to delete</label>
            <input class="input" v-model="form.account" placeholder="e.g. TikTok: @username, Facebook: Page Name" />
          </div>
          <div class="field">
            <label>Additional details (optional)</label>
            <textarea class="textarea" v-model="form.details" rows="3" placeholder="Any specific data you want deleted..."></textarea>
          </div>
          <button class="btn-primary" @click="submitRequest" :disabled="!form.name || !form.email || submitting">
            {{ submitting ? 'Submitting...' : 'Submit Deletion Request' }}
          </button>
          <p v-if="submitted" class="success-msg">
            ✓ Your deletion request has been submitted. We will process it within 30 days.
          </p>
        </div>
      </section>

      <section>
        <h2>Meta (Facebook / Instagram) data deletion</h2>
        <p>
          If you authorized Socialupload through Facebook Login, you can also request data
          deletion through Facebook's interface. Facebook will send us a deletion request
          automatically, and we will confirm deletion of your data.
        </p>
        <p>
          Our data deletion callback URL for Meta is:<br />
          <code>https://up.iamwillywang.com/oauth/meta/data-deletion</code>
        </p>
      </section>

      <section>
        <h2>Contact</h2>
        <p>
          For any privacy-related questions or requests, contact us through the
          <router-link to="/privacy">Privacy Policy</router-link> page.
        </p>
      </section>

      <div class="legal-links">
        <router-link to="/privacy">Privacy Policy</router-link>
        <router-link to="/terms">Terms of Service</router-link>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'

const form = ref({ name: '', email: '', account: '', details: '' })
const submitting = ref(false)
const submitted = ref(false)

const submitRequest = async () => {
  submitting.value = true
  try {
    // Send deletion request to backend
    const resp = await fetch('/api/data-deletion-request', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(form.value),
    })
    if (resp.ok) {
      submitted.value = true
      form.value = { name: '', email: '', account: '', details: '' }
    }
  } catch (e) {
    console.error('Deletion request failed:', e)
  } finally {
    submitting.value = false
  }
}
</script>

<style lang="scss" scoped>
.legal-page {
  min-height: 100vh;
  background: #f5f7fa;
  padding: 32px 16px;
}

.legal-card {
  max-width: 920px;
  margin: 0 auto;
  background: #fff;
  border-radius: 12px;
  padding: 32px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.06);

  .legal-header {
    margin-bottom: 28px;

    h1 {
      margin: 0 0 8px;
      font-size: 30px;
      color: #303133;
    }

    .meta {
      margin: 0 0 12px;
      color: #909399;
      font-size: 13px;
    }

    .summary {
      margin: 0;
      color: #606266;
      line-height: 1.8;
    }
  }

  section {
    margin-bottom: 24px;

    h2 {
      margin: 0 0 10px;
      font-size: 18px;
      color: #303133;
    }

    p, li {
      color: #606266;
      line-height: 1.85;
      font-size: 15px;
    }

    ol, ul {
      margin: 0;
      padding-left: 24px;
    }

    code {
      background: #f0f2f5;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 13px;
      color: #303133;
    }
  }

  .deletion-form {
    margin-top: 16px;
    padding: 20px;
    background: #f9fafb;
    border-radius: 8px;
    border: 1px solid #ebeef5;

    .field {
      margin-bottom: 14px;

      label {
        display: block;
        font-size: 13px;
        font-weight: 600;
        color: #303133;
        margin-bottom: 6px;
      }

      .input, .textarea {
        width: 100%;
        padding: 10px 12px;
        border: 1px solid #dcdfe6;
        border-radius: 6px;
        font-size: 14px;
        color: #303133;
        background: #fff;
        box-sizing: border-box;

        &:focus {
          outline: none;
          border-color: #409eff;
        }
      }

      .textarea {
        resize: vertical;
        font-family: inherit;
      }
    }

    .btn-primary {
      background: #409eff;
      color: #fff;
      border: none;
      padding: 10px 24px;
      border-radius: 6px;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;

      &:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }
    }

    .success-msg {
      margin-top: 12px;
      color: #67c23a;
      font-weight: 600;
      font-size: 14px;
    }
  }

  .legal-links {
    margin-top: 24px;
    padding-top: 20px;
    border-top: 1px solid #ebeef5;
    display: flex;
    gap: 20px;

    a {
      color: #409eff;
      text-decoration: none;
      font-weight: 500;
    }
  }
}
</style>
