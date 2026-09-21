import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import profiles as profile_store


def daily(at="02:00", catch_up=True):
    p = profile_store.new_profile()
    p.update(sched_type="Daily", sched_time=at, catch_up=catch_up)
    return p


def weekly(day="Wednesday", at="02:00", catch_up=True):
    p = profile_store.new_profile()
    p.update(sched_type="Weekly", sched_day=day, sched_time=at, catch_up=catch_up)
    return p


class TestDueCalculation(unittest.TestCase):
    def test_manual_is_never_due(self):
        self.assertIsNone(profile_store.due_since(profile_store.new_profile()))
        self.assertFalse(profile_store.is_due(profile_store.new_profile(), 0))

    def test_daily_slot_is_todays_when_past(self):
        now = datetime(2026, 9, 21, 10, 0)
        self.assertEqual(profile_store.due_since(daily("02:00"), now),
                         datetime(2026, 9, 21, 2, 0))

    def test_daily_slot_is_yesterdays_when_not_yet_reached(self):
        now = datetime(2026, 9, 21, 1, 0)
        self.assertEqual(profile_store.due_since(daily("02:00"), now),
                         datetime(2026, 9, 20, 2, 0))

    def test_weekly_slot_rolls_back_to_the_named_day(self):
        now = datetime(2026, 9, 21, 10, 0)          # a Monday
        self.assertEqual(profile_store.due_since(weekly("Wednesday", "02:00"), now),
                         datetime(2026, 9, 16, 2, 0))

    def test_due_when_never_run(self):
        now = datetime(2026, 9, 21, 10, 0)
        self.assertTrue(profile_store.is_due(daily("02:00"), 0, now))

    def test_not_due_when_already_run_after_the_slot(self):
        now = datetime(2026, 9, 21, 10, 0)
        ran = datetime(2026, 9, 21, 2, 5).timestamp()
        self.assertFalse(profile_store.is_due(daily("02:00"), ran, now))

    def test_due_again_the_next_day(self):
        now = datetime(2026, 9, 22, 3, 0)
        ran = datetime(2026, 9, 21, 2, 5).timestamp()
        self.assertTrue(profile_store.is_due(daily("02:00"), ran, now))

    def test_missed_slot_is_caught_up(self):
        # The old scheduler compared HH:mm for equality on a 60s timer, so a
        # machine asleep at 02:00 simply skipped the backup for that day.
        now = datetime(2026, 9, 21, 9, 30)
        ran = datetime(2026, 9, 20, 2, 0).timestamp()
        self.assertTrue(profile_store.is_due(daily("02:00"), ran, now))

    def test_catch_up_can_be_turned_off(self):
        now = datetime(2026, 9, 21, 9, 30)
        ran = datetime(2026, 9, 20, 2, 0).timestamp()
        self.assertFalse(profile_store.is_due(daily("02:00", catch_up=False), ran, now))

    def test_catch_up_off_still_runs_inside_the_grace_window(self):
        now = datetime(2026, 9, 21, 2, 3)
        ran = datetime(2026, 9, 20, 2, 0).timestamp()
        self.assertTrue(profile_store.is_due(daily("02:00", catch_up=False), ran, now))

    def test_bad_time_string_does_not_raise(self):
        broken = profile_store.new_profile()
        broken.update(sched_type="Daily", sched_time="not a time")
        self.assertIsNone(profile_store.due_since(broken))
        self.assertFalse(profile_store.is_due(broken, 0))


class TestProfileStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="milback-cfg-")
        self._real_dir = profile_store.CONFIG_DIR
        self._real_file = profile_store.CONFIG_FILE
        profile_store.CONFIG_DIR = self.tmp
        profile_store.CONFIG_FILE = os.path.join(self.tmp, "backup_profiles.json")

    def tearDown(self):
        profile_store.CONFIG_DIR = self._real_dir
        profile_store.CONFIG_FILE = self._real_file

    def test_round_trip(self):
        data = {"Docs": profile_store.new_profile()}
        data["Docs"]["jobs"] = [{"src": "/a", "dst": "/b"}]
        profile_store.save(data)
        self.assertEqual(profile_store.load()["Docs"]["jobs"],
                         [{"src": "/a", "dst": "/b"}])

    def test_old_profiles_gain_new_defaults(self):
        legacy = {"Old": {"jobs": [], "mode": "Full Overwrite", "retries": 3}}
        with open(profile_store.CONFIG_FILE, "w") as f:
            json.dump(legacy, f)
        loaded = profile_store.load()["Old"]
        self.assertEqual(loaded["retries"], 3)
        self.assertEqual(loaded["workers"], profile_store.DEFAULTS["workers"])
        self.assertEqual(loaded["max_delete_ratio"], 0.20)

    def test_corrupt_file_does_not_crash(self):
        with open(profile_store.CONFIG_FILE, "w") as f:
            f.write("{ this is not json")
        self.assertEqual(profile_store.load(), {})

    def test_save_is_atomic(self):
        profile_store.save({"A": profile_store.new_profile()})
        leftovers = [f for f in os.listdir(self.tmp) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_settings_translation(self):
        profile = profile_store.new_profile()
        profile.update(wait=15, deep=True, workers=8, mode="Exact Sync (Mirror)")
        settings = profile_store.to_settings(profile)
        self.assertEqual(settings["wait_timeout"], 900)
        self.assertTrue(settings["deep_verify"])
        self.assertEqual(settings["workers"], 8)
        self.assertEqual(settings["backup_mode"], "Exact Sync (Mirror)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
