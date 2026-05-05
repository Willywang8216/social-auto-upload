# TikTok app review demo script and transcript

This script is tailored to the current web deployment at **https://up.iamwillywang.com**.

Important review guidance:
- Only keep the TikTok products/scopes you actually use in the Developer Portal.
- For the implementation currently in this codebase, the review configuration should match the real app surface shown in the UI.

## Domain shown in the recording
The website shown in the video must be:
- `https://up.iamwillywang.com`

## Redirect URI
- `https://up.iamwillywang.com/oauth/tiktok/callback`

## Webhook callback URL
- `https://up.iamwillywang.com/webhooks/tiktok`

## Products and scopes selected right now
For the current implementation, keep only:
- **Login Kit for Web**
- **Content Posting API**
- **Webhooks**

Scopes actively used in the current flow:
- `user.info.basic`
- `video.publish`

If Share Kit, Display API, Research API, or other products are enabled in the TikTok portal but are not shown in the review video, remove them before submission.

---

## What the review video should prove
1. The reviewer can see the real web app domain.
2. The user opens the real authenticated web UI.
3. The user clicks a real **Connect with TikTok** button inside the app.
4. TikTok redirects back to the registered callback URL on `up.iamwillywang.com`.
5. The app shows a callback/webhook receipt status page in the web UI.
6. The user configures a TikTok account under a Profile.
7. The user prepares content in the actual app UI.
8. The app submits a TikTok Content Posting API request using `video.publish`.

---

## Recording shot list

### Shot 1 — Open the real site
- Open a browser.
- Type `https://up.iamwillywang.com`.
- Keep the full domain visible in the address bar.

Narration:
> This is the production web app deployed at up.iamwillywang.com. This is the exact website where the TikTok integration is used.

### Shot 2 — Show login / access gate
- Show the login screen or authenticated landing flow.
- Log in to the app.

Narration:
> The user first enters the real web application and signs in. The TikTok integration is only available inside this production website.

### Shot 3 — Go to Account Management
- Open **Account Management**.
- Show the TikTok account form.
- Show TikTok-specific fields such as publish mode, privacy level, interaction controls, and token status.

Narration:
> In Account Management, the operator configures a TikTok account under a Profile. The app validates TikTok-specific settings before the account can be saved or used.

### Shot 4 — Show real Login Kit for Web flow
- In the TikTok section, click **Connect with TikTok**.
- Show the popup or redirect leaving the app and opening the TikTok authorization screen.
- Approve access.
- Show the redirect landing on `https://up.iamwillywang.com/oauth/tiktok/callback`.

Narration:
> This step demonstrates TikTok Login Kit for Web. The user clicks Connect with TikTok inside the real application, authorizes access on TikTok, and TikTok redirects back to the registered callback URL on up.iamwillywang.com.

### Shot 5 — Show callback status page
- Open the tiny **TikTok callback status** admin page in the app.
- Show:
  - redirect URI
  - webhook URI
  - selected products
  - selected scopes
  - latest callback receipt

Narration:
> This admin view shows the latest TikTok callback receipt on the same production domain, including the configured callback URL and the exact products and scopes used in this implementation.

### Shot 6 — Upload media in the actual app
- Go to **Material Management** or **Publish Center**.
- Upload one video file in the actual UI.
- Show that the media appears inside the app.

Narration:
> The operator uploads media inside the real application interface. This video is used for TikTok content preparation and posting.

### Shot 7 — Prepare a TikTok campaign
- Go to **Publish Center**.
- Select the prepared Profile.
- Select the TikTok account.
- Enter title / notes / hashtags.
- Make sure the selected Profile does not use a prohibited watermark for TikTok.

Narration:
> In Publish Center, the user selects the Profile and the connected TikTok account, then enters the content details that will be sent through TikTok’s Content Posting API.

### Shot 8 — Submit the TikTok post
- Click publish.
- Show that the app creates a campaign and queues the TikTok posting job.
- If available, show the job progressing in **Jobs**.

Narration:
> The application prepares the content and sends a TikTok Content Posting API request through the connected TikTok account using the approved video.publish scope.

### Shot 9 — Show webhook receipt
- Return to the TikTok callback status page.
- If a webhook was received, show the latest webhook receipt and its signature-verification status.

Narration:
> The application is configured to receive TikTok webhook events on the same production domain, and the admin status page shows receipt of the latest webhook callback.

### Shot 10 — Final summary
- Return to the app UI and show the final job/result state.

Narration:
> This demonstrates the end-to-end TikTok integration on the real production website, including Login Kit for Web, callback handling, account configuration, content posting with the Content Posting API, and webhook receipt visibility.

---

## Short review-safe transcript

> This is the production web app deployed at up.iamwillywang.com.
>
> First, the user enters the real web application and signs in.
>
> Next, we open Account Management and configure a TikTok account under a Profile. The app validates TikTok-specific settings such as publish mode, privacy level, and required credentials.
>
> Then the user clicks Connect with TikTok inside the app. This uses TikTok Login Kit for Web.
>
> After the user authorizes access on TikTok, TikTok redirects back to the registered callback URL on up.iamwillywang.com.
>
> We then open the TikTok callback status page in the app, which shows the latest callback receipt, the webhook URL, and the exact selected products and scopes for this integration.
>
> Next, the user uploads media inside the real application interface.
>
> In Publish Center, the user selects the Profile and the TikTok account, enters the content details, and submits the campaign.
>
> The application sends the post through TikTok’s Content Posting API using the approved video.publish scope.
>
> The application is also configured to receive TikTok webhook events on the same production domain.
>
> This completes the end-to-end TikTok integration demonstration on the real production website.

---

## Notes for you before submission
- Keep only the products actually used in this flow: **Login Kit for Web**, **Content Posting API**, and **Webhooks**.
- Keep only the scopes actually used in this flow: **user.info.basic** and **video.publish**.
- Remove Share Kit, Display API, and any unused scopes or products before submission.
- Make sure the domain visible in the browser bar is exactly `up.iamwillywang.com`.
- Use a non-watermarked TikTok profile configuration in the demo, because TikTok prohibits unwanted promotional watermarks in Content Posting API uploads.
