#!/usr/bin/env python3
"""Report (and optionally cancel) language-routing violations in the queue.

Content language must match the account's audience language: Chinese content to
Chinese-audience accounts only, English to English-audience accounts (and to
Chinese ones only with ``options.includeZhForEnglish``). A misroute is invisible
in the draft — the copy is localised per account, so only the *media* is wrong.

    python scripts/audit_language_routing.py              # report
    python scripts/audit_language_routing.py --apply      # cancel the violations
    python scripts/audit_language_routing.py --apply --videos-only
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myUtils import routing_audit  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=REPO / "db" / "database.db")
    ap.add_argument("--apply", action="store_true", help="cancel the violating targets")
    ap.add_argument("--videos-only", action="store_true",
                    help="only cancel targets whose media is a video (spoken language)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    violations = routing_audit.pending_violations(conn)
    by_account = Counter((v["account_name"], v["content_language"], v["media_kind"]) for v in violations)

    print(f"pending targets: {conn.execute('select count(*) from publish_job_targets where status=?', ('pending',)).fetchone()[0]}")
    print(f"routing violations: {len(violations)}")
    for (name, language, kind), count in by_account.most_common():
        print(f"   {name:22s} receives {language} {kind:5s} content  x{count}")
    for v in violations[:10]:
        print(f"     target {v['target_id']:6d} {v['account_name']:20s} {v['schedule_at']} "
              f"({v['content_language']} {v['media_kind']}; {v['source']}: {v['title'][:34]})")

    if not args.apply or not violations:
        if violations:
            print("\n(dry run — pass --apply to cancel these targets)")
        return 0

    to_cancel = [v for v in violations if not args.videos_only or v["media_kind"] == "video"]
    for v in to_cancel:
        conn.execute(
            "UPDATE publish_job_targets SET status='cancelled', last_error=? WHERE id=?",
            (
                "cancelled: language routing violation — the content language does "
                "not match the account's audience_language",
                v["target_id"],
            ),
        )
    conn.commit()
    print(f"cancelled {len(to_cancel)} target(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
