#!/usr/bin/env bash
# The watcher and parent-controlled initial rollout share this implementation.
# No push, remote deploy, DB downgrade, forced drain, or untested :latest restart.
set -euo pipefail
umask 077
# Guard assertions in the config/receipt snippets must not be optimized away.
unset PYTHONOPTIMIZE
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="$SOURCE"
PYTHON="${SAU_RELEASE_PYTHON:-python3}"
REVISION=""
NO_BUILD=0 BUILD_ONLY=0 HOLD=0 RESUME=0 ALLOW_LEGACY=0
while (( $# )); do
  case "$1" in
    --revision) REVISION="$2"; shift 2 ;;
    --runtime-root) RUNTIME="$(cd "$2" && pwd)"; shift 2 ;;
    --no-build) NO_BUILD=1; shift ;;
    --build-only) BUILD_ONLY=1; shift ;;
    --hold) HOLD=1; shift ;;
    --resume) RESUME=1; NO_BUILD=1; shift ;;
    --legacy-stop-web) ALLOW_LEGACY=1; shift ;;
    -h|--help)
      echo 'deploy-local.sh [--revision FULL_SHA] [--runtime-root DIR] [--build-only|--hold|--resume] [--no-build] [--legacy-stop-web]'
      exit 0 ;;
    *) echo "Unknown release option: $1" >&2; exit 2 ;;
  esac
done
(( HOLD + RESUME + BUILD_ONLY <= 1 )) || exit 2
[[ "$(uname -s)" == Linux ]] || { echo 'Deployment requires Linux.' >&2; exit 1; }
for command in git docker flock "$PYTHON"; do command -v "$command" >/dev/null; done
CHECKS="$SOURCE/scripts/release_checks.py"
BACKUP="$SOURCE/scripts/sqlite_backup.py"
RECOVERY="$SOURCE/scripts/release_recovery.py"
HEAD="$("$PYTHON" "$CHECKS" --root "$SOURCE" clean)"
REVISION="${REVISION:-$HEAD}"
[[ "$REVISION" == "$HEAD" && "$REVISION" =~ ^[0-9a-f]{40}$ ]] || exit 1
LOG_DIR="$RUNTIME/logs"
mkdir -p "$LOG_DIR/releases"
LOCK="$LOG_DIR/ci-watch.lock"
# Inherit the watcher's OPEN lock descriptor, not an environment-only bypass.
if [[ "${SAU_RELEASE_LOCK_FD:-}" == 9 && -e /proc/$$/fd/9 && /proc/$$/fd/9 -ef "$LOCK" ]]; then
  flock -n 9 || exit 75
else
  exec 9>"$LOCK"
  flock -n 9 || { echo 'ci-watch/deploy/restore lock is held; retry later.' >&2; exit 75; }
fi
export SAU_RELEASE_LOCK_FD=9
RUNTIME_HEAD="$("$PYTHON" "$CHECKS" --root "$RUNTIME" clean)"
git -C "$RUNTIME" merge-base --is-ancestor "$RUNTIME_HEAD" "$REVISION" || {
  echo 'Unpublished/divergent local history: refusing to replace it.' >&2; exit 1;
}
IMAGE_BASE="ghcr.io/willywang8216/social-auto-upload"
IMAGE="${IMAGE_BASE}:commit-${REVISION}"
RECEIPTS="$LOG_DIR/releases/gates/$REVISION"
DETAIL_LOG="$LOG_DIR/releases/deploy-${REVISION}-$(date -u +%Y%m%dT%H%M%S)-$$.log"
DB="$RUNTIME/db/database.db"
PAUSE="$RUNTIME/db/publish.pause"
STAGED="$LOG_DIR/release-staged.json"
WEB=social-auto-upload
WORKER=social-auto-upload-worker
MAX_WAIT="${SAU_RELEASE_DRAIN_SECONDS:-${SAU_CI_WATCH_MAX_WAIT_SECONDS:-600}}"
STOP_WAIT="${SAU_RELEASE_STOP_SECONDS:-600}"
READY_WAIT="${SAU_RELEASE_READY_SECONDS:-180}"
for bound in "$MAX_WAIT" "$STOP_WAIT" "$READY_WAIT"; do
  [[ "$bound" =~ ^[1-9][0-9]*$ ]] || { echo 'Timeouts must be positive, not safety bypasses.' >&2; exit 2; }
done
BUILD_SCRATCH="" RELEASE_DIR="" OLD_WEB="" OLD_WORKER=""
LEGACY=0 TOUCHED=0 PAUSE_OWNED=0
log() { printf '[release] %s\n' "$*" | tee -a "$DETAIL_LOG"; }
fail() { log "$*"; exit 1; }
running() { [[ "$(docker inspect -f '{{.State.Running}}' "$1")" == true ]]; }
stop_gracefully() {
  local name="$1" deadline=$((SECONDS + STOP_WAIT))
  # Disable automatic restart before SIGTERM. Never use docker stop's eventual
  # SIGKILL: on timeout leave the process draining and the pause marker intact.
  docker update --restart=no "$name" >>"$DETAIL_LOG" 2>&1 || return 1
  if running "$name"; then
    docker kill --signal=TERM "$name" >>"$DETAIL_LOG" 2>&1 || return 1
  fi
  while running "$name"; do
    (( SECONDS < deadline )) || return 1
    sleep 2
  done
  [[ "$(docker inspect -f '{{.State.Running}}' "$name")" == false ]]
}
activity_check() {
  local args=()
  (( LEGACY == 0 )) || args+=(--legacy)
  "$PYTHON" "$BACKUP" activity "$DB" "${args[@]}" >>"$DETAIL_LOG" 2>&1
}
wait_drained() {
  local deadline=$((SECONDS + MAX_WAIT)) stable=0 rc
  while (( SECONDS < deadline )); do
    if activity_check; then
      stable=$((stable + 1))
      if [[ -n "$OLD_WORKER" ]] && running "$WORKER"; then
        if ! docker exec "$WORKER" python scripts/release_probe.py worker --allow-paused >>"$DETAIL_LOG" 2>&1; then
          stable=0
        fi
      fi
      (( stable < 2 )) || return 0
    else
      rc=$?
      (( rc == 3 )) || return 1  # query failure is NOT zero active work
      stable=0
    fi
    sleep 5
  done
  return 1
}
restore_images() {
  # Database rollback is deliberately absent, even if activation briefly ran:
  # posts may already exist remotely. Keep the current DB and all evidence.
  [[ -n "$OLD_WEB" && -f "$RELEASE_DIR/baseline/compose.json" ]] || return 1
  log 'Restoring previous images/config only; database and pause are retained.'
  # Even a broken readiness probe must permit image rollback once SIGTERM has
  # completed the worker's own drain. Never replace a still-running process.
  if docker inspect "$WORKER" >/dev/null 2>&1; then stop_gracefully "$WORKER" || return 1; fi
  stop_gracefully "$WEB" || return 1
  wait_drained || return 1
  docker tag "$OLD_WEB" "${IMAGE_BASE}:latest" || return 1
  local rollback=(docker compose --project-directory "$RUNTIME" -p "$PROJECT"
    -f "$RELEASE_DIR/baseline/compose.json" -f "$RELEASE_DIR/baseline/images.json")
  if [[ -n "$OLD_WORKER" ]]; then
    "${rollback[@]}" up -d --force-recreate --pull never --no-deps social-auto-upload worker >>"$DETAIL_LOG" 2>&1 || return 1
    [[ "$(docker inspect -f '{{.Image}}' "$WORKER")" == "$OLD_WORKER" ]] || return 1
  else
    # Legacy web embeds publishers and ignores the pause. Restore its image but
    # DO NOT start it automatically against an uncertain/newer database.
    "${rollback[@]}" up --no-start --force-recreate --pull never --no-deps social-auto-upload >>"$DETAIL_LOG" 2>&1 || return 1
  fi
  [[ "$(docker inspect -f '{{.Image}}' "$WEB")" == "$OLD_WEB" ]]
}
finish() {
  local status=$?
  trap - EXIT INT TERM
  set +e
  if (( status != 0 && PAUSE_OWNED == 1 )); then
    if [[ ! -e "$PAUSE" ]]; then (set -o noclobber; printf '%s\n' "$REVISION" >"$PAUSE"); fi
    log 'Release incomplete: pause retained (legacy publishers may ignore it). No automatic database restore.'
    if (( TOUCHED == 1 )); then
      restore_images || log 'Rollback could not be verified/drained; leave services paused/stopped for operator recovery.'
    fi
    log "Evidence: ${RELEASE_DIR:-$DETAIL_LOG}"
  fi
  if [[ -n "$BUILD_SCRATCH" ]]; then
    git -C "$SOURCE" worktree remove --force "$BUILD_SCRATCH/source" >/dev/null 2>&1
    rm -rf "$BUILD_SCRATCH"
  fi
  exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Every service-changing path requires passing Linux 3.10/3.12 + frontend
# receipts and an image-ID receipt for the same clean commit and lockfiles.
if ! bash "$SOURCE/scripts/release-gates.sh" verify --receipts "$RECEIPTS" >>"$DETAIL_LOG" 2>&1; then
  (( NO_BUILD == 0 )) || fail 'No matching passing gate receipts; --no-build cannot bypass tests.'
  log "Testing clean candidate $REVISION (details: $DETAIL_LOG)"
  bash "$SOURCE/scripts/release-gates.sh" all --receipts "$RECEIPTS" >>"$DETAIL_LOG" 2>&1
fi
IMAGE_ID="$(docker image inspect -f '{{.Id}}' "$IMAGE" 2>/dev/null || true)"
if [[ -z "$IMAGE_ID" ]] || ! "$PYTHON" "$CHECKS" verify --revision "$REVISION" --receipts "$RECEIPTS" --image-id "$IMAGE_ID" >>"$DETAIL_LOG" 2>&1; then
  (( NO_BUILD == 0 )) || fail 'Image has no matching gate/build receipt.'
  BUILD_SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/sau-release-build.XXXXXX")"
  git -C "$SOURCE" worktree add --detach --quiet "$BUILD_SCRATCH/source" "$REVISION"
  log "Building $IMAGE from source-only checkout"
  docker build -f "$BUILD_SCRATCH/source/Dockerfile" -t "$IMAGE" \
    --build-arg SAU_BUILD_REVISION="$REVISION" --build-arg GIT_SHA="$REVISION" \
    --build-arg BUILD_DATE="$(date -u +%FT%TZ)" "$BUILD_SCRATCH/source" >>"$DETAIL_LOG" 2>&1
  "$PYTHON" "$CHECKS" --root "$BUILD_SCRATCH/source" clean --revision "$REVISION" >/dev/null
  IMAGE_ID="$(docker image inspect -f '{{.Id}}' "$IMAGE")"
  # No production mounts, network or .env. This also catches missing runtime
  # dependencies and accidental background startup during WSGI import.
  docker run --rm --network none -e SAU_TESTING=1 -e APP_ENV=testing -e SAU_PROCESS_ROLE=web \
    -e SAU_DB_PATH=/tmp/release-smoke.db --entrypoint python "$IMAGE_ID" -c \
    'import os,sys,threading; before=set(threading.enumerate()); import wsgi; import myUtils.worker; assert set(threading.enumerate()) == before; assert os.environ["SAU_BUILD_REVISION"] == sys.argv[1]' \
    "$REVISION" >>"$DETAIL_LOG" 2>&1
  "$PYTHON" "$CHECKS" record-build --revision "$REVISION" --receipts "$RECEIPTS" --image-id "$IMAGE_ID"
fi
[[ "$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE_ID")" == "$REVISION" ]] || fail 'Image revision label mismatch.'
if (( BUILD_ONLY == 1 )); then log "Gated image ready: $IMAGE_ID (no services touched)"; exit 0; fi
[[ -f "$DB" && ! -L "$DB" ]] || fail 'Existing SQLite database required; initialization is an explicit separate action.'
[[ -f "$RUNTIME/.env" ]] || fail 'Runtime .env is missing; no default credentials/config will be guessed.'
PROJECT="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$WEB")"
[[ -n "$PROJECT" && "$PROJECT" != '<no value>' ]] || fail 'Cannot establish the existing Compose project.'

pair_ready() {
  local phase="$1" since="${2:-}" args=()
  [[ "$phase" != paused ]] || args+=(--allow-paused)
  [[ -z "$since" ]] || args+=(--since "$since")
  [[ "$(docker inspect -f '{{.Image}}' "$WEB")" == "$IMAGE_ID" ]] || return 1
  [[ "$(docker inspect -f '{{.Image}}' "$WORKER")" == "$IMAGE_ID" ]] || return 1
  for name in "$WEB" "$WORKER"; do
    [[ "$(docker inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$name")" == "$REVISION" ]] || return 1
  done
  docker exec "$WORKER" python scripts/release_probe.py worker --revision "$REVISION" "${args[@]}" || return 1
  docker exec "$WEB" python scripts/release_probe.py web --revision "$REVISION" "${args[@]}" || return 1
  if [[ "$phase" == active ]]; then
    for name in "$WEB" "$WORKER"; do
      [[ "$(docker inspect -f '{{.State.Health.Status}}' "$name")" == healthy ]] || return 1
    done
  fi
}
wait_ready() {
  local phase="$1" since="${2:-}" deadline=$((SECONDS + READY_WAIT))
  while (( SECONDS < deadline )); do
    if pair_ready "$phase" "$since" >>"$DETAIL_LOG" 2>&1; then return 0; fi
    sleep 3
  done
  return 1
}

if (( RESUME == 1 )); then
  [[ -f "$PAUSE" && ! -L "$PAUSE" && "$(cat "$PAUSE")" == "$REVISION" ]] || fail 'No matching release-owned pause to resume.'
  RELEASE_DIR="$("$PYTHON" - "$STAGED" "$REVISION" "$IMAGE_ID" "$RUNTIME/backups/releases" <<'PY'
import json, pathlib, sys
state = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert state['revision'] == sys.argv[2] and state['image_id'] == sys.argv[3]
path = pathlib.Path(state['release_dir']).resolve(strict=True)
assert path.is_relative_to(pathlib.Path(sys.argv[4]).resolve(strict=True))
print(path)
PY
)"
  # Recovery evidence must pass BEFORE assuming pause/rollback ownership. An
  # incomplete held bundle must not unpause or trigger an unsafe rollback.
  "$PYTHON" "$RECOVERY" verify "$RELEASE_DIR" --candidate-image "$IMAGE_ID" >>"$DETAIL_LOG" 2>&1 \
    || fail 'Recovery bundle verification failed; staged candidate remains paused.'
  OLD_WEB="$(cat "$RELEASE_DIR/baseline/web-image")"
  OLD_WORKER="$(cat "$RELEASE_DIR/baseline/worker-image")"
  for previous in "$OLD_WEB" "$OLD_WORKER"; do
    [[ -n "$previous" ]] || continue  # legacy release had no separate worker
    [[ "$(docker image inspect -f '{{.Id}}' "$previous" 2>>"$DETAIL_LOG")" == "$previous" ]] \
      || fail 'Previous release image is unavailable; staged candidate remains paused.'
  done
  PAUSE_OWNED=1 TOUCHED=1
  wait_ready paused || fail 'Staged candidate is no longer safe to activate.'
else
  [[ ! -e "$PAUSE" && ! -L "$PAUSE" && ! -e "$STAGED" ]] || fail 'Existing pause/staged release requires explicit operator recovery or --resume.'
  OLD_WEB="$(docker inspect -f '{{.Image}}' "$WEB")"
  OLD_WORKER="$(docker inspect -f '{{.Image}}' "$WORKER" 2>/dev/null || true)"
  if [[ -z "$OLD_WORKER" ]]; then
    LEGACY=1
    (( ALLOW_LEGACY == 1 )) || fail 'Legacy embedded web detected: quiesce ingress and use the documented --legacy-stop-web rollout.'
  else
    [[ "$OLD_WORKER" == "$OLD_WEB" ]] || fail 'Existing web/worker images differ; investigate instead of overwriting.'
  fi
  docker tag "$OLD_WEB" "${IMAGE_BASE}:previous"
  RELEASE_DIR="$RUNTIME/backups/releases/$(date -u +%Y%m%dT%H%M%S)-${REVISION}-$$"
  mkdir -p "$RELEASE_DIR/baseline"
  chmod 700 "$RELEASE_DIR" "$RELEASE_DIR/baseline"
  printf '%s\n' "$OLD_WEB" >"$RELEASE_DIR/baseline/web-image"
  printf '%s\n' "$OLD_WORKER" >"$RELEASE_DIR/baseline/worker-image"
  printf '%s\n' "$RUNTIME_HEAD" >"$RELEASE_DIR/baseline/git-revision"
  docker inspect "$WEB" >"$RELEASE_DIR/baseline/web-inspect.json"
  if [[ -n "$OLD_WORKER" ]]; then docker inspect "$WORKER" >"$RELEASE_DIR/baseline/worker-inspect.json"; fi
  render() {
    local root="$1" destination="$2" files=(-f "$1/docker-compose.yml")
    [[ ! -f "$root/docker-compose.override.yml" ]] || files+=(-f "$root/docker-compose.override.yml")
    docker compose --project-directory "$RUNTIME" --env-file "$RUNTIME/.env" -p "$PROJECT" "${files[@]}" config --format json >"$destination" 2>>"$DETAIL_LOG"
  }
  render "$RUNTIME" "$RELEASE_DIR/baseline/compose.json"
  SAU_IMAGE="$IMAGE_ID" render "$SOURCE" "$RELEASE_DIR/candidate.compose.json"
  # Validate the rendered pair and save config material privately. Never print
  # compose config, inspect environment, credentials, or plaintext cookie data.
  PYTHONPATH="$SOURCE" "$PYTHON" - "$RELEASE_DIR" "$RUNTIME" "$IMAGE_ID" "$OLD_WEB" "$OLD_WORKER" <<'PY'
import json, os, pathlib, shutil, sys
from scripts.release_recovery import RecoveryError, require_config_file
release, root = map(pathlib.Path, sys.argv[1:3])
config = json.loads((release / 'candidate.compose.json').read_text())
web, worker = (config['services'][name] for name in ('social-auto-upload', 'worker'))
assert web['image'] == worker['image'] == sys.argv[3]
assert web['volumes'] == worker['volumes']
assert web.get('env_file') == worker.get('env_file')
shared_web = {k: v for k, v in web['environment'].items() if k not in {'SAU_PROCESS_ROLE', 'SAU_REQUIRE_WORKER_HEALTH'}}
shared_worker = {k: v for k, v in worker['environment'].items() if k not in {'SAU_PROCESS_ROLE', 'SAU_REQUIRE_WORKER_HEALTH'}}
assert shared_web == shared_worker
for service, role in ((web, 'web'), (worker, 'worker')):
    env = service['environment']
    assert service['init'] is True and env['SAU_PROCESS_ROLE'] == role
    assert env['SAU_DB_PATH'] == '/app/db/database.db'
    assert env['SAU_PAUSE_CLAIMS_FILE'] == '/app/db/publish.pause'
    assert not env.get('DATABASE_URL') or env['DATABASE_URL'] == 'sqlite:////app/db/database.db'
    db_mount = next(v for v in service['volumes'] if v['target'] == '/app/db')
    assert pathlib.Path(db_mount['source']).resolve() == (root / 'db').resolve()
assert web['environment']['SAU_REQUIRE_WORKER_HEALTH'] == '1'
assert worker['command'] == ['python', '-m', 'myUtils.worker', '--max-concurrent', '3']
assert not worker.get('ports')
try:
    conf_source = require_config_file(pathlib.Path(next(v['source'] for v in web['volumes'] if v['target'] == '/app/conf.py')))
except RecoveryError:
    raise SystemExit('Selected conf.py bind source must be a readable regular file') from None
for name in ('.env', 'conf.py', 'rclone-cache.conf'):
    path = root / name
    if name == 'conf.py':
        path = conf_source
    if path.is_file():
        dest = release / 'baseline' / name
        shutil.copyfile(path, dest)
        os.chmod(dest, 0o600)
images = {'social-auto-upload': {'image': sys.argv[4]}}
if sys.argv[5]:
    images['worker'] = {'image': sys.argv[5]}
(release / 'baseline' / 'images.json').write_text(json.dumps({'services': images}))
# Freeze the actual prior container environment/command, even on the initial
# rollout where the host checkout may already carry the candidate compose file.
baseline_path = release / 'baseline' / 'compose.json'
baseline = json.loads(baseline_path.read_text())
for name, filename in (('social-auto-upload', 'web-inspect.json'), ('worker', 'worker-inspect.json')):
    inspected = release / 'baseline' / filename
    if not inspected.exists():
        continue
    actual = json.loads(inspected.read_text())[0]
    old = baseline['services'][name]
    old['image'] = actual['Image']
    literal_env = dict(pair.split('=', 1) for pair in actual['Config']['Env'])
    # Inspect returns literal values; prevent a second Compose interpolation
    # from changing passwords/tokens containing dollar signs during rollback.
    old['environment'] = {key: value.replace('$', '$$') for key, value in literal_env.items()}
    old.pop('env_file', None)
    old['command'] = actual['Config']['Cmd']
    old['entrypoint'] = actual['Config']['Entrypoint']
    old['user'] = actual['Config']['User']
    if sys.argv[5]:
        assert old['environment'].get('SAU_PROCESS_ROLE') == ('worker' if name == 'worker' else 'web')
    if name == 'worker':
        assert old['environment'].get('SAU_PROCESS_ROLE') == 'worker'
        assert old['environment'].get('SAU_PAUSE_CLAIMS_FILE') == '/app/db/publish.pause'
baseline_path.write_text(json.dumps(baseline))
PY
  "$PYTHON" "$CHECKS" --root "$RUNTIME" clean --revision "$RUNTIME_HEAD" >/dev/null
  (set -o noclobber; printf '%s\n' "$REVISION" >"$PAUSE")
  PAUSE_OWNED=1
  log 'Claims paused; waiting for targets, leased preps and worker heartbeat to drain.'
  wait_drained || fail 'Drain failed/timed out; no forced restart or migration is allowed.'
  if [[ -n "$OLD_WORKER" ]]; then stop_gracefully "$WORKER" || fail 'Worker has not exited gracefully; aborting.'; fi
  # Legacy web cannot acknowledge publish.pause. Stop it only in an explicitly
  # approved idle/outage window, then recheck; it is NEVER restarted on rollback.
  stop_gracefully "$WEB" || fail 'Web has not exited gracefully; aborting.'
  activity_check || fail 'Activity remained/appeared while stopping; preserve DB for reconciliation.'
  log 'Taking online SQLite snapshot and verifying an isolated restore.'
  "$PYTHON" "$BACKUP" snapshot "$DB" "$RELEASE_DIR/database.db" >>"$DETAIL_LOG" 2>&1
  "$PYTHON" "$BACKUP" verify "$RELEASE_DIR/database.db" --restore-check >>"$DETAIL_LOG" 2>&1
  "$PYTHON" "$RECOVERY" seal "$RELEASE_DIR" --candidate-image "$IMAGE_ID" \
    --previous-web "$OLD_WEB" --previous-worker "$OLD_WORKER" >>"$DETAIL_LOG" 2>&1 \
    || fail 'Recovery bundle could not be sealed; migration and activation are refused.'
  COMPOSE=(docker compose --project-directory "$RUNTIME" -p "$PROJECT" -f "$RELEASE_DIR/candidate.compose.json")
  TOUCHED=1
  log 'Migrating explicitly in candidate image with both writers stopped and claims paused.'
  "${COMPOSE[@]}" run --rm --no-deps --pull never --entrypoint python -e SAU_PROCESS_ROLE=web social-auto-upload db/createTable.py >>"$DETAIL_LOG" 2>&1
  log 'Starting the matched web/worker pair (no pulls, no dependency restarts).'
  "${COMPOSE[@]}" up -d --force-recreate --pull never --no-deps social-auto-upload worker >>"$DETAIL_LOG" 2>&1
  LEGACY=0
  wait_ready paused || fail 'Paused readiness/schema/heartbeat/revision gate failed.'
fi

# Advance only a still-clean, fast-forwardable checkout; never reset/clean or
# overwrite an operator's local/unpublished changes. This updates the cron's
# scripts only after the exact candidate has passed its paused verification.
"$PYTHON" "$CHECKS" --root "$RUNTIME" clean >/dev/null
git -C "$RUNTIME" merge --ff-only "$REVISION" >>"$DETAIL_LOG" 2>&1
"$PYTHON" - "$STAGED" "$REVISION" "$IMAGE_ID" "$RELEASE_DIR" <<'PY'
import json, os, pathlib, sys, tempfile
path = pathlib.Path(sys.argv[1])
fd, temp = tempfile.mkstemp(prefix='.staged-', dir=path.parent)
with os.fdopen(fd, 'w') as stream:
    json.dump(dict(revision=sys.argv[2], image_id=sys.argv[3], release_dir=sys.argv[4]), stream)
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temp, path)
PY
if (( HOLD == 1 )); then
  log "Verified while paused. No claims released; --resume is required. Baseline: $RELEASE_DIR"
  exit 0
fi
[[ -f "$PAUSE" && ! -L "$PAUSE" && "$(cat "$PAUSE")" == "$REVISION" ]] || fail 'Pause ownership changed; refusing activation.'
ACTIVE_SINCE="$(date -u +%FT%T.%6NZ)"
rm "$PAUSE"
log 'Claims released; requiring a new non-draining heartbeat and normal /readyz.'
wait_ready active "$ACTIVE_SINCE" || fail 'Activation health failed; re-pausing and attempting image-only rollback.'
# A single successful revision marker for both manual and watcher paths.
docker tag "$IMAGE_ID" "${IMAGE_BASE}:latest"
printf '%s\n' "$REVISION" >"$LOG_DIR/.ci-watch.state.$$"
mv "$LOG_DIR/.ci-watch.state.$$" "$LOG_DIR/ci-watch.state"
rm -f "$STAGED" "$LOG_DIR/ci-watch-defer.state"
log "Deployed $REVISION: matched images, schema, readiness and non-draining heartbeat verified."
