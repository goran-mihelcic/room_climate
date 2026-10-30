"""Optional debug log file for Room Climate, written off the event loop."""

from __future__ import annotations

import logging
import queue
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler

from homeassistant.core import HomeAssistant

LOGGER_NAME = "custom_components.room_climate"
LOG_FILE = "room_climate.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3
FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"


class _ForwardToRoot(logging.Handler):
    """While propagation is off, still pass the usual records on to Home Assistant's log."""

    def emit(self, record: logging.LogRecord) -> None:
        logging.getLogger().callHandlers(record)


class DebugFileLog:
    """Everything Room Climate logs, at debug level, in its own rotating file.

    Home Assistant's own log keeps getting only what it got before (normally warnings
    and errors), so debugging doesn't flood it. The file is written by a listener thread.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._listener: QueueListener | None = None
        self._file_handler: RotatingFileHandler | None = None
        self._handlers: list[logging.Handler] = []
        self._saved: tuple[int, bool] | None = None

    @property
    def path(self) -> str:
        return self.hass.config.path(LOG_FILE)

    @property
    def active(self) -> bool:
        return self._listener is not None

    def _open(self) -> RotatingFileHandler:
        handler = RotatingFileHandler(
            self.path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(FORMAT))
        return handler

    async def async_enable(self) -> None:
        if self.active:
            return
        self._file_handler = await self.hass.async_add_executor_job(self._open)
        records: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
        self._listener = QueueListener(records, self._file_handler)
        self._listener.start()

        logger = logging.getLogger(LOGGER_NAME)
        self._saved = (logger.level, logger.propagate)
        forward = _ForwardToRoot(level=logger.getEffectiveLevel())
        self._handlers = [QueueHandler(records), forward]
        for handler in self._handlers:
            logger.addHandler(handler)
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        logger.info("Debug log file started: %s", self.path)

    async def async_disable(self) -> None:
        if not self.active:
            return
        logger = logging.getLogger(LOGGER_NAME)
        logger.info("Debug log file stopped")
        for handler in self._handlers:
            logger.removeHandler(handler)
        self._handlers = []
        if self._saved is not None:
            logger.setLevel(self._saved[0])
            logger.propagate = self._saved[1]
            self._saved = None
        listener, file_handler = self._listener, self._file_handler
        self._listener = self._file_handler = None

        def _close() -> None:
            listener.stop()  # flushes the queue, joins the thread
            if file_handler is not None:
                file_handler.close()

        await self.hass.async_add_executor_job(_close)
