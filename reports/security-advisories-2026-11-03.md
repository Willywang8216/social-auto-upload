# Dependency advisory scan — 2026-11-03

Method: `pip-audit` against the **exported locked runtime graph**
(`uv export --frozen --no-dev --extra web`), so the audited set is exactly what
the production image installs — not the developer environment. The frontend was
audited separately with `npm audit --omit=dev`.

## Python runtime — before

108 packages; **15 with advisories**:

`authlib`, `certifi`, `cryptography`, `dnspython`, `fastmcp`, `flask`, `idna`,
`joserfc`, `opentelemetry-api`, `pillow`, `pyasn1`, `pyjwt`, `requests`,
`urllib3`, `werkzeug`.

## Python runtime — remediated

Upgraded in `pyproject.toml` / `requirements.txt` / `uv.lock`, then the affected
suites re-run (**545 passed**):

| Package | From | To |
|---|---|---|
| Flask | 3.1.1 | 3.1.3 |
| Werkzeug | 3.1.6 | 3.1.9 |
| requests | 2.32.3 | 2.33.0 |
| urllib3 | 2.6.3 | 2.8.0 |
| cryptography | 47.0.0 | 50.0.2 |
| authlib | 1.7.2 | 1.9.2 |
| certifi | 2026.2.25 | 2026.10.23 |
| idna | 3.11 | 3.20 |
| pyjwt | 2.14.0 | 2.15.1 |
| pyasn1 | 0.6.4 | 0.6.5 |
| dnspython | 2.8.0 | 2.9.1 |
| joserfc | 1.7.3 | 1.7.7 |
| opentelemetry-api | 1.44.0 | 1.45.2 |

Post-remediation: **2 remaining**, both delivered later the same day (below).

## Deferred majors, then delivered

Both were deferred in this pass because they are major bumps, then completed in
the follow-up pass once the full suites could be re-run:

| Package | From | To | Verification |
|---|---|---|---|
| `pillow` | 11.2.1 | **13.0.0** | full media-prep, watermark and campaign-artifact suites, plus an isolated non-root image smoke |
| `fastmcp` | 3.4.7 | **4.1.2** | MCP server and containment suites pass unchanged; no tool-registration API migration was needed |

With those two, the exported locked runtime graph carries **0 advisories**.

## Frontend (production dependencies)

`npm audit --omit=dev`: **0 critical, 9 high, 0 moderate, 0 low.**

The nine are transitive in the Vue/Element Plus render graph and were already
recorded in `reports/reliability-audit.md`. They are **not remediated here**:
`npm audit fix --force` broke 22 frontend tests, so a forced upgrade of that
graph needs its own build + browser-test verification.

**Required follow-up:** upgrade the frontend graph on a branch, run `npm test` +
the production build + the browser journeys, and record the delta.

## Container image

Trivy 0.76.1 against the running production image, driven by
`scripts/audit/image_cve_scan.sh`. The script flattens the image into a single
export and scans that as a rootfs, because Trivy's own layer-by-layer export
needs roughly 4 GB of scratch space and failed repeatedly on this host's full
disk.

**The first scan found a real defect.** The app image carried two Python
environments: the application venv at `/opt/venv`, and the base image's own
site-packages at `/usr/local/lib/python3.12/site-packages` holding **1,295 stale
distributions** - old Pillow 11.2.1, Authlib 1.7.2, aiohttp 3.9.5, cryptography
46.0.0 among them. The venv could not import any of it
(`include-system-site-packages = false`, and `sys.path` confirms), so none of it
was reachable, but every scanner reported it as if it were live.

| Scan | CRITICAL | HIGH | MEDIUM | LOW | UNKNOWN |
|---|---:|---:|---:|---:|---:|
| Previous image `e98ad86d`, whole image | 12 | 301 | 481 | 235 | 88 |
| Previous image, `/opt/venv` only | **0** | **0** | 1 | 0 | 0 |
| Current image `ea125a0`, whole image | 3 | 227 | 414 | 216 | 88 |

The current image's language-package findings are 2 × Mako MEDIUM
(CVE-2026-102991, a Windows drive-letter path traversal in `TemplateLookup`,
not applicable to this Linux runtime) and 5 advisories against the bundled `uv`
build tool. The remaining 941 findings are Debian 13.7 OS packages, including
3 CRITICAL: `libxml2` (CVE-2026-6653) and `rclone` twice (CVE-2026-41176,
CVE-2026-41179). Debian publishes no fixed version for those yet.

**Gate policy.** `scripts/audit/image_cve_scan.sh` fails on CRITICAL or HIGH in
language packages, the class the runtime actually imports, and records OS and
base-image findings without gating on them: this image cannot fix them, and a
gate that cannot pass is a gate people learn to ignore. It runs as
`release-gates.sh image` and as part of `all`, skipping cleanly when no image
reference or no scanner is available.

Raw reports: `backups/auth-storage-review-20261102/validation/trivy-image-scan-ea125a0.json`
and `...-e98ad86d-previous.json`.
