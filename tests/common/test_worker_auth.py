"""Phase 16.12: trading/common/worker_auth.py -- WorkerAuthRegistry in
isolation. Proves fail-closed defaults, from_env() parsing, provision/
revoke, and that verify() never leaks whether a worker_id is known."""
from __future__ import annotations

from trading.common.worker_auth import WorkerAuthRegistry


def test_unconfigured_worker_never_authenticates():
    registry = WorkerAuthRegistry()
    assert registry.verify("w1", "anything") is False


def test_provisioned_worker_authenticates_with_correct_secret():
    registry = WorkerAuthRegistry()
    registry.provision("w1", "s3cr3t")
    assert registry.verify("w1", "s3cr3t") is True


def test_wrong_secret_is_rejected():
    registry = WorkerAuthRegistry()
    registry.provision("w1", "s3cr3t")
    assert registry.verify("w1", "wrong") is False


def test_empty_or_none_presented_secret_is_rejected():
    registry = WorkerAuthRegistry()
    registry.provision("w1", "s3cr3t")
    assert registry.verify("w1", "") is False
    assert registry.verify("w1", None) is False


def test_one_workers_secret_does_not_authenticate_another_worker_id():
    registry = WorkerAuthRegistry()
    registry.provision("w1", "s3cr3t")
    registry.provision("w2", "other")
    assert registry.verify("w2", "s3cr3t") is False


def test_revoke_removes_access():
    registry = WorkerAuthRegistry()
    registry.provision("w1", "s3cr3t")
    registry.revoke("w1")
    assert registry.verify("w1", "s3cr3t") is False


def test_is_provisioned_never_reveals_the_secret():
    registry = WorkerAuthRegistry()
    assert registry.is_provisioned("w1") is False
    registry.provision("w1", "s3cr3t")
    assert registry.is_provisioned("w1") is True


def test_from_env_parses_comma_separated_pairs():
    registry = WorkerAuthRegistry.from_env("worker-cvn:abc123,worker-ds:def456, worker-vh:ghi789 ")
    assert registry.verify("worker-cvn", "abc123") is True
    assert registry.verify("worker-ds", "def456") is True
    assert registry.verify("worker-vh", "ghi789") is True
    assert registry.verify("worker-cvn", "def456") is False


def test_from_env_empty_string_provisions_nothing():
    registry = WorkerAuthRegistry.from_env("")
    assert registry.is_provisioned("w1") is False


def test_from_env_ignores_malformed_pairs():
    registry = WorkerAuthRegistry.from_env("garbage-no-colon,worker-a:secret-a,:no-worker-id,worker-b:")
    assert registry.verify("worker-a", "secret-a") is True
    assert registry.is_provisioned("worker-b") is False  # empty secret half is skipped


def test_provisioning_a_second_secret_replaces_the_first():
    registry = WorkerAuthRegistry()
    registry.provision("w1", "old-secret")
    registry.provision("w1", "new-secret")
    assert registry.verify("w1", "old-secret") is False
    assert registry.verify("w1", "new-secret") is True
