#!/usr/bin/env python3
"""Collect papers, then send the current digest to Enterprise WeChat."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOG_DIR / f"collect-{date.today().isoformat()}.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("paper-daily")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def run(command: list[str], timeout: int) -> None:
    logger.info("Running: %s", " ".join(command))
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.stdout:
        logger.info(result.stdout.rstrip())
    if result.stderr:
        logger.warning(result.stderr.rstrip())
    result.check_returncode()


def main() -> None:
    if not os.getenv("WECHAT_WEBHOOK_URL", "").strip():
        raise SystemExit("WECHAT_WEBHOOK_URL is required")

    collector = [
        sys.executable,
        "scripts/collect_papers.py",
        "--days",
        str(env_int("LOOKBACK_DAYS", 1)),
        "--max-per-topic",
        str(env_int("MAX_PER_TOPIC", 25)),
        "--max-summaries",
        str(env_int("MAX_SUMMARIES", 20)),
        "--max-new-papers",
        str(env_int("MAX_NEW_PAPERS", 30)),
        "--max-stored-papers",
        str(env_int("MAX_STORED_PAPERS", 150)),
        "--incremental-since-last-run",
    ]

    try:
        run(collector, timeout=env_int("COLLECT_TIMEOUT_SECONDS", 900))
        run(
            [sys.executable, "scripts/wechat_bot.py"],
            timeout=env_int("WECHAT_TIMEOUT_SECONDS", 180),
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.exception("Collection or push failed: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
