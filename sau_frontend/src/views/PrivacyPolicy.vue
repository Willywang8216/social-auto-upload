<template>
  <div class="legal-page">
    <div class="legal-card">
      <div class="legal-header">
        <h1>Privacy Policy</h1>
        <p class="meta">Last updated: 2026-05-04</p>
        <p class="summary">
          This Privacy Policy applies to the Social Auto Upload service made available through
          <strong>up.iamwillywang.com</strong> and <strong>up.sexualwill.com</strong>.
          Both domains operate the same underlying tool for different audience groups.
        </p>
      </div>

      <section>
        <h2>1. Scope</h2>
        <p>
          This Privacy Policy explains how the service collects, uses, stores, and protects information
          when you use the web application, upload media, connect social accounts, generate content,
          schedule publishing tasks, or otherwise interact with the service.
        </p>
      </section>

      <section>
        <h2>2. Information we collect</h2>
        <ul>
          <li><strong>Account and profile information</strong>, such as profile names, connected platform account names, and account configuration details you provide.</li>
          <li><strong>Authentication materials</strong>, such as cookies, tokens, storage-state files, or API credentials that you explicitly provide or connect for supported platforms.</li>
          <li><strong>Uploaded content</strong>, including images, videos, thumbnails, captions, transcripts, generated drafts, and related campaign assets.</li>
          <li><strong>Publishing and workflow data</strong>, including selected profiles, target platforms, scheduling choices, queue status, logs, and publishing results.</li>
          <li><strong>Technical and security data</strong>, such as request metadata, server logs, and audit or error information reasonably required to operate and secure the service.</li>
        </ul>
      </section>

      <section>
        <h2>3. How we use information</h2>
        <ul>
          <li>To provide account management, media management, content generation, scheduling, and publishing features.</li>
          <li>To process uploaded media, including optional watermarking, transcription, formatting, or platform-specific preparation.</li>
          <li>To send content or media to third-party services you choose to use, such as social platforms, cloud storage providers, LLM providers, or Google Sheets.</li>
          <li>To monitor reliability, troubleshoot failures, improve the service, and protect the service from misuse or security incidents.</li>
          <li>To comply with legal obligations, enforce service rules, or respond to valid lawful requests.</li>
        </ul>
      </section>

      <section>
        <h2>4. Third-party services and data sharing</h2>
        <p>
          The service may transmit data to third-party services only as needed to perform the actions you request.
          Depending on your configuration, that may include social media platforms, cloud storage providers,
          LLM or transcription providers, Google Sheets, or other publishing-related services.
        </p>
        <p>
          You are responsible for reviewing the privacy terms, API terms, and policy requirements of every platform
          or provider you choose to connect. The service does not control the privacy practices of those third parties.
        </p>
      </section>

      <section>
        <h2>5. Sensitive or restricted content</h2>
        <p>
          Some deployments of the service may be used for different audience segments or content categories.
          You remain solely responsible for ensuring that your content, account usage, audience targeting,
          and publishing behavior comply with applicable law, platform rules, age-gating requirements,
          intellectual property rules, and advertising or disclosure requirements.
        </p>
      </section>

      <section>
        <h2>6. Storage and retention</h2>
        <ul>
          <li>Uploaded media, campaign records, logs, generated drafts, and connected account materials may be stored to provide the requested workflow.</li>
          <li>Connected account cookies or similar files may be stored locally or on the service infrastructure and may be encrypted at rest where supported by deployment configuration.</li>
          <li>Data may remain in backups, logs, queue records, or third-party systems for a reasonable operational period even after deletion from the main application view.</li>
        </ul>
      </section>

      <section>
        <h2>7. Security</h2>
        <p>
          Reasonable technical and organizational measures may be used to protect stored credentials, uploaded media,
          generated content, and operational records. However, no internet-based system or storage mechanism can be
          guaranteed to be completely secure, and use of the service is at your own risk.
        </p>
      </section>

      <section>
        <h2>8. Your responsibilities</h2>
        <ul>
          <li>Provide only credentials, tokens, cookies, and content that you are authorized to use.</li>
          <li>Do not upload malware, illegal content, infringing material, or content that violates platform rules.</li>
          <li>Do not use the service to bypass security, anti-abuse systems, consent requirements, or lawful restrictions.</li>
          <li>Ensure that any personal data you process through the service has an appropriate legal basis.</li>
        </ul>
      </section>

      <section>
        <h2>9. International use</h2>
        <p>
          Depending on your configuration and the third-party platforms you use, data may be processed in multiple
          jurisdictions. By using the service, you acknowledge that your data may be transferred to and processed by
          external services outside your local jurisdiction.
        </p>
      </section>

      <section>
        <h2>10. Changes to this policy</h2>
        <p>
          This Privacy Policy may be updated from time to time. The latest version will be posted on the service,
          and continued use of the service after updates means you accept the revised policy.
        </p>
      </section>

      <section>
        <h2>11. Contact</h2>
        <p>
          For privacy-related questions, deletion requests, or legal notices, please contact the operator through the
          support or contact method provided on the active service deployment.
        </p>
      </section>

      <div class="legal-links">
        <router-link to="/terms">View Terms of Use</router-link>
      </div>
    </div>
  </div>
</template>

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

    p,
    li {
      color: #606266;
      line-height: 1.85;
      font-size: 15px;
    }

    ul {
      margin: 0;
      padding-left: 20px;
    }
  }

  .legal-links {
    margin-top: 24px;
    padding-top: 20px;
    border-top: 1px solid #ebeef5;

    a {
      color: #409eff;
      text-decoration: none;
      font-weight: 500;
    }
  }
}
</style>
