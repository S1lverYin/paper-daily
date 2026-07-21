#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from scripts.analyze_schedule_probe import choose_recommendation, load_records, summarize
except ModuleNotFoundError:  # pragma: no cover - used when executed as a script
    from analyze_schedule_probe import choose_recommendation, load_records, summarize


CHINA_TZ = ZoneInfo("Asia/Shanghai")
UTC = dt.timezone.utc
DEFAULT_CANDIDATE_SLOTS = ["07:00", "07:15", "07:30", "07:45", "08:00"]


def parse_china_date(value: str) -> dt.date | None:
    if not value:
        return None
    return dt.date.fromisoformat(value)


def parse_slot(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{2}):(\d{2})", value)
    if not match:
        raise ValueError(f"Invalid China slot: {value}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid China slot: {value}")
    return hour, minute


def china_slot_to_utc_cron(slot: str) -> str:
    hour, minute = parse_slot(slot)
    utc_minutes = (hour * 60 + minute - 8 * 60) % (24 * 60)
    return f"{utc_minutes % 60} {utc_minutes // 60} * * *"


def record_china_date(record: dict) -> dt.date | None:
    value = str(record.get("planned_china") or record.get("recorded_at_china") or "")
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(CHINA_TZ).date()
    except ValueError:
        return None


def filtered_records(
    records: list[dict],
    candidate_slots: set[str],
    probe_start_date: dt.date | None,
) -> list[dict]:
    result = []
    for record in records:
        if str(record.get("slot_china") or "") not in candidate_slots:
            continue
        china_date = record_china_date(record)
        if probe_start_date and (not china_date or china_date < probe_start_date):
            continue
        result.append(record)
    return result


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_schedule_payload(
    current: dict,
    selected: dict,
    candidate_slots: list[str],
    min_samples: int,
    not_before_date: dt.date | None,
    probe_start_date: dt.date | None,
) -> dict:
    selected_slot = str(selected["slot"])
    now_utc = dt.datetime.now(UTC).isoformat()
    return {
        "version": 1,
        "active_china_slot": selected_slot,
        "active_cron": china_slot_to_utc_cron(selected_slot),
        "mode": "auto-adjusted",
        "updated_at": now_utc,
        "updated_by": "auto_adjust_daily_schedule",
        "previous_active_china_slot": current.get("active_china_slot", ""),
        "previous_active_cron": current.get("active_cron", ""),
        "decision": {
            "not_before_china_date": not_before_date.isoformat() if not_before_date else "",
            "probe_start_china_date": probe_start_date.isoformat() if probe_start_date else "",
            "min_samples": min_samples,
            "candidate_slots": candidate_slots,
            "selected": selected,
        },
        "note": "Daily workflow has multiple candidate schedules; only this active cron actually collects and pushes papers.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Automatically adjust the active Paper Daily schedule from probe records.")
    parser.add_argument("--probe-dir", type=Path, default=Path("web/data/schedule-probe"))
    parser.add_argument("--config", type=Path, default=Path("config/active_schedule.json"))
    parser.add_argument("--candidate-slots", default=",".join(DEFAULT_CANDIDATE_SLOTS))
    parser.add_argument("--min-samples", type=int, default=7)
    parser.add_argument("--not-before-china-date", default="")
    parser.add_argument("--probe-start-china-date", default="")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    candidate_slots = [slot.strip() for slot in args.candidate_slots.split(",") if slot.strip()]
    candidate_slot_set = set(candidate_slots)
    not_before_date = parse_china_date(args.not_before_china_date)
    probe_start_date = parse_china_date(args.probe_start_china_date)
    today_china = dt.datetime.now(CHINA_TZ).date()

    if not args.force and not_before_date and today_china < not_before_date:
        print(f"Not adjusting before {not_before_date}; today is {today_china}.")
        return

    records = filtered_records(load_records(args.probe_dir), candidate_slot_set, probe_start_date)
    summaries = summarize(records)
    recommendation = choose_recommendation(summaries, args.min_samples)
    if not recommendation:
        print("No recommendation: not enough valid schedule probe records.")
        return
    if int(recommendation.get("samples") or 0) < args.min_samples:
        print(
            f"No adjustment: best slot {recommendation['slot']} has "
            f"{recommendation['samples']} samples, need {args.min_samples}."
        )
        return

    current = load_json(args.config)
    selected_cron = china_slot_to_utc_cron(str(recommendation["slot"]))
    if current.get("active_cron") == selected_cron and current.get("active_china_slot") == recommendation["slot"]:
        print(f"No adjustment needed: active schedule is already {recommendation['slot']} ({selected_cron}).")
        return

    payload = build_schedule_payload(
        current,
        recommendation,
        candidate_slots,
        args.min_samples,
        not_before_date,
        probe_start_date,
    )
    write_json(args.config, payload)
    print(
        "Adjusted active schedule to "
        f"{payload['active_china_slot']} China time ({payload['active_cron']})."
    )
    print(json.dumps(payload["decision"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
