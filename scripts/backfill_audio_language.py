#!/usr/bin/env python3
"""Detect the real spoken language of every video still queued, from its audio.

The user asked for exactly this: "detects the language in the video process".
Filenames lie — ``DeerOdds概率鹿梦Pro…__sfw.mp4`` has a Chinese name and English
audio; ``20261024095109313__nsfw.mp4`` has no name at all. So the pending queue is
re-classified by listening to it.

For each campaign with a pending target and a video source it:

  1. builds a 75 s probe from three 25 s windows at 20 % / 50 % / 80 % of the
     video (the first 30 s of a long upload is often silence — the DeerOdds clip
     is 93 minutes and opens on music);
  2. sends it to the local STT sidecar, which samples five windows across the
     probe, drops silent ones and takes the confident majority;
  3. records ``contentLanguage`` / ``contentLanguageSource="audio"`` on the
     campaign, so routing and the audit both use the audio from now on.

    python backfill_audio_language.py --list      # what would be probed
    python backfill_audio_language.py --apply     # probe and record
    python backfill_audio_language.py --apply --cancel-violations
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path("/home/will/social-auto-upload")
sys.path.insert(0, str(REPO))

from myUtils import campaigns as campaign_store  # noqa: E402
from myUtils import content_language as cl  # noqa: E402
from myUtils import routing_audit  # noqa: E402

DB = REPO / "db" / "database.db"
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"}
PROBE_WINDOWS = (0.2, 0.5, 0.8)
PROBE_SECONDS = 25


def _candidates(conn: sqlite3.Connection) -> list[dict]:
    """Campaigns with a pending target whose media includes a video."""
    rows = conn.execute(
        """
        SELECT DISTINCT c.id AS campaign_id, c.metadata_json
        FROM publish_job_targets jt
        JOIN campaign_posts cp ON cp.id = CAST(substr(jt.file_ref, 15) AS INTEGER)
        JOIN campaigns c ON c.id = cp.campaign_id
        WHERE jt.status = 'pending'
        ORDER BY c.id
        """
    ).fetchall()
    out = []
    for campaign_id, metadata_json in rows:
        filenames = [
            row[0] for row in conn.execute(
                """SELECT fr.filename FROM media_group_items mgi
                   JOIN file_records fr ON fr.id = mgi.file_record_id
                   JOIN campaigns c ON c.media_group_id = mgi.media_group_id
                   WHERE c.id = ?""",
                (campaign_id,),
            )
        ]
        videos = [name for name in filenames if Path(str(name)).suffix.lower() in VIDEO_SUFFIXES]
        if not videos:
            continue
        try:
            metadata = json.loads(metadata_json or "{}")
        except ValueError:
            metadata = {}
        out.append({
            "campaign_id": campaign_id,
            "filenames": filenames,
            "videos": videos,
            "stored": cl.from_metadata(metadata),
        })
    return out


def _locate_source(conn: sqlite3.Connection, campaign_id: int, videos: list[str]) -> Path | None:
    """The best local file to listen to: the staged source, else the output."""
    cache = REPO / "videoFile" / "_inbox_cache"
    for name in videos:
        candidate = cache / Path(str(name)).name
        if candidate.is_file():
            return candidate
        # file_records may store a path relative to videoFile/ or absolute.
        for prefix in (REPO / "videoFile", Path("/app/videoFile")):
            candidate = prefix / str(name)
            if candidate.is_file():
                return candidate
    # The offload moved the source out of videoFile/; its recorded path is the
    # only reliable pointer, and the local copy may still be there.
    for row in conn.execute(
        "SELECT local_path FROM campaign_artifacts WHERE campaign_id=? AND local_path LIKE '%.mp4'",
        (campaign_id,),
    ):
        recorded = str(row[0] or "")
        host = REPO / recorded.replace("/app/", "", 1) if recorded.startswith("/app/") else Path(recorded)
        if host.is_file():
            return host
    generated = REPO / "generated" / "campaigns" / f"campaign-{campaign_id}"
    if generated.is_dir():
        for candidate in sorted(generated.glob("*.mp4")):
            if "_pub" in candidate.name:
                return candidate
        for candidate in sorted(generated.glob("*.mp4")):
            return candidate
    return None


RCLONE = ["rclone", "--config", "/home/will/.config/rclone/rclone.conf"]
#: Where the offload lifecycle can have put a campaign's media. A pending target
#: whose campaign was already partly published has its source under `published/`,
#: which is why a live-tier-only lookup found nothing for 338 campaigns.
REMOTE_TIERS = ("sau/generated", "sau/published/generated")
REMOTE = "GDrive-willywang8216"
FETCH_TIMEOUT = int(os.environ.get("SAU_BACKFILL_FETCH_TIMEOUT", "240"))


def _fetch_from_drive(campaign_id: int, destination_dir: Path) -> Path | None:
    """Pull the campaign's video back off Drive, newest tier first."""
    for root in REMOTE_TIERS:
        spec = f"{REMOTE}:{root}/campaigns/campaign-{campaign_id}"
        try:
            subprocess.run(
                [*RCLONE, "copy", spec, str(destination_dir), "--include", "*.mp4"],
                capture_output=True, text=True, timeout=FETCH_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            print(f"    drive fetch timed out for {root}")
            continue
        candidates = sorted(destination_dir.rglob("*.mp4"))
        if candidates:
            # The probe only needs the shortest part when a video was split.
            return min(candidates, key=lambda item: item.stat().st_size)
    return None


def _locate_url(conn: sqlite3.Connection, campaign_id: int) -> str | None:
    """A public URL for the campaign's video, when one is recorded.

    Most campaigns have their media on R2 with a public URL, and R2 honours
    range requests — so ffmpeg can seek to a timestamp and fetch only the bytes
    around it. That turns a ~120 s Drive download into a ~2 s read, which is the
    difference between probing the queue in an hour and in a day.
    """
    for row in conn.execute(
        """SELECT public_url FROM campaign_artifacts
           WHERE campaign_id=? AND public_url LIKE 'http%'
             AND (artifact_kind LIKE '%video%' OR local_path LIKE '%.mp4')
           ORDER BY id DESC""",
        (campaign_id,),
    ):
        url = str(row[0] or "").strip()
        if url and not url.startswith("http://localhost"):
            return url
    return None


def _probe_wav_seeking(source: str, destination: Path) -> bool:
    """Three windows via *input* seeking, concatenated.

    Works for a local file and an HTTP URL alike, and — unlike the filter-based
    version — only reads the bytes it needs, which is what makes probing over
    the network viable.
    """
    duration = _duration(source)
    if duration <= 0:
        return False
    spans = [max(0.0, duration * fraction - PROBE_SECONDS / 2) for fraction in PROBE_WINDOWS]
    if duration < PROBE_SECONDS * len(PROBE_WINDOWS):
        spans = [0.0]
    pieces = []
    try:
        for index, start in enumerate(spans):
            piece = destination.with_name(f"{destination.stem}.{index}.wav")
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.2f}", "-t", str(PROBE_SECONDS),
                 "-i", source, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(piece)],
                capture_output=True, text=True, timeout=600, check=True,
            )
            if piece.is_file() and piece.stat().st_size > 44:
                pieces.append(piece)
        if not pieces:
            return False
        if len(pieces) == 1:
            pieces[0].replace(destination)
            return True
        listing = destination.with_suffix(".txt")
        listing.write_text("".join(f"file '{piece}'\n" for piece in pieces), encoding="utf-8")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(destination)],
            capture_output=True, text=True, timeout=300, check=True,
        )
        return destination.is_file() and destination.stat().st_size > 44
    except Exception as exc:  # noqa: BLE001
        print(f"    seek probe failed: {str(exc)[:110]}")
        return False
    finally:
        for piece in pieces:
            piece.unlink(missing_ok=True)
        destination.with_suffix(".txt").unlink(missing_ok=True)


def _duration(path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout.strip()
        return float(out)
    except Exception:  # noqa: BLE001
        return 0.0


def _probe_language(wav: Path) -> str | None:
    """Ask the local STT sidecar for the language only (no transcript)."""
    import base64
    import json as _json
    import urllib.error
    import urllib.request

    base = (os.environ.get("SAU_STT_BASE_URL") or "http://127.0.0.1:9099").rstrip("/")
    key = (os.environ.get("SAU_STT_API_KEY") or "").strip()
    body = _json.dumps({
        "model": os.environ.get("SAU_STT_MODEL") or "local-whisper-tiny",
        "messages": [{"role": "user", "content": [
            {"type": "input_audio",
             "input_audio": {"data": base64.b64encode(wav.read_bytes()).decode(), "format": "wav"}},
            {"type": "text", "text": "language only"},
        ]}],
    }).encode()
    request = urllib.request.Request(
        f"{base}/v1/chat/completions?purpose=language", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "claude-cli/2.0.30 (external, cli)", "x-app": "cli"},
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        payload = _json.loads(response.read())
    content = str(((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    match = re.search(r"LANGUAGE=([A-Za-z-]+)", content)
    value = (match.group(1).lower() if match else "")
    return value if value in {"zh", "en"} else None


def _probe_wav(source: Path, destination: Path) -> bool:
    """Three windows spread across the video, concatenated into one WAV."""
    duration = _duration(source)
    if duration <= 0:
        return False
    parts = []
    for fraction in PROBE_WINDOWS:
        start = max(0.0, duration * fraction - PROBE_SECONDS / 2)
        if duration < PROBE_SECONDS * len(PROBE_WINDOWS):
            start = 0.0
        parts.append(f"[0:a]atrim=start={start:.2f}:duration={PROBE_SECONDS},asetpts=PTS-STARTPTS[a{len(parts)}]")
    if duration < PROBE_SECONDS * len(PROBE_WINDOWS):
        parts = [f"[0:a]atrim=start=0:duration={PROBE_SECONDS},asetpts=PTS-STARTPTS[a0]"]
    labels = "".join(f"[a{i}]" for i in range(len(parts)))
    filtergraph = ";".join(parts) + f";{labels}concat=n={len(parts)}:v=0:a=1[out]"
    try:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(source), "-filter_complex", filtergraph,
             "-map", "[out]", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(destination)],
            capture_output=True, text=True, timeout=600, check=True,
        )
        return destination.is_file() and destination.stat().st_size > 44
    except Exception as exc:  # noqa: BLE001
        print(f"    ffmpeg probe failed: {str(exc)[:120]}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--redo", action="store_true", help="re-probe even when a strong language is stored")
    ap.add_argument("--cancel-violations", action="store_true",
                    help="after recording, cancel the violating pending targets")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-drive", action="store_true",
                    help="skip the slow Drive fetch; only probe local files and public URLs")
    ap.add_argument("--zh-accounts-only", action="store_true",
                    help="only campaigns whose pending targets include a Chinese-audience account "
                         "(where an English video is the operator's actual complaint)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    candidates = _candidates(conn)
    if args.zh_accounts_only:
        risky: set[int] = set()
        for row in conn.execute(
            """SELECT DISTINCT cp.campaign_id, a.config_json
               FROM publish_job_targets jt
               JOIN campaign_posts cp ON cp.id = CAST(substr(jt.file_ref, 15) AS INTEGER)
               JOIN accounts a ON 'account:' || a.id = jt.account_ref
               WHERE jt.status = 'pending'"""
        ):
            try:
                lang = str(json.loads(row[1] or "{}").get("audience_language") or "").lower()
            except ValueError:
                lang = ""
            if lang.startswith("zh"):
                risky.add(int(row[0]))
        candidates = [c for c in candidates if c["campaign_id"] in risky]
    if args.limit:
        candidates = candidates[: args.limit]
    todo = [c for c in candidates if args.redo or c["stored"][1] not in cl.STRONG_SOURCES]
    print(f"campaigns with a pending video target: {len(candidates)}")
    print(f"needing an audio probe: {len(todo)}  (already strong: {len(candidates) - len(todo)})")

    if args.list:
        for entry in todo[:40]:
            source = _locate_source(conn, entry["campaign_id"], entry["videos"])
            print(f"  camp {entry['campaign_id']:5d} {str(source)[-70:] if source else '(no local source)'}")
        return 0

    if not args.apply:
        print("(dry run — pass --apply to probe)")
        return 0

    from myUtils import llm_client

    probed = recorded = unreadable = unknown = 0
    started = time.time()
    for index, entry in enumerate(todo, 1):
        campaign_id = entry["campaign_id"]
        source = _locate_source(conn, campaign_id, entry["videos"])
        url = None if source is not None else _locate_url(conn, campaign_id)
        fetch_dir: Path | None = None
        if source is None and url is None and args.no_drive:
            unreadable += 1
            print(f"  [{index}/{len(todo)}] camp {campaign_id}: no URL and --no-drive, skipped")
            continue
        if source is None and url is None:
            fetch_dir = Path(tempfile.mkdtemp(prefix="sau-lang-"))
            source = _fetch_from_drive(campaign_id, fetch_dir)
        if source is None and url is None:
            unreadable += 1
            print(f"  [{index}/{len(todo)}] camp {campaign_id}: source not retrievable, skipped")
            continue
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            probe = Path(handle.name)
        try:
            probed_ok = (_probe_wav_seeking(url, probe) if url
                         else _probe_wav(source, probe))
            if not probed_ok:
                unreadable += 1
                continue
            language = _probe_language(probe)
            probed += 1
            if language is None:
                unknown += 1
            # The app and the worker write this DB continuously; a plain write
            # can lose the lock race, so retry rather than abandon the campaign.
            for attempt in range(4):
                try:
                    campaign_store.record_content_language(
                        campaign_id, language, "audio" if language else None, db_path=args.db
                    )
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc) or attempt == 3:
                        raise
                    time.sleep(2 * (attempt + 1))
            recorded += 1
            elapsed = time.time() - started
            rate = elapsed / max(1, probed)
            remaining = (len(todo) - index) * rate
            label = (source.name if source is not None else (url or "?")[-40:])
            print(f"  [{index}/{len(todo)}] camp {campaign_id:5d} -> {language or 'unknown':8s} "
                  f"({label[:40]}) ~{remaining / 60:.0f} min left")
        except Exception as exc:  # noqa: BLE001
            print(f"  [{index}/{len(todo)}] camp {campaign_id}: {type(exc).__name__}: {str(exc)[:90]}")
        finally:
            probe.unlink(missing_ok=True)
            if fetch_dir is not None:
                import shutil

                shutil.rmtree(fetch_dir, ignore_errors=True)

    print(f"\nprobed={probed} recorded={recorded} unknown={unknown} unreadable={unreadable}")

    if args.cancel_violations:
        violations = routing_audit.pending_violations(conn)
        print(f"\nrouting violations after the probe: {len(violations)}")
        for violation in violations:
            conn.execute(
                "UPDATE publish_job_targets SET status='cancelled', last_error=? WHERE id=?",
                ("cancelled: language routing violation — the spoken language of the media "
                 "does not match the account's audience_language", violation["target_id"]),
            )
        conn.commit()
        print(f"cancelled {len(violations)} target(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
