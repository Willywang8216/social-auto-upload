"""Operations containers preserve identity/state without publishing test content."""
from __future__ import annotations

import fcntl
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE = 'sha256:' + '1' * 64
IMAGE = 'sha256:' + '2' * 64


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runner():
    return load('ops_entrypoint_tests', ROOT / 'docker/ops-entrypoint.py')


@pytest.fixture
def data(tmp_path):
    root = tmp_path / 'app'
    (root / 'sau-inbox').mkdir(parents=True)
    (root / 'db').mkdir()
    (root / 'sau-inbox/state.json').write_text('{"ready": [], "pending": []}')
    (root / 'db/database.db').write_bytes(b'fake database')
    (root / '.env').write_text('SAU_API_TOKENS=fake-app-token\nSAU_WATCH_TG_BOT_TOKEN=fake-app-notifier\nSAU_TEACHING_REPURPOSE_RELEASE_ENABLED=0\n')
    (root / 'sau-inbox/.env').write_text('SAU_WATCH_TG_BOT_TOKEN=fake-inbox-notifier\nSAU_INBOX_AUTOPUBLISH=0\n')
    return root


def test_fixed_jobs_keep_original_metadata_flags_and_no_arbitrary_arguments(runner):
    assert runner.JOBS['watcher'][-1] == '--once'
    assert runner.JOBS['inbox-archive'][-3:] == ('--include-scheduled', '--limit', '60')
    assert '--reset-failures' not in str(runner.JOBS)
    assert '--publish' not in str(runner.JOBS)
    with pytest.raises(ValueError):
        runner.environment('sh', Path('/missing'), {})


def test_watcher_and_watchdog_never_receive_application_secrets(runner, data):
    for job in ('watcher', 'inbox-monitor'):
        env = runner.environment(job, data, {})
        assert 'SAU_API_TOKENS' not in env
        assert env['SAU_WATCH_TG_BOT_TOKEN'] == 'fake-inbox-notifier'
        assert env['SAU_WATCH_HOME'] == str(data / 'sau-inbox')
        assert env['SAU_WATCH_STATE'] == str(data / 'sau-inbox/state.json')
    assert runner.environment('watcher', data, {})['SAU_INBOX_RCLONE_MODE'] == 'container'


def test_autopublisher_keeps_kill_switch_notification_precedence_and_schedule(runner, data):
    env = runner.environment('inbox-autopublish', data, {})
    assert env['SAU_INBOX_AUTOPUBLISH'] == '0'
    assert env['SAU_WATCH_TG_BOT_TOKEN'] == 'fake-inbox-notifier'
    assert env['SAU_INBOX_PUBLISH_URL'] == 'http://social-auto-upload:5409/api/inbox/items/{item_id}/publish'
    assert env['SAU_TEACHING_REPURPOSE_RELEASE_ENABLED'] == '0'
    assert 'SAU_INBOX_AUTOPUBLISH_HOURS' not in env  # leave the existing 24-hour default


def test_only_known_loopback_endpoints_are_translated(runner):
    assert runner.internal_url('http://localhost:9099/custom?q=1', '', 'sau-stt', 9099) == 'http://sau-stt:9099/custom?q=1'
    custom = 'https://example.invalid/api/{item_id}'
    assert runner.internal_url(custom, '', 'social-auto-upload', 5409) == custom
    with pytest.raises(ValueError):
        runner.internal_url('http://user:password@127.0.0.1:5409/readyz', '', 'social-auto-upload', 5409)


def test_vault_uses_explicit_env_file_and_scoped_token_refresh_config(runner, data):
    for job in ('vault-credentials', 'vault-llm-stt'):
        env = runner.environment(job, data, {})
        assert env['SAU_VAULT_ENV_FILE'] == str(data / '.env')
        assert env['RCLONE_CONFIG'] == '/run/rclone/rclone.conf'
    literal = data / 'literal.env'
    literal.write_text('export VALUE="$HOME; $(touch /tmp/never)"\n')
    assert runner.read_values(literal)['VALUE'] == '$HOME; $(touch /tmp/never)'


def test_missing_state_or_config_is_not_a_false_mount_check_pass(runner, data):
    env = runner.environment('watcher', data, {})
    with pytest.raises(RuntimeError, match='required operations mount'):
        runner.check_mounts('watcher', data, env)
    (data / 'sau-inbox/state.json').unlink()
    with pytest.raises(RuntimeError, match='required operations mount'):
        runner.check_mounts('inbox-monitor', data, {})


@pytest.fixture
def watcher(monkeypatch, data):
    monkeypatch.syspath_prepend(str(ROOT / 'sau_inbox'))
    monkeypatch.setenv('SAU_WATCH_HOME', str(data / 'sau-inbox'))
    monkeypatch.setenv('SAU_INBOX', str(data / 'sau-inbox'))
    monkeypatch.setenv('SAU_INBOX_RCLONE_MODE', 'container')
    return load('ops_watcher_tests', ROOT / 'sau_inbox/watch.py')


def test_direct_rclone_and_chown_do_not_need_docker_or_privilege(watcher, monkeypatch):
    monkeypatch.setenv('SAU_INBOX_RCLONE_CONFIG', '/run/rclone/rclone.conf')
    command = watcher._rclone_command(['lsjson', 'fake:SAU-Inbox'])
    assert command == ['rclone', '--config', '/run/rclone/rclone.conf', 'lsjson', 'fake:SAU-Inbox']
    dest = watcher.INBOX / '.sau-inbox-scan-test/0/nw/video/a.mp4'
    assert watcher._scan_host_to_container(dest) == str(dest.resolve())
    monkeypatch.setattr(watcher.subprocess, 'run', lambda *a, **k: pytest.fail('unexpected Docker/chown'))
    watcher._chown_scan_root(dest.parent)
    monkeypatch.setenv('SAU_INBOX_RCLONE_MODE', 'host')
    assert watcher._scan_host_to_container(dest) == str(dest.resolve())
    monkeypatch.setenv('SAU_INBOX_RCLONE_MODE', 'invalid')
    with pytest.raises(ValueError):
        watcher._rclone_command([])


def test_direct_cleanup_deletes_only_generated_directory_and_refuses_symlinks(watcher, data):
    keep = watcher.INBOX / 'authored'
    keep.mkdir()
    (keep / 'note.md').write_text('keep')
    scan = watcher.INBOX / '.sau-inbox-scan-owned'
    scan.mkdir()
    (scan / 'temporary').write_bytes(b'tmp')
    link = watcher.INBOX / '.sau-inbox-scan-alias'
    link.symlink_to(scan, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        watcher._cleanup_scan_root(link)
    with pytest.raises(ValueError):
        watcher._cleanup_scan_root(keep)
    with pytest.raises(ValueError):
        watcher._cleanup_scan_root(data.parent / '.sau-inbox-scan-outside')
    watcher._cleanup_scan_root(scan)
    assert not scan.exists() and (keep / 'note.md').read_text() == 'keep'
    assert (watcher.INBOX / 'state.json').is_file()


def test_busy_watcher_skips_without_scan_notification_or_false_heartbeat(watcher, monkeypatch):
    monkeypatch.setattr(watcher, 'process_paths', lambda *a: pytest.fail('must not scan'))
    monkeypatch.setattr(watcher, 'write_health', lambda **k: pytest.fail('must not fake success'))
    monkeypatch.setattr(watcher, 'check_cron_guard', lambda: pytest.fail('host cron is not inside the container'))
    with (watcher.INBOX / '.watch-run.lock').open('a+') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert watcher.scan_once() == 0
    with watcher._watch_run_lock() as acquired:
        assert acquired is True


def test_vault_dry_run_never_prints_the_credential_payload(monkeypatch, capsys):
    module = load('llm_vault_ops_tests', ROOT / 'scripts/push_llm_stt_vault.py')
    fake = {'llm': {'pool_size': 1, 'legacy_api_key': 'do-not-log-this-secret'},
            'agentrouter': {'working_accounts': 1, 'api_keys': ['do-not-log-this-secret']},
            'stt': {'local': {'base_url_in_container': 'http://sau-stt:9099', 'model': 'tiny'}}}
    monkeypatch.setattr(module, 'build', lambda: fake)
    monkeypatch.setattr(sys, 'argv', ['push_llm_stt_vault.py', '--dry-run'])
    assert module.main() == 0
    assert 'do-not-log-this-secret' not in capsys.readouterr().out


@pytest.fixture
def launcher(tmp_path):
    root = tmp_path / 'sau'
    ops = root / 'ops/container-jobs'
    for rel in ('locks', 'config/drive', 'config/onedrive'):
        (ops / rel).mkdir(parents=True, exist_ok=True)
    (ops / 'image-id.txt').write_text(IMAGE + '\n')
    for rel in ('config/drive/rclone.conf', 'config/onedrive/rclone.conf'):
        (ops / rel).write_text('[fake]\ntype = drive\n')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    fake = bin_dir / 'docker'
    fake.write_text('#!/bin/sh\n'
                    'printf "%s\\n" "$*" >> "$FAKE_LOG"\n'
                    'case "$1 $2" in\n'
                    '  "image inspect") case "$*" in *operations-tools*) echo 1;; *) echo "$FAKE_BASE";; esac;;\n'
                    '  "inspect --format") echo "$FAKE_BASE";;\n'
                    '  "container inspect") test -e "$FAKE_ORPHAN";;\n'
                    '  "run --rm") case "$*" in *"--log-driver local"*) exit 0;; *) echo "journald default rejects file rotation options" >&2; exit 125;; esac;;\n'
                    '  *) exit 2;;\n'
                    'esac\n')
    fake.chmod(0o755)
    env = dict(os.environ, SAU_VPS_ROOT=str(root), PATH=str(bin_dir) + ':' + os.environ['PATH'],
               FAKE_LOG=str(tmp_path / 'docker.log'), FAKE_BASE=BASE, FAKE_ORPHAN=str(tmp_path / 'orphan'))
    return root, env


def invoke(launcher, job):
    root, env = launcher
    result = subprocess.run(['bash', str(ROOT / 'scripts/vps-ops.sh'), job], env=env, capture_output=True, text=True, timeout=10)
    log = Path(env['FAKE_LOG']).read_text()
    return result, log


def test_host_controller_scopes_mounts_and_is_not_docker_in_docker(launcher):
    result, log = invoke(launcher, 'watcher')
    assert result.returncode == 0, result.stderr
    run = [line for line in log.splitlines() if line.startswith('run ')][0]
    assert '/app/.env' not in run and '/app/db' not in run
    assert '/config/drive,dst=/run/rclone' in run
    for option in ('--read-only', '--user 1000:1000', '--cap-drop ALL', '--memory 1g', '--pids-limit 128'):
        assert option in run
    assert '/var/run/docker.sock' not in run
    result, log = invoke(launcher, 'vault-credentials')
    assert result.returncode == 0, result.stderr
    run = [line for line in log.splitlines() if line.startswith('run ')][-1]
    assert '/app/.env,readonly' in run and '/app/cookiesFile,readonly' in run
    assert '/config/onedrive,dst=/run/rclone' in run
    assert '/home/will/.config/rclone' not in run


@pytest.mark.parametrize('job', ['watcher', 'inbox-monitor', 'pipeline-monitor', 'inbox-archive',
                                 'inbox-autopublish', 'vault-credentials', 'vault-llm-stt'])
def test_host_controller_pins_bounded_logging_on_journald_hosts(launcher, job):
    # The fake daemon rejects file rotation options under its journald default
    # unless the controller selects the supported, bounded local driver.
    result, log = invoke(launcher, job)
    assert result.returncode == 0, result.stderr
    run = [line for line in log.splitlines() if line.startswith('run ')][-1]
    assert '--log-driver local' in run
    assert '--log-opt max-size=10m' in run and '--log-opt max-file=2' in run


def test_host_controller_skips_competing_lock_and_refuses_existing_container(launcher):
    root, env = launcher
    with (root / 'ops/container-jobs/locks/watcher.lock').open('a+') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result, log = invoke(launcher, 'watcher')
    assert result.returncode == 0 and 'skipped' in result.stdout
    assert 'run --rm' not in log
    Path(env['FAKE_ORPHAN']).touch()
    result, log = invoke(launcher, 'watcher')
    assert result.returncode == 75 and 'reconcile' in result.stderr
    assert 'run --rm' not in log


def test_operations_build_context_is_strictly_allowlisted():
    lines = (ROOT / 'Dockerfile.ops.dockerignore').read_text().splitlines()
    assert '**' in lines
    assert not any('.env' in row or 'ops/' in row or 'cookies/' in row or '.git' in row for row in lines)
    assert 'USER 1000:1000' in (ROOT / 'Dockerfile.ops').read_text()
    subprocess.run(['bash', '-n', str(ROOT / 'scripts/vps-ops.sh')], check=True, timeout=10)


def test_missing_optional_notification_file_keeps_native_startup_behavior(runner, data):
    (data / 'sau-inbox/.env').unlink()
    for job in ('watcher', 'inbox-monitor'):
        env = runner.environment(job, data, {})
        assert 'SAU_API_TOKENS' not in env
        assert 'SAU_WATCH_TG_BOT_TOKEN' not in env
    assert runner.environment('inbox-autopublish', data, {})['SAU_WATCH_TG_BOT_TOKEN'] == 'fake-app-notifier'
    (data / '.env').unlink()
    with pytest.raises(FileNotFoundError):
        runner.environment('vault-llm-stt', data, {})


@pytest.mark.parametrize('prefix', ['/home/will/sau-inbox', '/home/will/social-auto-upload/ops/inbox', '/app/sau-inbox'])
def test_legacy_processed_and_queue_identities_are_not_reingested(watcher, monkeypatch, prefix):
    import json

    state = {
        'processed': {prefix + '/nw/video/rejected__sfw.mp4': 'original-owner-receipt'},
        'ready': [{'sourcePath': prefix + '/nw/video/claimed__sfw.mp4', 'autoPublishState': 'publishing', 'autoPublishAttempts': 3}],
        'pending': [{'path': prefix + '/nw/video/pending.mp4', 'lease': 'preserve'}],
        'quarantined': [{'sourcePath': prefix + '/teaching/video/held__nsfw.mp4', 'reason': 'preserve'}],
    }
    before = json.dumps(state, sort_keys=True)
    names = ['nw/video/rejected__sfw.mp4', 'nw/video/claimed__sfw.mp4', 'nw/video/pending.mp4', 'teaching/video/held__nsfw.mp4']
    monkeypatch.setattr(watcher, '_drive_list', lambda: [{'Path': name} for name in names])
    monkeypatch.setattr(watcher.subprocess, 'run', lambda *a, **k: pytest.fail('must not download/replay retained work'))
    assert watcher.detect_new_files(state) == []
    assert json.dumps(state, sort_keys=True) == before


def test_nested_media_processing_cleans_the_scan_root_and_reports_success(watcher, monkeypatch):
    import json

    scan = watcher.INBOX / '.sau-inbox-scan-nested'
    source = scan / '0/nw/video/subdir/clip__nsfw.mp4'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'synthetic media, no provider')
    remote = 'nw/video/subdir/clip__nsfw.mp4'
    monkeypatch.setattr(watcher, 'detect_new_files', lambda state: [(source, 'nw', remote)])
    monkeypatch.setattr(watcher, 'detect_orphans', lambda state: [])
    monkeypatch.setattr(watcher, 'build_thumb', lambda entry, *args: entry.update(thumbKind='titlecard', needsFrameSelection=True))
    monkeypatch.setattr(watcher, '_maybe_auto_title', lambda *args: None)
    announced = []
    monkeypatch.setattr(watcher, '_announce_ready', lambda entry: announced.append(entry['remotePath']))
    assert watcher.scan_once() == 1
    assert not scan.exists()
    assert announced == [remote]
    assert json.loads(watcher.HEALTH_PATH.read_text())['new_ready'] == 1


def test_concurrent_legacy_rejection_is_not_resurrected_or_overwritten(watcher, monkeypatch):
    scan = watcher.INBOX / '.sau-inbox-scan-race'
    source = scan / '0/nw/video/clip__nsfw.mp4'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'synthetic media')
    remote = 'nw/video/clip__nsfw.mp4'
    legacy = '/home/will/sau-inbox/' + remote

    def during_transfer(state):
        latest = watcher.load_state()
        latest['processed'] = {legacy: 'signed-owner-rejection-original'}
        latest['budget'] = {'attempts': 3, 'claim': 'keep'}
        watcher.save_state(latest)
        return [(source, 'nw', remote)]

    monkeypatch.setattr(watcher, 'detect_new_files', during_transfer)
    monkeypatch.setattr(watcher, 'detect_orphans', lambda state: [])
    monkeypatch.setattr(watcher, 'build_thumb', lambda *args: None)
    monkeypatch.setattr(watcher, '_maybe_auto_title', lambda *args: None)
    result = watcher.process_paths(watcher.load_state())
    latest = watcher.load_state()
    assert result['ready'] == [] and latest['ready'] == []
    assert latest['processed'] == {legacy: 'signed-owner-rejection-original'}
    assert latest['budget'] == {'attempts': 3, 'claim': 'keep'}
    assert not scan.exists()


def test_rclone_mount_check_rejects_unrelated_remote_grants(runner, data):
    config = data / 'rclone.conf'
    config.write_text('[GDrive-willywang8216]\ntype = drive\n')
    env = {'RCLONE_CONFIG': str(config)}
    runner.check_mounts('watcher', data, env)
    config.write_text(config.read_text() + '[unrelated]\ntype = s3\n')
    with pytest.raises(RuntimeError, match='only its required remote'):
        runner.check_mounts('watcher', data, env)


def test_operations_overlay_keeps_known_inbox_aliases_and_shared_archive_lock():
    dockerfile = (ROOT / 'Dockerfile.ops').read_text()
    assert 'COPY myUtils/inbox_archive.py /app/myUtils/inbox_archive.py' in dockerfile
    assert 'ln -s /app/sau-inbox /home/will/sau-inbox' in dockerfile
    assert 'ln -s /app/sau-inbox /home/will/social-auto-upload/ops/inbox' in dockerfile
