"""Release build contract for the verified stable-rclone/fixed-Go CVE remedy."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "docker" / "build-rclone-fixed.sh"


def test_fixed_rclone_inputs_are_immutable_and_patch_level_checked():
    script = SCRIPT.read_text(encoding="utf-8")
    for value in (
        "e982ff36668f5130d94b4f70409babb3d964df42",
        "8846fd686bca91a5208287ba673c0f044183618ec630ac112d470c241354e882",
        "3392582988a273a87ab5043ee1b2c9c17a2c3c00580bf9326c47a2e0aaaeac92",
        "7226736c2339719cda28fbf3a54d71aa63bcefe376e98cef00034268dab24840",
    ):
        assert value in script
    assert "go1.27.4.linux-${TARGETARCH}.tar.gz" in script
    assert "- go/version: go1.27.4" in script
    assert "rclone v1.76.0" in script
    assert "sha256sum -c -" in script
    assert "-mod=readonly" in script
    assert "GOTOOLCHAIN=local" in script
    assert "CGO_ENABLED=0" in script
    assert 'sha256sum -c "$work/module-locks.sha256"' in script


def test_runtime_copies_fixed_binary_not_compiler_or_vulnerable_image():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG TARGETARCH" in dockerfile
    assert "sh /tmp/build-rclone-fixed.sh" in dockerfile
    assert "COPY --from=builder /rclone-fixed /usr/local/bin/rclone" in dockerfile
    assert "COPY --from=rclone/rclone:1.76.0" not in dockerfile
    runtime = dockerfile.split("FROM ghcr.io/willywang8216/sau-base:", 1)[1]
    assert "build-rclone-fixed.sh" not in runtime
    assert "COPY --from=builder /tmp" not in runtime
    assert "USER 1000:1000" in runtime


def test_fixed_rclone_shell_is_posix_syntax_valid():
    subprocess.run(["/bin/sh", "-n", str(SCRIPT)], check=True)
