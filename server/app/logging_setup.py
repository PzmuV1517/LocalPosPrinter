"""
Server-side audit/operational logging.

This is the server's *own* log (who authenticated, what was ingested/printed, admin actions)
— separate from the Watchtower log stream that devices report into. Goes to stdout (so it's
captured by systemd/journald or Docker) and to a size-rotating file under ``DATA_DIR``.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

_CONFIGURED = False


def setup(data_dir: str, level: str = "INFO") -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("printhub")
    if _CONFIGURED:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S"
    )

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    try:
        os.makedirs(data_dir, exist_ok=True)
        fileh = RotatingFileHandler(
            os.path.join(data_dir, "server.log"), maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        fileh.setFormatter(fmt)
        logger.addHandler(fileh)
    except OSError:
        pass  # stdout logging still works even if the file can't be opened

    logger.propagate = False
    _CONFIGURED = True
    return logger
