"""Existing-key signed owner receipts. No HTTP body can grant publication authority."""
from __future__ import annotations
import base64
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
from datetime import datetime
from .contracts import RepurposeError, checksum, json_bytes, loads, require, validate_manifest
from .files import private_path, read_file, write_new
from .release import ReleaseAuthority, authority_check, identities
from .store import utc

PRINCIPAL = 'teaching-manager'
NAMESPACE = 'teaching-repurpose-v2'
FIELDS = {'kind','schema_version','release_id','review_version','package_version','task_id','page_id','article_sha256',
          'canonical_url','observed_at','intent_id','website_commit','website_evidence_sha256','queue_slot_at','approval_time','source_guard_sha256'}


def ticket(data, manifest, *, now=None):
    require(isinstance(data, dict) and set(data) == FIELDS, 'owner_ticket_fields')
    require(data['kind'] == 'teaching-owner-handoff' and type(data['schema_version']) is int and data['schema_version'] == 2, 'owner_ticket_version')
    identities(data['release_id'], data['review_version'])
    require(type(data['task_id']) is int and data['task_id'] > 0 and isinstance(data['intent_id'], str) and re.fullmatch(r'[a-f0-9]{32}', data['intent_id']), 'owner_queue_intent')
    require(isinstance(data['website_commit'], str) and re.fullmatch(r'[a-f0-9]{40}', data['website_commit'])
            and checksum(data['website_evidence_sha256']) and checksum(data['source_guard_sha256']), 'owner_website_evidence')
    times = {}
    for name in ('observed_at','queue_slot_at','approval_time'):
        require(isinstance(data[name], str), 'owner_ticket_clock')
        try:
            value = datetime.fromisoformat(data[name].replace('Z', '+00:00'))
        except ValueError as exc:
            raise RepurposeError('owner_ticket_clock') from exc
        require(value.tzinfo is not None, 'owner_ticket_clock')
        times[name] = utc(value)
    require(times['approval_time'] <= times['observed_at'] and times['queue_slot_at'] <= times['observed_at'], 'owner_ticket_before_release')
    proof = ReleaseAuthority(data['release_id'], data['review_version'], data['package_version'], data['page_id'], data['article_sha256'], data['canonical_url'], times['observed_at'])
    authority_check(lambda *args: proof, data['release_id'], data['review_version'], validate_manifest(manifest), utc(now))
    return proof


def verify_signature(envelope, *, allowed_signers, private_root, runner=subprocess.run):
    """Pin signer+namespace to an existing SSH public key, never a supplied key."""
    require(isinstance(envelope, dict) and set(envelope) == {'ticket','signature'}, 'owner_signed_envelope')
    require(isinstance(envelope['signature'], str) and len(envelope['signature']) <= 16384, 'owner_signature')
    try:
        signature = base64.b64decode(envelope['signature'], validate=True)
    except ValueError as exc:
        raise RepurposeError('owner_signature') from exc
    require(signature.startswith(b'-----BEGIN SSH SIGNATURE-----') and len(signature) <= 12288, 'owner_signature')
    root = private_path(private_root); signers = private_path(allowed_signers)
    require(root.is_dir() and signers.is_file() and signers.stat().st_size <= 65536, 'owner_signers_not_configured')
    if os.name == 'posix':
        require(root.stat().st_mode & 0o077 == 0 and signers.stat().st_mode & 0o022 == 0, 'owner_receipt_permissions')
    body = json_bytes(envelope['ticket'])
    with tempfile.TemporaryDirectory(prefix='owner-verify-', dir=root) as name:
        signature_path = Path(name) / 'handoff.sig'; write_new(signature_path, signature)
        try:
            result = runner(['ssh-keygen','-Y','verify','-f',str(signers),'-I',PRINCIPAL,'-n',NAMESPACE,'-s',str(signature_path)],
                            input=body, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RepurposeError('owner_signature_verifier_unavailable') from exc
    require(result.returncode == 0, 'owner_signature_invalid')
    return envelope['ticket']


def verify_signed(envelope, manifest, *, allowed_signers, private_root, now=None, runner=subprocess.run):
    data = verify_signature(envelope, allowed_signers=allowed_signers, private_root=private_root, runner=runner)
    return ticket(data, manifest, now=now)


def receipt_path(root, release_id, review_version, package_version):
    return private_path(root) / ('owner-' + hashlib.sha256(json_bytes([release_id,review_version,package_version])).hexdigest() + '.json')


def install_signed(envelope, manifest, *, allowed_signers, private_root, now=None):
    proof = verify_signed(envelope, manifest, allowed_signers=allowed_signers, private_root=private_root, now=now)
    path = receipt_path(private_root, proof.release_id, proof.review_version, proof.package_version)
    raw = json_bytes(envelope) + b'\n'
    if path.exists():
        # One durable handoff per intent. An expired receipt is not permission to renew/repost.
        require(read_file(path, 65536) == raw, 'immutable_owner_handoff_conflict')
    else:
        write_new(path, raw)
    return proof


def file_authorizer(store, workspace, *, allowed_signers, private_root):
    def authorize(release_id, review_version, package_version):
        path = receipt_path(private_root, release_id, review_version, package_version)
        envelope = loads(read_file(path, 65536))
        with store.connect() as conn:
            row = conn.execute("SELECT manifest_path FROM repurpose_tasks WHERE workspace_id=? AND package_version=? AND state='prepared'", (workspace,package_version)).fetchone()
        require(row is not None, 'owner_package_not_prepared')
        from .files import verify_package
        manifest = verify_package(row['manifest_path'])
        return verify_signed(envelope, manifest, allowed_signers=allowed_signers, private_root=private_root)
    return authorize
