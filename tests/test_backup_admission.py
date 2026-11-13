"""Fresh secondary grants are admission-bound without rewriting the primary route."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_worker_route_budgets import fixture as route_fixture
from myUtils import backup_preparation, jobs, media_restore, profiles, publish_admission as admission, publish_readiness


@pytest.fixture
def verified_backup(route_fixture):
    path, account, _job = route_fixture
    account = profiles.update_account(account.id, config_patch={"_nativeCredentialError": {
        "kind": "unknown", "code": "invalid_request", "httpStatus": 400}}, db_path=path)
    settings = profiles.get_profile(account.profile_id, db_path=path).settings
    context = backup_preparation.resolve_constraints(platform=account.platform, account=account,
        settings=settings, db_path=path, admit_backup=True)
    spec = jobs.JobSpec(platform=account.platform, profile_id=account.profile_id,
        payload={"message": "Exact approved secondary-route intent.", "artifacts": []},
        targets=[(f"account:{account.id}", "text-only", datetime.now(timezone.utc) + timedelta(hours=6))])
    return path, account, settings, context["admission_verification"], spec


def prepare(case):
    path, account, _settings, verification, spec = case
    return admission.prepare_job_spec(spec, db_path=path,
        backup_verifications={f"account:{account.id}": verification})


def test_live_backup_proof_admits_without_changing_native_hold_or_sql_route(verified_backup):
    path, account, settings, proof, spec = verified_backup
    before = profiles.get_account(account.id, db_path=path).config
    assert publish_readiness.destination_readiness(account, settings, db_path=path)["reasonCode"] == "native_hold"
    native, native_snapshot = admission.prepare_job_spec(spec, db_path=path)
    assert not native_snapshot.ready
    admitted, snapshot = prepare(verified_backup)
    assert snapshot.ready and admitted.payload["message"] == spec.payload["message"]
    assert profiles.get_account(account.id, db_path=path).config == before
    assert publish_readiness.destination_readiness(account, settings, db_path=path)["reasonCode"] == "native_hold"
    from myUtils import sociamonials_fallback as sm
    session = sm._get_session()
    calls = len(session.calls)
    with jobs._transaction(path) as conn:
        admission.validate_admission_snapshot(conn, admitted, snapshot)
    assert len(session.calls) == calls, "provider call occurred in the SQL transaction"
    assert not session.calls_for("POST", "/posts", create=True)
    for secret in ("fixture-only", "password", "accessToken"):
        assert secret not in snapshot.facts_json


@pytest.mark.parametrize("forgery", ["json", "seal", "account", "proof"])
def test_backup_proof_cannot_be_forged_or_reassigned(verified_backup, forgery):
    path, account, _settings, proof, spec = verified_backup
    value = dict(vars(proof)) if forgery == "json" else replace(proof, seal="invalid") if forgery == "seal" else replace(proof, account_id=account.id + 1) if forgery == "account" else replace(proof, proof_json='{"verified":true}')
    with pytest.raises(admission.AdmissionError, match="JSON|authentic"):
        admission.prepare_job_spec(spec, db_path=path, backup_verifications={f"account:{account.id}": value})


@pytest.mark.parametrize("change", ["credential", "mapping", "profile", "disabled"])
def test_backup_selection_changes_invalidate_inflight_admission(verified_backup, monkeypatch, change):
    path, account, _settings, proof, spec = verified_backup
    admitted, snapshot = prepare(verified_backup)
    if change == "credential":
        monkeypatch.setenv("SOCIAMONIALS_API_KEY", "rotated-offline-credential")
    elif change == "mapping":
        profiles.update_account(account.id, config_patch={"sociamonials": {"network": "blsk", "profile_refs": ["foreign"]}}, db_path=path)
    elif change == "profile":
        profiles.update_profile(account.profile_id, settings={"publishing": {"requiredCapabilities": ["first_comment"]}}, db_path=path)
    else:
        profiles.update_account(account.id, enabled=False, db_path=path)
    with pytest.raises((admission.AdmissionError, ValueError)):
        with jobs._transaction(path) as conn:
            admission.validate_admission_snapshot(conn, admitted, snapshot)


def test_new_backup_dependency_hold_invalidates_inflight_admission(verified_backup):
    path, account, settings, _proof, _spec = verified_backup
    admitted, snapshot = prepare(verified_backup)
    scope = publish_readiness.destination_scope_key(account, settings) + ":backup"
    media_restore._admit(path, scope, "a" * 64, "destination_verification")
    assert media_restore._record_state(path, scope, "a" * 64, "transport")
    with pytest.raises(admission.AdmissionError, match="held"):
        with jobs._transaction(path) as conn:
            admission.validate_admission_snapshot(conn, admitted, snapshot)


def test_expired_backup_verification_never_becomes_dispatch_permission(verified_backup, monkeypatch):
    path, _account, _settings, _proof, _spec = verified_backup
    admitted, snapshot = prepare(verified_backup)
    later = datetime.now(timezone.utc) + timedelta(seconds=publish_readiness.DEFAULT_MAX_AGE_SECONDS + 1)
    monkeypatch.setattr(publish_readiness, "_now", lambda: later)
    with pytest.raises(ValueError, match="stale|expired"):
        with jobs._transaction(path) as conn:
            admission.validate_admission_snapshot(conn, admitted, snapshot)


def test_profile_only_backup_keeps_the_primary_native_configuration(route_fixture):
    from tests.test_worker_route_budgets import _move_mapping_to_profile
    _move_mapping_to_profile(route_fixture)
    path, account, _job = route_fixture
    account = profiles.update_account(account.id, config_patch={"_nativeCredentialError": {
        "kind": "unknown", "code": "invalid_request", "httpStatus": 400}}, db_path=path)
    settings = profiles.get_profile(account.profile_id, db_path=path).settings
    before = dict(account.config)
    context = backup_preparation.resolve_constraints(platform=account.platform, account=account,
        settings=settings, db_path=path, admit_backup=True)
    spec = jobs.JobSpec(platform=account.platform, profile_id=account.profile_id, payload={"message": "Approved text"},
                       targets=[(f"account:{account.id}", "text-only", None)])
    _admitted, snapshot = admission.prepare_job_spec(spec, db_path=path,
        backup_verifications={f"account:{account.id}": context["admission_verification"]})
    assert snapshot.ready
    current = profiles.get_account(account.id, db_path=path).config
    assert current == before and "sociamonials" not in current
    assert current.get("publishingProvider", "direct") == "direct"
    assert publish_readiness.destination_readiness(account, settings, db_path=path)["reasonCode"] == "native_hold"
