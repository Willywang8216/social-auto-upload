"""VPS verification isolation and single-root layout contracts."""
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_private_operations_are_excluded_from_git_and_all_image_contexts():
    assert '/ops/' in (ROOT / '.gitignore').read_text().splitlines()
    for name in ('.dockerignore', 'Dockerfile.dockerignore'):
        assert 'ops' in (ROOT / name).read_text().splitlines()


def test_shared_runtime_mount_uses_canonical_inbox_with_explicit_legacy_override():
    services = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())['services']
    for name in ('social-auto-upload', 'worker'):
        assert '${SAU_INBOX_HOST_DIR:-./ops/inbox}:/app/sau-inbox' in services[name]['volumes']


def test_vps_gate_runtime_is_isolated_and_fixed_toolchain():
    launcher = (ROOT / 'scripts/vps-gates.sh').read_text()
    entry = (ROOT / 'docker/verify-entrypoint.sh').read_text()
    tools = (ROOT / 'Dockerfile.verify').read_text()
    for path in ('/git-objects,readonly', '/rules,readonly', '/katex,readonly'):
        assert path in launcher
    assert '/var/run/docker.sock' not in launcher
    assert '--read-only --no-healthcheck --init --pids-limit 256 --cpus 2' in launcher
    assert '--memory 6g --memory-swap 6g' in launcher
    assert '/work:rw,exec,nosuid,nodev,size=6g,' in launcher
    assert '--cap-drop ALL --security-opt no-new-privileges' in launcher
    assert '/ops/verification/' in launcher and 'Existing container' in launcher
    assert 'node:22.21.1-slim' in tools and '^uv 0.12.7' in tools
    assert 'patch=3.12.14' in entry and 'patch=3.13.15' in entry
    assert '! -e .env' in entry and '! -e db/database.db' in entry
    assert 'backend --python "$version"' in entry


def test_vps_shell_scripts_parse_without_executing_jobs():
    for name in ('scripts/vps-gates.sh', 'docker/verify-entrypoint.sh', 'scripts/release-gates.sh'):
        result = subprocess.run(['bash', '-n', str(ROOT / name)], capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr.decode(errors='replace')


def test_frontend_readiness_is_gated_before_the_passing_receipt():
    gates = (ROOT / 'scripts/release-gates.sh').read_text()
    entry = (ROOT / 'docker/verify-entrypoint.sh').read_text()
    command = 'run_clean node --test sau_frontend/src/utils/__tests__/publish-readiness.node-test.mjs'
    assert gates.index(command) < gates.index('--component frontend')
    assert 'node --test' not in entry  # no unchecked post-receipt test


def test_readonly_verification_has_bounded_non_executable_standard_tmp():
    launcher = (ROOT / 'scripts/vps-gates.sh').read_text()
    assert '--tmpfs /tmp:rw,nosuid,nodev,size=256m,mode=1777' in launcher


def test_all_vps_images_define_a_real_non_login_identity_for_ssh_signatures():
    for name in ('Dockerfile', 'Dockerfile.verify', 'Dockerfile.ops'):
        image = (ROOT / name).read_text()
        assert 'getent group 1000' in image, name
        assert 'groupadd --gid 1000 sau' in image, name
        assert 'getent passwd 1000' in image, name
        assert 'useradd --uid 1000 --gid 1000 --no-create-home' in image, name
        assert '--home-dir /nonexistent --shell /usr/sbin/nologin sau' in image, name
        assert image.index('useradd --uid 1000') < image.rindex('USER 1000:1000'), name
