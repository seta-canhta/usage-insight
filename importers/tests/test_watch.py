"""Tests for the reporting watchdog.

    python3 -m pytest importers/tests/test_watch.py -q

The failure mode this guards against is a channel everyone mutes. That happens
when the watchdog cries wolf -- alerting on a quiet afternoon, on a weekend, or
repeating the same outage every run -- so most of what is asserted here is when
it stays silent.
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importers import watch as watch_mod  # noqa: E402

TZ = timezone(timedelta(hours=7))

# 2026-08-25 is a Tuesday; 2026-08-22/23 the weekend before.
TUE = date(2026, 8, 25)
MON = date(2026, 8, 24)
FRI = date(2026, 8, 21)
THU = date(2026, 8, 20)


def config(**over):
    base = {
        "tz": TZ,
        "work_start": (9, 0),
        "work_end": (19, 0),
        "work_days": {0, 1, 2, 3, 4},
        "threshold": 3,
        "ntfy_url": "https://ntfy.sh/seta-insight",
        "ntfy_token": "",
    }
    base.update(over)
    return base


def upload(person, when):
    return {"key": "k/" + person, "email": person, "machine": "m",
            "uploaded_at": when}


class ConfigTests(unittest.TestCase):
    def test_defaults_are_seta_working_hours(self):
        loaded = watch_mod.load_config({}, use_dotenv=False)
        self.assertEqual(loaded["work_start"], (9, 0))
        self.assertEqual(loaded["work_end"], (19, 0))
        self.assertEqual(loaded["work_days"], {0, 1, 2, 3, 4})
        self.assertEqual(loaded["threshold"], 3)
        self.assertEqual(loaded["ntfy_url"], "https://ntfy.sh/seta-insight")

    def test_env_overrides_every_default(self):
        loaded = watch_mod.load_config({
            "INSIGHT_WORK_START": "08:30",
            "INSIGHT_WORK_END": "17:00",
            "INSIGHT_WORK_DAYS": "0,1,2,3,4,5",
            "INSIGHT_MISS_THRESHOLD": "5",
            "INSIGHT_NTFY_URL": "https://ntfy.example/alerts",
        }, use_dotenv=False)
        self.assertEqual(loaded["work_start"], (8, 30))
        self.assertEqual(loaded["work_end"], (17, 0))
        self.assertEqual(loaded["work_days"], {0, 1, 2, 3, 4, 5})
        self.assertEqual(loaded["threshold"], 5)
        self.assertEqual(loaded["ntfy_url"], "https://ntfy.example/alerts")

    def test_a_threshold_of_zero_is_refused(self):
        # It would alert on everyone, every run, forever.
        for bad in ("0", "-1"):
            with self.assertRaises(watch_mod.WatchError, msg=bad):
                watch_mod.load_config({"INSIGHT_MISS_THRESHOLD": bad},
                                      use_dotenv=False)

    def test_nonsense_config_is_refused_rather_than_silently_defaulted(self):
        cases = [
            {"INSIGHT_WORK_START": "25:00"},
            {"INSIGHT_WORK_DAYS": "monday"},
            {"INSIGHT_WORK_DAYS": "9"},
            {"INSIGHT_WORK_DAYS": ","},
            {"INSIGHT_TZ_OFFSET": "+99:00"},
            {"INSIGHT_MISS_THRESHOLD": "soon"},
        ]
        for bad in cases:
            with self.assertRaises(watch_mod.WatchError, msg=str(bad)):
                watch_mod.load_config(bad, use_dotenv=False)

    def test_an_empty_setting_means_unset_not_empty(self):
        # `INSIGHT_WORK_DAYS=` in a .env is how people write "not configured",
        # and it must not be read as "no working days", which would silence the
        # watchdog completely.
        loaded = watch_mod.load_config(
            {"INSIGHT_WORK_DAYS": "", "INSIGHT_WORK_START": "",
             "INSIGHT_MISS_THRESHOLD": ""}, use_dotenv=False)
        self.assertEqual(loaded["work_days"], {0, 1, 2, 3, 4})
        self.assertEqual(loaded["work_start"], (9, 0))
        self.assertEqual(loaded["threshold"], 3)

    def test_a_negative_offset_is_understood(self):
        loaded = watch_mod.load_config({"INSIGHT_TZ_OFFSET": "-05:00"},
                                       use_dotenv=False)
        self.assertEqual(loaded["tz"].utcoffset(None), timedelta(hours=-5))


class WorkingDayTests(unittest.TestCase):
    def test_today_is_never_judged(self):
        # A day is only a miss once it is over. Judging Tuesday at 10:00 would
        # flag everyone who starts at 10:30.
        days = watch_mod.working_days_before(TUE, {0, 1, 2, 3, 4}, 3)
        self.assertNotIn(TUE, days)
        self.assertEqual(days[0], MON)

    def test_weekends_are_skipped(self):
        days = watch_mod.working_days_before(TUE, {0, 1, 2, 3, 4}, 3)
        self.assertEqual(days, [MON, FRI, THU])

    def test_a_six_day_week_counts_saturday(self):
        days = watch_mod.working_days_before(TUE, {0, 1, 2, 3, 4, 5}, 3)
        self.assertEqual(days, [MON, date(2026, 8, 22), FRI])


class StreakTests(unittest.TestCase):
    def test_reporting_yesterday_is_a_streak_of_zero(self):
        self.assertEqual(watch_mod.missed_streak({MON}, [MON, FRI, THU]), 0)

    def test_silence_across_a_weekend_counts_working_days_only(self):
        # Someone last seen Thursday is two working days silent on Tuesday,
        # not four calendar days.
        self.assertEqual(watch_mod.missed_streak({THU}, [MON, FRI, THU]), 2)

    def test_never_reported_is_the_whole_window(self):
        self.assertEqual(watch_mod.missed_streak(set(), [MON, FRI, THU]), 3)


class CheckTests(unittest.TestCase):
    ROSTER = ["canh@seta-international.vn", "minh@seta-international.vn"]
    # Both established well before the window, so these tests are about
    # reporting rather than about the new-joiner grace period.
    SETTLED = {p: {"roster_since": "2026-07-01"}
               for p in ["canh@seta-international.vn", "minh@seta-international.vn",
                         "lan@seta-international.vn"]}

    def test_someone_reporting_daily_is_not_alerted(self):
        objects = [upload("canh@seta-international.vn", "2026-08-24T10:00:00+07:00"),
                   upload("minh@seta-international.vn", "2026-08-24T11:00:00+07:00")]
        report = watch_mod.check(objects, self.ROSTER, config(), TUE)
        self.assertEqual(report["to_alert"], [])
        self.assertEqual(report["silent"], [])

    def test_three_missed_working_days_alerts(self):
        objects = [upload("canh@seta-international.vn", "2026-08-24T10:00:00+07:00")]
        report = watch_mod.check(objects, self.ROSTER, config(), TUE, self.SETTLED)
        alerted = [p["person"] for p in report["to_alert"]]
        self.assertEqual(alerted, ["minh@seta-international.vn"])

    def test_two_missed_days_is_below_the_threshold(self):
        objects = [upload("canh@seta-international.vn", "2026-08-24T10:00:00+07:00"),
                   upload("minh@seta-international.vn", "2026-08-20T10:00:00+07:00")]
        report = watch_mod.check(objects, self.ROSTER, config(), TUE)
        self.assertEqual(report["to_alert"], [])
        minh = [p for p in report["people"]
                if p["person"] == "minh@seta-international.vn"][0]
        self.assertEqual(minh["missed_working_days"], 2)

    def test_a_late_night_upload_counts_for_the_local_day(self):
        # 20:30 local is outside working hours but plainly not a broken machine.
        objects = [upload("minh@seta-international.vn", "2026-08-24T20:30:00+07:00"),
                   upload("canh@seta-international.vn", "2026-08-24T10:00:00+07:00")]
        report = watch_mod.check(objects, self.ROSTER, config(), TUE)
        self.assertEqual(report["to_alert"], [])

    def test_utc_stamps_are_converted_before_the_day_is_decided(self):
        # 2026-08-24T02:00Z is 09:00 on the 24th in Vietnam. Comparing the raw
        # UTC date would still work here; comparing 23:00Z would not.
        objects = [upload("minh@seta-international.vn", "2026-08-24T16:00:00Z"),
                   upload("canh@seta-international.vn", "2026-08-24T02:00:00Z")]
        report = watch_mod.check(objects, self.ROSTER, config(), TUE)
        self.assertEqual(report["to_alert"], [])

    def test_someone_who_never_reported_is_alerted(self):
        report = watch_mod.check([], ["lan@seta-international.vn"], config(),
                                 TUE, self.SETTLED)
        self.assertEqual(len(report["to_alert"]), 1)
        self.assertIsNone(report["to_alert"][0]["last_reported"])

    def test_only_the_roster_is_judged(self):
        objects = [upload("stranger@elsewhere.com", "2026-08-24T10:00:00+07:00")]
        report = watch_mod.check(objects, ["canh@seta-international.vn"],
                                 config(), TUE)
        self.assertEqual([p["person"] for p in report["people"]],
                         ["canh@seta-international.vn"])


class AlertOnceTests(unittest.TestCase):
    def test_the_same_outage_is_not_repeated(self):
        # A fortnight-long outage is one problem, not ten messages. A channel
        # that repeats is a channel that gets muted, and a muted channel is
        # worse than none because everyone believes it is still watching.
        state = {"minh@seta-international.vn": {"alerted_streak": 3}}
        self.assertFalse(watch_mod.should_alert(
            "minh@seta-international.vn", 3, 3, state))

    def test_a_worsening_outage_speaks_again(self):
        state = {"minh@seta-international.vn": {"alerted_streak": 3}}
        self.assertTrue(watch_mod.should_alert(
            "minh@seta-international.vn", 4, 3, state))

    def test_below_the_threshold_never_alerts(self):
        self.assertFalse(watch_mod.should_alert("x", 2, 3, {}))

    def test_a_first_crossing_alerts(self):
        self.assertTrue(watch_mod.should_alert("x", 3, 3, {}))


class NotifyTests(unittest.TestCase):
    def test_posts_the_message_to_the_configured_topic(self):
        calls = []

        def post(url, body, headers, timeout):
            calls.append((url, body, headers))
            return 200

        watch_mod.notify(config(), "title here", "body here", post=post)
        url, body, headers = calls[0]
        self.assertEqual(url, "https://ntfy.sh/seta-insight")
        self.assertEqual(body, b"body here")
        self.assertEqual(headers["Title"], "title here")
        self.assertNotIn("Authorization", headers)

    def test_a_token_is_sent_when_the_topic_is_protected(self):
        calls = []

        def post(url, body, headers, timeout):
            calls.append(headers)
            return 200

        watch_mod.notify(config(ntfy_token="tk_abc"), "t", "m", post=post)
        self.assertEqual(calls[0]["Authorization"], "Bearer tk_abc")

    def test_a_unicode_message_survives_the_wire(self):
        calls = []

        def post(url, body, headers, timeout):
            calls.append(body)
            return 200

        watch_mod.notify(config(), "t", "khôi phục", post=post)
        self.assertEqual(calls[0].decode("utf-8"), "khôi phục")

    def test_the_message_says_what_to_do_about_it(self):
        text = watch_mod.message_for({
            "person": "minh@seta-international.vn",
            "missed_working_days": 4, "last_reported": "2026-08-19"})
        self.assertIn("minh@seta-international.vn", text)
        self.assertIn("4 working days", text)
        self.assertIn("schedule --status", text)


class RecentWeeksTests(unittest.TestCase):
    def test_covers_a_streak_that_crosses_a_week_boundary(self):
        weeks = watch_mod.recent_weeks(TUE, count=3)
        self.assertIn("2026-W35", weeks)
        self.assertIn("2026-W34", weeks)
        self.assertEqual(len(set(weeks)), len(weeks))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_state_round_trips(self):
        path = os.path.join(self.tmp, "state", "watch.json")
        watch_mod.save_state(path, {"a": {"alerted_streak": 3}})
        self.assertEqual(watch_mod.load_state(path), {"a": {"alerted_streak": 3}})

    def test_a_damaged_state_file_does_not_stop_the_check(self):
        # Failing here would mean the watchdog goes quiet, which is the exact
        # thing it exists to notice.
        path = os.path.join(self.tmp, "watch.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        self.assertEqual(watch_mod.load_state(path), {})


if __name__ == "__main__":
    unittest.main()


class NewJoinerTests(unittest.TestCase):
    """The first thing the watchdog does must not be to page the team about
    someone who joined yesterday."""

    def test_someone_added_today_is_not_alerted(self):
        report = watch_mod.check([], ["lan@seta-international.vn"],
                                 config(), TUE, state={})
        self.assertEqual(report["to_alert"], [])
        self.assertTrue(report["people"][0]["new_to_the_roster"])

    def test_they_are_alerted_once_the_grace_period_passes(self):
        state = {"lan@seta-international.vn": {"roster_since": "2026-08-18"}}
        report = watch_mod.check([], ["lan@seta-international.vn"],
                                 config(), TUE, state=state)
        self.assertEqual(len(report["to_alert"]), 1)

    def test_anyone_who_has_ever_uploaded_skips_the_grace_period(self):
        # They are demonstrably set up, so silence now is a real outage.
        objects = [upload("lan@seta-international.vn", "2026-08-19T10:00:00+07:00")]
        report = watch_mod.check(objects, ["lan@seta-international.vn"],
                                 config(), TUE, state={})
        self.assertFalse(report["people"][0]["new_to_the_roster"])
        self.assertEqual(len(report["to_alert"]), 1)

    def test_an_unreadable_first_sighting_does_not_silence_an_outage(self):
        state = {"lan@seta-international.vn": {"roster_since": "not a date"}}
        report = watch_mod.check([], ["lan@seta-international.vn"],
                                 config(), TUE, state=state)
        self.assertEqual(len(report["to_alert"]), 1)
