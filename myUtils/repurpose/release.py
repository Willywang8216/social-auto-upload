"""Website-first owner handoff. Ordinary JSON cannot manufacture ReleaseAuthority."""
from __future__ import annotations
import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from .contracts import ACCOUNTS, RepurposeError, checksum, json_bytes, loads, require, validate_manifest
from .files import read_file, verify_package, write_new
from .store import utc


@dataclass(frozen=True)
class ReleaseAuthority:
    """Only a trusted owner adapter after fresh QueueCore/Notion/live checks."""
    release_id: str
    review_version: str
    package_version: str
    page_id: str
    article_sha256: str
    canonical_url: str
    observed_at: datetime


def authority_check(authorize, release_id, review_version, manifest, now):
    require(callable(authorize), 'teaching_owner_authorizer_required')
    proof = authorize(release_id, review_version, manifest['package_version'])
    require(type(proof) is ReleaseAuthority, 'independent_owner_authority_required')
    source = manifest['material']['source']
    require(proof.release_id == release_id and proof.review_version == review_version and checksum(review_version)
            and proof.package_version == manifest['package_version'] and proof.page_id == source['page_id']
            and proof.article_sha256 == source['article_sha256'] and proof.canonical_url == 'https://iamwillywang.com/post/' + source['slug'], 'owner_source_binding')
    age = (utc(now) - utc(proof.observed_at)).total_seconds()
    require(0 <= age <= 60, 'owner_authority_stale')
    return proof


def identities(release_id, review_version):
    require(isinstance(release_id, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', release_id), 'release_identity')
    require(checksum(review_version), 'review_version')


def delivery_filename(manifest, asset):
    return 'sfw-teaching-' + manifest['package_version'][:16] + '-' + asset['sha256'] + '-sfw' + Path(asset['name']).suffix


def stage_delivery(manifest, package_root, media_root, *, db_path):
    """Create-only exact bytes in the existing protected data/publish store."""
    root = Path(media_root)
    require(root.is_absolute() and root.is_dir() and '..' not in root.parts, 'delivery_store_missing')
    require(all(not part.is_symlink() and not (hasattr(part, 'is_junction') and part.is_junction()) for part in (root, *root.parents)), 'delivery_store_alias')
    paths = {}; lookup = {a['name']: a for a in manifest['material']['assets']}
    names = {name for d in manifest['material']['deliveries'] for name in d['asset_names']}
    from .pipeline import check_capacity
    from myUtils.media_restore import PrerequisiteUnavailable
    try:
        check_capacity(root, db_path, budget=sum(lookup[name]['size_bytes'] for name in names))
    except PrerequisiteUnavailable as exc:
        raise RepurposeError('repurpose_delivery_capacity') from exc
    for name in sorted(names):
        asset = lookup[name]; raw = read_file(Path(package_root) / name)
        require(len(raw) == asset['size_bytes'] and hashlib.sha256(raw).hexdigest() == asset['sha256'], 'delivery_asset_changed')
        path = root / delivery_filename(manifest, asset)
        if path.exists():
            require(not path.is_symlink() and path.stat().st_size == len(raw) and path.read_bytes() == raw, 'immutable_delivery_store_conflict')
        else:
            write_new(path, raw)
        paths[name] = path
    return paths


def artifacts(manifest, delivery, paths, public_base_url):
    lookup = {a['name']: a for a in manifest['material']['assets']}; result = []
    for name in delivery['asset_names']:
        asset = lookup[name]
        result.append({'artifact_kind': 'image' if delivery['format'] == 'cards' else 'video',
                       'local_path': str(paths[name]), 'sha256': asset['sha256'], 'size_bytes': asset['size_bytes'],
                       'public_url': public_base_url.rstrip('/') + '/teaching-repurpose-media/' + manifest['package_version'] + '/' + name,
                       'metadata': {'role': 'image' if delivery['format'] == 'cards' else 'video',
                                    'article_repurpose': {'package_version': manifest['package_version'], 'article_sha256': manifest['material']['source']['article_sha256'], 'delivery_id': delivery['id']}}})
    return result


def context(release_id, review_version, manifest, delivery_id):
    return {'release_id': release_id, 'review_version': review_version, 'package_version': manifest['package_version'],
            'page_id': manifest['material']['source']['page_id'], 'delivery_id': delivery_id}


def release(store, workspace, task_id, release_id, review_version, *, authorize, media_root, public_base_url, now=None):
    """Install a whole approved plan atomically. Repeated requests are read-only."""
    from myUtils import destination_pacing, jobs, publish_admission, publish_media_guard, publish_orchestrator, profiles
    from myUtils.publish_orchestrator import slot_reservation_lock
    identities(release_id, review_version)
    require(type(task_id) is int and task_id > 0, 'repurpose_task_identity')
    with store.connect() as conn:
        old = conn.execute('SELECT * FROM repurpose_releases WHERE release_id=? AND workspace_id=?', (release_id, workspace)).fetchone()
    if old:
        require(old['task_id'] == task_id and old['review_version'] == review_version, 'immutable_release_conflict')
        return inspect(store, workspace, release_id)
    require(os.environ.get('SAU_TEACHING_REPURPOSE_RELEASE_ENABLED') == '1', 'repurpose_release_disabled')
    url = urlsplit(public_base_url)
    require(url.scheme == 'https' and bool(url.hostname) and not url.username and not url.password and not url.query and not url.fragment, 'public_delivery_base')
    task = store.task(workspace, task_id)
    require(task['state'] == 'prepared', 'package_not_prepared')
    manifest = verify_package(task['manifest_path']); moment = utc(now)
    authority_check(authorize, release_id, review_version, manifest, moment)
    require(not store.delivery_rows(workspace, release_id), 'partial_release_journal')
    with store.connect() as conn:
        profile = conn.execute('SELECT workspace_id FROM profiles WHERE id=4').fetchone()
        require(profile is not None and (profile['workspace_id'] or 'legacy') == workspace, 'teaching_workspace_changed')
        account_workspace = profile['workspace_id']
        require(not conn.execute("SELECT 1 FROM repurpose_releases WHERE workspace_id=? AND (package_version=? OR json_extract(manifest_json,'$.material.source.page_id')=?)",
                                 (workspace, manifest['package_version'], manifest['material']['source']['page_id'])).fetchone(), 'released_source_requires_reconciliation_not_repost')
    paths = stage_delivery(manifest, Path(task['manifest_path']).parent, media_root, db_path=store.db_path)
    specs = []
    with slot_reservation_lock(store.db_path):
        with store.connect() as conn:
            refs = destination_pacing.account_refs(conn)
            booked = destination_pacing.booked_slots(conn, refs, now=moment)
        base = moment.replace(tzinfo=None) + timedelta(minutes=35)
        for delivery in manifest['material']['deliveries']:
            account = profiles.get_account(delivery['account_id'], db_path=store.db_path)
            require(account.profile_id == 4 and account.platform == ACCOUNTS[delivery['account_id']] and account.workspace_id == account_workspace, 'teaching_destination_changed')
            identity = destination_pacing.identity_for_ref(refs, 'account:' + str(account.id))
            taken = booked.setdefault(identity, [])
            slot = publish_orchestrator._next_free_slot(0, base, 0, {0: taken}, min_gap_minutes=30, max_per_day=3, now_fn=lambda: moment.replace(tzinfo=None))
            taken.append(slot.isoformat())
            payload = {'profileId': 4, 'message': delivery['message'], 'draft': {'message': delivery['message'], 'hashtags': []},
                       'artifacts': artifacts(manifest, delivery, paths, public_base_url), '_preserveApprovedText': True,
                       'useLlm': False, 'allowLlmFallback': False, 'articleRepurpose': context(release_id, review_version, manifest, delivery['id'])}
            publish_media_guard.validate_payload(payload, platform=account.platform, account_config=account.config)
            key = 'teaching-repurpose-' + hashlib.sha256(json_bytes([workspace, release_id, review_version, delivery['id']])).hexdigest()
            spec = jobs.JobSpec(platform=account.platform, payload=payload, profile_id=4, idempotency_key=key,
                                targets=[('account:' + str(account.id), str(paths[delivery['asset_names'][0]]), slot.isoformat())])
            normalized, snapshot = publish_admission.prepare_job_spec(spec, workspace_id=account_workspace, db_path=store.db_path)
            specs.append((delivery, normalized, snapshot))
        authority_check(authorize, release_id, review_version, manifest, utc())
        verify_package(task['manifest_path'])
        with store.connect(write=True) as conn:
            fresh = conn.execute('SELECT * FROM repurpose_tasks WHERE id=? AND workspace_id=?', (task_id, workspace)).fetchone()
            require(fresh is not None and dict(fresh) == task, 'prepared_package_changed_before_release')
            require(not conn.execute('SELECT 1 FROM repurpose_releases WHERE release_id=?', (release_id,)).fetchone(), 'release_already_claimed')
            require(not conn.execute("SELECT 1 FROM repurpose_releases WHERE workspace_id=? AND (package_version=? OR json_extract(manifest_json,'$.material.source.page_id')=?)",
                                     (workspace, manifest['package_version'], manifest['material']['source']['page_id'])).fetchone(), 'released_source_requires_reconciliation_not_repost')
            conn.execute('INSERT INTO repurpose_releases VALUES(?,?,?,?,?,?,?,?,?,?)',
                         (release_id, workspace, task_id, review_version, manifest['package_version'], json_bytes(manifest).decode(), str(Path(media_root)), public_base_url.rstrip('/'), json_bytes(account_workspace).decode(), moment.isoformat()))
            for delivery, spec, snapshot in specs:
                job = jobs.enqueue_job_in_transaction(conn, spec, workspace_id=account_workspace, admission_snapshot=snapshot, calendar_now=moment.replace(tzinfo=None))
                targets = [dict(r) for r in conn.execute('SELECT id,account_ref,workspace_id FROM publish_job_targets WHERE job_id=?', (job.id,))]
                require(len(targets) == 1 and targets[0]['account_ref'] == 'account:' + str(delivery['account_id']) and targets[0]['workspace_id'] == account_workspace, 'delivery_job_binding')
                receipt = {'delivery_id': delivery['id'], 'account_id': delivery['account_id'], 'job_id': job.id, 'target_ids': [targets[0]['id']], 'schedule_at': spec.targets[0][2]}
                conn.execute('INSERT INTO repurpose_deliveries VALUES(?,?,?,?,?,?,?,?,?)', (release_id, delivery['id'], workspace, manifest['package_version'], json_bytes(delivery).decode(), 'submitted', json_bytes(receipt).decode(), moment.isoformat(), moment.isoformat()))
    return inspect(store, workspace, release_id)


def receipt_identity(receipt, planned):
    require(isinstance(receipt, dict) and set(receipt) == {'delivery_id','account_id','job_id','target_ids','schedule_at'}, 'delivery_response_unknown')
    require(receipt['delivery_id'] == planned['id'] and type(receipt['account_id']) is int and receipt['account_id'] == planned['account_id']
            and type(receipt['job_id']) is int and receipt['job_id'] > 0 and isinstance(receipt['target_ids'], list) and len(receipt['target_ids']) == 1
            and type(receipt['target_ids'][0]) is int and receipt['target_ids'][0] > 0 and isinstance(receipt['schedule_at'], str), 'delivery_receipt_binding')


def confirmed_receipt(target, checkpoint, platform):
    from myUtils import delivery
    try:
        routes = delivery.publish_routes({'_delivery': checkpoint})
    except (ValueError, delivery.DeliveryUnknown):
        return False
    proof = checkpoint.get('provider_receipt')
    if not isinstance(proof, dict):
        return False
    provider = proof.get('provider')
    if not (target['status'] == 'succeeded' and target['delivery_state'] == 'delivered' and checkpoint.get('submission_started') is True
            and target['attempts'] > 0 and proof.get('status') == 'delivered' and isinstance(provider, str) and provider in {platform, 'sociamonials'}):
        return False
    route_key = 'sociamonials' if provider == 'sociamonials' else 'native'
    route = routes.get(route_key) or {}
    if type(route.get('attempts')) is not int or route['attempts'] < 1:
        return False
    try:
        if delivery.active_route({'_delivery': checkpoint}) != route_key:
            return False
    except delivery.DeliveryUnknown:
        return False
    identifier = proof.get('id')
    if isinstance(identifier, bool) or not isinstance(identifier, (str, int)):
        return False
    if isinstance(identifier, int) and identifier <= 0:
        return False
    identifier = str(identifier).strip()
    if not identifier or identifier.lower() in {'0','confirmed','unknown','pending','processing','true','false'}:
        return False
    delivered_at = proof.get('delivered_at')
    try:
        if not isinstance(delivered_at, str) or datetime.fromisoformat(delivered_at.replace('Z', '+00:00')).tzinfo is None:
            return False
    except ValueError:
        return False
    return True


def inspect(store, workspace, release_id):
    """Reconcile retained immutable bindings without files, regeneration or retries."""
    from myUtils import jobs
    outcomes = []
    with store.connect() as conn:
        header = conn.execute('SELECT * FROM repurpose_releases WHERE release_id=? AND workspace_id=?', (release_id, workspace)).fetchone()
        require(header is not None, 'release_not_found')
        manifest = validate_manifest(loads(header['manifest_json']))
        require(manifest['package_version'] == header['package_version'], 'release_manifest_changed')
        rows = [dict(r) for r in conn.execute('SELECT * FROM repurpose_deliveries WHERE release_id=? AND workspace_id=?', (release_id, workspace))]
        planned = manifest['material']['deliveries']; lookup = {r['delivery_id']: r for r in rows}
        require(len(rows) == len(planned) and set(lookup) == {d['id'] for d in planned}, 'partial_release_journal')
        account_workspace = loads(header['account_workspace_json']); seen_jobs = set(); seen_targets = set()
        asset_lookup = {a['name']: a for a in manifest['material']['assets']}
        paths = {name: Path(header['media_root']) / delivery_filename(manifest, asset) for name, asset in asset_lookup.items()}
        for delivery in planned:
            row = lookup[delivery['id']]
            require(row['package_version'] == manifest['package_version'] and loads(row['request_json']) == delivery and row['state'] == 'submitted', 'delivery_contract_changed')
            receipt = loads(row['receipt_json']) if row['receipt_json'] else None; receipt_identity(receipt, delivery)
            require(receipt['job_id'] not in seen_jobs and receipt['target_ids'][0] not in seen_targets, 'duplicate_delivery_binding')
            seen_jobs.add(receipt['job_id']); seen_targets.add(receipt['target_ids'][0])
            job = conn.execute('SELECT * FROM publish_jobs WHERE id=?', (receipt['job_id'],)).fetchone()
            target = conn.execute('SELECT * FROM publish_job_targets WHERE id=? AND job_id=?', (receipt['target_ids'][0], receipt['job_id'])).fetchone()
            require(job is not None and job['profile_id'] == 4 and job['platform'] == ACCOUNTS[delivery['account_id']] and job['workspace_id'] == account_workspace, 'delivery_job_changed')
            require(target is not None and target['account_ref'] == 'account:' + str(delivery['account_id']) and target['workspace_id'] == account_workspace
                    and target['file_ref'] == str(paths[delivery['asset_names'][0]]), 'delivery_target_changed')
            payload = jobs._recovery_json_object(job['payload_json'])
            require(payload.get('articleRepurpose') == context(release_id, header['review_version'], manifest, delivery['id'])
                    and payload.get('_preserveApprovedText') is True and payload.get('useLlm') is False and payload.get('allowLlmFallback') is False
                    and payload.get('message') == delivery['message'] and isinstance(payload.get('draft'), dict) and payload['draft'].get('message') == delivery['message'], 'delivery_payload_changed')
            expected = artifacts(manifest, delivery, paths, header['public_base_url']); actual = payload.get('artifacts')
            keys = ('artifact_kind','local_path','sha256','size_bytes','public_url')
            require(isinstance(actual, list) and len(actual) == len(expected) and all(isinstance(a, dict) and all(a.get(k) == b[k] for k in keys) for a,b in zip(actual,expected)), 'delivery_artifact_changed')
            checkpoint = jobs._recovery_json_object(target['checkpoint_json'])
            confirmed = confirmed_receipt(target, checkpoint, job['platform'])
            status = 'succeeded' if confirmed else 'active' if target['status'] in {'pending','running','retrying','awaiting_delivery'} else 'unknown'
            outcomes.append({**receipt, 'status': status, 'actual_delivery_confirmed': confirmed})
    complete = all(o['actual_delivery_confirmed'] for o in outcomes)
    return {'protocol_version': 2, 'release_id': release_id, 'review_version': header['review_version'], 'task_id': header['task_id'],
            'package_version': header['package_version'], 'profile_id': 4, 'receipt_valid': True, 'errors': [],
            'state': 'succeeded' if complete else 'unknown' if any(o['status'] == 'unknown' for o in outcomes) else 'active',
            'outcomes': outcomes, 'all_deliveries_confirmed': complete}
