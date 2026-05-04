<template>
  <div class="legal-page">
    <div class="legal-card">
      <div class="legal-header">
        <h1>Terms of Use</h1>
        <p class="meta">Last updated: 2026-05-04</p>
        <p class="summary">
          These Terms of Use govern access to and use of the Social Auto Upload service made available through
          <strong>up.iamwillywang.com</strong>.
        </p>
      </div>

      <section>
        <h2>1. Acceptance of terms</h2>
        <p>
          By accessing or using the service, you agree to be bound by these Terms of Use and by the Privacy Policy.
          If you do not agree, you must not use the service.
        </p>
      </section>

      <section>
        <h2>2. The service</h2>
        <p>
          The service is a media workflow and publishing tool that may allow account management, content generation,
          media processing, cloud transfer, scheduling, campaign preparation, and direct or assisted posting to
          third-party services.
        </p>
      </section>

      <section>
        <h2>3. Eligibility and authority</h2>
        <p>
          You may use the service only if you have the legal capacity and the necessary authority to use the accounts,
          media, credentials, tokens, and content you submit through the service.
        </p>
      </section>

      <section>
        <h2>4. Your responsibilities</h2>
        <ul>
          <li>You are responsible for all media, captions, tokens, cookies, prompts, schedules, and publishing instructions you provide.</li>
          <li>You must comply with all applicable laws, regulations, intellectual property rules, disclosure requirements, and platform terms.</li>
          <li>You must ensure that content submitted through the service is lawful and suitable for the target platform and audience.</li>
          <li>You must not use the service to harass, defraud, spam, impersonate, distribute malware, infringe rights, or bypass access controls or moderation systems.</li>
        </ul>
      </section>

      <section>
        <h2>5. Platform and API compliance</h2>
        <p>
          Many workflows in the service depend on third-party APIs, cookies, browser automation, or platform-specific
          account permissions. You are solely responsible for ensuring that your use of those connected platforms is
          allowed under their rules and under your relationship with those platforms.
        </p>
      </section>

      <section>
        <h2>6. Sensitive content and restricted workflows</h2>
        <p>
          You are responsible for ensuring that your content, targeting, and use of connected platforms comply with any
          age restrictions, sensitive-content requirements, advertising rules, sanctions restrictions, export controls,
          or local legal requirements that apply to your audience or jurisdiction.
        </p>
      </section>

      <section>
        <h2>7. Credentials and security</h2>
        <p>
          You are responsible for safeguarding your access tokens, cookies, API credentials, and connected account
          permissions. You must not intentionally submit credentials that you are not authorized to use.
        </p>
      </section>

      <section>
        <h2>8. Availability and changes</h2>
        <p>
          The service may change, be updated, suspended, restricted, or discontinued at any time without notice.
          Features may behave differently depending on deployment settings, third-party API changes, or moderation and
          security requirements.
        </p>
      </section>

      <section>
        <h2>9. No guarantee of publication or reach</h2>
        <p>
          The service does not guarantee that content will be accepted, published, remain live, reach any audience,
          avoid moderation, or perform in any particular way on any third-party platform.
        </p>
      </section>

      <section>
        <h2>10. Disclaimer</h2>
        <p>
          To the maximum extent permitted by law, the service is provided on an “as is” and “as available” basis,
          without warranties of any kind, whether express or implied, including warranties of availability,
          merchantability, fitness for a particular purpose, non-infringement, or uninterrupted operation.
        </p>
      </section>

      <section>
        <h2>11. Limitation of liability</h2>
        <p>
          To the maximum extent permitted by law, the operator of the service will not be liable for any indirect,
          incidental, special, consequential, exemplary, or punitive damages, or for any loss of data, profits,
          business, platform access, account access, or goodwill arising out of or related to use of the service.
        </p>
      </section>

      <section>
        <h2>12. Termination</h2>
        <p>
          Access may be suspended or terminated at any time, with or without notice, if use of the service is believed
          to create legal, operational, security, or abuse risk, or if continued access is otherwise not appropriate.
        </p>
      </section>

      <section>
        <h2>13. Changes to these terms</h2>
        <p>
          These Terms of Use may be updated from time to time. The latest version posted on the service controls.
          Continued use of the service after changes means you accept the updated terms.
        </p>
      </section>

      <section>
        <h2>14. Contact</h2>
        <p>
          For questions about these terms, please use the support or contact method provided on
          <strong>up.iamwillywang.com</strong>.
        </p>
      </section>

      <div class="legal-links">
        <router-link to="/privacy">View Privacy Policy</router-link>
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
