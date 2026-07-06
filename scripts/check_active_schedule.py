#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


DEFAULT_ACTIVE_CRON = "0 1 * * *"
DEFAULT_ACTIVE_SLOT = "09:00"


def load_active_schedule(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "active_cron": str(data.get("active_cron") or DEFAULT_ACTIVE_CRON),
        "active_china_slot": str(data.get("active_china_slot") or DEFAULT_ACTIVE_SLOT),
    }


def should_run(event_name: str, event_schedule: str, active_cron: str) -> tuple[bool, str]:
    if event_name != "schedule":
        return True, "manual event"
    if not event_schedule:
        return False, "scheduled event did not include a cron expression"
    if event_schedule.strip() == active_cron.strip():
        return True, "scheduled cron matches active schedule"
    return False, f"scheduled cron {event_schedule!r} does not match active cron {active_cron!r}"


def write_github_outputs(values: dict[str, str]) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Gate scheduled runs to the configured active cron.")
    parser.add_argument("--config", type=Path, default=Path("config/active_schedule.json"))
    parser.add_argument("--event-name", default="")
    parser.add_argument("--event-schedule", default="")
    args = parser.parse_args()

    active = load_active_schedule(args.config)
    run, reason = should_run(args.event_name, args.event_schedule, active["active_cron"])
    outputs = {
        "should_run": "true" if run else "false",
        "active_cron": active["active_cron"],
        "active_china_slot": active["active_china_slot"],
        "reason": reason,
    }
    write_github_outputs(outputs)
    print(json.dumps(outputs, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
