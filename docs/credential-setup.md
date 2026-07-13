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

## Google application login (multi-user conversion — Phase 3)

The dedicated Google OAuth client for **application login** (separate from the
YouTube `YT_CLIENT_ID` connection client):

- Client ID (public identifier):
  `854051528643-4um1scqqigof6g1ojojb1nh1ebjjhf1j.apps.googleusercontent.com`
- Registered redirect URIs (both hosts are registered in the Google console, so
  either may serve as `SAU_PUBLIC_BASE_URL`):
  - `https://socialupload.iamwillywang.com/auth/google/callback`
  - `https://up.iamwillywang.com/auth/google/callback`

To switch the login on, set in the deployment `.env` (the client **secret**
lives only here — never in git or chat):

```bash
SAU_GOOGLE_LOGIN_ENABLED=true
SAU_AUTH_MODE=hybrid            # bearer token AND Google session both accepted
GOOGLE_LOGIN_CLIENT_ID=854051528643-4um1scqqigof6g1ojojb1nh1ebjjhf1j.apps.googleusercontent.com
GOOGLE_LOGIN_CLIENT_SECRET=<from the Google console — env only>
SAU_PUBLIC_BASE_URL=https://up.iamwillywang.com   # or socialupload.iamwillywang.com
SECRET_KEY=<long random string>
```

The app derives the callback as `<SAU_PUBLIC_BASE_URL>/auth/google/callback` and
refuses to start if the flag is on but any of these are missing. Login scopes
are only `openid email profile` — no YouTube scopes, no app review needed.
Verify after deploy: `GET /auth/google/start` should 302 to accounts.google.com.
