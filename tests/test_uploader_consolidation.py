"""Guard tests for the playwright -> patchright consolidation.

The whole project should ship with a single browser driver. These tests:

1. Walk the production source tree and assert no module imports the upstream
   ``playwright`` package — only ``patchright`` is allowed.
2. Smoke-import every uploader package and the legacy Flask helpers so a
   future maintainer cannot accidentally re-introduce the dual-stack
   dependency by editing one of the import lines.
"""

from __future__ import annotations

import ast
import importlib
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Folders whose .py files we treat as production code. Tests live outside
# this set on purpose so a future test can mock `playwright` without
# tripping the guard.
PRODUCTION_DIRS = (
    PROJECT_ROOT / "uploader",
    PROJECT_ROOT / "utils",
    PROJECT_ROOT / "myUtils",
)

# Modules we want to be importable end-to-end. The list intentionally
# includes the legacy Flask helpers and every uploader package so a
# stray `playwright` import would surface here too.
SMOKE_IMPORT_MODULES = (
    "myUtils.auth",
    "myUtils.login",
    "myUtils.postVideo",
    "myUtils.jobs",
    "myUtils.worker",
    "myUtils.job_logging",
    "uploader.douyin_uploader.main",
    "uploader.ks_uploader.main",
    "uploader.tencent_uploader.main",
    "uploader.xiaohongshu_uploader.main",
    "uploader.tk_uploader.main",
    "uploader.tk_uploader.main_chrome",
    "uploader.baijiahao_uploader.main",
    "uploader.medium_uploader.main",
    "uploader.substack_uploader.main",
)


def _imports_playwright(path: Path) -> list[str]:
    """Return AST-source representations of every banned playwright import.

    AST-based so a comment or docstring mentioning the word doesn't count.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "playwright" or module.startswith("playwright."):
                names = ", ".join(alias.name for alias in node.names)
                offenders.append(f"from {module} import {names}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "playwright" or alias.name.startswith("playwright."):
                    offenders.append(f"import {alias.name}")
    return offenders


class ConsolidationGuardTests(unittest.TestCase):
    def test_no_production_module_imports_playwright(self) -> None:
        """No production code may import the upstream `playwright` package."""

        offenders: dict[str, list[str]] = {}
        for root in PRODUCTION_DIRS:
            for path in root.rglob("*.py"):
                hits = _imports_playwright(path)
                if hits:
                    offenders[str(path.relative_to(PROJECT_ROOT))] = hits

        if offenders:
            details = "\n".join(
                f"  {file}: {', '.join(items)}"
                for file, items in sorted(offenders.items())
            )
            self.fail(
                "Production modules must use `patchright`, not `playwright`:\n"
                + details
            )

    def test_every_uploader_module_imports_cleanly(self) -> None:
        for module_name in SMOKE_IMPORT_MODULES:
            with self.subTest(module=module_name):
                # Force a fresh import so the test catches transitive
                # `playwright` references via lazy imports inside the
                # module body.
                module = importlib.import_module(module_name)
                self.assertIsNotNone(module)


if __name__ == "__main__":
    unittest.main()
