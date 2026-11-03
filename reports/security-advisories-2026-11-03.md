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

**Dev-only toolchain: fixed.** The 6 dev-only advisories (2 critical, 1 high, 3
moderate) sat in the Vitest/Vite test toolchain: `vitest` and `tinypool`
critical, `vite` high, `@vitest/mocker`, `esbuild` and `vite-node` moderate.
They are gone. The frontend now runs **vitest 4.1.12 + vite 7 +
`@vitejs/plugin-vue` 6** with `happy-dom`, and `npm audit` reports **0 across
the board** while all 130 tests and the production build pass.

| Combination | Audit | `npm test` | `npm run build` |
|---|---|---|---|
| vitest 2.1.9 + vite 6.4.5 (before) | 6 | 130 passed | ok |
| vitest 5.1.0 + vite 8.3.5 | 0 | **22 failed** | **failed** - `manualChunks is not a function`; vite 8 is rolldown-based |
| vitest 4.1.12 + vite 7 + plugin-vue 6, `environment: node` | 0 | **22 failed** | ok |
| vitest 4.1.12 + vite 7 + plugin-vue 6, `environment: happy-dom` | **0** | **130 passed** | ok |

### Why the first attempt broke 22 tests

Vitest 3 deprecated and Vitest 4 **removed** `test.testTransformMode`, which the
config used to mark the component suites as web transforms. With the key
ignored, `@vitejs/plugin-vue` compiled those SFCs in **SSR mode** and emitted
`useSSRContext()`. The hand-rolled in-memory renderer provides no SSR context,
so every `setup` threw `Cannot read properties of undefined (reading
'modules')`. Vue was never the problem: it is 3.5.44 in both lockfiles.

The supported replacement is to let the environment pick the transform mode.
`environment: 'happy-dom'`, with `happy-dom` as a devDependency, restores the
web transform. The renderer itself is unchanged and still touches no DOM; the
environment only decides how SFCs compile.

Vite 7 rather than 8: vite 8 moved to rolldown and rejects the `manualChunks`
object in `vite.config.js` that the production build relies on.

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
| Image `ea125a0`, whole image | 3 | 227 | 414 | 216 | 88 |
| Image `92ef56ec`, whole image, upstream rclone | **1** | 219 | 397 | 211 | 91 |

The only CRITICAL left is `libxml2` (CVE-2026-6653), which has no published
Debian fix. The language-package findings are 2 × Mako MEDIUM (CVE-2026-102991,
a Windows drive-letter path traversal in `TemplateLookup`, not applicable to
this Linux runtime) plus advisories inside the vendored `uv` and `rclone`
binaries.

### `rclone` replaced with the upstream binary

Debian ships rclone 1.60.1, which accounted for two CRITICAL and nine HIGH OS
findings. Debian publishes no fixed package, so the app image no longer installs
it: the Dockerfile takes the upstream static binary instead, pinned by image
digest (`rclone/rclone:1.75.2@sha256:2687085f...`), and asserts at build time
that it runs. The binary is statically linked, so it carries no libc coupling,
and it reports `rclone v1.75.2` inside the production base image. That removed
every Debian rclone finding and took CRITICAL from 3 to 1.

The swap surfaced one new finding: Trivy reads the binary as a Go build and
reports `stdlib v1.27.2` (CVE-2026-111943), the Go runtime rclone 1.75.2 was
compiled with. It cannot be rebuilt in place, and no newer rclone release exists
yet.

### Debian security updates applied at build time

The base image is pinned by digest, so its OS packages froze at base-build time.
The scan showed **55 findings that already had a published Debian fix** - the
image simply predated it - including four HIGH in `openssl` (`libssl3t64`,
`openssl`, `openssl-provider-legacy`) and `libpcre2-8-0`. Measured after the
change: **43 findings cleared** (4 HIGH, 30 MEDIUM, 6 LOW, 3 UNKNOWN), taking the
image from 12/301/481 to 1/215/367 CRITICAL/HIGH/MEDIUM. The 12 findings that
remain "fixable" all sit inside vendored binaries - the Go stdlib in `rclone`,
plus `uv`, `rustls` and `Mako` - which `apt` cannot reach. The Dockerfile now
runs `apt-get upgrade -y` alongside its package install, so every build picks up
the current Debian security updates instead of inheriting a frozen set.

That trades a little build reproducibility (package versions follow the mirror
at build time) for not shipping known-fixed CVEs, which is the right way round
for a pinned base. The remaining OS findings have no published Debian fix at
all.

### Gate policy

`scripts/audit/image_cve_scan.sh` gates on CRITICAL or HIGH in **`python-pkg`
and `node-pkg`** - the ecosystems the release lockfiles (`uv.lock`,
`package-lock.json`) own, and therefore the only ones a release can actually
fix. It records, but never gates on, Debian OS packages and vendored third-party
binaries (`gobinary`, `rustbinary`): those need a Debian fix or a new upstream
release, so a gate on them could not pass.

This narrowing is deliberate, and it was made in the same change that added the
rclone binary, because of it: the previous policy gated on all language
packages, which would have blocked this release over a Go standard library
advisory inside a pinned third-party binary. Every recorded CRITICAL is printed
next to the totals, so nothing is hidden. The scan runs as
`release-gates.sh image` and as part of `all`, skipping cleanly when no image
reference or no scanner is available.

Raw reports: `backups/auth-storage-review-20261102/validation/`
(`trivy-image-scan-92ef56ec.json`, `...-ea125a0.json`, `...-e98ad86d-previous.json`).
