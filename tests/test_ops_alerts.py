"""Tests for myUtils.ops_alerts.public_app_origin.

The resolver decides where operator-facing links point. It exists because the
digest and the worker both used to read only SAU_PUBLIC_APP_URL while the app
stores its origin in SAU_PUBLIC_BASE_URL, so every link in both channels was
silently omitted on a box that had the second variable set and not the first.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from myUtils import ops_alerts


class PublicAppOriginTests(unittest.TestCase):
    def setUp(self) -> None:
        # Never let the host's own configuration decide the outcome.
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("SAU_PUBLIC_APP_URL", None)
        os.environ.pop("SAU_PUBLIC_BASE_URL", None)
        self.addCleanup(self._env.stop)

    def test_unconfigured_yields_empty_so_callers_omit_the_link(self):
        self.assertEqual(ops_alerts.public_app_origin(), "")

    def test_falls_back_to_the_apps_own_public_base_url(self):
        with patch.dict(os.environ, {"SAU_PUBLIC_BASE_URL": "https://socialupload.example.com"}):
            self.assertEqual(
                ops_alerts.public_app_origin(), "https://socialupload.example.com"
            )

    def test_app_url_wins_when_both_are_set(self):
        with patch.dict(
            os.environ,
            {
                "SAU_PUBLIC_APP_URL": "https://app.example.net",
                "SAU_PUBLIC_BASE_URL": "https://socialupload.example.com",
            },
        ):
            self.assertEqual(ops_alerts.public_app_origin(), "https://app.example.net")

    def test_explicit_argument_wins_over_the_environment(self):
        with patch.dict(os.environ, {"SAU_PUBLIC_BASE_URL": "https://socialupload.example.com"}):
            self.assertEqual(
                ops_alerts.public_app_origin("https://explicit.example.org/"),
                "https://explicit.example.org",
            )

    def test_explicit_empty_string_forces_no_link(self):
        # The digest CLI passes --app-url through, and an explicit "" must mean
        # "no link" rather than falling back to a configured default.
        with patch.dict(os.environ, {"SAU_PUBLIC_BASE_URL": "https://socialupload.example.com"}):
            self.assertEqual(ops_alerts.public_app_origin(""), "")

    def test_trailing_slashes_are_stripped(self):
        with patch.dict(os.environ, {"SAU_PUBLIC_BASE_URL": "https://socialupload.example.com///"}):
            self.assertEqual(
                ops_alerts.public_app_origin(), "https://socialupload.example.com"
            )

    def test_whitespace_only_value_is_treated_as_unset(self):
        with patch.dict(os.environ, {"SAU_PUBLIC_APP_URL": "   ", "SAU_PUBLIC_BASE_URL": ""}):
            self.assertEqual(ops_alerts.public_app_origin(), "")


if __name__ == "__main__":
    unittest.main()
