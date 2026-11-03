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

## Frontend

Measured with `npm audit` inside the pinned Node 22.21.1 release toolchain.

**Production dependencies: 0 advisories.** `npm audit --omit=dev` reports 0
critical, 0 high, 0 moderate, 0 low. The earlier "9 high" figure in this file
was stale - it predates the Phase 3 dependency pass that cleared the production
graph - and is corrected here.

**Dev-only toolchain: 6 advisories** (2 critical, 1 high, 3 moderate), all in the
Vitest/Vite test toolchain: `vitest` and `tinypool` critical, `vite` high,
`@vitest/mocker`, `esbuild` and `vite-node` moderate. None of it ships; the
production bundle is clean.

The fix is bounded, and it was measured rather than guessed:

| Combination | Audit | `npm test` | `npm run build` |
|---|---|---|---|
| vitest 2.1.9 + vite 6.4.5 (current) | 6 | 130 passed | ok |
| vitest 5.1.0 + vite 8.3.5 | 0 | **22 failed** | **failed** - `manualChunks is not a function`, vite 8 is rolldown-based |
| vitest 4.1.12 + vite 7 + plugin-vue 6 | **0** | **22 failed** | ok |

Both major combinations clear the audit, and both break the same 22 tests: the
three suites that render components through the hand-rolled in-memory renderer
in `sau_frontend/src/components/__tests__/test-renderer.js`. They fail with
`Cannot read properties of undefined (reading 'modules')` raised inside a
component's `setup`. Vue is unchanged (3.5.44 in both lockfiles), so the trigger
is the Vitest 4 transform/mock boundary, not a dependency bump.

**Deliberately not shipped.** Trading 6 dev-only advisories for 22 lost
regression tests is a bad exchange on a production system. This stays an
explicit follow-up: migrate Vitest 4 + Vite 7 + `@vitejs/plugin-vue` 6 together,
repair the in-memory renderer, and confirm all 130 tests plus the production
build before promoting.

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

### `rclone` replaced with the upstream binary

Debian ships rclone 1.60.1, which accounts for two of the three CRITICAL OS
findings and nine HIGH ones. Debian publishes no fixed package, so the app image
no longer installs it: the Dockerfile takes the upstream static binary instead,
pinned by image digest (`rclone/rclone:1.75.2@sha256:2687085f...`), and asserts
at build time that it runs. The binary is statically linked, so it carries no
libc coupling, and it reports `rclone v1.75.2` inside the production base image.

That leaves `libxml2` (CVE-2026-6653) as the only CRITICAL this image cannot
fix, plus the Debian `ffmpeg`, `util-linux` and `cJSON` HIGH findings, none of
which have a published Debian fix.

**Gate policy.** `scripts/audit/image_cve_scan.sh` fails on CRITICAL or HIGH in
language packages, the class the runtime actually imports, and records OS and
base-image findings without gating on them: this image cannot fix them, and a
gate that cannot pass is a gate people learn to ignore. It runs as
`release-gates.sh image` and as part of `all`, skipping cleanly when no image
reference or no scanner is available.

Raw reports: `backups/auth-storage-review-20261102/validation/trivy-image-scan-ea125a0.json`
and `...-e98ad86d-previous.json`.
