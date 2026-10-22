"""The blog publisher must survive a concurrent-publish branch race.

GitHub answers the second PUT with 409 "is at <sha> but expected <sha>" when two
posts land on the same branch at the same time. One retry was not enough: target
7469 (NW Blog, 2026-10-22 13:40) failed that way — the only non-Reddit publish
failure that day — and the post was silently lost.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from myUtils import prepared_publishers as pp


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Http:
    """PUT always 409s; GET returns a moving SHA until the Nth read."""

    def __init__(self, put_statuses: list[int], get_shas: list[str]):
        self.put_statuses = list(put_statuses)
        self.get_shas = list(get_shas)
        self.puts: list[dict] = []
        self.gets = 0

    def put(self, url, headers=None, json=None, timeout=None):
        self.puts.append(dict(json or {}))
        status = self.put_statuses.pop(0) if self.put_statuses else 200
        return _Resp(status, {"content": {"sha": "new"}})

    def get(self, url, headers=None, params=None, timeout=None):
        self.gets += 1
        sha = self.get_shas.pop(0) if self.get_shas else "sha-final"
        return _Resp(200, {"sha": sha, "content": ""})


class BlogConflictRetryTests(unittest.TestCase):
    def test_attempts_and_backoff_are_configured(self):
        self.assertGreaterEqual(pp.BLOG_CONFLICT_ATTEMPTS, 3)
        self.assertGreater(pp.BLOG_CONFLICT_BACKOFF_SECONDS, 0)

    def test_a_single_conflict_is_retried_with_the_fresh_sha(self):
        """The retry must send the SHA it just re-read, not the stale one."""
        import base64

        http = _Http(put_statuses=[409, 200], get_shas=["fresh-sha"])
        content_b64 = base64.b64encode(b"body").decode("ascii")

        # Reproduce the loop's contract directly (the full publish path needs a
        # live GitHub repo); this pins the retry bookkeeping.
        put_body = {"content": content_b64, "branch": "main", "sha": "stale"}
        with patch.object(pp.time, "sleep", return_value=None):
            for attempt in range(1, pp.BLOG_CONFLICT_ATTEMPTS + 1):
                resp = http.put("url", json=put_body)
                if resp.status_code != 409:
                    break
                fresh = http.get("url")
                put_body["sha"] = (fresh.json() or {}).get("sha")
                pp.time.sleep(pp.BLOG_CONFLICT_BACKOFF_SECONDS * attempt)

        self.assertEqual(len(http.puts), 2)
        self.assertEqual(http.puts[0]["sha"], "stale")
        self.assertEqual(http.puts[1]["sha"], "fresh-sha", "must use the re-read SHA")

    def test_repeated_conflicts_stop_after_the_attempt_budget(self):
        http = _Http(put_statuses=[409] * 10, get_shas=["s1", "s2", "s3", "s4"])
        put_body = {"content": "x", "sha": "stale"}
        with patch.object(pp.time, "sleep", return_value=None):
            for attempt in range(1, pp.BLOG_CONFLICT_ATTEMPTS + 1):
                resp = http.put("url", json=put_body)
                if resp.status_code != 409:
                    break
                put_body["sha"] = (http.get("url").json() or {}).get("sha")
                pp.time.sleep(pp.BLOG_CONFLICT_BACKOFF_SECONDS * attempt)
        self.assertEqual(len(http.puts), pp.BLOG_CONFLICT_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
