"""
telegram_handler.py
A logging.Handler that forwards log records to a Telegram chat.

Sending happens on a background daemon thread through a queue, so a slow or
failed Telegram API call never blocks the strategy/scheduler loop. Attach it
to the bot's logger (done automatically in logger.py when credentials are
configured) and every log.info/warning/error/... call also reaches Telegram.

Setup:
1. Create a bot with @BotFather on Telegram -> copy the bot token.
2. Message your bot once (or add it to a group), then visit
       https://api.telegram.org/bot<TOKEN>/getUpdates
   to find the chat id -> "chat":{"id": ...}
3. Set env vars (or edit config.py directly):
       BOT_TELEGRAM_TOKEN=<token>
       BOT_TELEGRAM_CHAT_ID=<chat_id>
       BOT_TELEGRAM_LOG_LEVEL=INFO   # or WARNING to reduce noise
"""

import logging
import queue
import threading
import time

import requests

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

# Telegram messages have a 4096 character hard limit.
_MAX_LEN = 4000


class TelegramHandler(logging.Handler):
    """logging.Handler subclass that ships formatted records to Telegram."""

    def __init__(self, bot_token, chat_id, level=logging.INFO,
                 rate_limit_seconds=0.3):
        super().__init__(level=level)
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.rate_limit_seconds = rate_limit_seconds
        self._queue = queue.Queue()
        self._worker = None
        if self.bot_token and self.chat_id:
            self._worker = threading.Thread(
                target=self._run, name="TelegramLogHandler", daemon=True
            )
            self._worker.start()

    def _run(self):
        url = TELEGRAM_API_URL.format(token=self.bot_token)
        while True:
            msg = self._queue.get()
            try:
                resp = requests.post(
                    url, data={"chat_id": self.chat_id, "text": msg}, timeout=5
                )
                if resp.status_code != 200:
                    # Use the stdlib root logger directly to avoid any risk of
                    # feeding back into this same handler.
                    logging.getLogger("telegram_handler").warning(
                        "Telegram send failed: %s %s", resp.status_code, resp.text
                    )
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("telegram_handler").warning(
                    "Telegram send error: %s", exc
                )
            finally:
                self._queue.task_done()
            time.sleep(self.rate_limit_seconds)

    def emit(self, record):
        if not self.bot_token or not self.chat_id:
            return
        try:
            msg = self.format(record)
            if len(msg) > _MAX_LEN:
                msg = msg[:_MAX_LEN] + "... [truncated]"
            self._queue.put_nowait(msg)
        except Exception:  # noqa: BLE001
            self.handleError(record)
