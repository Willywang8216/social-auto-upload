"""Tests for the lightweight repo-local .env loader."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from myUtils import env_loader


class EnvLoaderTests(unittest.TestCase):
    def test_load_repo_env_reads_values_without_overwriting_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / '.env'
            env_path.write_text('FOO=bar\nEXISTING=from-file\n')
            with patch.dict(os.environ, {'EXISTING': 'from-env'}, clear=False):
                env_loader.load_repo_env(env_path=env_path)
                self.assertEqual(os.environ['FOO'], 'bar')
                self.assertEqual(os.environ['EXISTING'], 'from-env')

    def test_load_repo_env_can_override_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / '.env'
            env_path.write_text('EXISTING=from-file\n')
            with patch.dict(os.environ, {'EXISTING': 'from-env'}, clear=False):
                env_loader.load_repo_env(env_path=env_path, override=True)
                self.assertEqual(os.environ['EXISTING'], 'from-file')

    def test_parse_env_line_handles_export_and_quotes(self) -> None:
        self.assertEqual(
            env_loader._parse_env_line('export NAME="value here"'),
            ('NAME', 'value here'),
        )
        self.assertIsNone(env_loader._parse_env_line('# comment'))


if __name__ == '__main__':
    unittest.main()
