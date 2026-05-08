# Credential setup guide

This file is now a short pointer so we do not maintain two competing setup guides.

Use these instead:

- Full platform setup, callback URLs, webhook URLs, provider review wording, and `.env` placement:
  - `docs/platform-integration-runbook.md`
- Fastest practical path from empty setup to working system:
  - `docs/step-by-step-setup-checklist.md`

Important current behavior:

- the backend / worker / CLI auto-load the repo root `.env`
- structured account forms usually store the **env var name**, not the secret value itself
- TikTok in this repo uses the server-side OAuth + Content Posting API flow, not the browser-only Share Kit flow
- X/Twitter is still legacy cookie/browser-driven in the current codebase

If you need exact provider answers for app-review forms, callback URLs, scope explanations, or what to put into each env variable, use:

- `docs/platform-integration-runbook.md`
