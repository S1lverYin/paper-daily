#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
from collections import defaultdict
from pathlib import Path


TARGET_START_MINUTE = 9 * 60
TARGET_END_MINUTE = 10 * 60


def parse_datetime(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def minute_of_day(value: dt.datetime) -> float:
    return value.hour * 60 + value.minute + value.second / 60


def hhmm(minutes: float) -> str:
    minutes = minutes % (24 * 60)
    hour = int(minutes // 60)
    minute = int(round(minutes % 60))
    if minute == 60:
        hour = (hour + 1) % 24
        minute = 0
    return f"{hour:02d}:{minute:02d}"


def load_records(input_dir: Path) -> list[dict]:
    records = []
    for path in sorted(input_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict) and data.get("slot_china") != "manual":
            data["_path"] = str(path)
            records.append(data)
    return records


def summarize(records: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        grouped[str(record.get("slot_china") or "unknown")].append(record)

    summaries = []
    for slot, items in sorted(grouped.items()):
        delays = [float(item["queue_delay_minutes"]) for item in items if item.get("queue_delay_minutes") is not None]
        recorded_times = [
            parse_datetime(str(item.get("recorded_at_china") or ""))
            for item in items
        ]
        recorded_minutes = [minute_of_day(value) for value in recorded_times if value]
        in_window = [
            minute
            for minute in recorded_minutes
            if TARGET_START_MINUTE <= minute <= TARGET_END_MINUTE
        ]
        if not delays or not recorded_minutes:
            continue
        median_recorded_minute = statistics.median(recorded_minutes)
        summaries.append(
            {
                "slot": slot,
                "samples": len(items),
                "median_queue_delay": round(statistics.median(delays), 2),
                "median_recorded_time": hhmm(median_recorded_minute),
                "median_offset_to_09": round(median_recorded_minute - TARGET_START_MINUTE, 2),
                "in_09_10_rate": round(len(in_window) / len(recorded_minutes), 3),
            }
        )
    return summaries


def choose_recommendation(summaries: list[dict], min_samples: int) -> dict | None:
    candidates = [item for item in summaries if item["samples"] >= min_samples]
    if not candidates:
        candidates = summaries
    if not candidates:
        return None

    def key(item: dict) -> tuple[int, float, float]:
        offset = float(item["median_offset_to_09"])
        in_window_penalty = 0 if 0 <= offset <= 60 else 1
        early_penalty = 0 if offset >= 0 else 1
        return in_window_penalty, early_penalty, abs(offset)

    return min(candidates, key=key)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze GitHub Actions schedule probe records.")
    parser.add_argument("--input-dir", type=Path, default=Path("web/data/schedule-probe"))
    parser.add_argument("--min-samples", type=int, default=3)
    args = parser.parse_args()

    records = load_records(args.input_dir)
    summaries = summarize(records)
    if not summaries:
        print(f"No schedule probe records found in {args.input_dir}")
        return

    print("slot  samples  median_delay  median_recorded  offset_to_09  in_09_10")
    for item in summaries:
        print(
            f"{item['slot']:>5}  "
            f"{item['samples']:>7}  "
            f"{item['median_queue_delay']:>12.2f}  "
            f"{item['median_recorded_time']:>15}  "
            f"{item['median_offset_to_09']:>12.2f}  "
            f"{item['in_09_10_rate']:>8.3f}"
        )

    recommendation = choose_recommendation(summaries, args.min_samples)
    if recommendation:
        print()
        print(
            "Recommended slot after current samples: "
            f"{recommendation['slot']} China time "
            f"(median recorded {recommendation['median_recorded_time']}, "
            f"delay {recommendation['median_queue_delay']} min)."
        )
        if float(recommendation["median_offset_to_09"]) < 0:
            print("Warning: the best current slot still records before 09:00; keep the real collector at or after 09:00.")


if __name__ == "__main__":
    main()
