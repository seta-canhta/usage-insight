"""Tests for the daily poller refresh.

    python3 -m unittest importers.tests.test_daily_pull

No poller is ever executed: `daily_pull` takes a runner, and every test here
passes a fake one. A test that reached AIO would be rate-limited by the same
service the job is careful about.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import Config  # noqa: E402
from importers import daily_pull as daily  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Everything configured, so a test says what it is about rather than which
#: credential it happened to leave out.
FULL = Config(
    bitbucket_username="u", bitbucket_token="t",
    jira_url="https://jira.test", jira_username="u", jira_token="t",
    aio_token="t", jira_project_keys=("APR", "IML"))

ENV = {"BITBUCKET_REPOS": "aeriscom/wt-playwrite-taf", "AIO_PROJECTS": "IML"}

DAY = date(2026, 8, 27)
SINCE = "2026-08-01T00:00:00Z"


class Poller:
    """A stand-in for the four pollers.

    Writes the requested number of lines to whatever `--out` names, so the
    module's line counting and its .part promotion are exercised for real
    rather than mocked around.
    """

    def __init__(self, lines=1, code=0, fails=(), writes_on_failure=False):
        self.lines = lines
        self.code = code
        self.fails = tuple(fails)          # substrings of argv that should fail
        self.writes_on_failure = writes_on_failure
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        failing = any(f in joined for f in self.fails)
        code = 3 if failing else self.code
        if not failing or self.writes_on_failure:
            out = argv[argv.index("--out") + 1]
            with open(out, "w", encoding="utf-8") as handle:
                for n in range(self.lines):
                    handle.write(json.dumps({"n": n}) + "\n")
        return code, '{"msg":"aio_api_error","status":429}' if failing else ""


class PlanTests(unittest.TestCase):
    def test_plans_all_four_kinds_of_pull(self):
        names = [s.name for s in daily.plan(FULL, ENV, SINCE)]
        self.assertEqual(names, [
            "bitbucket aeriscom/wt-playwrite-taf",
            "jira APR", "jira IML",
            "aio runs IML", "aio coverage IML"])

    def test_file_names_match_the_workbook_input_cache(self):
        # `admin.py pull` writes these same names into reports/<period>/exports.
        # A dated cache directory is only a drop-in replacement if they agree.
        files = {s.filename for s in daily.plan(FULL, ENV, SINCE)}
        self.assertEqual(files, {
            "bitbucket-wt-playwrite-taf.ndjson",
            "jira-APR.ndjson", "jira-IML.ndjson",
            "aio-runs-IML.ndjson", "aio-coverage-IML.ndjson"})

    def test_project_keys_are_never_invented(self):
        # AR-1. With no JIRA_PROJECT_KEYS there is no safe default: polling
        # whatever Jira returns is how the allow-list stops being one. Nothing
        # Jira-shaped is planned at all.
        blank = Config(bitbucket_username="u", bitbucket_token="t")
        planned = daily.plan(blank, {"BITBUCKET_REPOS": "ws/repo"}, SINCE)
        self.assertEqual([s.name for s in planned], ["bitbucket ws/repo"])

    def test_every_poller_is_given_an_explicit_project(self):
        for source in daily.plan(FULL, ENV, SINCE):
            if source.needs in ("jira", "aio"):
                self.assertIn("--project", source.argv, source.name)

    def test_a_repo_without_a_workspace_is_dropped_not_guessed(self):
        planned = daily.plan(FULL, {"BITBUCKET_REPOS": "wt-playwrite-taf"}, SINCE)
        self.assertEqual([s.name for s in planned if s.needs == "bitbucket"], [])

    def test_aio_coverage_is_scoped_to_cycles_not_the_case_estate(self):
        # CLAUDE.md: metric 2 is measured per test cycle, never over the
        # estate. The two readings of the same data are 93.1% and 22.8%, and an
        # unattended job must not be what ships the misleading one.
        coverage = [s for s in daily.plan(FULL, ENV, SINCE)
                    if s.name.startswith("aio coverage")][0]
        self.assertNotIn("project", coverage.argv)
        self.assertNotIn("--coverage-scope", coverage.argv)

    def test_aio_falls_back_to_the_jira_keys_when_unset(self):
        planned = daily.plan(FULL, {"BITBUCKET_REPOS": "ws/repo"}, SINCE)
        self.assertEqual(
            [s.name for s in planned if s.name.startswith("aio runs")],
            ["aio runs APR", "aio runs IML"])


class WindowTests(unittest.TestCase):
    def test_the_default_window_is_the_first_of_the_month(self):
        # Not a rolling 30 days: the workbook is monthly, and a rolling window
        # moves the denominator of every rate by a day each morning.
        self.assertEqual(daily.window_start(DAY, None, None), SINCE)

    def test_days_and_since_both_override(self):
        self.assertEqual(daily.window_start(DAY, None, 7), "2026-08-20T00:00:00Z")
        self.assertEqual(daily.window_start(DAY, "2026-07-15", None),
                         "2026-07-15T00:00:00Z")

    def test_since_and_days_together_are_refused(self):
        with self.assertRaises(SystemExit):
            daily.window_start(DAY, "2026-07-15", 7)


class DailyPullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache = os.path.join(self.tmp, "cache")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pull(self, poller, **kwargs):
        return daily.daily_pull(DAY, self.cache, SINCE, config=FULL, env=ENV,
                                runner=poller, **kwargs)

    def day_dir(self):
        return os.path.join(self.cache, DAY.isoformat())

    def test_writes_one_ndjson_per_source_into_a_dated_directory(self):
        manifest = self.pull(Poller(lines=3))
        self.assertTrue(manifest["ok"])
        self.assertEqual(sorted(os.listdir(self.day_dir())), [
            "_status.json",
            "aio-coverage-IML.ndjson", "aio-runs-IML.ndjson",
            "bitbucket-wt-playwrite-taf.ndjson",
            "jira-APR.ndjson", "jira-IML.ndjson"])
        self.assertTrue(all(r["events"] == 3 for r in manifest["sources"]))

    def test_one_failing_source_does_not_cost_the_others(self):
        # A poller that 401s must not take the four that would have worked --
        # otherwise the daily cache is all-or-nothing on the least reliable
        # source in the set, which is AIO.
        manifest = self.pull(Poller(fails=["poll_aio.py"]))
        by_status = {r["source"]: r["status"] for r in manifest["sources"]}
        self.assertEqual(by_status["jira IML"], "ok")
        self.assertEqual(by_status["bitbucket aeriscom/wt-playwrite-taf"], "ok")
        self.assertEqual(by_status["aio runs IML"], "failed")
        self.assertFalse(manifest["ok"])

    def test_a_failed_source_leaves_no_file_at_all(self):
        # Absent is never zero. An empty NDJSON where the pull should have been
        # is indistinguishable downstream from a real day with no activity.
        self.pull(Poller(fails=["poll_aio.py"]))
        present = os.listdir(self.day_dir())
        self.assertNotIn("aio-runs-IML.ndjson", present)
        self.assertEqual([p for p in present if p.endswith(".part")], [])

    def test_a_poller_that_wrote_then_failed_leaves_nothing_promoted(self):
        self.pull(Poller(fails=["poll_aio.py"], writes_on_failure=True))
        self.assertFalse(os.path.exists(
            os.path.join(self.day_dir(), "aio-runs-IML.ndjson")))

    def test_a_clean_exit_that_wrote_nothing_is_a_failure_not_a_zero(self):
        class Silent:
            def __call__(self, argv):
                return 0, ""

        manifest = self.pull(Silent())
        self.assertTrue(all(r["status"] == "failed" for r in manifest["sources"]))

    def test_a_missing_credential_is_blocked_and_names_the_variable(self):
        no_aio = Config(bitbucket_username="u", bitbucket_token="t",
                        jira_url="https://jira.test", jira_username="u",
                        jira_token="t", jira_project_keys=("IML",))
        manifest = daily.daily_pull(DAY, self.cache, SINCE, config=no_aio,
                                    env=ENV, runner=Poller())
        aio = [r for r in manifest["sources"] if r["source"].startswith("aio")]
        self.assertTrue(aio)
        for entry in aio:
            self.assertEqual(entry["status"], "blocked")
            self.assertIn("AIO_API_TOKEN", entry["detail"])
        self.assertEqual(
            [r["status"] for r in manifest["sources"] if r["source"] == "jira IML"],
            ["ok"])

    def test_a_blocked_source_never_starts_a_process(self):
        poller = Poller()
        no_bitbucket = Config(jira_url="https://jira.test", jira_username="u",
                              jira_token="t", aio_token="t",
                              jira_project_keys=("IML",))
        daily.daily_pull(DAY, self.cache, SINCE, config=no_bitbucket, env=ENV,
                         runner=poller)
        self.assertEqual(
            [c for c in poller.calls if "pollers/poll_bitbucket.py" in c], [])

    def test_running_twice_in_a_day_does_not_refetch(self):
        # The normal case, not an edge one: something failed at 09:15 and the
        # fix is to run it again at 11:00. That re-run should cost one API
        # budget, not five.
        first = Poller(lines=2)
        self.pull(first)
        second = Poller(lines=9)
        manifest = self.pull(second)

        self.assertEqual(second.calls, [])
        self.assertTrue(all(r["status"] == "cached" for r in manifest["sources"]))
        self.assertTrue(all(r["events"] == 2 for r in manifest["sources"]))
        self.assertTrue(manifest["ok"])

    def test_a_rerun_picks_up_only_what_failed(self):
        self.pull(Poller(fails=["poll_aio.py"]))
        second = Poller()
        manifest = self.pull(second)

        attempted = {c[0] for c in second.calls}
        self.assertEqual(attempted, {"pollers/poll_aio.py"})
        self.assertTrue(manifest["ok"])

    def test_force_refetches_what_is_already_cached(self):
        self.pull(Poller(lines=2))
        manifest = self.pull(Poller(lines=5), force=True)
        self.assertTrue(all(r["events"] == 5 for r in manifest["sources"]))

    def test_a_force_run_that_fails_keeps_the_earlier_good_file(self):
        # The reason the poller writes to a .part and is renamed on success:
        # unlinking on failure instead would destroy this morning's good pull.
        self.pull(Poller(lines=4))
        self.pull(Poller(fails=["poll_jira.py"]), force=True)
        path = os.path.join(self.day_dir(), "jira-IML.ndjson")
        self.assertTrue(os.path.exists(path))
        self.assertEqual(daily._line_count(path), 4)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache = os.path.join(self.tmp, "cache")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read(self):
        with open(os.path.join(self.cache, DAY.isoformat(), "_status.json"),
                  "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_the_manifest_is_written_even_when_every_source_failed(self):
        # A directory with no manifest and a directory with a manifest full of
        # failures say different things, and only the second is readable by
        # somebody who was asleep when it ran.
        daily.daily_pull(DAY, self.cache, SINCE, config=FULL, env=ENV,
                         runner=Poller(fails=["pollers/"]))
        manifest = self.read()
        self.assertFalse(manifest["ok"])
        self.assertEqual(len(manifest["sources"]), 5)

    def test_the_manifest_is_written_when_nothing_could_be_planned(self):
        daily.daily_pull(DAY, self.cache, SINCE, config=Config(), env={},
                         runner=Poller())
        manifest = self.read()
        self.assertEqual(manifest["sources"], [])
        self.assertFalse(manifest["ok"])

    def test_the_manifest_carries_no_absolute_path(self):
        # An absolute path carries the username, and the manifest is the file a
        # reader is most likely to paste into a ticket. Asserted against the
        # real cache root without writing to it -- a suite that leaves files in
        # the repository it is testing is one nobody trusts to run twice.
        self.assertEqual(
            daily._relative(os.path.join(daily._ROOT, daily.DEFAULT_CACHE,
                                         DAY.isoformat())),
            os.path.join("reports", "cache", DAY.isoformat()))

        daily.daily_pull(DAY, self.cache, SINCE, config=FULL, env=ENV,
                         runner=Poller())
        with open(os.path.join(self.cache, DAY.isoformat(), "_status.json"),
                  "r", encoding="utf-8") as handle:
            self.assertNotIn(os.path.expanduser("~"), handle.read())

    def test_latest_points_at_the_day_and_is_relative(self):
        daily.daily_pull(DAY, self.cache, SINCE, config=FULL, env=ENV,
                         runner=Poller())
        link = os.path.join(self.cache, "latest")
        self.assertEqual(os.readlink(link), DAY.isoformat())

    def test_latest_is_repointed_by_a_later_day(self):
        daily.daily_pull(DAY, self.cache, SINCE, config=FULL, env=ENV,
                         runner=Poller())
        later = date(2026, 8, 28)
        daily.daily_pull(later, self.cache, SINCE, config=FULL, env=ENV,
                         runner=Poller())
        self.assertEqual(os.readlink(os.path.join(self.cache, "latest")),
                         later.isoformat())


class ExitCodeTests(unittest.TestCase):
    """`main` is what launchd reads, so its exit code is the whole report."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_main(self, config, env, poller, extra=()):
        # `main` reads the repos and projects from the real environment, so
        # this restores it rather than leaving the suite's later tests running
        # against whatever the developer's .env happens to say.
        saved = dict(os.environ)
        os.environ.update(env)
        try:
            # `main` pretty-prints the manifest; swallowed so a suite run stays
            # readable. The stderr log lines are one-liners and are left alone.
            with contextlib.redirect_stdout(io.StringIO()):
                return daily.main(["--date", DAY.isoformat(),
                                   "--cache", self.tmp] + list(extra),
                                  runner=poller, config=config)
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_a_clean_day_exits_zero(self):
        self.assertEqual(self.run_main(FULL, ENV, Poller()), 0)

    def test_one_failed_source_exits_one(self):
        # Non-zero, so a bad day reads as a bad day in `$?` and in the log --
        # the partial cache on disk is still usable, and the exit code is what
        # says it is partial.
        self.assertEqual(
            self.run_main(FULL, ENV, Poller(fails=["poll_aio.py"])), 1)

    def test_a_blocked_source_also_exits_non_zero(self):
        no_aio = Config(bitbucket_username="u", bitbucket_token="t",
                        jira_url="https://jira.test", jira_username="u",
                        jira_token="t", jira_project_keys=("IML",))
        self.assertEqual(self.run_main(no_aio, ENV, Poller()), 1)

    def test_nothing_planned_exits_two_not_zero(self):
        # Distinct from a failed pull and from a clean one: no project or repo
        # is configured, so AR-1 leaves nothing safe to poll. Exiting 0 here
        # would report a day of no data as a successful day.
        self.assertEqual(
            self.run_main(Config(), {"BITBUCKET_REPOS": "", "AIO_PROJECTS": ""},
                          Poller()), 2)


class ScheduleTemplateTests(unittest.TestCase):
    """The launchd template ships in git, so it must not carry a home path."""

    PATH = os.path.join(_ROOT, "tools", "launchd",
                        "vn.seta.insight.dailypull.plist.in")

    def setUp(self):
        with open(self.PATH, "r", encoding="utf-8") as handle:
            self.text = handle.read()

    def test_no_absolute_path_is_committed(self):
        self.assertNotIn(os.path.expanduser("~"), self.text)
        self.assertIn("@REPO@", self.text)
        self.assertIn("@PYTHON@", self.text)

    def test_the_label_is_not_the_client_agents(self):
        # Sharing `vn.seta.insight` would mean `insight schedule --off`
        # silently removing this job. A bootout against the wrong label already
        # deleted a live agent here once.
        self.assertIn("vn.seta.insight.dailypull", self.text)
        self.assertNotIn("<string>vn.seta.insight</string>", self.text)

    def test_it_carries_no_credential(self):
        for name in ("JIRA_API_TOKEN", "BITBUCKET_ACCESS_TOKEN",
                     "AIO_API_TOKEN", "EnvironmentVariables"):
            self.assertNotIn(name + "</key>", self.text)

    def test_it_runs_the_daily_pull_on_a_calendar(self):
        self.assertIn("importers/daily_pull.py", self.text)
        self.assertIn("StartCalendarInterval", self.text)


if __name__ == "__main__":
    unittest.main()
