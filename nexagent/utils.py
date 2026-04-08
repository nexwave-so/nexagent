from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        if record.exc_info:
            log["exc"] = self.formatException(record.exc_info)
        for key in ("symbol", "type", "direction", "strength", "confidence",
                    "side", "size_usd", "price", "reason", "pnl_usd",
                    "exchange_id", "channel", "event_type"):
            if hasattr(record, key):
                log[key] = getattr(record, key)
        return json.dumps(log)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers.clear()
    root.addHandler(handler)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def fmt_usd(amount: float) -> str:
    sign = "+" if amount >= 0 else ""
    return f"{sign}${amount:,.2f}"


def fmt_pct(pct: float) -> str:
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"


def mask_key(key: str, visible: int = 6) -> str:
    if not key:
        return "(not set)"
    return f"{key[:visible]}..."
