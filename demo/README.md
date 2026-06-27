# TikTok Direct Post API — Demo Video Recording

## Overview

Records a TikTok API audit demo video using Playwright + FFmpeg + MIMO voice cloning.
Output: single MP4 (≤50 MB) with browser recording, English subtitles, and voice narration.

## Prerequisites

```bash
# Playwright (already in devDependencies)
cd sau_frontend
npx playwright install chromium

# FFmpeg
which ffmpeg  # must be ≥ 4.x

# MIMO TTS Voice Clone (primary narration)
# Requires: pip install requests
# Get API keys from: https://platform.xiaomimimo.com

# Edge TTS (fallback narration)
uv run --with edge-tts edge-tts --list-voices | head -3
```

## Quick Start

```bash
cd sau_frontend

# One-shot: narrations → record → compose
npm run demo:all

# Or step-by-step:
npm run demo:narration   # generate narration audio (MIMO voice clone or edge-tts)
npm run demo:record      # run Playwright (headless, records WebM)
npm run demo:convert     # FFmpeg: WebM → MP4 + subtitles + narration
```

## How it works

1. **`generate_narrations.py`** generates 17 narration files using MIMO TTS voice clone
   (`mimo-v2.5-tts-voiceclone`) with your voice from `111.m4a`. Falls back to
   Microsoft edge-tts (`en-US-AndrewNeural`) if MIMO is unavailable.
2. **`demo.spec.ts`** is a single Playwright test that:
   - Injects auth token via localStorage (open mode)
   - Walks through the full Publish Center → TikTok flow in one browser session
   - Records one continuous WebM video at 1280×720
   - Overlays scene labels and step badges for reviewer clarity
3. **`compose_video.sh`** concatenates narration audio, burns English subtitles,
   mixes narration audio over the video, and outputs ≤50 MB MP4

## Voice Cloning Setup (MIMO)

The narration uses Xiaomi MiMo-V2.5-TTS-VoiceClone to generate speech in your voice
from the `111.m4a` sample file. To set up:

1. Get an API key from [Xiaomi MiMo Platform](https://platform.xiaomimimo.com)
2. Set environment variables:
   ```bash
   export MIMO_API_KEY=tp-xxxx
   export MIMO_API_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1
   export MIMO_VOICE_SAMPLE=/path/to/111.m4a
   ```
3. Or copy `demo/.env.example` to `demo/.env` and fill in the values

If MIMO is unavailable (quota exhausted, network error), the script automatically
falls back to edge-tts with a high-quality Microsoft neural voice.

## Recording Checklist (TikTok Review Points)

| # | Requirement | Scene |
|---|-------------|-------|
| — | App branding & purpose | Scene 1 |
| — | Publish Center overview | Scene 2 |
| — | Account selection | Scene 3 |
| 1a | Creator nickname displayed | Scene 4 |
| 1b | Post limit enforcement | Scene 5 |
| 1c | Video duration validation | Scene 6 |
| — | AI draft generation | Scene 7 |
| 2a | Title field (no default) | Scene 8 |
| 2b | Privacy dropdown (from API, no default) | Scene 9 |
| 2c | Interaction toggles (all off, greyed if disabled) | Scene 10 |
| 3a | Commercial disclosure (off by default) | Scene 11 |
| 3b | Branded + private guard | Scene 12 |
| 4 | Declaration text changes | Scene 13 |
| 5a | Content preview | Scene 14 |
| 5c | Consent before upload | Scene 15 |
| — | Review modal (final confirmation) | Scene 16 |
| 5d | Processing time notice | Scene 17 |

## Files

```
demo/
  generate_narrations.py   ← TTS narration generator (MIMO voice clone / edge-tts)
  compose_video.sh         ← FFmpeg composition pipeline
  .env.example             ← Environment config template
  narrations/              ← Generated audio files (scene_01.wav/mp3 … scene_17.wav/mp3)
  output/                  ← Playwright WebM + final MP4
  README.md

sau_frontend/
  demo/demo.spec.ts        ← Playwright test (17 scenes, continuous recording)
  playwright.config.ts     ← Playwright config (1280×720, video on)
  package.json             ← npm scripts (demo:record, demo:convert, demo:all)
```

## Output

```
demo/output/tiktok-review-demo.mp4   ← Final ≤50 MB MP4
```

## Troubleshooting

**No video.webm found:**
Make sure the backend is running at `http://localhost:5409`.

**MIMO quota exhausted:**
The script has 4 API keys and rotates on 429 errors. If all are exhausted,
it falls back to edge-tts automatically. Check your quota at
https://platform.xiaomimimo.com

**Narration sounds robotic (edge-tts fallback):**
edge-tts uses Microsoft's neural TTS. For best results, ensure MIMO keys
have quota available.

**FFmpeg subtitle errors:**
Ensure libass is available: `ffmpeg -filters 2>/dev/null | grep ass`

**Video exceeds 50 MB:**
The compose script auto-compresses. For manual control:
```bash
ffmpeg -i input.mp4 -c:v libx264 -crf 28 -preset fast -c:a aac -b:a 96k output.mp4
```
