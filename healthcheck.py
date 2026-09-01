#!/usr/bin/env python3
"""Exit 0 if the poll loop has run recently, else 1 (Docker HEALTHCHECK)."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
POLL_INTERVAL_HOURS = float(os.environ.get("POLL_INTERVAL_HOURS", "12"))
# Allow one missed interval plus the length of a slow Playwright run
MAX_AGE_SECONDS = POLL_INTERVAL_HOURS * 3600 * 2.5 + 600

last_run = Path(DATA_DIR) / "last_run"
if not last_run.exists():
    sys.exit(1)
if time.time() - last_run.stat().st_mtime > MAX_AGE_SECONDS:
    sys.exit(1)
sys.exit(0)
