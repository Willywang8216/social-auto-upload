"""Exercise the exact maintained scan policy without Docker or network access."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit" / "image_cve_scan.sh"


def _run_policy(tmp_path: Path, report: object) -> subprocess.CompletedProcess[str]:
    # Execute the actual embedded policy, not a duplicate test implementation.
    match = re.search(r"<<'PY'\n(.*?)\nPY\s*\Z", SCRIPT.read_text(encoding="utf-8"), re.S)
    assert match is not None, "maintained image scan policy block is missing"
    path = tmp_path / "scan.json"
    original = json.dumps(report)
    path.write_text(original, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-I", "-c", match.group(1), str(path)],
        capture_output=True, text=True, timeout=20,
    )
    assert path.read_text(encoding="utf-8") == original, "policy must not rewrite scan evidence"
    return result


def _report(kind: str, severity: str, *, fixed: str | None = "2.0") -> dict:
    return {"Results": [{
        "Type": kind,
        "Target": "synthetic-runtime-component",
        "Vulnerabilities": [{
            "Severity": severity, "PkgName": "synthetic-package",
            "InstalledVersion": "1.0", "FixedVersion": fixed,
            "VulnerabilityID": "CVE-2099-12345",
        }],
    }]}


@pytest.mark.parametrize("kind", ["python-pkg", "node-pkg", "gobinary"])
@pytest.mark.parametrize("severity", ["HIGH", "CRITICAL"])
def test_owned_high_critical_findings_block_release(tmp_path, kind, severity):
    result = _run_policy(tmp_path, _report(kind, severity))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "BLOCKING" in result.stdout and "CVE-2099-12345" in result.stdout


@pytest.mark.parametrize("kind", ["python-pkg", "node-pkg", "gobinary"])
@pytest.mark.parametrize("severity", ["LOW", "MEDIUM", "UNKNOWN"])
def test_lower_severity_does_not_silently_raise_the_gate(tmp_path, kind, severity):
    result = _run_policy(tmp_path, _report(kind, severity))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "BLOCKING" not in result.stdout
    assert severity in result.stdout


def test_unfixed_go_findings_are_not_ignored(tmp_path):
    result = _run_policy(tmp_path, _report("gobinary", "HIGH", fixed=None))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "BLOCKING" in result.stdout


@pytest.mark.parametrize("kind", ["debian", "rustbinary"])
@pytest.mark.parametrize("severity", ["HIGH", "CRITICAL"])
def test_other_ecosystem_findings_remain_recorded(tmp_path, kind, severity):
    result = _run_policy(tmp_path, _report(kind, severity, fixed=None))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "recorded CRITICAL/HIGH not gated: 1" in result.stdout
    if severity == "CRITICAL":
        assert "RECORDED CRITICAL" in result.stdout
        assert "CVE-2099-12345" in result.stdout


def test_valid_scanned_component_without_findings_passes(tmp_path):
    result = _run_policy(tmp_path, {"Results": [{"Type": "gobinary", "Vulnerabilities": None}]})
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("report", [{}, {"Results": []}, {"Results": None}, []])
def test_absent_scan_results_cannot_count_as_a_pass(tmp_path, report):
    result = _run_policy(tmp_path, report)
    assert result.returncode != 0, result.stdout + result.stderr
