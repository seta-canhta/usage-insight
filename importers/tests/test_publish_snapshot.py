"""Tests for the /insights snapshot publisher.

    python3 -m unittest importers.tests.test_publish_snapshot

Nothing is built and nothing is pushed: `publish` takes a runner, and every
test here passes a fake one. A test that ran the real builder would need a
month of cached NDJSON; a test that ran the real `scp` would put a file on the
endpoint.

Every name and account id below is invented, for the same reason the real
config is gitignored.
"""

import datetime
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importers import publish_snapshot as publish  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DAY = datetime.date(2026, 8, 27)          # a Thursday, in 2026-W35
SINCE = "2026-08-01T00:00:00Z"

CONFIG = {
    "destination": "example-host:/srv/insight/insights/insights.json",
    "weeks_back": 5,
    "prices": {"some-model": "3.0/15.0"},
    "members": [
        {"name": "Alex Example", "account_id": "0000000000000000000000aa",
         "short": "Alex", "role": "runs the tests", "pronouns": "she"},
        {"name": "Sam Sample", "account_id": "0000000000000000000000bb"},
    ],
}

#: What `report/dashboard_data.py` puts on disk, reduced to the keys the
#: publisher insists on before it will ship anything.
SNAPSHOT = {"schema": 1, "generated_at": "2026-08-27T02:15:00Z",
            "people": [], "metrics": [], "weeks": []}


class Fake:
    """A stand-in for the builder and for scp/ssh.

    Writes the snapshot to whatever `--out` names, so the publisher's own parse
    check runs against a real file rather than a mock.
    """

    def __init__(self, code=0, fails=(), payload=SNAPSHOT):
        self.code = code
        self.fails = tuple(fails)          # substrings of argv that should fail
        self.payload = payload
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if any(f in joined for f in self.fails):
            return 3, "connection refused"
        if "--out" in argv and self.payload is not None:
            with open(argv[argv.index("--out") + 1], "w",
                      encoding="utf-8") as handle:
                json.dump(self.payload, handle)
        return self.code, ""


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, payload):
        path = os.path.join(self.tmp, "dashboard.json")
        with open(path, "w", encoding="utf-8") as handle:
            if isinstance(payload, str):
                handle.write(payload)
            else:
                json.dump(payload, handle)
        return path

    def error(self, payload):
        with self.assertRaises(publish.ConfigError) as caught:
            publish.load_config(self.write(payload))
        return str(caught.exception)

    def test_a_complete_config_loads(self):
        got = publish.load_config(self.write(CONFIG))
        self.assertEqual(got["destination"], CONFIG["destination"])
        self.assertEqual([m["name"] for m in got["members"]],
                         ["Alex Example", "Sam Sample"])

    def test_a_comment_key_is_left_alone(self):
        # The example file explains itself in a `_comment`, because JSON has no
        # comments and an operator editing this needs the reasoning in front of
        # them. An unknown key at the top level is therefore not an error.
        publish.load_config(self.write(dict(CONFIG, _comment=["hello"])))

    def test_each_missing_key_is_named(self):
        for key in ("destination", "members", "prices"):
            missing = {k: v for k, v in CONFIG.items() if k != key}
            self.assertIn(repr(key), self.error(missing))

    def test_a_member_missing_an_account_id_is_named_by_position(self):
        # By position and not by name: the message goes to a log, and the log
        # is not the place to put somebody's name.
        broken = json.loads(json.dumps(CONFIG))
        del broken["members"][1]["account_id"]
        message = self.error(broken)
        self.assertIn("members[1]", message)
        self.assertIn("account_id", message)
        self.assertNotIn("Sam Sample", message)

    def test_a_misspelt_member_key_is_refused_not_dropped(self):
        # `pronoun` for `pronouns` would otherwise be silently ignored and the
        # page would render a they/them nobody chose.
        broken = json.loads(json.dumps(CONFIG))
        broken["members"][0]["pronoun"] = "she"
        self.assertIn("'pronoun'", self.error(broken))

    def test_an_empty_member_list_is_refused(self):
        self.assertIn("members", self.error(dict(CONFIG, members=[])))

    def test_a_destination_without_a_host_is_refused(self):
        self.assertIn("destination",
                      self.error(dict(CONFIG, destination="/srv/x.json")))

    def test_a_relative_remote_path_is_refused(self):
        # scp would resolve it against the login's home directory: the push
        # succeeds, and the endpoint keeps serving last month's snapshot.
        message = self.error(dict(CONFIG, destination="host:insights.json"))
        self.assertIn("absolute", message)

    def test_a_malformed_price_is_refused_here_not_four_minutes_in(self):
        broken = self.error(dict(CONFIG, prices={"some-model": "3.0"}))
        self.assertIn("some-model", broken)

    def test_no_prices_at_all_is_a_real_answer(self):
        # A model with no price is counted and left unpriced, never guessed.
        self.assertEqual(publish.load_config(
            self.write(dict(CONFIG, prices={})))["prices"], {})

    def test_a_single_week_window_is_refused(self):
        # The week being published is always partial, so a one-week window has
        # nothing complete to compare volume across.
        self.assertIn("weeks_back",
                      self.error(dict(CONFIG, weeks_back=1)))

    def test_a_missing_file_says_where_the_example_is(self):
        with self.assertRaises(publish.ConfigError) as caught:
            publish.load_config(os.path.join(self.tmp, "nope.json"))
        self.assertIn("dashboard.json.example", str(caught.exception))

    def test_the_tracked_example_is_a_config_that_would_load(self):
        # It is the only documentation of this shape that cannot go stale
        # without a test failing.
        loaded = publish.load_config(
            os.path.join(_ROOT, "reports", "dashboard.json.example"))
        self.assertTrue(loaded["members"])
        self.assertTrue(loaded["destination"])

    def test_the_config_has_no_place_to_put_a_credential(self):
        # The endpoint runs untrusted workflow code and must never hold one, so
        # neither the shape nor the example offers anywhere to write it down.
        with open(os.path.join(_ROOT, "reports", "dashboard.json.example"),
                  "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        for key in raw:
            self.assertNotRegex(key, "(?i)token|password|secret|passphrase")
        for key in publish.MEMBER_KEYS:
            self.assertNotRegex(key, "(?i)token|password|secret|passphrase")


class WindowTests(unittest.TestCase):
    """Which weeks the build is asked for, and which of them may be compared."""

    def test_five_weeks_back_ends_with_the_week_being_published(self):
        got = publish.window(DAY, 5, since=SINCE)
        self.assertEqual(got["weeks"],
                         ["2026-W31", "2026-W32", "2026-W33", "2026-W34",
                          "2026-W35"])

    def test_the_week_still_running_is_never_full(self):
        got = publish.window(DAY, 5, since=SINCE)
        self.assertNotIn("2026-W35", got["full"])
        self.assertEqual(got["partial"]["2026-W35"], "week not finished")

    def test_a_week_starting_before_the_pull_window_is_partial(self):
        # 2026-W31 opens on 27 July; a month-to-date pull holds none of those
        # days. Left in the trend it is a week that looks quiet because nobody
        # fetched it.
        got = publish.window(DAY, 5, since=SINCE)
        self.assertIn("2026-08-01", got["partial"]["2026-W31"])
        self.assertEqual(got["full"], ["2026-W32", "2026-W33", "2026-W34"])

    def test_without_a_manifest_only_the_running_week_is_partial(self):
        got = publish.window(DAY, 5)
        self.assertEqual(list(got["partial"]), ["2026-W35"])

    def test_every_full_week_is_one_the_build_was_asked_for(self):
        got = publish.window(DAY, 12, since=SINCE)
        for week in got["full"]:
            self.assertIn(week, got["weeks"])

    def test_a_window_with_nothing_complete_in_it_is_refused(self):
        with self.assertRaises(publish.ConfigError):
            publish.window(DAY, 2, since="2026-08-24T00:00:00Z")


class BuildArgvTests(unittest.TestCase):
    def argv(self, config=None):
        weeks = publish.window(DAY, 5, since=SINCE)
        return publish.build_argv(config or CONFIG, "cache", "out.json", weeks)

    def test_every_member_is_named_once(self):
        argv = self.argv()
        people = [argv[i + 1] for i, a in enumerate(argv) if a == "--person"]
        self.assertEqual(people, ["Alex Example=0000000000000000000000aa",
                                  "Sam Sample=0000000000000000000000bb"])

    def test_optional_fields_are_passed_only_where_they_are_set(self):
        # Never defaulted here: `report/dashboard_data.py` decides what an
        # unset short name or pronoun means, and deciding it twice is how the
        # two disagree.
        argv = self.argv()
        self.assertIn("Alex Example=she", argv)
        self.assertEqual(sum(1 for a in argv if a == "--pronouns"), 1)
        self.assertEqual(sum(1 for a in argv if a == "--short"), 1)

    def test_the_spans_are_the_derived_window(self):
        argv = self.argv()
        self.assertEqual(argv[argv.index("--weeks") + 1],
                         "2026-W31..2026-W35")
        self.assertEqual(argv[argv.index("--full-weeks") + 1],
                         "2026-W32..2026-W34")

    def test_every_partial_week_carries_its_reason(self):
        argv = self.argv()
        reasons = [argv[i + 1] for i, a in enumerate(argv) if a == "--partial"]
        self.assertEqual(len(reasons), 2)
        self.assertTrue(any(r.startswith("2026-W35=") for r in reasons))


class PushArgvTests(unittest.TestCase):
    DEST = "example-host:/srv/insight/insights/insights.json"

    def test_the_copy_lands_on_a_tmp_beside_the_target(self):
        # The endpoint re-stats this file and re-reads it the moment the mtime
        # moves, so a copy straight onto the live path gives it a window in
        # which to parse half a document.
        copy, _ = publish.push_argv("local.json", self.DEST)
        self.assertEqual(copy[0], "scp")
        self.assertTrue(copy[-1].endswith("insights.json.tmp"))
        self.assertNotEqual(copy[-1], self.DEST)

    def test_the_rename_is_within_one_directory(self):
        # Same directory means rename(2) rather than a copy, and rename(2) has
        # no half-written moment for the endpoint to read.
        _, move = publish.push_argv("local.json", self.DEST)
        self.assertEqual(move[0], "ssh")
        self.assertIn("mv --", move[-1])
        self.assertIn("/srv/insight/insights/insights.json.tmp", move[-1])
        self.assertIn("/srv/insight/insights/insights.json", move[-1])

    def test_neither_command_can_hang_on_a_prompt(self):
        # No terminal under launchd: ssh's default on a missing key is to ask,
        # and a scheduled job that is asked never exits.
        for argv in publish.push_argv("local.json", self.DEST):
            self.assertIn("BatchMode=yes", argv)

    def test_a_failed_copy_never_reaches_the_rename(self):
        runner = Fake(fails=["scp"])
        with self.assertRaises(publish.ConfigError):
            publish.push("local.json", self.DEST, runner)
        self.assertEqual([a[0] for a in runner.calls], ["scp"])


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache = os.path.join(self.tmp, "2026-08-27")
        os.makedirs(self.cache)
        with open(os.path.join(self.cache, "_status.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"date": "2026-08-27", "since": SINCE, "ok": True},
                      handle)
        self.out = os.path.join(self.tmp, "insights.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_publish(self, runner, **kwargs):
        return publish.publish(CONFIG, self.cache, DAY, self.out,
                               runner=runner, **kwargs)

    def test_it_builds_then_copies_then_renames(self):
        runner = Fake()
        result = self.run_publish(runner)
        self.assertEqual([a[0] for a in runner.calls[1:]], ["scp", "ssh"])
        self.assertEqual(result["weeks"][-1], "2026-W35")
        self.assertTrue(os.path.exists(self.out))

    def test_the_window_is_clamped_by_the_pulls_own_manifest(self):
        # Not written down in the config: a `--weeks` span pasted into a file
        # is correct on the day it is pasted and wrong every Monday after.
        result = self.run_publish(Fake())
        self.assertEqual(result["full_weeks"],
                         ["2026-W32", "2026-W33", "2026-W34"])

    def test_a_failed_build_pushes_nothing(self):
        runner = Fake(fails=["dashboard_data.py"])
        with self.assertRaises(publish.ConfigError):
            self.run_publish(runner)
        self.assertEqual(len(runner.calls), 1)

    def test_a_snapshot_that_does_not_parse_is_never_pushed(self):
        class Truncated(Fake):
            def __call__(self, argv):
                self.calls.append(list(argv))
                if "--out" in argv:
                    with open(argv[argv.index("--out") + 1], "w",
                              encoding="utf-8") as handle:
                        handle.write('{"schema": 1, "peo')
                return 0, ""

        runner = Truncated()
        with self.assertRaises(ValueError):
            self.run_publish(runner)
        self.assertEqual(len(runner.calls), 1)

    def test_a_snapshot_missing_a_top_level_key_is_never_pushed(self):
        runner = Fake(payload={"schema": 1})
        with self.assertRaises(publish.ConfigError):
            self.run_publish(runner)
        self.assertEqual(len(runner.calls), 1)

    def test_dry_run_builds_and_stops(self):
        runner = Fake()
        result = self.run_publish(runner, dry_run=True)
        self.assertEqual(len(runner.calls), 1)
        self.assertTrue(os.path.exists(self.out))
        # Said in the result, because a dry run that reported "published" is
        # how somebody concludes the endpoint has today's figures.
        self.assertFalse(result["pushed"])

    def test_no_credential_is_ever_handed_to_the_endpoint(self):
        # The box being pushed to runs untrusted workflow code from pull
        # requests. It gets derived counts and nothing else.
        runner = Fake()
        self.run_publish(runner)
        for argv in runner.calls:
            for word in ("--token", "PASSWORD", "SECRET", "-i"):
                self.assertNotIn(word, argv)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "cache"))
        self.config = os.path.join(self.tmp, "dashboard.json")
        with open(self.config, "w", encoding="utf-8") as handle:
            json.dump(CONFIG, handle)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_main(self, runner, config=None):
        # Absolute paths in the temporary directory: a test that wrote into
        # the repository would leave a snapshot naming invented people in
        # `git status`.
        return publish.main(["--config", config or self.config,
                             "--cache", os.path.join(self.tmp, "cache"),
                             "--date", DAY.isoformat(),
                             "--out", os.path.join(self.tmp, "insights.json")],
                            runner=runner)

    def test_a_clean_publish_exits_zero(self):
        self.assertEqual(self.run_main(Fake()), 0)

    def test_a_bad_config_exits_two_and_pushes_nothing(self):
        runner = Fake()
        self.assertEqual(
            self.run_main(runner, config=os.path.join(self.tmp, "nope.json")),
            2)
        self.assertEqual(runner.calls, [])

    def test_a_push_that_failed_exits_non_zero(self):
        self.assertEqual(self.run_main(Fake(fails=["scp"])), 2)


class NoNamesInSourceTests(unittest.TestCase):
    """The member list is data. No source file may carry one.

    `reports/identities.txt` and `reports/dashboard.json` are gitignored for
    this reason; a name or an account id pasted into a module is in git history
    from the first edit onwards, and nothing removes it afterwards. A name is
    hard to recognise mechanically; an Atlassian account id is not, and it is
    the half that is actually pasted.
    """

    #: The two shapes Atlassian issues: 24 hex characters, and the newer
    #: `712020:<uuid>`.
    ACCOUNT_ID = re.compile(r"\b(?:712020:)?[0-9a-f]{8}-?(?:[0-9a-f]{4}-?){3}"
                            r"[0-9a-f]{8,12}\b")

    PATHS = ("importers/publish_snapshot.py",
             "importers/daily_pull.py",
             "importers/tests/test_publish_snapshot.py",
             "reports/dashboard.json.example",
             "tools/launchd/vn.seta.insight.dailypull.plist.in")

    def test_no_account_id_is_committed(self):
        for name in self.PATHS:
            with open(os.path.join(_ROOT, *name.split("/")), "r",
                      encoding="utf-8") as handle:
                text = handle.read()
            for found in self.ACCOUNT_ID.findall(text):
                # A placeholder is almost all zeros. A real one is not, and
                # that is the only difference worth testing for mechanically.
                bare = found.replace("712020:", "").replace("-", "")
                self.assertLessEqual(
                    len(bare.replace("0", "")), 4,
                    "%s carries what looks like a real account id" % name)


if __name__ == "__main__":
    unittest.main()
