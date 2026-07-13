#!/usr/bin/env python3
"""Shared diagnostic logging for wechat-narrator components.

- One file per component under ~/.wechat-narrator/logs/<name>.log
- Daily rotation at midnight, keep 30 days (TimedRotatingFileHandler backupCount=30)
- Format carries full date + milliseconds (fixes the "timestamp has no date" confusion
  that let yesterday's lines leak into today's greps during 2026-07 debugging)
- On import, sweep logs/ and delete any file (incl. forensic screenshots) older than 30d,
  a backstop for renamed/orphaned files the rotator won't catch.

Business log lines (the human-facing group2email.log etc.) are untouched; this is the
separate DEBUG-level diagnostic layer. Components stay usable if this import fails.
"""
import logging
import os
import time
from logging.handlers import TimedRotatingFileHandler

LOG_DIR = os.path.join(os.path.expanduser("~"), ".wechat-narrator", "logs")
RETAIN_DAYS = 30
_loggers = {}
_swept = False


def _sweep():
    global _swept
    if _swept:
        return
    _swept = True
    cutoff = time.time() - RETAIN_DAYS * 86400
    try:
        for fn in os.listdir(LOG_DIR):
            p = os.path.join(LOG_DIR, fn)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


def get_logger(name):
    """Component logger writing to logs/<name>.log (30-day daily rotation)."""
    if name in _loggers:
        return _loggers[name]
    os.makedirs(LOG_DIR, exist_ok=True)
    _sweep()
    lg = logging.getLogger(f"wn.{name}")
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    if not lg.handlers:
        try:
            h = TimedRotatingFileHandler(
                os.path.join(LOG_DIR, f"{name}.log"),
                when="midnight", backupCount=RETAIN_DAYS, encoding="utf-8")
            fmt = logging.Formatter(
                "%(asctime)s.%(msecs)03d [%(levelname)-5s] [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S")
            h.setFormatter(fmt)
            lg.addHandler(h)
        except Exception:
            lg.addHandler(logging.NullHandler())
    _loggers[name] = lg
    return lg
