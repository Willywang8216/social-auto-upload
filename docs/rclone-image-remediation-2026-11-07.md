# Rclone image remediation evidence — 2026-11-07

## Recorded findings and verified remedy

The exact previous candidate image contained rclone 1.75.2, compiled with Go 1.27.2 and `golang.org/x/net` v0.60.0. Its Trivy report recorded two HIGH Go findings:

| Finding | Affected bundled version | Official fixed version |
|---|---|---|
| [CVE-2026-78661 / GO-2026-6860](https://pkg.go.dev/vuln/GO-2026-6860) | `golang.org/x/net` v0.60.0 | v0.62.0 |
| [CVE-2026-111943 / GO-2026-6809](https://pkg.go.dev/vuln/GO-2026-6809) | Go 1.27.2 | Go 1.27.3 (or 1.26.10 on that branch) |

Fresh Exa run `agent_run_95edbcb4f5dd4cf9b1d54bf04ad33316` found the remedy. A separate full official-page retrieval confirmed the [rclone changelog](https://rclone.org/changelog/): stable **v1.76.0, released 2026-11-06**, explicitly upgrades Go to **1.27.3** and `golang.org/x/net` to **v0.62.0**. This is fix evidence, not an assumption based on version ordering.

Registry inspection of official `rclone/rclone:1.76.0` returned:

- OCI multi-platform index: `sha256:7cbeec24a44f206067597c0368be4de90ae19a64d5113c7d1938bd737a81e466`.
- Linux ARM64 manifest: `sha256:fbcca19e4dc192f6a12169a7e033e713877e07abca796928bbed27d2a334c8e0`.
- Linux AMD64 manifest: `sha256:075cf0e4e0f623996646cfb8a434c71cb4fcfe75bbbe27ccbb296ca04a7cd260`.

The pinned image was pulled, and its binary reported **rclone v1.76.0 / Go 1.27.3 / static / linux arm64** in a network-disabled, read-only, capability-dropped container. The application Dockerfile now copies from the immutable multi-platform index so both architectures retain a fixed source.

The image policy now gates HIGH/CRITICAL **Go** findings as well as Python and Node; it does not use `--ignore-unfixed`, suppress CVEs, or alter reports. Missing/empty scan results fail rather than become a false clean pass. Tests execute the maintained policy block itself. The new candidate still requires its own complete release gates and actual image scan; this source evidence is not that scan result.

## Debian findings are not silently claimed fixed

The earlier full scan also recorded **230 HIGH/CRITICAL Debian package findings**. None contained a fixed package version; statuses were 75 affected, 154 fix-deferred, and one will-not-fix. A package may appear repeatedly because shared source advisories affect multiple installed binary packages.

The [official Debian tracker for CVE-2026-6653](https://security-tracker.debian.org/tracker/CVE-2026-6653) marks installed libxml2 `2.12.7+dfsg+really2.9.14-2.1+deb13u3` as vulnerable and records `[trixie] <no-dsa> (Minor issue)`. The upstream fix exists, but the tracker does not provide a supported fixed Debian 13 package. The scanner's CRITICAL label and Debian's differing assessment are both preserved. An unstable-distribution library is not substituted into the pinned runtime, and the CVE is not hidden.

The Dockerfile continues to install available Debian security updates. The remaining OS findings require supported vendor fixes or a separately verified runtime migration; they are not solved by changing a scan threshold. Deployment reports must distinguish the exact newly scanned result from this older inventory and must not call the image vulnerability-free.
