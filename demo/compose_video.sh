#!/usr/bin/env bash
# demo/compose_video.sh
# Composes final TikTok review MP4 from Playwright recording with subtitles + narration
#
# Run from sau_frontend/:
#   npm run demo:convert
#   or: bash ../demo/compose_video.sh

set -e

DEMO_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$DEMO_DIR")"
OUT_DIR="$DEMO_DIR/output"
NARRATIONS_DIR="$DEMO_DIR/narrations"
FINAL="$OUT_DIR/tiktok-review-demo.mp4"

CRF=23
AUDIO_BITRATE=128k
TOTAL_SCENES=17

echo "=== TikTok Review Demo — Video Composer ==="
echo "Demo dir:       $DEMO_DIR"
echo "Project dir:    $PROJECT_DIR"
echo "Output dir:     $OUT_DIR"
echo "Narrations dir: $NARRATIONS_DIR"
echo "Final video:    $FINAL"
echo ""

# Check FFmpeg
ffmpeg -version 2>&1 | head -1

mkdir -p "$OUT_DIR"

# ─── Step 1: Find the Playwright recording ────────────────────────────────────

# Playwright saves video.webm inside a subdirectory like:
#   demo/output/<test-title-hash>/video.webm
# Also check sau_frontend/demo/output/ (Playwright config saves there)
FRONTEND_OUT="$(dirname "$DEMO_DIR")/sau_frontend/demo/output"
WEBM_FILE=""
for f in "$OUT_DIR"/*/video.webm "$OUT_DIR"/video.webm \
         "$FRONTEND_OUT"/*/video.webm "$FRONTEND_OUT"/video.webm \
         "$FRONTEND_OUT"/*.webm "$OUT_DIR"/*.webm; do
  if [ -f "$f" ]; then
    WEBM_FILE="$f"
    break
  fi
done

if [ -z "$WEBM_FILE" ]; then
  echo "ERROR: No video.webm found in $OUT_DIR"
  echo "Run 'npx playwright test demo/demo.spec.ts' first."
  exit 1
fi

echo "Found recording: $WEBM_FILE"
WEBM_DUR=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$WEBM_FILE" 2>/dev/null)
echo "Duration: ${WEBM_DUR}s"

# ─── Step 2: Concatenate narration audio files ────────────────────────────────

NARRATION_CONCAT="$OUT_DIR/narration_concat.wav"
CONCAT_LIST="$OUT_DIR/narration_list.txt"
> "$CONCAT_LIST"

# Find audio files for each scene (wav from MIMO, mp3 from edge-tts)
# Prefer wav over mp3
for i in $(seq 1 $TOTAL_SCENES); do
  padded=$(printf "%02d" $i)
  found=""
  # Prefer wav (MIMO voice clone)
  for ext in wav mp3; do
    audio="$NARRATIONS_DIR/scene_${padded}.${ext}"
    if [ -f "$audio" ]; then
      found="$audio"
      break
    fi
  done
  if [ -n "$found" ]; then
    echo "file '$found'" >> "$CONCAT_LIST"
    echo "  Scene $padded: $(basename "$found") ($(du -k "$found" | cut -f1)KB)"
  else
    echo "  Scene $padded: MISSING"
  fi
done

echo ""

if [ -s "$CONCAT_LIST" ]; then
  # If mixing wav and mp3, normalize to wav first
  HAS_WAV=$(grep -c '\.wav' "$CONCAT_LIST" || true)
  HAS_MP3=$(grep -c '\.mp3' "$CONCAT_LIST" || true)

  if [ "$HAS_WAV" -gt 0 ] && [ "$HAS_MP3" -gt 0 ]; then
    echo "Mixed formats detected — normalizing to wav..."
    NORMALIZED_LIST="$OUT_DIR/narration_normalized.txt"
    > "$NORMALIZED_LIST"
    while IFS= read -r line; do
      # Extract filename from "file '/path/to/file'"
      filepath=$(echo "$line" | sed "s/file '//;s/'//")
      if [[ "$filepath" == *.mp3 ]]; then
        wavpath="${filepath%.mp3}_norm.wav"
        ffmpeg -y -i "$filepath" -ar 44100 -ac 1 "$wavpath" 2>/dev/null
        echo "file '$wavpath'" >> "$NORMALIZED_LIST"
      else
        echo "$line" >> "$NORMALIZED_LIST"
      fi
    done < "$CONCAT_LIST"
    CONCAT_LIST="$NORMALIZED_LIST"
  fi

  ffmpeg -y -f concat -safe 0 -i "$CONCAT_LIST" -c copy "$NARRATION_CONCAT" 2>&1 | tail -3
  NARRATION_DUR=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$NARRATION_CONCAT" 2>/dev/null)
  echo "Narration duration: ${NARRATION_DUR}s"
else
  echo "No narration files found — will produce silent video."
  NARRATION_CONCAT=""
fi

# ─── Step 3: Generate SRT subtitles ──────────────────────────────────────────

SRT_FILE="$OUT_DIR/subtitles.srt"
python3 -c "
# 17 scenes matching the Playwright demo spec
scenes = [
    (1, 'Social Auto Upload — Multi-Platform Content Publisher'),
    (2, 'Publish Center — Full Post Workflow'),
    (3, 'Selecting TikTok Account — Creator Info Loading'),
    (4, 'Creator Info: Nickname + Avatar from creator_info/query API'),
    (5, 'Post Limit Enforcement — Blocks publishing when limit reached'),
    (6, 'Video Uploaded — Duration Validated Against TikTok Max'),
    (7, 'AI Draft Generation — Per-Account Content'),
    (8, 'Title Field: Required, No Default Value — User Must Type'),
    (9, 'Privacy Dropdown: No Default, Options from creator_info API'),
    (10, 'Interaction Settings: Comment / Duet / Stitch — All OFF'),
    (11, 'Commercial Disclosure: OFF by Default, Your Brand / Branded Content'),
    (12, 'Branded Content: Private Option Disabled'),
    (13, 'Declaration Text Changes Based on Disclosure Selection'),
    (14, 'Content Preview: Video Thumbnail + Filename'),
    (15, 'Consent Required — Publish Button Disabled Until Checked'),
    (16, 'Review Modal: All Settings for Final Confirmation'),
    (17, 'Processing Notice — TikTok Content Takes Minutes to Process'),
]

# Calculate timing
try:
    dur = float('$WEBM_DUR')
except:
    dur = 120.0

scene_dur = dur / len(scenes)

with open('$SRT_FILE', 'w') as f:
    for i, (num, text) in enumerate(scenes):
        start = i * scene_dur
        end = start + scene_dur - 0.3
        def fmt(t):
            h = int(t // 3600)
            m = int((t % 3600) // 60)
            s = t % 60
            return f'{h:02d}:{m:02d}:{s:06.3f}'.replace('.', ',')
        f.write(f'{num}\n{fmt(start)} --> {fmt(end)}\n{text}\n\n')

print(f'SRT generated: $SRT_FILE ({len(scenes)} entries, {scene_dur:.1f}s each)')
" 2>&1

# ─── Step 4: Compose final MP4 ────────────────────────────────────────────────

echo ""
echo "Composing final video..."

if [ -n "$NARRATION_CONCAT" ] && [ -f "$NARRATION_CONCAT" ]; then
  # Video + narration + subtitles
  ffmpeg -y \
    -i "$WEBM_FILE" \
    -i "$NARRATION_CONCAT" \
    -vf "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,subtitles='$SRT_FILE':force_style='FontName=Arial,FontSize=24,PrimaryColour=&HFFFFFF,OutlineColour=&H000000,Outline=2,Bold=1,MarginV=40'" \
    -c:v libx264 -preset medium -crf $CRF \
    -c:a aac -b:a $AUDIO_BITRATE \
    -shortest \
    "$FINAL" 2>&1 | tail -10
else
  # Video only + subtitles (no narration)
  ffmpeg -y \
    -i "$WEBM_FILE" \
    -vf "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,subtitles='$SRT_FILE':force_style='FontName=Arial,FontSize=24,PrimaryColour=&HFFFFFF,OutlineColour=&H000000,Outline=2,Bold=1,MarginV=40'" \
    -c:v libx264 -preset medium -crf $CRF \
    -c:a aac -b:a $AUDIO_BITRATE \
    "$FINAL" 2>&1 | tail -10
fi

# ─── Step 5: Check size ───────────────────────────────────────────────────────

SIZE=$(du -h "$FINAL" | cut -f1)
SIZE_KB=$(du -k "$FINAL" | cut -f1)
DUR=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$FINAL" 2>/dev/null)

echo ""
echo "=== Done ==="
echo "Final video: $FINAL"
echo "Duration:    ${DUR}s"
echo "File size:   $SIZE"

if [ "$SIZE_KB" -gt 50000 ]; then
  echo "⚠️  File exceeds 50 MB — compressing further..."
  COMPRESSED="${FINAL%.mp4}_compressed.mp4"
  ffmpeg -y -i "$FINAL" \
    -c:v libx264 -preset fast -crf 28 \
    -vf "scale=1920:-2" \
    -c:a aac -b:a 96k \
    "$COMPRESSED" 2>&1 | tail -5
  mv "$COMPRESSED" "$FINAL"
  echo "Compressed size: $(du -h "$FINAL" | cut -f1)"
fi

echo ""
echo "✓ Ready at: $FINAL"
