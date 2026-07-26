from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

_STANDARD_RECORD_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class JsonlFormatter(logging.Formatter):
    """One valid, UTF-8 JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        created = datetime.fromtimestamp(record.created, tz=UTC).isoformat()
        payload: dict[str, Any] = {
            "timestamp": created,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_FIELDS and not key.startswith("_"):
                payload[key] = _json_safe(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class _CappedRotatingFileHandler(RotatingFileHandler):
    def doRollover(self) -> None:
        if self.backupCount > 0:
            super().doRollover()
            return
        if self.stream:
            self.stream.close()
            self.stream = None
        Path(self.baseFilename).write_text("", encoding=self.encoding or "utf-8")
        if not self.delay:
            self.stream = self._open()


class JsonlRotatingLogger:
    """Owns an isolated rotating JSONL logger and closes it deterministically."""

    def __init__(
        self,
        path: Path,
        *,
        level: str = "INFO",
        max_bytes: int = 20 * 1024 * 1024,
        max_files: int = 15,
        logger_name: str | None = None,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if max_files <= 0:
            raise ValueError("max_files must be positive")
        numeric_level = logging.getLevelNamesMapping().get(level.upper())
        if numeric_level is None:
            raise ValueError(f"unknown logging level: {level}")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._logger = logging.getLogger(
            logger_name or f"alga_vector.runtime.{uuid4().hex}"
        )
        self._logger.setLevel(numeric_level)
        self._logger.propagate = False
        self._handler = _CappedRotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=max_files - 1,
            encoding="utf-8",
            delay=False,
        )
        self._handler.setFormatter(JsonlFormatter())
        self._logger.handlers.clear()
        self._logger.addHandler(self._handler)
        self._closed = False
        self._lock = RLock()

    @property
    def closed(self) -> bool:
        return self._closed

    def event(
        self,
        event: str,
        message_ru: str,
        *,
        level: int = logging.INFO,
        **context: Any,
    ) -> None:
        with self._lock:
            if self._closed:
                return
            self._logger.log(
                level,
                message_ru,
                extra={"event": event, "context": _json_safe(context)},
            )

    def flush(self) -> None:
        with self._lock:
            if not self._closed:
                self._handler.flush()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._handler.flush()
            self._logger.removeHandler(self._handler)
            self._handler.close()
            self._closed = True

    def __enter__(self) -> JsonlRotatingLogger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _json_safe(model_dump(mode="json"))
    return str(value)
