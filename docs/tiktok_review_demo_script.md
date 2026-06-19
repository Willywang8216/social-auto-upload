# TikTok Direct Post Review Demo Script

This script maps every TikTok Content Sharing Guidelines requirement to a visible screen in the Social Auto Upload app.

## Pre-requisites

1. A TikTok account connected via OAuth (not sandbox mode)
2. A video file ready to upload
3. The app running at https://up.iamwillywang.com

## Demo Flow

### Step 1: Open TikTok Account Connection

1. Navigate to **Account Management** page
2. Show the connected TikTok account with creator avatar and nickname
3. Point out the account is connected via OAuth (not cookie-based)

**TikTok Requirement:** App intended for creators who knowingly connect their own account.

---

### Step 2: Open Publish Center

1. Navigate to **Publish Center**
2. Upload a video file (drag & drop or click to upload)
3. Show the video preview with filename and duration

**TikTok Requirement:** Show video preview before publishing.

---

### Step 3: Select Profile and Account

1. Select a profile that contains the TikTok account
2. Show the TikTok account is selected
3. Click "Generate drafts" to create per-account content

---

### Step 4: TikTok Review Modal Opens

When clicking "Publish" with a TikTok account selected, a dedicated **TikTok Review Modal** opens.

**TikTok Requirement:** Dedicated review page before Direct Post API call.

---

### Step 5: Show Creator Info Panel

The modal shows:
- Creator avatar
- Creator nickname (from `creator_info/query` API)
- Remaining post count
- "Cannot post" warning if creator_info says so

**TikTok Requirement:** Query latest creator info before posting. Show creator nickname. Stop posting when creator cannot post.

---

### Step 6: Enter Editable Title

1. Type a title in the "標題" field
2. Show the character count

**TikTok Requirement:** Editable title field.

---

### Step 7: Open Privacy Dropdown

1. Click the privacy dropdown
2. Show options populated from `creator_info.privacy_level_options`:
   - 公開 (PUBLIC_TO_EVERYONE)
   - 追蹤者 (FOLLOWER_OF_CREATOR)
   - 朋友 (MUTUAL_FOLLOW_FRIENDS)
   - 僅自己 (SELF_ONLY)
3. **No default value is selected** — user must manually choose
4. Select "公開"

**TikTok Requirement:** Privacy dropdown populated only from creator_info. No default value. User must manually select.

---

### Step 8: Show Interaction Checkboxes

All checkboxes are **unchecked by default**:
- ☐ 允許留言 (Allow Comment)
- ☐ 允許 Duet (Allow Duet) — video only
- ☐ 允許 Stitch (Allow Stitch) — video only

If creator_info disables any interaction:
- The checkbox is greyed out with tooltip: "此帳號在 TikTok 設定中已停用 [功能] 功能"

For photo posts: Duet and Stitch checkboxes are hidden.

**TikTok Requirement:** Interaction checkboxes unchecked by default. Disabled if creator_info says so. Photo posts only show Comment.

---

### Step 9: Commercial Content Disclosure

1. The "商業內容揭露" toggle is **off by default**
2. Turn it on
3. Two checkboxes appear:
   - ☐ 你的品牌 (Your Brand) — "你的影片/照片將被標記為「推廣內容」"
   - ☐ 品牌合作內容 (Branded Content) — "你的影片/照片將被標記為「付費合作」"
4. If neither is selected, publish is disabled with warning
5. Select "品牌合作內容"
6. Show that "僅自己" privacy is now disabled: "品牌合作內容的隱私設定不能設為僅自己可見"

**TikTok Requirement:** Commercial content disclosure toggle. Your Brand / Branded Content checkboxes. Branded content cannot be private.

---

### Step 10: Declaration/Consent

The declaration text changes based on selection:
- Normal or Your Brand only: "發佈即表示您同意 TikTok 的音樂使用確認。"
- Branded Content: "發佈即表示您同意 TikTok 的品牌合作內容政策和音樂使用確認。"

1. Read the declaration text
2. Check the consent checkbox
3. Publish button becomes enabled

**TikTok Requirement:** Explicit user consent before upload. Declaration text for Music Usage Confirmation and Branded Content Policy.

---

### Step 11: Click Publish

1. Click the "發佈" button
2. Show the post-processing notice: "TikTok 內容可能需要幾分鐘的處理時間才會在平台上顯示。"
3. Show the job link for tracking

**TikTok Requirement:** No upload before explicit final consent. Post-processing notice.

---

### Step 12: Status Polling

After publish, the app polls `publish/status/fetch` and shows:
- 處理中... (Processing)
- 已發佈 (Published)
- 發佈失敗 (Failed)

**TikTok Requirement:** Poll publish status and show user.

---

## Key Points for Reviewers

1. **Creator info is always fresh** — fetched via `creator_info/query` when the review modal opens
2. **No default privacy** — user must manually select
3. **Interaction checkboxes unchecked by default** — user must opt-in
4. **Disabled interactions are greyed out** — based on creator_info flags
5. **Photo posts don't show Duet/Stitch** — only Comment
6. **Commercial content disclosure is explicit** — toggle + checkboxes
7. **Branded content cannot be private** — enforced with visible warning
8. **Declaration text is context-aware** — changes for Branded Content
9. **Consent is required** — publish disabled until checkbox checked
10. **No upload before consent** — API call only after explicit Publish click
11. **Status is visible** — polling shows processing/published/failed
12. **No promotional watermark for TikTok** — warning shown if watermark enabled

## TikTok API Fields Used

- `creator_info/query` — creator nickname, privacy options, interaction flags, max duration, post limits
- `post_info.privacy_level` — from user selection
- `post_info.disable_comment` — inverse of Allow Comment
- `post_info.disable_duet` — inverse of Allow Duet (video only)
- `post_info.disable_stitch` — inverse of Allow Stitch (video only)
- `post_info.brand_content_toggle` — Branded Content selected
- `post_info.brand_organic_toggle` — Your Brand selected
- `publish/status/fetch` — post-publish status polling
