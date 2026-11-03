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

Post-remediation: **2 remaining**.

## Remaining — explicitly accepted, not silently ignored

Both require a **major** version bump, and both sit on a path where an
unverified change is worse than a known-and-tracked advisory.

### `pillow 11.2.1` → 12.3.0

Pillow is the image engine for media prep: downscaling, watermarking, format
detection and the platform size checks. A major bump can change decoder
behaviour, default encoders and deprecations across that whole path.

**Accepted because** verifying it properly means re-running the media-prep,
watermark and campaign-artifact suites *and* an isolated image smoke on the
built release image — work that belongs in its own reviewed increment.

**Required follow-up:** upgrade on a branch, run the full media suites plus a
release-image smoke, and only then promote.

### `fastmcp 3.4.7` → 4.1.2

Powers the optional MCP server (`sau-mcp`). A major bump can change the
tool-registration API and the transport contract.

**Accepted because** the MCP surface is opt-in, not part of the publishing
path, and a 3→4 migration needs its own tool-by-tool verification.

**Required follow-up:** migrate on a branch with `tests/test_mcp_server.py` and
`tests/test_mcp_containment.py` extended, and confirm stdio + loopback HTTP.

Neither is exposed to the public internet: the MCP server binds loopback by
default and refuses a non-loopback bind without `SAU_MCP_ALLOW_REMOTE=1`.

## Frontend (production dependencies)

`npm audit --omit=dev`: **0 critical, 9 high, 0 moderate, 0 low.**

The nine are transitive in the Vue/Element Plus render graph and were already
recorded in `reports/reliability-audit.md`. They are **not remediated here**:
the frontend lockfile is shared with the release build, and a forced upgrade of
that graph needs its own build + browser-test verification.

**Required follow-up:** upgrade the frontend graph on a branch, run
`npm test` + the production build + the browser journeys, and record the delta.

## Container / base image

The base image (`ghcr.io/willywang8216/sau-base:slim`) and the app image were
not scanned with an image CVE scanner in this pass — no scanner is configured
on this host. **This is a gap, not a clean result.**

**Required follow-up:** add an image scan (e.g. Trivy/Grype) to the release
gate so base-OS packages are covered too, not just Python and npm.
