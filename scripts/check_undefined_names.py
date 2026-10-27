#!/usr/bin/env python3
"""Fail on names that are used but never defined — a dependency-free pyflakes.

Why this exists: a single missing import (`timedelta` used but only `datetime`
and `timezone` imported in the inbox watcher) crashed the scan loop every minute
for days and silently stopped new uploads from being queued. The project ships no
linter, and `python -m compileall` (which the image build runs) only catches
syntax errors, not undefined names.

Uses the stdlib `symtable` module, so it needs no third-party packages and can run
inside the image, in CI, or from cron.

    python scripts/check_undefined_names.py FILE_OR_DIR [more...]
    python scripts/check_undefined_names.py myUtils sau_backend.py
"""

from __future__ import annotations

import builtins
import symtable
import sys
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "generated", "sau_frontend"}
BUILTINS = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__builtins__", "__spec__",
}


def _is_bound(sym: symtable.Symbol) -> bool:
    return bool(
        sym.is_assigned()
        or sym.is_imported()
        or sym.is_parameter()
        or sym.is_namespace()
        or getattr(sym, "is_annotated", lambda: False)()
    )


def check_file(path: Path) -> list[tuple[str, int, str]]:
    """Undefined names in one file: (path, lineno, name)."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        table = symtable.symtable(source, str(path), "exec")
    except SyntaxError as exc:
        return [(str(path), exc.lineno or 0, f"SyntaxError: {exc.msg}")]

    # Names that exist at module scope: every global read in a nested scope is
    # resolved there, so anything else is a genuine miss.
    module_names = {s.get_name() for s in table.get_symbols() if _is_bound(s)} | BUILTINS

    findings: list[tuple[str, int, str]] = []

    def visit(scope: symtable.SymbolTable, is_module: bool) -> None:
        local_bound = {s.get_name() for s in scope.get_symbols() if _is_bound(s)} | BUILTINS
        for sym in scope.get_symbols():
            name = sym.get_name()
            if not sym.is_referenced() or _is_bound(sym):
                continue
            if name in local_bound:
                continue
            if sym.is_free() or sym.is_nonlocal():
                continue  # resolved by an enclosing function scope
            if not is_module and sym.is_global() and name in module_names:
                continue  # a real module-level name
            if name in module_names and is_module:
                continue
            findings.append((str(path), scope.get_lineno(), name))
        for child in scope.get_children():
            visit(child, False)

    visit(table, True)
    return findings


def iter_targets(paths: list[str]):
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            for candidate in sorted(path.rglob("*.py")):
                if any(part in SKIP_DIRS for part in candidate.parts):
                    continue
                yield candidate
        elif path.suffix == ".py":
            yield path


def main(argv: list[str]) -> int:
    targets = list(iter_targets(argv or ["."]))
    problems = 0
    for path in targets:
        for file_path, lineno, name in check_file(path):
            print(f"{file_path}:{lineno}: undefined name '{name}'")
            problems += 1
    if problems:
        print(f"\n{problems} undefined name(s) across {len(targets)} file(s)")
        return 1
    print(f"ok: {len(targets)} file(s), no undefined names")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
