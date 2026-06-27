#!/usr/bin/env python3
"""
generate_narrations.py
Generates TTS narration audio for the TikTok audit demo video.

Voice cloning: uses MIMO TTS voice clone (mimo-v2.5-tts-voiceclone) with 111.m4a.
Falls back to edge-tts (Microsoft neural voice) if MIMO is unavailable.

Usage:
    python3 generate_narrations.py              # auto-detect best backend
    python3 generate_narrations.py --backend mimo      # force MIMO
    python3 generate_narrations.py --backend edge-tts  # force edge-tts
    # or from sau_frontend/:
    #   npm run demo:narration

Output:
    demo/narrations/scene_01.wav (MIMO) or scene_01.mp3 (edge-tts)
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

SCRIPT_DIR = Path(__file__).parent
VOICE_SAMPLE = Path(os.environ.get("MIMO_VOICE_SAMPLE", "/home/will/social-auto-upload/111.m4a"))
OUTPUT_DIR = SCRIPT_DIR / "narrations"
OUTPUT_DIR.mkdir(exist_ok=True)

# MIMO API config
MIMO_API_BASE = os.environ.get("MIMO_API_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1")
MIMO_API_KEYS = [
    os.environ.get("MIMO_API_KEY", ""),
    "tp-cx97ld5slqbenq1zi3gb3wau3um3pp9cbnln0jt625qh3uih",
    "tp-sxq08qcbtu1kiyjcug3yfmcwmcykn09hnickrw29jn06u3ia",
    "tp-s850zr9ssk822vq4z8at91ktxss73apyth819idueb3yr7lt",
    "tp-co6v9yr6xtwlccrpavid2gw93hkg0feuqiahfynr30c0pl5o",
]
# Deduplicate, filter empty
MIMO_API_KEYS = list(dict.fromkeys(k for k in MIMO_API_KEYS if k))
MIMO_MODEL = "mimo-v2.5-tts-voiceclone"
MIMO_STYLE = "Professional, clear narration tone for a product demo video. Speak at a moderate pace with clear articulation. Warm and confident voice."

# edge-tts config
EDGE_TTS_VOICE = "en-US-AndrewNeural"

# 17 scenes matching the Playwright demo spec (full TikTok review checklist)
SCENES = [
    (1, "Welcome to Social Auto Upload. This is a multi-platform content publishing tool that lets you manage and publish videos to TikTok and other platforms from a single dashboard. Let's walk through the complete TikTok direct post flow."),
    (2, "This is the Publish Center. From here, you can upload media, select target accounts, configure post settings, and publish to multiple platforms at once. The entire workflow is designed for content creators who need full control over every post."),
    (3, "Here we select a profile containing our TikTok account. When the TikTok account is selected, the app immediately begins fetching the latest creator information from TikTok's API to ensure all settings are current."),
    (4, "The creator's nickname and avatar are displayed, fetched directly from TikTok's creator info API. This ensures users always know exactly which account will receive their content. The remaining daily post count is also shown."),
    (5, "The app checks the creator's post limit. If the creator cannot make more posts at this moment, publishing is blocked with a clear warning message, and the user is prompted to try again later."),
    (6, "Now we upload a sample video. The app validates the video duration against the maximum allowed duration returned by TikTok's creator info API. If the video exceeds the limit, publishing is blocked with an error message."),
    (7, "Next, we enter a brief describing what the post should convey, and click Generate Drafts. The app uses AI to create per-account content previews tailored to each platform."),
    (8, "The title field is editable and has no default value. Users must manually enter their caption text before publishing. This ensures full user control over the content."),
    (9, "The privacy settings dropdown has no default value. Users must manually select from options provided by TikTok's creator info API, which ensures only valid choices are available."),
    (10, "Interaction settings — Allow Comment, Allow Duet, Allow Stitch — are all unchecked by default. Users must manually enable them. If the creator has disabled any interaction in their TikTok settings, the checkbox is greyed out."),
    (11, "Commercial content disclosure is off by default. When enabled, users can indicate if they are promoting their own brand or third-party branded content. At least one option must be selected before publishing."),
    (12, "If a user selects branded content, the private visibility option is automatically disabled, because TikTok prohibits branded content from being posted as private. A clear tooltip explains this restriction."),
    (13, "The declaration text dynamically changes based on commercial content selection. When branded content is selected, it includes acceptance of TikTok's Branded Content Policy. When only the user's own brand is selected, only the Music Usage Confirmation is shown."),
    (14, "The app displays a preview of the content to be posted, including the video thumbnail, filename, and file type. This gives users a clear view of what will appear on their TikTok profile."),
    (15, "The publish button remains disabled until the user explicitly checks the consent declaration. Content is only sent to TikTok's servers after express user consent has been given. This is a critical compliance requirement."),
    (16, "Before publishing, a mandatory review modal displays all settings for final confirmation: creator info, content preview, title, privacy level, interaction settings, commercial disclosure, and the consent declaration."),
    (17, "After submission, users are clearly notified that TikTok content may take a few minutes to process before appearing on their profile. The system polls for publish status updates to keep users informed."),
]


def convert_voice_sample_to_mp3() -> Path | None:
    """Convert 111.m4a to mp3 for MIMO API (mp3/wav only)."""
    if not VOICE_SAMPLE.exists():
        print(f"  ⚠ Voice sample not found: {VOICE_SAMPLE}")
        return None

    mp3_path = SCRIPT_DIR / "voice_sample.mp3"
    if mp3_path.exists() and mp3_path.stat().st_size > 10000:
        return mp3_path

    print(f"  Converting {VOICE_SAMPLE.name} → mp3...")
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(VOICE_SAMPLE),
         "-acodec", "libmp3lame", "-b:a", "128k", "-ar", "22050",
         str(mp3_path)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0 and mp3_path.exists():
        size_kb = mp3_path.stat().st_size // 1024
        print(f"  ✓ Voice sample: {mp3_path.name} ({size_kb} KB)")
        return mp3_path
    else:
        print(f"  ✗ FFmpeg conversion failed: {result.stderr[:200]}", file=sys.stderr)
        return None


def load_voice_base64(mp3_path: Path) -> str | None:
    """Load and base64-encode the voice sample."""
    data = mp3_path.read_bytes()
    if len(data) > 10 * 1024 * 1024:
        print(f"  ⚠ Voice sample too large: {len(data)//1024} KB (max 10 MB)", file=sys.stderr)
        return None
    return base64.b64encode(data).decode()


def generate_with_mimo(text: str, output_path: Path, voice_b64: str, key_index: int = 0) -> bool:
    """Generate with MIMO TTS voice clone. Returns True on success."""
    if requests is None:
        return False

    url = f"{MIMO_API_BASE}/chat/completions"

    for attempt in range(min(3, len(MIMO_API_KEYS))):
        key = MIMO_API_KEYS[(key_index + attempt) % len(MIMO_API_KEYS)]
        try:
            resp = requests.post(
                url,
                headers={
                    "api-key": key,
                    "Content-Type": "application/json",
                },
                json={
                    "model": MIMO_MODEL,
                    "messages": [
                        {"role": "user", "content": MIMO_STYLE},
                        {"role": "assistant", "content": text},
                    ],
                    "audio": {
                        "format": "wav",
                        "voice": f"data:audio/mpeg;base64,{voice_b64}",
                    },
                    "stream": False,
                },
                timeout=120,
            )

            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices", [])
                if choices:
                    msg = choices[0].get("message", {})
                    audio = msg.get("audio", {})
                    audio_data_b64 = audio.get("data", "")
                    if audio_data_b64:
                        audio_bytes = base64.b64decode(audio_data_b64)
                        output_path.write_bytes(audio_bytes)
                        print(f"  ✓ MIMO: {output_path.name} ({len(audio_bytes)//1024}KB)")
                        return True
                    else:
                        print(f"  ✗ MIMO: no audio data in response", file=sys.stderr)
                        return False
            elif resp.status_code == 429:
                print(f"  ⚠ MIMO key ...{key[-6:]} quota exhausted, trying next...", file=sys.stderr)
                continue
            elif resp.status_code == 401:
                print(f"  ⚠ MIMO key ...{key[-6:]} invalid, trying next...", file=sys.stderr)
                continue
            else:
                print(f"  ✗ MIMO {resp.status_code}: {resp.text[:150]}", file=sys.stderr)
                return False
        except requests.exceptions.Timeout:
            print(f"  ✗ MIMO timeout", file=sys.stderr)
            continue
        except Exception as e:
            print(f"  ✗ MIMO error: {e}", file=sys.stderr)
            return False

    return False


def generate_with_edge_tts(text: str, output_path: Path) -> bool:
    """Generate with Microsoft edge-tts. Returns True on success."""
    # edge-tts outputs mp3
    mp3_path = output_path.with_suffix(".mp3") if output_path.suffix != ".mp3" else output_path
    try:
        result = subprocess.run(
            [
                "uv", "run", "--with", "edge-tts",
                "edge-tts",
                "--voice", EDGE_TTS_VOICE,
                "--rate", "+5%",
                "--text", text,
                "--write-media", str(mp3_path),
            ],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and mp3_path.exists() and mp3_path.stat().st_size > 1000:
            print(f"  ✓ edge-tts: {mp3_path.name} ({mp3_path.stat().st_size // 1024}KB)")
            return True
        else:
            print(f"  ✗ edge-tts failed: {result.stderr[:200]}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"  ✗ edge-tts error: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="Generate TikTok demo narrations")
    parser.add_argument("--backend", choices=["mimo", "edge-tts", "auto"], default="auto",
                        help="TTS backend to use (default: auto)")
    args = parser.parse_args()

    print("=== TikTok Demo — Narration Generator ===")
    print(f"Backend: {args.backend}")
    print(f"Voice sample: {VOICE_SAMPLE} ({'found' if VOICE_SAMPLE.exists() else 'NOT FOUND'})")
    print(f"Output dir: {OUTPUT_DIR}")
    print()

    # Determine backend
    use_mimo = False
    voice_b64 = None

    if args.backend in ("mimo", "auto"):
        mp3_path = convert_voice_sample_to_mp3()
        if mp3_path and MIMO_API_KEYS:
            voice_b64 = load_voice_base64(mp3_path)
            if voice_b64:
                # Quick health check
                print("  Testing MIMO API connectivity...")
                try:
                    resp = requests.post(
                        f"{MIMO_API_BASE}/chat/completions",
                        headers={"api-key": MIMO_API_KEYS[0], "Content-Type": "application/json"},
                        json={
                            "model": MIMO_MODEL,
                            "messages": [
                                {"role": "user", "content": "Test."},
                                {"role": "assistant", "content": "Hello."},
                            ],
                            "audio": {"format": "wav", "voice": f"data:audio/mpeg;base64,{voice_b64}"},
                            "stream": False,
                        },
                        timeout=30,
                    )
                    if resp.status_code == 200:
                        use_mimo = True
                        print("  ✓ MIMO API available — using voice clone")
                    else:
                        print(f"  ⚠ MIMO API returned {resp.status_code} — falling back to edge-tts")
                except Exception as e:
                    print(f"  ⚠ MIMO API unreachable ({e}) — falling back to edge-tts")
            else:
                print("  ⚠ Could not encode voice sample — falling back to edge-tts")
        elif args.backend == "mimo":
            print("  ✗ MIMO requested but voice sample or API keys missing", file=sys.stderr)
            sys.exit(1)

    if not use_mimo:
        if args.backend == "mimo":
            print("  ✗ MIMO unavailable", file=sys.stderr)
            sys.exit(1)
        print("  Using edge-tts (Microsoft neural voice)")

    # Clean old files
    for f in OUTPUT_DIR.glob("scene_*.*"):
        f.unlink()

    # Generate narrations
    success_count = 0
    for num, text in SCENES:
        print(f"\nScene {num:02d}...", flush=True)

        if use_mimo:
            output_path = OUTPUT_DIR / f"scene_{num:02d}.wav"
            if generate_with_mimo(text, output_path, voice_b64, key_index=num % len(MIMO_API_KEYS)):
                success_count += 1
                continue
            # MIMO failed for this scene — fall back
            print(f"  → Falling back to edge-tts for scene {num:02d}")

        output_path = OUTPUT_DIR / f"scene_{num:02d}.mp3"
        if generate_with_edge_tts(text, output_path):
            success_count += 1
            continue

        print(f"  ✗ FAILED for scene {num:02d}")

    # Summary
    print("\n=== Done ===")
    audio_files = sorted(OUTPUT_DIR.glob("scene_*.*"))
    print(f"Generated {len(audio_files)}/{len(SCENES)} narration files:")
    total_kb = 0
    for f in audio_files:
        size_kb = f.stat().st_size // 1024
        total_kb += size_kb
        print(f"  {f.name} ({size_kb}KB)")
    print(f"Total: {total_kb}KB")
    print(f"Backend used: {'MIMO voice clone' if use_mimo else 'edge-tts'}")


if __name__ == "__main__":
    main()
