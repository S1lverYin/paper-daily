import datetime as dt
import unittest

from scripts.record_schedule_probe import infer_planned_utc, parse_exact_cron_time


class ScheduleProbeTest(unittest.TestCase):
    def test_parse_exact_cron_time_accepts_minute_slots(self):
        self.assertEqual(parse_exact_cron_time("30 23 * * *"), (23, 30))
        self.assertEqual(parse_exact_cron_time("15 0 * * *"), (0, 15))

    def test_parse_exact_cron_time_rejects_ranges(self):
        self.assertIsNone(parse_exact_cron_time("*/15 23 * * *"))
        self.assertIsNone(parse_exact_cron_time("0 24 * * *"))
        self.assertIsNone(parse_exact_cron_time("60 23 * * *"))

    def test_infer_planned_utc_keeps_cron_minute(self):
        recorded_at = dt.datetime(2026, 7, 5, 23, 52, tzinfo=dt.timezone.utc)
        planned_at = infer_planned_utc("45 23 * * *", recorded_at)
        self.assertEqual(planned_at, dt.datetime(2026, 7, 5, 23, 45, tzinfo=dt.timezone.utc))


if __name__ == "__main__":
    unittest.main()
