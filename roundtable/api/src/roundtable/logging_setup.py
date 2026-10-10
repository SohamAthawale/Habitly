"""Logging configuration: one JSON object per line in containers, readable text locally."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

_STD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Formats records as JSON, including any ``extra=`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        """Render ``record`` as a single JSON line."""
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        out.update({k: v for k, v in vars(record).items() if k not in _STD_ATTRS})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Install a single stderr handler on the root logger (idempotent)."""
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter()
        if json_output
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)
