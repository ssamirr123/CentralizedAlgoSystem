"""Canonical logging -- trading/common/logger.py (Stage 10)."""
from __future__ import annotations

import json
import logging
import subprocess

import pytest

from trading.common import logger as clog


@pytest.fixture
def fresh_root():
    """Snapshot/restore the root logger + the module flag around a test."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_flag = clog._root_configured
    yield root
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    clog._root_configured = saved_flag


def _our_handlers(root):
    return [h for h in root.handlers if getattr(h, "name", "") == clog._ROOT_HANDLER_NAME]


# --------------------------- one handler ---------------------------
def test_configure_logging_installs_exactly_one_named_handler(fresh_root):
    clog.configure_logging(force=True)
    ours = _our_handlers(fresh_root)
    assert len(ours) == 1
    assert isinstance(ours[0], logging.StreamHandler)
    assert isinstance(ours[0].formatter, clog.RootJsonFormatter)


def test_configure_logging_is_idempotent(fresh_root):
    clog.configure_logging(force=True)
    first = _our_handlers(fresh_root)[0]
    clog.configure_logging()  # no force
    clog.configure_logging()
    ours = _our_handlers(fresh_root)
    assert len(ours) == 1 and ours[0] is first


def test_configure_logging_removes_basicconfig_duplicate(fresh_root):
    # simulate a stray logging.basicConfig() having run first
    logging.basicConfig()
    assert any(type(h) is logging.StreamHandler for h in fresh_root.handlers)
    clog.configure_logging(force=True)
    plain = [h for h in fresh_root.handlers
             if type(h) is logging.StreamHandler and getattr(h, "name", "") != clog._ROOT_HANDLER_NAME]
    assert plain == []
    assert len(_our_handlers(fresh_root)) == 1


# --------------------------- no duplicate logs ---------------------------
def test_no_duplicate_lines(fresh_root, capsys):
    clog.configure_logging(force=True)
    logging.getLogger("some.random.module").warning("hello-once")
    out = capsys.readouterr().out
    assert out.count("hello-once") == 1


# --------------------------- structured output ---------------------------
def test_root_output_is_json(fresh_root, capsys):
    clog.configure_logging(force=True)
    logging.getLogger("x.y").error("boom")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    obj = json.loads(line)
    assert obj["level"] == "ERROR"
    assert obj["logger"] == "x.y"
    assert obj["message"] == "boom"
    assert "timestamp" in obj


def test_root_formatter_escapes_non_ascii(fresh_root, capsys):
    clog.configure_logging(force=True)
    logging.getLogger("emoji.test").warning("done ✅")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert "\\u2705" in line          # escaped, not raw
    json.loads(line)                  # still valid JSON


def test_get_logger_preserves_structured_event_logging():
    lg = clog.get_logger("comp_test", server="srv", algo="algo")
    records = []
    lg.addHandler(_ListHandler(records))
    clog.log_event(lg, logging.INFO, "ENTRY", strike=24750)
    assert records[-1].getMessage() == "ENTRY"
    assert getattr(records[-1], "event") == "ENTRY"
    assert getattr(records[-1], "details") == {"strike": 24750}
    # JsonFormatter still renders the per-component shape
    rendered = json.loads(clog.JsonFormatter("comp_test", "srv", "algo").format(records[-1]))
    assert rendered["event"] == "ENTRY" and rendered["details"] == {"strike": 24750}
    assert rendered["component"] == "comp_test"


class _ListHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def emit(self, record):
        self.sink.append(record)


# --------------------------- read-only filesystem ---------------------------
def test_get_logger_survives_readonly_filesystem(monkeypatch):
    import logging.handlers as h

    def _boom(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(h, "RotatingFileHandler", _boom)
    lg = clog.get_logger("ro_test_component", server="s")
    assert lg.handlers  # still usable -- stdout handler attached
    lg.info("still works")  # must not raise


def test_configure_logging_touches_no_filesystem(fresh_root, tmp_path, monkeypatch):
    # even if the cwd / logs dir were read-only, root config is stdout-only
    monkeypatch.setattr(clog.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    clog.configure_logging(force=True)  # must not raise
    assert len(_our_handlers(fresh_root)) == 1


# --------------------------- Telegram disabled ---------------------------
def test_telegram_service_no_crash_without_credentials(monkeypatch):
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    from alerts.telegram import AlertService

    svc = AlertService()  # must not raise
    assert svc.strategy_started("algo", "srv") is False       # logged, not sent
    assert svc.heartbeat_missing("algo", "srv", minutes=3) is False
    assert svc.day_loss_exceeded("algo", "srv", loss=-1.0, limit=1.0) is False


def test_telegram_logger_has_no_own_handlers_and_propagates():
    import alerts.telegram as tg

    assert tg.logger.propagate is True
    assert tg.logger.handlers == []          # no basicConfig / FileHandler here anymore
    assert not hasattr(tg, "LOG_FORMAT")     # removed


def test_telegram_warning_reaches_root_handler(fresh_root, capsys, monkeypatch):
    clog.configure_logging(force=True)
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    from alerts.telegram import AlertService

    AlertService().strategy_crashed("algo", "srv", reason="boom")
    out = capsys.readouterr().out
    # the "Alert NOT sent (missing credentials)" WARNING is emitted as one
    # JSON line via the root handler, no UnicodeEncodeError
    assert '"logger": "alerts.telegram"' in out
    assert "Alert NOT sent" in out


# --------------------------- git hygiene ---------------------------
def test_no_log_files_tracked_by_git():
    root = __import__("pathlib").Path(__file__).resolve().parents[2]
    tracked = subprocess.run(
        ["git", "ls-files", "*.log"], cwd=root, capture_output=True, text=True,
    ).stdout.strip()
    assert tracked == "", f"generated .log files are tracked: {tracked}"
