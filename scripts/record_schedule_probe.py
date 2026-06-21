#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo


CHINA_TZ = ZoneInfo("Asia/Shanghai")
UTC = dt.timezone.utc


def parse_exact_cron_hour(schedule: str) -> int | None:
    parts = schedule.split()
    if len(parts) != 5:
        return None
    minute, hour = parts[0], parts[1]
    if minute != "0" or not re.fullmatch(r"\d{1,2}", hour):
        return None
    parsed = int(hour)
    return parsed if 0 <= parsed <= 23 else None


def infer_planned_utc(schedule: str, recorded_at_utc: dt.datetime) -> dt.datetime | None:
    hour = parse_exact_cron_hour(schedule)
    if hour is None:
        return None

    candidates = []
    for day_offset in (-1, 0, 1):
        candidate_date = recorded_at_utc.date() + dt.timedelta(days=day_offset)
        candidates.append(dt.datetime.combine(candidate_date, dt.time(hour=hour, tzinfo=UTC)))

    past_candidates = [candidate for candidate in candidates if candidate <= recorded_at_utc + dt.timedelta(minutes=1)]
    if past_candidates:
        return max(past_candidates)
    return min(candidates, key=lambda candidate: abs((candidate - recorded_at_utc).total_seconds()))


def safe_filename(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "-", value).strip("-") or "manual"


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Record GitHub Actions schedule delay for later analysis.")
    parser.add_argument("--output-dir", type=Path, default=Path("web/data/schedule-probe"))
    parser.add_argument("--schedule", default="")
    parser.add_argument("--event-name", default="")
    parser.add_argument("--workflow", default="")
    parser.add_argument("--repository", default="")
    parser.add_argument("--server-url", default="https://github.com")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--run-attempt", default="")
    parser.add_argument("--actor", default="")
    args = parser.parse_args()

    recorded_at_utc = dt.datetime.now(UTC)
    recorded_at_china = recorded_at_utc.astimezone(CHINA_TZ)
    planned_utc = infer_planned_utc(args.schedule, recorded_at_utc) if args.schedule else None
    planned_china = planned_utc.astimezone(CHINA_TZ) if planned_utc else None
    queue_delay_minutes = (
        round((recorded_at_utc - planned_utc).total_seconds() / 60, 2)
        if planned_utc
        else None
    )
    run_url = (
        f"{args.server_url.rstrip('/')}/{args.repository}/actions/runs/{args.run_id}"
        if args.repository and args.run_id
        else ""
    )
    slot_china = planned_china.strftime("%H:%M") if planned_china else "manual"
    record = {
        "version": 1,
        "event_name": args.event_name,
        "workflow": args.workflow,
        "repository": args.repository,
        "schedule": args.schedule,
        "slot_china": slot_china,
        "planned_utc": planned_utc.isoformat() if planned_utc else "",
        "planned_china": planned_china.isoformat() if planned_china else "",
        "recorded_at_utc": recorded_at_utc.isoformat(),
        "recorded_at_china": recorded_at_china.isoformat(),
        "queue_delay_minutes": queue_delay_minutes,
        "run_id": args.run_id,
        "run_attempt": args.run_attempt,
        "run_url": run_url,
        "actor": args.actor,
        "note": "recorded_at includes runner startup and checkout time, so it is a conservative delay estimate",
    }

    date_part = recorded_at_china.strftime("%Y%m%d")
    run_part = safe_filename(args.run_id or recorded_at_utc.strftime("%H%M%S"))
    slot_part = safe_filename(slot_china.replace(":", ""))
    output_path = args.output_dir / f"{date_part}-{slot_part}-{run_part}.json"
    write_json(output_path, record)
    print(f"Wrote schedule probe record to {output_path}")
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
