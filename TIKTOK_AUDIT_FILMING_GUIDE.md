# TikTok Direct Post API — Demo Video Filming Guide

## 📋 Pre-Filming Checklist

- [ ] TikTok account logged in and connected
- [ ] At least one video file ready to upload (MP4, under max duration)
- [ ] Browser in full-screen mode (1920x1080 recommended)
- [ ] All UI elements visible and readable
- [ ] Screen recording software ready (OBS, QuickTime, etc.)
- [ ] Audio narration prepared (or text overlays)

---

## 🎬 Scene-by-Scene Filming Script

### Scene 1: Creator Info Display (Requirement 1)

**Duration**: 15-20 seconds

**What to show**:
1. Navigate to the Publish Center page
2. Select a TikTok account
3. Wait for creator info to load
4. **Zoom in** on the creator nickname display (in TikTokPostSettings panel)

**Narration**:
> "When a TikTok account is selected, the app fetches the latest creator info from the TikTok API. The creator's nickname is displayed so users know which account will receive the content."

**Key points to highlight**:
- Creator nickname is visible ✅
- Creator info is fetched when account is selected ✅

---

### Scene 2: Post Limit Check (Requirement 1b)

**Duration**: 10-15 seconds

**What to show**:
1. If the account has reached its post limit, show the warning message
2. Show that the form is disabled and publishing is blocked

**Narration**:
> "If the creator cannot make more posts at this moment, the app stops the publishing attempt and prompts the user to try again later."

**Key points to highlight**:
- Warning message: "此帳號目前無法發佈更多貼文，請稍後再試" ✅
- Form is disabled when limit reached ✅

---

### Scene 3: Video Duration Check (Requirement 1c)

**Duration**: 10-15 seconds

**What to show**:
1. Upload a video that exceeds the max duration
2. Show the error message

**Narration**:
> "The app checks if the video duration follows the max_video_post_duration_sec returned by the creator_info API. If it exceeds the limit, an error is shown."

**Key points to highlight**:
- Error message: "影片時長 X 秒，超過 TikTok 限制的 Y 秒" ✅
- Publishing is blocked ✅

---

### Scene 4: Title Field (Requirement 2a)

**Duration**: 10-15 seconds

**What to show**:
1. Show the draft/message editor
2. Type or edit the title/caption

**Narration**:
> "Users can enter and edit the title for their post. The text is editable before publishing."

**Key points to highlight**:
- Title field is visible and editable ✅
- Character count is shown ✅

---

### Scene 5: Privacy Status Dropdown (Requirement 2b)

**Duration**: 20-25 seconds

**What to show**:
1. **Zoom in** on the privacy dropdown
2. Show that it has **no default value** (placeholder text is shown)
3. Click the dropdown to show all options from creator_info API
4. Select a privacy level

**Narration**:
> "Users must manually select the privacy status from a dropdown. There is no default value. The options listed follow the privacy_level_options returned in the creator_info API."

**Key points to highlight**:
- Placeholder: "請選擇隱私設定" (no default) ✅
- Options come from creator_info API ✅
- User must manually select ✅

---

### Scene 6: Interaction Settings (Requirement 2c)

**Duration**: 25-30 seconds

**What to show**:
1. **Zoom in** on the interaction checkboxes (Allow Comment, Allow Duet, Allow Stitch)
2. Show that **all are unchecked by default**
3. If any are disabled by creator settings, show the **greyed out** state with tooltip
4. Manually check each one

**Narration**:
> "Users can toggle Allow Comment, Allow Duet, and Allow Stitch. All are unchecked by default — users must manually turn them on. If the creator has disabled any interaction in their TikTok app settings, the checkbox is disabled and greyed out."

**Key points to highlight**:
- All unchecked by default ✅
- Disabled checkboxes are greyed out ✅
- Tooltip explains why disabled ✅
- Duet and Stitch hidden for photo posts ✅

---

### Scene 7: Music Usage Confirmation Declaration (Requirement 2 NOTE)

**Duration**: 15-20 seconds

**What to show**:
1. **Zoom in** on the declaration section
2. Show the checkbox with declaration text
3. **Check the checkbox** to give consent

**Narration**:
> "Before allowing users to post, there is a declaration asking for user's consent. It clearly states: 'By posting, you agree to TikTok's Music Usage Confirmation.' The user must check this box before the publish button becomes active."

**Key points to highlight**:
- Declaration text is visible ✅
- Checkbox requires active consent ✅
- Publish button is disabled until checked ✅

---

### Scene 8: Commercial Content Disclosure (Requirement 3a)

**Duration**: 30-40 seconds

**What to show**:
1. **Zoom in** on the "Commercial Content Disclosure" toggle
2. Show it is **off by default**
3. Turn it **on**
4. Show the "Your Brand" and "Branded Content" checkboxes appear
5. Check "Your Brand" — show the prompt: "你的影片/照片將被標記為「推廣內容」"
6. Uncheck "Your Brand", check "Branded Content" — show the prompt: "你的影片/照片將被標記為「付費合作」"
7. Check both — show the combined prompt: "你的影片/照片將被標記為「付費合作」"

**Narration**:
> "The commercial content disclosure toggle is off by default. When enabled, users can select 'Your Brand' or 'Branded Content'. Each selection shows a label indicating how the content will be classified. If both are selected, the content is labeled as 'Paid partnership'."

**Key points to highlight**:
- Toggle off by default ✅
- "Your Brand" checkbox with prompt ✅
- "Branded Content" checkbox with prompt ✅
- Combined prompt when both selected ✅

---

### Scene 9: Disclosure Validation (Requirement 3a continued)

**Duration**: 15-20 seconds

**What to show**:
1. Turn on disclosure but **don't check any option**
2. Show the warning message
3. Show that the **publish button is disabled**
4. **Hover over** the disabled publish button to show tooltip

**Narration**:
> "If the disclosure toggle is on but no option is selected, the publish button is disabled. Hovering over it shows: 'You need to indicate if your content promotes yourself, a third party, or both.'"

**Key points to highlight**:
- Warning message visible ✅
- Publish button disabled ✅
- Tooltip on hover ✅

---

### Scene 10: Privacy Management for Branded Content (Requirement 3b)

**Duration**: 20-25 seconds

**What to show**:
1. Enable disclosure and check "Branded Content"
2. Open the privacy dropdown
3. Show that "Only me" (SELF_ONLY) is **disabled**
4. Hover over disabled option to show tooltip

**Narration**:
> "When Branded Content is selected, the 'Only me' privacy option is disabled. Hovering over it shows: 'Branded content visibility cannot be set to private.'"

**Key points to highlight**:
- "Only me" is disabled when Branded Content is checked ✅
- Tooltip explains why ✅

---

### Scene 11: Compliance Declarations (Requirement 4)

**Duration**: 25-30 seconds

**What to show**:
1. **Case 1**: Disclosure ON + only "Your Brand" checked
   - Show declaration: "發佈即表示您同意 TikTok 的音樂使用確認。"
2. **Case 2**: Disclosure ON + only "Branded Content" checked
   - Show declaration: "發佈即表示您同意 TikTok 的品牌合作內容政策和音樂使用確認。"
3. **Case 3**: Disclosure ON + both checked
   - Show same declaration as Case 2

**Narration**:
> "The declaration text changes based on the commercial content settings. When only 'Your Brand' is selected, it mentions Music Usage Confirmation. When 'Branded Content' is selected (with or without 'Your Brand'), it also mentions the Branded Content Policy."

**Key points to highlight**:
- Declaration text updates dynamically ✅
- Correct text for each combination ✅

---

### Scene 12: Content Preview (Requirement 5a)

**Duration**: 15-20 seconds

**What to show**:
1. After uploading media, show the **video thumbnail preview**
2. Show the file name and type tag

**Narration**:
> "The app displays a preview of the to-be-posted content, including video thumbnails and file information."

**Key points to highlight**:
- Video/image preview visible ✅
- File name and type shown ✅

---

### Scene 13: No Promotional Watermarks (Requirement 5b)

**Duration**: 10-15 seconds

**What to show**:
1. Show the watermark option in processing options
2. Show the warning: "TikTok 不允許促銷浮水印，發佈到 TikTok 的內容將不會套用浮水印。"

**Narration**:
> "The app does not add promotional watermarks to TikTok content. A warning is shown when the watermark option is enabled."

**Key points to highlight**:
- Watermark warning visible ✅
- TikTok content is watermark-free ✅

---

### Scene 14: Editable Title/Hashtags (Requirement 5b continued)

**Duration**: 10-15 seconds

**What to show**:
1. Show the draft editor with pre-filled text
2. Edit the title/hashtags before publishing

**Narration**:
> "Preset text, including the title and hashtags, can be edited by the user before posting."

**Key points to highlight**:
- Title is editable ✅
- User has full control ✅

---

### Scene 15: Explicit Consent Before Upload (Requirement 5c)

**Duration**: 20-25 seconds

**What to show**:
1. Click the "Publish" button
2. Show the **confirmation modal** with declaration text
3. Show the "Confirm" and "Cancel" buttons
4. Click "Confirm" to proceed

**Narration**:
> "The app only starts sending content to TikTok after the user expressly consents. A confirmation dialog shows the compliance declaration and requires the user to confirm."

**Key points to highlight**:
- Confirmation modal appears ✅
- Declaration text is shown ✅
- User must actively confirm ✅

---

### Scene 16: Processing Time Notification (Requirement 5d)

**Duration**: 10-15 seconds

**What to show**:
1. After confirming, show the submit result
2. Show the notification: "TikTok 內容可能需要幾分鐘的處理時間才會在平台上顯示。"

**Narration**:
> "After publishing, the app clearly notifies users that it may take a few minutes for the content to process and be visible on their profile."

**Key points to highlight**:
- Processing time notification visible ✅

---

### Scene 17: Publish Status Polling (Requirement 5e)

**Duration**: 20-25 seconds

**What to show**:
1. Navigate to the Jobs page
2. Show the TikTok publish status (processing/publish_complete)
3. Show the "查看貼文" link

**Narration**:
> "The app polls the publish status API so users can understand the status of their posts. Status updates are displayed in real-time."

**Key points to highlight**:
- Publish status is shown ✅
- Link to view the post ✅

---

## 🎥 Filming Tips

### Technical Setup
- **Resolution**: 1920x1080 (Full HD)
- **Frame rate**: 30fps or 60fps
- **Screen recording**: OBS Studio, QuickTime, or similar
- **Browser**: Chrome or Edge in full-screen mode

### Visual Clarity
- **Zoom in** on important UI elements
- Use **mouse highlighting** or **cursor effects**
- Add **text overlays** for key points
- Use **arrows** or **circles** to draw attention

### Pacing
- **Don't rush** — let each element be visible for at least 3-5 seconds
- **Pause** after showing each requirement
- Use **transitions** between scenes

### Audio
- **Clear narration** explaining each requirement
- **Background music** (optional, low volume)
- **Sound effects** for key moments (optional)

### Post-Production
- **Add subtitles** in English and/or Chinese
- **Add timestamps** for each requirement
- **Add a table of contents** at the beginning
- **Add a summary** at the end

---

## 📝 Video Structure Summary

| Scene | Requirement | Duration | Key Element |
|-------|-------------|----------|-------------|
| 1 | 1a | 15-20s | Creator nickname |
| 2 | 1b | 10-15s | Post limit check |
| 3 | 1c | 10-15s | Video duration check |
| 4 | 2a | 10-15s | Title field |
| 5 | 2b | 20-25s | Privacy dropdown |
| 6 | 2c | 25-30s | Interaction checkboxes |
| 7 | 2 NOTE | 15-20s | Consent declaration |
| 8 | 3a | 30-40s | Content disclosure |
| 9 | 3a | 15-20s | Disclosure validation |
| 10 | 3b | 20-25s | Privacy management |
| 11 | 4 | 25-30s | Compliance declarations |
| 12 | 5a | 15-20s | Content preview |
| 13 | 5b | 10-15s | No watermarks |
| 14 | 5b | 10-15s | Editable title |
| 15 | 5c | 20-25s | Explicit consent |
| 16 | 5d | 10-15s | Processing notification |
| 17 | 5e | 20-25s | Status polling |

**Total estimated duration**: 4-5 minutes

---

## ✅ Final Checklist

Before submitting the video:

- [ ] All 17 scenes are included
- [ ] Each requirement is clearly demonstrated
- [ ] UI elements are readable
- [ ] Narration is clear and concise
- [ ] Video is 4-5 minutes long
- [ ] Video quality is 1080p or higher
- [ ] Subtitles are added (optional but recommended)
- [ ] Table of contents is included (optional but recommended)

---

## 🔗 Reference Links

- [TikTok Content Sharing Guidelines](https://developers.tiktok.com/doc/content-sharing-guidelines)
- [TikTok Content Posting API Documentation](https://developers.tiktok.com/doc/content-posting-api)
- [TikTok Developer Portal](https://developers.tiktok.com/)

---

## 📞 Support

If you have questions about the filming guide or the audit requirements, refer to the TikTok Developer Documentation or contact TikTok Developer Support.
