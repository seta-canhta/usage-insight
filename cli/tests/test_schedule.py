"""Tests for hourly collection.

    python3 -m pytest cli/tests/test_schedule.py -q
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schedule as schedule_mod  # noqa: E402


class Runner:
    """Stands in for launchctl/systemctl."""

    def __init__(self, *codes):
        self.codes = list(codes)
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        code = self.codes[min(len(self.calls) - 1, len(self.codes) - 1)] \
            if self.codes else 0
        return code, "" if code == 0 else "refused"


class UnitContentTests(unittest.TestCase):
    def test_launchd_runs_auto_hourly(self):
        plist = schedule_mod.launchd_plist("/opt/usage-insight/insight", "/tmp/x.log")
        self.assertIn("<string>auto</string>", plist)
        self.assertIn("<integer>3600</integer>", plist)
        self.assertIn("/opt/usage-insight/insight", plist)

    def test_launchd_runs_at_login_rather_than_an_hour_after_it(self):
        # `StartInterval` counts from load, and launchd loads a LaunchAgent at
        # each login. With RunAtLoad false a laptop opened at 09:00 collected
        # nothing on the clock until 10:00 -- one hour in ten of a working day,
        # every day. This is the launchd spelling of systemd's Persistent=true,
        # which the Linux units have always had.
        self.assertIn("<key>RunAtLoad</key><true/>",
                      schedule_mod.launchd_plist("/x/insight", "/tmp/x.log"))

    def test_both_platforms_catch_up_after_a_machine_was_off(self):
        # The property that matters is one claim in two dialects: a machine
        # that missed its window runs when it comes back. Asserted together so
        # that changing one platform's answer without the other fails here.
        plist = schedule_mod.launchd_plist("/x/insight", "/tmp/x.log")
        timer = schedule_mod.systemd_units("/x/insight")["insight-collect.timer"]
        self.assertIn("<key>RunAtLoad</key><true/>", plist)
        self.assertIn("Persistent=true", timer)

    def test_systemd_timer_catches_up_once_not_once_per_missed_hour(self):
        units = schedule_mod.systemd_units("/x/insight")
        timer = units["insight-collect.timer"]
        self.assertIn("Persistent=true", timer)
        self.assertIn("OnUnitActiveSec=3600s", timer)
        self.assertIn("ExecStart=/x/insight auto",
                      units["insight-collect.service"])

    def test_the_cron_fallback_is_a_line_someone_can_paste(self):
        line = schedule_mod.cron_line("/x/insight")
        self.assertTrue(line.startswith("0 * * * * /x/insight auto"))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.real_system = schedule_mod.system

    def tearDown(self):
        schedule_mod.system = self.real_system
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_darwin_writes_a_plist_and_loads_it(self):
        schedule_mod.system = lambda: "Darwin"
        run = Runner()
        result = schedule_mod.install("/x/insight", "/tmp/x.log",
                                      home=self.tmp, run=run)
        self.assertEqual(result["kind"], "launchd")
        self.assertTrue(os.path.exists(schedule_mod.launchd_path(self.tmp)))
        self.assertTrue(any("bootstrap" in " ".join(c) for c in run.calls))

    def test_reinstalling_replaces_rather_than_failing_on_a_loaded_label(self):
        schedule_mod.system = lambda: "Darwin"
        run = Runner()
        schedule_mod.install("/x/insight", "/tmp/x.log", home=self.tmp, run=run)
        self.assertIn("bootout", " ".join(run.calls[0]))

    def test_darwin_falls_back_to_load_when_bootstrap_is_unavailable(self):
        schedule_mod.system = lambda: "Darwin"
        run = Runner(0, 1, 0)          # bootout ok, bootstrap fails, load ok
        result = schedule_mod.install("/x/insight", "/tmp/x.log",
                                      home=self.tmp, run=run)
        self.assertEqual(result["kind"], "launchd")
        self.assertTrue(any("load" in c for call in run.calls for c in call))

    def test_a_refused_agent_raises_rather_than_reporting_success(self):
        # Reporting a schedule that is not running is the one failure that
        # produces a month of silence nobody investigates.
        schedule_mod.system = lambda: "Darwin"
        with self.assertRaises(schedule_mod.ScheduleError):
            schedule_mod.install("/x/insight", "/tmp/x.log", home=self.tmp,
                                 run=Runner(0, 1, 1))

    def test_linux_writes_both_units_and_enables_the_timer(self):
        schedule_mod.system = lambda: "Linux"
        run = Runner()
        result = schedule_mod.install("/x/insight", "/tmp/x.log",
                                      home=self.tmp, run=run)
        self.assertEqual(result["kind"], "systemd")
        directory = schedule_mod.systemd_dir(self.tmp)
        self.assertTrue(os.path.exists(
            os.path.join(directory, "insight-collect.timer")))
        self.assertTrue(any("enable" in " ".join(c) for c in run.calls))

    def test_a_machine_without_user_systemd_is_given_the_cron_line(self):
        schedule_mod.system = lambda: "Linux"
        with self.assertRaises(schedule_mod.ScheduleError) as caught:
            schedule_mod.install("/x/insight", "/tmp/x.log", home=self.tmp,
                                 run=Runner(1))
        self.assertIn("crontab -e", str(caught.exception))

    def test_an_unknown_platform_says_so_instead_of_pretending(self):
        schedule_mod.system = lambda: "Windows"
        with self.assertRaises(schedule_mod.ScheduleError) as caught:
            schedule_mod.install("/x/insight", "/tmp/x.log", home=self.tmp,
                                 run=Runner())
        self.assertIn("crontab", str(caught.exception))


class RemoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.real_system = schedule_mod.system

    def tearDown(self):
        schedule_mod.system = self.real_system
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_removing_deletes_the_agent(self):
        schedule_mod.system = lambda: "Darwin"
        schedule_mod.install("/x/insight", "/tmp/x.log", home=self.tmp,
                             run=Runner())
        self.assertTrue(schedule_mod.installed(self.tmp))
        schedule_mod.remove(home=self.tmp, run=Runner())
        self.assertFalse(schedule_mod.installed(self.tmp))

    def test_removing_something_already_off_is_success(self):
        # Someone switching this off wants it off. A non-zero exit because it
        # was already off reads as a failure to switch it off.
        schedule_mod.system = lambda: "Darwin"
        result = schedule_mod.remove(home=self.tmp, run=Runner(1, 1))
        self.assertEqual(result["removed"], [])

    def test_linux_removal_takes_both_units(self):
        schedule_mod.system = lambda: "Linux"
        schedule_mod.install("/x/insight", "/tmp/x.log", home=self.tmp,
                             run=Runner())
        result = schedule_mod.remove(home=self.tmp, run=Runner())
        self.assertEqual(len(result["removed"]), 2)


if __name__ == "__main__":
    unittest.main()


class TestScratchHomeCannotTouchTheRealMachine(unittest.TestCase):
    """A schedule is machine-global, and `SETA_INSIGHT_HOME` never scoped it.

    On 2026-08-26 a test run with a scratch home booted out and deleted the
    real user's launchd agent: `remove()` took a `home` for the plist *path*
    and then ran `launchctl bootout gui/$UID/vn.seta.insight` against the live
    session regardless. It had to be regenerated by hand. These tests hold the
    guard that stops it.
    """

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="insight-sched-")
        self.addCleanup(shutil.rmtree, self.home, True)
        os.environ["SETA_INSIGHT_HOME"] = self.home
        self.addCleanup(os.environ.pop, "SETA_INSIGHT_HOME", None)
        self.calls = []

    def _run(self, argv):
        self.calls.append(argv)
        return 0, ""

    def test_install_writes_the_file_but_runs_no_launchctl(self):
        if schedule_mod.system() != "Darwin":
            self.skipTest("launchd only")
        result = schedule_mod.install("/x/insight", "/x/log", home=self.home,
                                  run=self._run)
        self.assertTrue(os.path.exists(schedule_mod.launchd_path(self.home)))
        self.assertEqual(self.calls, [])
        self.assertFalse(result["loaded"])

    def test_remove_never_boots_out_the_live_label(self):
        if schedule_mod.system() != "Darwin":
            self.skipTest("launchd only")
        schedule_mod.install("/x/insight", "/x/log", home=self.home, run=self._run)
        self.calls = []
        schedule_mod.remove(home=self.home, run=self._run)
        # The whole point: not one launchctl invocation reaches the real
        # `gui/$UID` session, which is the only session there is.
        self.assertEqual(self.calls, [])
        self.assertFalse(os.path.exists(schedule_mod.launchd_path(self.home)))

    def test_the_generated_plist_is_still_under_test(self):
        # The guard skips the half that reaches the running system, not the
        # half worth testing.
        body = schedule_mod.launchd_plist("/x/insight", "/x/log")
        self.assertIn("<string>/x/insight</string>", body)
        self.assertIn(schedule_mod.LABEL, body)
