"""Opaque child-runner arguments must never be interpreted as new options."""
from __future__ import annotations

import argparse
import asyncio
from types import SimpleNamespace

import pytest

from myUtils import jobs, target_runner


class ParsedArguments(Exception):
    def __init__(self, parsed):
        super().__init__("parsed before database or worker execution")
        self.parsed = parsed


@pytest.fixture
def parsed_before_execution(monkeypatch):
    original = argparse.ArgumentParser.parse_args

    def stop_after_parse(parser, args=None, namespace=None):
        parsed = original(parser, args, namespace)
        raise ParsedArguments(parsed)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", stop_after_parse)


@pytest.mark.parametrize("token", ["plain-token", "-opaque-token", "--opaque-token", "_opaque-token", "token=tail"])
def test_target_child_preserves_opaque_lease_and_parent_budget(tmp_path, monkeypatch, parsed_before_execution, token):
    database = tmp_path / "not-created database.db"
    target = SimpleNamespace(id=42, job_id=17, account_ref="account:7", attempts=2,
                             lease_token=token, status=jobs.TARGET_BLOCKED)
    parent = SimpleNamespace(_db_path=database, _config=SimpleNamespace(retry=SimpleNamespace(max_attempts=7)))
    calls = []

    async def child(arguments, *, timeout):
        with pytest.raises(ParsedArguments) as parsed:
            target_runner.main(arguments)
        value = parsed.value.parsed
        assert value.target == 42
        assert value.lease_token == token
        assert value.db == database
        assert value.max_attempts == 7
        assert timeout == 4
        calls.append(arguments)
        return 0

    monkeypatch.setattr(target_runner, "run_child", child)
    monkeypatch.setattr(jobs, "get_target", lambda *args, **kwargs: target)
    asyncio.run(target_runner.run_isolated_target(parent, target, timeout=4))
    assert len(calls) == 1
    assert not database.exists()


@pytest.mark.parametrize("owner", ["plain-owner", "-opaque-owner", "--opaque-owner", "_opaque-owner", "owner=tail"])
def test_prep_child_preserves_opaque_owner_without_running_prep(tmp_path, monkeypatch, parsed_before_execution, owner):
    database = tmp_path / "not-created prep database.db"
    worker = SimpleNamespace(_prep_owner=owner, _db_path=database)
    campaign = SimpleNamespace(id=19)
    calls = []

    async def child(arguments, *, timeout):
        with pytest.raises(ParsedArguments) as parsed:
            target_runner.main(arguments)
        value = parsed.value.parsed
        assert value.campaign == 19
        assert value.owner == owner
        assert value.db == database
        assert timeout == 5
        calls.append(arguments)
        return 0

    monkeypatch.setattr(target_runner, "run_child", child)
    asyncio.run(target_runner.run_isolated_prep(worker, campaign, timeout=5))
    assert len(calls) == 1
    assert not database.exists()
