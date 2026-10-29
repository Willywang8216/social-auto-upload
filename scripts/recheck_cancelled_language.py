#!/usr/bin/env python3
"""Re-check the targets cancelled by the language sweep, and restore the mistakes.

The first sweep had to fall back to filename/brief evidence for campaigns whose
media was offloaded, and that evidence is imperfect:

  * ``action_20260722131814150_sys_part1_pub.mp4`` was read as English because the
    name contains the word "action" — it is an internal naming pattern, not prose;
  * ``SFW 20260830112542557 — natural, honest, everyday naturist life.`` is an
    English *brief* on a video that may well be Chinese.

Cancelling is cheap and reversible; publishing the wrong language to the wrong
audience is not. So each affected campaign is now probed properly — the audio is
fetched from whichever Drive tier holds it and listened to — and any target whose
account accepts the real language is put back.

    python recheck_cancelled_language.py            # report
    python recheck_cancelled_language.py --apply
"""
from __future__ import annotations

import argparse
import importlib.util
import sqlite3
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


def _backfill():
    spec = importlib.util.spec_from_file_location(
        "backfill_audio_language", REPO / "scripts" / "backfill_audio_language.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _affected(conn: sqlite3.Connection) -> dict[int, list[int]]:
    """campaign_id -> cancelled target ids (only the language-routing ones)."""
    out: dict[int, list[int]] = {}
    for row in conn.execute(
        """
        SELECT cp.campaign_id, jt.id
        FROM publish_job_targets jt
        JOIN campaign_posts cp ON cp.id = CAST(substr(jt.file_ref, 15) AS INTEGER)
        WHERE jt.status = 'cancelled' AND jt.last_error LIKE 'cancelled: language routing%'
        """
    ):
        out.setdefault(int(row[0]), []).append(int(row[1]))
    return out


def _accepts(conn: sqlite3.Connection, target_id: int, language: str) -> bool:
    row = conn.execute(
        """
        SELECT json_extract(a.config_json, '$.audience_language') AS lang
        FROM publish_job_targets jt JOIN accounts a ON 'account:' || a.id = jt.account_ref
        WHERE jt.id = ?
        """,
        (target_id,),
    ).fetchone()
    tokens = {token for token in str(row[0] or "").lower().replace(",", " ").replace("+", " ").split() if token}
    if not tokens:
        return True  # unclassified account: never filtered
    return any(token.startswith(language) for token in tokens)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    backfill = _backfill()
    affected = _affected(conn)
    items = sorted(affected.items())
    if args.limit:
        items = items[: args.limit]
    print(f"campaigns with cancelled routing targets: {len(affected)}")

    from myUtils import llm_client

    restored = kept = unknown = unreadable = 0
    started = time.time()
    for index, (campaign_id, target_ids) in enumerate(items, 1):
        row = conn.execute(
            "SELECT metadata_json FROM campaigns WHERE id=?", (campaign_id,)
        ).fetchone()
        try:
            import json

            metadata = json.loads((row["metadata_json"] if row else "") or "{}")
        except ValueError:
            metadata = {}
        stored_language, stored_source = cl.from_metadata(metadata)
        if stored_source not in cl.STRONG_SOURCES:
            filenames = [
                r[0] for r in conn.execute(
                    """SELECT fr.filename FROM media_group_items mgi
                       JOIN file_records fr ON fr.id = mgi.file_record_id
                       JOIN campaigns c ON c.media_group_id = mgi.media_group_id
                       WHERE c.id = ?""",
                    (campaign_id,),
                )
            ]
            source = backfill._locate_source(conn, campaign_id, filenames)
            fetch_dir = None
            if source is None:
                fetch_dir = Path(tempfile.mkdtemp(prefix="sau-recheck-"))
                source = backfill._fetch_from_drive(campaign_id, fetch_dir)
            if source is None:
                unreadable += 1
                print(f"  [{index}/{len(items)}] camp {campaign_id}: media not retrievable")
                if fetch_dir:
                    import shutil
                    shutil.rmtree(fetch_dir, ignore_errors=True)
                continue
            probe = Path(tempfile.mkstemp(suffix=".wav")[1])
            try:
                if backfill._probe_wav(source, probe):
                    result = llm_client.transcribe_audio_via_chat(probe)
                    stored_language = result.language if result.language in {"zh", "en"} else None
                    campaign_store.record_content_language(
                        campaign_id, stored_language, "audio" if stored_language else None,
                        db_path=args.db,
                    )
                else:
                    unreadable += 1
                    continue
            except Exception as exc:  # noqa: BLE001
                unreadable += 1
                print(f"  [{index}/{len(items)}] camp {campaign_id}: probe failed: {str(exc)[:70]}")
                continue
            finally:
                probe.unlink(missing_ok=True)
                if fetch_dir:
                    import shutil
                    shutil.rmtree(fetch_dir, ignore_errors=True)

        if stored_language is None:
            unknown += 1
            print(f"  [{index}/{len(items)}] camp {campaign_id}: language unknown, leaving cancelled")
            continue
        keep = [tid for tid in target_ids if not _accepts(conn, tid, stored_language)]
        back = [tid for tid in target_ids if _accepts(conn, tid, stored_language)]
        if back:
            restored += len(back)
            if args.apply:
                conn.executemany(
                    "UPDATE publish_job_targets SET status='pending', attempts=0, "
                    "last_error=NULL, finished_at=NULL, started_at=NULL WHERE id=?",
                    [(tid,) for tid in back],
                )
                conn.commit()
        kept += len(keep)
        elapsed = time.time() - started
        print(f"  [{index}/{len(items)}] camp {campaign_id:5d} lang={stored_language:8s} "
              f"restore={len(back)} keep-cancelled={len(keep)} (~{(len(items)-index)*elapsed/max(1,index)/60:.0f} min left)")

    print(f"\nrestored={restored} still-cancelled={kept} unknown={unknown} unreadable={unreadable}")
    if not args.apply:
        print("(dry run — pass --apply to restore)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
