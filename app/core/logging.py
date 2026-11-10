"""Structured JSON audit logger."""

from datetime import datetime, timezone
import json
import logging
from typing import Any


class StructuredJsonFormatter(logging.Formatter):
  def format(self, record: logging.LogRecord) -> str:
    payload: dict[str, Any] = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "level": record.levelname,
        "logger": record.name,
        "message": record.getMessage(),
    }
    if hasattr(record, "event_data") and isinstance(record.event_data, dict):
      payload.update(record.event_data)
    return json.dumps(payload, default=str)


def get_logger(name: str = "alphatrade") -> logging.Logger:
  logger = logging.getLogger(name)
  if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredJsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
  return logger
