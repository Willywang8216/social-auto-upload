# TikTok API Audit Demo Recording

Records a 14-scene walkthrough of the TikTok direct-post UX flow in Social Auto Upload.

## Prerequisites

- Social Auto Upload backend running at `http://localhost:5409`
- Frontend dev server running (`cd sau_frontend && npm run dev`) OR app served by backend
- Chromium browser available (Playwright)
- FFmpeg at `/usr/bin/ffmpeg`
- Python 3 with `gtts` installed (`uv run --with gtts python3 ...`)
- (Optional) XTTS v2 for voice cloning — tries automatically, falls back to gTTS

## Quick Start

```bash
# Generate narrations (gTTS or XTTS if available)
npm run demo:narration

# Record all scenes (headed Chromium)
npm run demo:record

# Compose final video with subtitles + narration
npm run demo:convert

# Run all three in sequence
npm run demo:all
```

## Output

- Screenshots: `sau_frontend/demo/output/screenshot_*.png`
- Recorded video clips: `sau_frontend/demo/output/` (Playwright video files)
- Composed narration audio: `sau_frontend/demo/narrations/scene_*.mp3`
- Final demo video: `sau_frontend/demo/output/tiktok-review-demo.mp4`

## Scene List

| # | Scene | Description |
|---|-------|-------------|
| 1 | App landing | Social Auto Upload title/branding |
| 2 | Publish Center | Entry point, login redirect handled |
| 3 | App chrome | Header/sidebar showing app name |
| 4 | TikTok account select | Profile → TikTok account, creator info loads |
| 5 | Creator nickname | From `creator_info` API response |
| 6 | Post settings expand | Title / privacy / interactions visible |
| 7 | Title field | Required, no default value |
| 8 | Privacy dropdown | No default, from `creator_info.privacy_level_options` |
| 9 | Interaction settings | Comment / Duet / Stitch — all OFF by default |
| 10 | Commercial disclosure | Toggle OFF by default |
| 11 | Branded content + private | SELF_ONLY disabled when branded content ON |
| 12 | Declaration text | Changes based on commercial content selection |
| 13 | Consent required | Publish button disabled until consent checked |
| 14 | Processing notice | Post-submit polling for publish status |

## Manual Intervention

If TikTok OAuth is required during recording, the test will display a pause overlay
in the browser. Complete the login manually, then close the overlay (click it or press Escape)
to continue.

## Narration Generation

```bash
# Try XTTS voice cloning first (requires TTS package + Python 3.9-3.11)
uv run --with TTS python3 demo/generate_narrations.py

# Fallback to gTTS
python3 demo/generate_narrations.py
```

## FFmpeg Composition

```bash
bash demo/compose_video.sh
```

Requires:
- Scene screenshots at `demo/output/screenshot_*.png`
- Narration audio at `demo/narrations/scene_*.mp3`
- FFmpeg with libx264, aac, and fade filters

## Environment Variables

```bash
# Optional: override narration speed
NARRATION_SPEED=0.9   # slower = more natural

# Optional: scene duration override (seconds per scene)
SCENE_DURATION=4
```