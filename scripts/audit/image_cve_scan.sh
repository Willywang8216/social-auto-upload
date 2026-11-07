#!/usr/bin/env bash
# Image CVE evidence and policy gate for the production image.
#
# `trivy image` extracts every layer into the scanner container's writable
# layer, which needs roughly 4 GB of scratch space and failed repeatedly on the
# release host while it ran near capacity. Flattening the image into a single
# export first needs about 1.5 GB and scans the same package set.
#
# Policy: fail on CRITICAL or HIGH findings in Python, Node and Go packages.
# The release owns Python/Node locks and the pinned rclone binary. Debian and
# other vendor findings remain visible evidence; absence of a supported package
# fix is not a claim that the image is vulnerability-free.
#
# Usage: image_cve_scan.sh <image-ref> [report.json]
#   SAU_SCAN_CACHE  trivy database cache directory
#   SAU_SCAN_WORK   scratch directory (default: a fresh mktemp -d)
#   SAU_SCAN_KEEP=1 keep the flattened rootfs for inspection
set -euo pipefail

IMAGE="${1:-${SAU_SCAN_IMAGE:-}}"
OUT="${2:-${SAU_SCAN_OUT:-image-cve-scan.json}}"

skip() { printf 'image-scan: %s\n' "$1" >&2; exit 0; }

[[ -n "$IMAGE" ]] || skip 'no image reference supplied; skipping.'
command -v docker >/dev/null 2>&1 || skip 'docker is unavailable; skipping.'
docker image inspect "$IMAGE" >/dev/null 2>&1 || skip "image '$IMAGE' is not present locally; skipping."

if command -v trivy >/dev/null 2>&1; then
  TRIVY_MODE=local
elif docker image inspect aquasec/trivy >/dev/null 2>&1; then
  TRIVY_MODE=docker
else
  skip 'trivy is unavailable; skipping.'
fi

if [[ -n "${SAU_SCAN_WORK:-}" ]]; then
  WORK="$SAU_SCAN_WORK"
  CREATED_WORK=0
else
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/sau-image-scan.XXXXXX")"
  CREATED_WORK=1
fi
CACHE="${SAU_SCAN_CACHE:-$WORK/cache}"
CONTAINER="sau-image-scan-$$"
mkdir -p "$WORK/rootfs" "$CACHE"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  if [[ "$CREATED_WORK" == 1 && "${SAU_SCAN_KEEP:-0}" != 1 ]]; then
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

# Flatten the image into one directory. A created (never started) container has
# an empty writable layer, so the export is exactly the image filesystem.
docker create --name "$CONTAINER" "$IMAGE" >/dev/null
docker export "$CONTAINER" | tar -xf - -C "$WORK/rootfs" --no-same-owner

if [[ "$TRIVY_MODE" == local ]]; then
  TRIVY_TARGET="$WORK/rootfs"
  run_trivy() { trivy "$@"; }
else
  TRIVY_TARGET=/rootfs
  run_trivy() {
    docker run --rm \
      -v "$WORK/rootfs:/rootfs:ro" \
      -v "$CACHE:/root/.cache/trivy" \
      aquasec/trivy "$@"
  }
fi

mkdir -p "$(dirname "$OUT")"
run_trivy rootfs --scanners vuln --no-progress --format json "$TRIVY_TARGET" >"$OUT"
printf 'image-scan: wrote %s\n' "$OUT" >&2

"${SAU_SCAN_PYTHON:-python3}" - "$OUT" <<'PY'
import collections
import json
import sys

report = json.load(open(sys.argv[1], encoding="utf-8"))
if not isinstance(report, dict) or not isinstance(report.get("Results"), list) or not report["Results"]:
    raise SystemExit("image-scan: missing scan results; no completed scan was verified")
if any(not isinstance(result, dict) or not result.get("Type") for result in report["Results"]):
    raise SystemExit("image-scan: malformed scan result; refusing a false pass")
severities = collections.Counter()
blocking = []
recorded = []
# The pinned upstream rclone can be upgraded to a verified fixed release.
# Do not silently allow its Go vulnerabilities or ignore unfixed gated findings.
GATED_TYPES = ("python-pkg", "node-pkg", "gobinary")
for result in report["Results"]:
    gated = result.get("Type") in GATED_TYPES
    for finding in result.get("Vulnerabilities") or []:
        severity = finding.get("Severity", "UNKNOWN")
        severities[severity] += 1
        if severity not in ("CRITICAL", "HIGH"):
            continue
        row = (
            severity,
            result.get("Type"),
            result.get("Target"),
            finding.get("PkgName"),
            finding.get("InstalledVersion"),
            finding.get("FixedVersion") or "-",
            finding.get("VulnerabilityID"),
        )
        (blocking if gated else recorded).append(row)

print("image-scan: severity totals:", dict(sorted(severities.items())))
print("image-scan: gated ecosystems: python-pkg, node-pkg and gobinary.")
print("image-scan: recorded, not gated: Debian OS and other vendor components;")
print("image-scan: those findings still require supported upstream remedies.")
print("image-scan: recorded CRITICAL/HIGH not gated:", len(recorded))
for row in sorted(recorded):
    if row[0] == "CRITICAL":
        print("image-scan: RECORDED CRITICAL", " | ".join(str(value) for value in row))
for row in sorted(blocking):
    print("image-scan: BLOCKING", " | ".join(str(value) for value in row))
sys.exit(1 if blocking else 0)
PY
