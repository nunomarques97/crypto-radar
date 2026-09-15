"""Logging for radar v0.8: human-readable radar.log + structured runs.jsonl.

Never logs credentials. Since the security guards abort before any network
call if KRAKEN_API_KEY/KRAKEN_SECRET are set, there is nothing credential-like
in the process to accidentally log in the first place.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import config


def configure_logging(path: str = config.TEXT_LOG_PATH) -> logging.Logger:
    logger = logging.getLogger("radar_v08")
    logger.setLevel(logging.INFO)

    if not any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == path for h in logger.handlers):
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)

    return logger


def append_run_record(record: dict[str, Any], path: str = config.RUN_LOG_PATH) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
