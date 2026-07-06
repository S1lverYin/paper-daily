import unittest

from scripts.auto_adjust_daily_schedule import china_slot_to_utc_cron, filtered_records
from scripts.check_active_schedule import should_run


class ActiveScheduleTest(unittest.TestCase):
    def test_china_slot_to_utc_cron(self):
        self.assertEqual(china_slot_to_utc_cron("07:30"), "30 23 * * *")
        self.assertEqual(china_slot_to_utc_cron("08:15"), "15 0 * * *")
        self.assertEqual(china_slot_to_utc_cron("09:00"), "0 1 * * *")

    def test_should_run_manual_events(self):
        run, reason = should_run("workflow_dispatch", "", "0 1 * * *")
        self.assertTrue(run)
        self.assertIn("manual", reason)

    def test_should_run_only_active_schedule(self):
        run, _ = should_run("schedule", "30 23 * * *", "30 23 * * *")
        self.assertTrue(run)
        run, reason = should_run("schedule", "0 0 * * *", "30 23 * * *")
        self.assertFalse(run)
        self.assertIn("does not match", reason)

    def test_filtered_records_uses_candidate_slots_and_probe_start_date(self):
        records = [
            {"slot_china": "07:30", "planned_china": "2026-07-06T07:30:00+08:00"},
            {"slot_china": "07:30", "planned_china": "2026-07-07T07:30:00+08:00"},
            {"slot_china": "09:00", "planned_china": "2026-07-07T09:00:00+08:00"},
        ]
        kept = filtered_records(records, {"07:30"}, __import__("datetime").date(2026, 7, 7))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["planned_china"], "2026-07-07T07:30:00+08:00")


if __name__ == "__main__":
    unittest.main()
