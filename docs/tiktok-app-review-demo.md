# TikTok app review demo script and transcript

This script is tailored to the current web deployment at **https://up.iamwillywang.com**.

Important review guidance:
- Only keep the TikTok products/scopes you actually use in the Developer Portal.
- If you are only demonstrating **Login Kit for Web** and **Content Posting API / video.publish**, remove Share Kit, Display API, and unused scopes before submission.
- If your app has not been approved before, use the TikTok Developer Portal sandbox where required.

## Domain shown in the recording
The website shown in the video must be:
- `https://up.iamwillywang.com`

## Redirect URI
- `https://up.iamwillywang.com/oauth/tiktok/callback`

## Webhook callback URL
- `https://up.iamwillywang.com/webhooks/tiktok`

## What the review video should prove
1. The reviewer can see the real web app domain.
2. The user logs in or connects TikTok via TikTok for Developers capabilities.
3. The user prepares content in the actual app UI.
4. The user selects a TikTok account configured in the app.
5. The app prepares and submits a TikTok Content Posting API request.
6. The user can see the result in the app, including any publish/job status.

## Recommended products/scopes to keep enabled for this review
If you are reviewing the implementation currently closest to this codebase, keep only:
- Login Kit for Web
- Content Posting API
- `video.publish`

If you do **not** actively demonstrate Share Kit or Display API in the video, remove them before submission.

---

## Recording shot list

### Shot 1 — Open the real site
- Open a browser.
- Type `https://up.iamwillywang.com`.
- Keep the full domain visible in the address bar.

Narration:
> This is the production web app deployed at up.iamwillywang.com. This is the website where the TikTok integration is actually used.

### Shot 2 — Show login / access gate
- Show the login screen or authenticated landing flow.
- Log in to the app.

Narration:
> The user first enters the web app and signs in. The TikTok integration is only available inside the real production website.

### Shot 3 — Go to account management
- Open **Account Management**.
- Show the TikTok account entry or create one.
- Show TikTok-specific fields such as access token env, publish mode, privacy level, and comment/duet/stitch options.

Narration:
> In Account Management, the operator configures a TikTok account for the profile. The app validates TikTok-specific settings before allowing the account to be used.

### Shot 4 — Show TikTok Login Kit / OAuth callback flow
- If you have a real Login Kit flow wired in your review build, show the user clicking the TikTok connect or authorize action.
- Show the redirect landing on `https://up.iamwillywang.com/oauth/tiktok/callback`.
- Show the app receiving the TikTok authorization code and token exchange result.

Narration:
> This step demonstrates TikTok Login Kit for Web. After the user authorizes access on TikTok, TikTok redirects back to the registered callback URL on up.iamwillywang.com.

If you are **not** actually using Login Kit in the current review build, do **not** claim it in the video. Remove Login Kit from the selected products before submission.

### Shot 5 — Upload media in the actual app
- Go to **Material Management** or **Publish Center**.
- Upload one video file to the real UI.
- Show that the media appears inside the app.

Narration:
> The operator uploads a video inside the actual application interface. This media will be used for TikTok content preparation and posting.

### Shot 6 — Prepare a TikTok campaign
- Go to **Publish Center**.
- Select the prepared Profile.
- Select the TikTok account.
- Enter title / notes / hashtags.
- Make sure the selected Profile does not use a prohibited watermark for TikTok.

Narration:
> In Publish Center, the user selects the Profile and the connected TikTok account, then enters the content details that will be sent to TikTok through the Content Posting API.

### Shot 7 — Show content preparation and TikTok posting
- Click publish.
- Show that the app creates a campaign and queues the TikTok posting job.
- If available in your environment, show the job progressing in **Jobs**.

Narration:
> The application prepares the content and sends a TikTok Content Posting API request. The post is submitted through the user-authorized TikTok account using the approved video.publish scope.

### Shot 8 — Show callback/webhook handling
- Show the configured webhook callback URL in documentation or admin config if helpful.
- If you can demonstrate it live, show the server receiving TikTok webhook events at `/webhooks/tiktok`.

Narration:
> The application is configured to receive TikTok webhook events at the registered callback URL on the same production domain.

### Shot 9 — Final summary
- Return to the app UI and show the final job/result state.

Narration:
> This demonstrates the end-to-end TikTok integration on the real production website, including account configuration, media preparation, TikTok authorization flow where applicable, content posting, and server-side callback handling.

---

## Short review-safe transcript

Use this if you want a concise spoken script:

> This is the production web app deployed at up.iamwillywang.com.
>
> First, the user enters the web application and signs in.
>
> Next, we open Account Management and configure a TikTok account under a Profile. The app validates TikTok-specific settings such as publish mode, privacy level, and required credentials.
>
> If Login Kit for Web is enabled for this review, the user authorizes TikTok and TikTok redirects back to the registered callback URL on up.iamwillywang.com.
>
> Then we upload media inside the real application interface.
>
> In Publish Center, the user selects the Profile and the TikTok account, enters the content details, and submits the campaign.
>
> The application sends the post through TikTok’s Content Posting API using the approved video.publish scope.
>
> The app also supports a TikTok webhook callback URL on the same production domain for receiving TikTok events.
>
> This completes the end-to-end TikTok integration demonstration on the real production website.

---

## Notes for you before submission
- If Login Kit is not actually demonstrated in your build, remove it from the selected TikTok products before review.
- If Share Kit or Display API are not actively shown, remove them too.
- Make sure the domain visible in the browser bar is exactly `up.iamwillywang.com`.
- Use a non-watermarked TikTok profile configuration in the demo, because TikTok prohibits unwanted promotional watermarks in Content Posting API uploads.
