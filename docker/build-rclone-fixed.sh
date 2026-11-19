#!/bin/sh
# Build the unchanged upstream stable rclone with the fixed Go standard library.
# Only /rclone-fixed is copied into runtime; no compiler or mutable source ships.
set -eu
: "${TARGETARCH:?BuildKit TARGETARCH is required}"
case "$TARGETARCH" in
  arm64) go_sha=3392582988a273a87ab5043ee1b2c9c17a2c3c00580bf9326c47a2e0aaaeac92 ;;
  amd64) go_sha=7226736c2339719cda28fbf3a54d71aa63bcefe376e98cef00034268dab24840 ;;
  *) echo "Unsupported rclone build architecture: $TARGETARCH" >&2; exit 1 ;;
esac
source_commit=e982ff36668f5130d94b4f70409babb3d964df42
source_sha=8846fd686bca91a5208287ba673c0f044183618ec630ac112d470c241354e882
work=/tmp/rclone-fixed-build
mkdir "$work"
curl --fail --silent --show-error --location --retry 3 \
  "https://go.dev/dl/go1.27.4.linux-${TARGETARCH}.tar.gz" -o "$work/go.tar.gz"
printf '%s  %s\n' "$go_sha" "$work/go.tar.gz" | sha256sum -c -
curl --fail --silent --show-error --location --retry 3 \
  "https://codeload.github.com/rclone/rclone/tar.gz/${source_commit}" -o "$work/source.tar.gz"
printf '%s  %s\n' "$source_sha" "$work/source.tar.gz" | sha256sum -c -
tar -xzf "$work/go.tar.gz" -C "$work"
mkdir "$work/source"
tar -xzf "$work/source.tar.gz" --strip-components=1 -C "$work/source"
"$work/go/bin/go" version | grep -F "go version go1.27.4 linux/$TARGETARCH"
cd "$work/source"
sha256sum go.mod go.sum > "$work/module-locks.sha256"
CGO_ENABLED=0 GOTOOLCHAIN=local GOOS=linux GOARCH="$TARGETARCH" \
  GOCACHE="$work/cache" GOPATH="$work/modules" \
  "$work/go/bin/go" build -mod=readonly -trimpath \
  -ldflags '-s -w -X github.com/rclone/rclone/fs.Version=v1.76.0' \
  -o /rclone-fixed .
sha256sum -c "$work/module-locks.sha256"
/rclone-fixed version | grep -Fx 'rclone v1.76.0'
/rclone-fixed version | grep -Fx -- '- go/version: go1.27.4'
/rclone-fixed version | grep -Fx -- '- go/tags: none'
cd /
rm -rf "$work"
