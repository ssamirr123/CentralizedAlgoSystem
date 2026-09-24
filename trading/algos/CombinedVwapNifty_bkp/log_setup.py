"""
Logging bootstrap - identical pattern to the reference project's
`log_setup.py`: mirrors all print()/stderr output to logs/<date>/app.log,
plus optional Telegram forwarding.

Usage:
    import log_setup   # must be the FIRST import in main.py
    log_setup.init()
"""
import os
import sys
import atexit
import threading
import queue
from datetime import datetime

try:
    import requests
except Exception:
    requests = None

_LOG_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')

_TG_MAX_CHARS = 3800
_TG_FLUSH_INTERVAL = 2.0
_TG_API = 'https://api.telegram.org/bot{token}/sendMessage'

_tg_queue: "queue.Queue[str]" = queue.Queue()
_tg_thread = None


def _tg_config():
    try:
        import config
        if not getattr(config, 'telegram_enabled', False):
            return None
        token = getattr(config, 'telegram_bot_token', '') or ''
        chat_id = getattr(config, 'telegram_chat_id', '') or ''
        if token and chat_id:
            return token, str(chat_id)
    except Exception:
        pass
    return None


def _tg_send(token, chat_id, text):
    if requests is None:
        return
    try:
        requests.post(
            _TG_API.format(token=token),
            data={'chat_id': chat_id, 'text': text, 'disable_web_page_preview': True},
            timeout=10,
        )
    except Exception:
        pass


def _tg_worker(token, chat_id):
    import time as _time
    while True:
        line = _tg_queue.get()
        if line is None:
            break
        batch = [line]
        size = len(line)
        end = _time.time() + _TG_FLUSH_INTERVAL
        while True:
            timeout = end - _time.time()
            if timeout <= 0:
                break
            try:
                nxt = _tg_queue.get(timeout=timeout)
            except queue.Empty:
                break
            if nxt is None:
                if batch:
                    _tg_send(token, chat_id, ''.join(batch))
                return
            if size + len(nxt) > _TG_MAX_CHARS:
                _tg_send(token, chat_id, ''.join(batch))
                batch = [nxt]
                size = len(nxt)
            else:
                batch.append(nxt)
                size += len(nxt)
        if batch:
            _tg_send(token, chat_id, ''.join(batch))


def _tg_enqueue(message):
    if _tg_thread is None:
        return
    if message and message.strip():
        try:
            _tg_queue.put_nowait(message)
        except Exception:
            pass


class _Tee:
    def __init__(self, console):
        self._console = console

    def write(self, message):
        self._console.write(message)
        self._console.flush()
        try:
            log_path = _todays_logfile()
            with open(log_path, 'a', encoding='utf-8') as fh:
                fh.write(message)
        except Exception:
            pass
        _tg_enqueue(message)

    def flush(self):
        self._console.flush()

    def isatty(self):
        return getattr(self._console, 'isatty', lambda: False)()

    def fileno(self):
        return self._console.fileno()


def _todays_logfile():
    day_dir = os.path.join(_LOG_ROOT, datetime.now().strftime('%Y-%m-%d'))
    os.makedirs(day_dir, exist_ok=True)
    return os.path.join(day_dir, 'app.log')


_initialised = False


def init():
    global _initialised, _tg_thread
    if _initialised:
        return
    banner = f"\n===== Session started {datetime.now():%Y-%m-%d %H:%M:%S} =====\n"
    try:
        with open(_todays_logfile(), 'a', encoding='utf-8') as fh:
            fh.write(banner)
    except Exception:
        pass

    cfg = _tg_config()
    if cfg is not None:
        token, chat_id = cfg
        _tg_thread = threading.Thread(target=_tg_worker, args=(token, chat_id), daemon=True)
        _tg_thread.start()
        atexit.register(_shutdown)

    sys.stdout = _Tee(sys.stdout)
    sys.stderr = _Tee(sys.stderr)
    _initialised = True


def _shutdown():
    if _tg_thread is not None:
        try:
            _tg_queue.put_nowait(None)
            _tg_thread.join(timeout=_TG_FLUSH_INTERVAL + 5)
        except Exception:
            pass
