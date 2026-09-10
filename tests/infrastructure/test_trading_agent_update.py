"""trading_agent.py's update_algo() self-healing against stray untracked
files that collide with an incoming path -- this is a real incident:
a leftover untracked file on the strategy box silently blocked every
scheduled `git pull` for days, with no visible error anywhere an
operator would normally look. These tests use real git repos (a scratch
"origin" + a scratch "deploy target" clone) rather than mocks, since the
whole point is verifying actual git plumbing behavior."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from trading.agent import trading_agent


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com",
             "PATH": __import__("os").environ.get("PATH", "")},
    )


@pytest.fixture
def repo_pair(tmp_path):
    """A bare 'origin' + a clone (the simulated deploy target), one commit in."""
    origin = tmp_path / "origin.git"
    clone = tmp_path / "clone"
    _run_git(tmp_path, "init", "--bare", str(origin))

    seed = tmp_path / "seed"
    seed.mkdir()
    _run_git(seed, "init")
    (seed / "README.md").write_text("hello\n")
    _run_git(seed, "add", "README.md")
    _run_git(seed, "commit", "-m", "seed")
    _run_git(seed, "remote", "add", "origin", str(origin))
    _run_git(seed, "push", "origin", "HEAD:refs/heads/main")

    _run_git(tmp_path, "clone", str(origin), str(clone))
    _run_git(clone, "checkout", "-b", "main", "origin/main")
    return seed, clone


def _add_and_push(seed: Path, rel_path: str, content: str):
    p = seed / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _run_git(seed, "add", rel_path)
    _run_git(seed, "commit", "-m", f"add {rel_path}")
    _run_git(seed, "push", "origin", "HEAD:refs/heads/main")


def test_clean_pull_no_conflict(monkeypatch, repo_pair):
    seed, clone = repo_pair
    _add_and_push(seed, "new_file.txt", "content\n")
    monkeypatch.setattr(trading_agent, "PROJECT_ROOT", clone)

    result = trading_agent._pull_ff_only_with_conflict_recovery()

    assert result == {"self_healed": False, "cleared_paths": []}
    assert (clone / "new_file.txt").read_text() == "content\n"


def test_identical_untracked_file_is_cleared_and_pull_succeeds(monkeypatch, repo_pair):
    seed, clone = repo_pair
    _add_and_push(seed, "alembic/versions/dupe.py", "# same content\n")
    # simulate the real incident: an untracked copy already sitting in the
    # working tree, byte-identical to what origin is about to add.
    (clone / "alembic" / "versions").mkdir(parents=True)
    (clone / "alembic" / "versions" / "dupe.py").write_text("# same content\n")
    monkeypatch.setattr(trading_agent, "PROJECT_ROOT", clone)

    result = trading_agent._pull_ff_only_with_conflict_recovery()

    assert result["self_healed"] is True
    assert result["cleared_paths"] == ["alembic/versions/dupe.py"]
    assert (clone / "alembic" / "versions" / "dupe.py").read_text() == "# same content\n"
    # HEAD actually advanced -- the pull genuinely succeeded, not a no-op.
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(clone),
                           capture_output=True, text=True, check=True).stdout.strip()
    origin_head = subprocess.run(["git", "rev-parse", "origin/main"], cwd=str(clone),
                                  capture_output=True, text=True, check=True).stdout.strip()
    assert head == origin_head


def test_differing_untracked_file_is_left_alone_and_pull_fails(monkeypatch, repo_pair):
    seed, clone = repo_pair
    _add_and_push(seed, "alembic/versions/dupe.py", "# incoming content\n")
    (clone / "alembic" / "versions").mkdir(parents=True)
    (clone / "alembic" / "versions" / "dupe.py").write_text("# a REAL local file, different content\n")
    monkeypatch.setattr(trading_agent, "PROJECT_ROOT", clone)

    with pytest.raises(subprocess.CalledProcessError):
        trading_agent._pull_ff_only_with_conflict_recovery()

    # never touched -- the differing local file must survive untouched.
    assert (clone / "alembic" / "versions" / "dupe.py").read_text() == \
        "# a REAL local file, different content\n"


def test_update_algo_reports_self_heal_in_result(monkeypatch, repo_pair, tmp_path):
    seed, clone = repo_pair
    _add_and_push(seed, "trading/algos/fake_algo/main.py", "print('hi')\n")
    (clone / "trading" / "algos" / "fake_algo").mkdir(parents=True)
    (clone / "trading" / "algos" / "fake_algo" / "main.py").write_text("print('hi')\n")
    monkeypatch.setattr(trading_agent, "PROJECT_ROOT", clone)
    monkeypatch.setattr(trading_agent, "ALGOS_DIR", clone / "trading" / "algos")
    monkeypatch.setattr(trading_agent, "read_pid_file", lambda algo_name: None)
    monkeypatch.setattr(trading_agent, "_write_state", lambda *a, **kw: None)

    result = trading_agent.update_algo("fake_algo")

    assert result["updated"] is True
    assert "warning" in result
    assert "trading/algos/fake_algo/main.py" in result["warning"] or \
        "trading\\algos\\fake_algo\\main.py" in result["warning"]
