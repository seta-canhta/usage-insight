"""Tests for the unattended hourly run.

    python3 -m pytest cli/tests/test_auto.py -q

Nobody watches this one. Everything asserted here is a behaviour whose failure
would be invisible for weeks: a run that uploads a duplicate every hour, a run
that stops collecting because Copilot was not installed, a lock left behind by
a crash, or a buffer pruned before it was ever sent.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class AutoTestCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="insight-auto-")
        os.environ["SETA_INSIGHT_HOME"] = self.home
        os.environ["SETA_INSIGHT_ENDPOINT"] = "https://endpoint.test"
        sys.modules.pop("insight", None)
        import insight
        self.insight = insight
        self.addCleanup(shutil.rmtree, self.home, True)
        self.addCleanup(os.environ.pop, "SETA_INSIGHT_HOME", None)
        self.addCleanup(os.environ.pop, "SETA_INSIGHT_ENDPOINT", None)

    def run_cli(self, *argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = self.insight.main(list(argv))
        return code, buffer.getvalue()

    def init(self, email="canh@seta-international.vn"):
        args = ["init", "--yes"]
        if email:
            args += ["--email", email]
        self.run_cli(*args)

    def seed(self, day=None, count=2, tag="a"):
        day = day or self.insight.now()[:10]
        import common
        path = os.path.join(self.home, "buffer", day + ".ndjson")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            for i in range(count):
                handle.write(json.dumps({
                    "schema_version": common.SCHEMA_VERSION,
                    "event_id": "evt_{}_{}_{}".format(day, tag, i),
                    "event_type": "run.phase.completed",
                    "event_time": "{}T09:0{}:00Z".format(day, i),
                    "attributes": {"phase_name": "implement", "status": "ok",
                                   "duration_ms": 10 + i},
                }, sort_keys=True) + "\n")

    def log_lines(self):
        if not os.path.exists(self.insight.LOG_PATH):
            return []
        with open(self.insight.LOG_PATH, "r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def fake_ship(self, receipts):
        """Replace the network with something that records what it was given."""
        import ship as ship_mod
        sent = []

        def ship_bundle(path, endpoint, token=None, previous_token=None, **kw):
            # Mirrors the real receipt, content_sha256 included. `auto` dedupes
            # on that field, so a fake without it would let every one of these
            # tests pass against code that uploads hourly duplicates.
            _, digest = ship_mod.digest_of(path)
            manifest = ship_mod.read_manifest(path)
            sent.append({"path": path, "sha256": digest,
                         "content_sha256": manifest.get("sha256")})
            return {"file": os.path.basename(path), "status": 201,
                    "already_stored": False, "key": "bundles/k/" + digest,
                    "sha256": digest, "content_sha256": manifest.get("sha256"),
                    "bytes": 1, "window": "2026-08-17/2026-08-23",
                    "shipped_at": "2026-08-25T00:00:00Z"}

        self.addCleanup(setattr, ship_mod, "ship_bundle", ship_mod.ship_bundle)
        ship_mod.ship_bundle = ship_bundle
        return sent


class TestQuietHours(AutoTestCase):
    def test_an_unchanged_hour_uploads_nothing(self):
        # 24 identical bundles a day is the obvious way to build this wrong,
        # and it shows up as a team that looks 24x busier than it is.
        self.init()
        self.seed()
        sent = self.fake_ship({})

        self.run_cli("auto")
        self.assertEqual(len(sent), 1)

        self.run_cli("auto")
        self.assertEqual(len(sent), 1, "second run re-uploaded unchanged data")
        self.assertEqual(self.log_lines()[-1]["event"], "no_change")

    def test_an_unchanged_hour_leaves_no_bundle_behind(self):
        self.init()
        self.seed()
        self.fake_ship({})
        self.run_cli("auto")
        before = len(os.listdir(self.insight.REPORTS_DIR))
        self.run_cli("auto")
        self.assertEqual(len(os.listdir(self.insight.REPORTS_DIR)), before)

    def test_new_events_are_uploaded_on_the_next_run(self):
        self.init()
        self.seed(tag="a")
        sent = self.fake_ship({})
        self.run_cli("auto")
        self.seed(tag="b")
        self.run_cli("auto")
        self.assertEqual(len(sent), 2)
        self.assertNotEqual(sent[0]["content_sha256"], sent[1]["content_sha256"])

    def test_a_quiet_day_uploads_one_empty_bundle_then_stops(self):
        # An empty bundle with a declared day window is a *measured* zero: the
        # machine was on and did no AI work. That is the distinction the whole
        # report depends on -- a day with no bundle at all is missing data, and
        # rendering the two the same is the failure ARCHITECTURE.md warns about.
        # Once is the point, though: not once an hour.
        self.init()
        sent = self.fake_ship({})
        self.run_cli("auto")
        self.assertEqual(len(sent), 1)
        self.run_cli("auto")
        self.assertEqual(len(sent), 1)

    def test_repacking_the_same_events_is_not_a_change(self):
        # `packed_at` moves on every pack, so the file differs every hour even
        # when nothing happened. Deduping on the whole file would upload 24
        # near-identical bundles a day and show a team 24x busier than it is.
        self.init()
        self.seed()
        sent = self.fake_ship({})
        self.run_cli("auto")

        import time as _time
        _time.sleep(1.1)          # force a different `packed_at`
        self.run_cli("auto")

        self.assertEqual(len(sent), 1, "a new packed_at was mistaken for new data")


class TestKeepsGoing(AutoTestCase):
    def test_a_failing_step_does_not_stop_the_others(self):
        # Copilot not being installed is not a reason to stop uploading the
        # agent events already buffered.
        self.init()
        self.seed()
        sent = self.fake_ship({})

        def explode(args):
            raise RuntimeError("no span file")

        self.addCleanup(setattr, self.insight, "cmd_otel", self.insight.cmd_otel)
        self.insight.cmd_otel = explode

        self.run_cli("auto")
        self.assertEqual(len(sent), 1)
        self.assertTrue(any("otel" in p for p in self.log_lines()[-1]["problems"]))

    def test_a_failed_upload_keeps_the_bundle_for_the_next_run(self):
        self.init()
        self.seed()
        import ship as ship_mod

        def refuse(path, endpoint, **kw):
            raise ship_mod.ShipError("endpoint down")

        self.addCleanup(setattr, ship_mod, "ship_bundle", ship_mod.ship_bundle)
        ship_mod.ship_bundle = refuse

        self.run_cli("auto")
        self.assertEqual(self.log_lines()[-1]["event"], "ship_failed")
        self.assertTrue(os.listdir(self.insight.REPORTS_DIR),
                        "a failed upload threw the bundle away")

    def test_a_machine_with_no_upload_secret_still_packs(self):
        self.init(email=None)
        self.seed()
        self.run_cli("auto")
        self.assertEqual(self.log_lines()[-1]["event"], "packed_not_sent")
        self.assertTrue(os.listdir(self.insight.REPORTS_DIR))

    def test_an_uninitialised_machine_exits_quietly(self):
        # The scheduler may outlive a `purge --all`. It must not error hourly.
        code, _ = self.run_cli("auto")
        self.assertEqual(code, 0)
        self.assertEqual(self.log_lines()[-1]["reason"], "not initialised")


class TestLocking(AutoTestCase):
    def test_a_second_run_skips_while_the_first_holds_the_lock(self):
        self.init()
        self.seed()
        sent = self.fake_ship({})
        with open(self.insight.LOCK_PATH, "w") as handle:
            handle.write("99999")
        self.run_cli("auto")
        self.assertEqual(sent, [])
        self.assertIn("lock", self.log_lines()[-1]["reason"])

    def test_a_stale_lock_from_a_crashed_run_is_broken(self):
        # Otherwise one crash stops collection permanently and silently.
        self.init()
        self.seed()
        sent = self.fake_ship({})
        with open(self.insight.LOCK_PATH, "w") as handle:
            handle.write("99999")
        old = time.time() - 8 * 3600
        os.utime(self.insight.LOCK_PATH, (old, old))

        self.run_cli("auto")
        self.assertEqual(len(sent), 1)

    def test_the_lock_is_released_even_when_a_run_fails(self):
        self.init()
        self.seed()
        import ship as ship_mod

        def refuse(path, endpoint, **kw):
            raise ship_mod.ShipError("down")

        self.addCleanup(setattr, ship_mod, "ship_bundle", ship_mod.ship_bundle)
        ship_mod.ship_bundle = refuse
        self.run_cli("auto")
        self.assertFalse(os.path.exists(self.insight.LOCK_PATH))


class TestPruning(AutoTestCase):
    def test_an_old_day_that_was_uploaded_is_dropped(self):
        self.init()
        self.seed()
        self.fake_ship({})
        self.seed(day="2020-01-01", tag="old")
        # Pretend that day was covered by an earlier upload.
        import ship as ship_mod
        ship_mod.save_receipts(self.insight.RECEIPTS_PATH, {
            "old.ndjson": {"status": 201, "sha256": "x",
                           "window": "2020-01-01/2020-01-07"}})

        self.run_cli("auto")
        self.assertNotIn("2020-01-01", self.insight.buffer_days())

    def test_an_old_day_that_was_never_uploaded_is_kept(self):
        # Pruning on age alone turns a fixable outage into permanent data loss.
        self.init()
        self.seed()
        self.fake_ship({})
        self.seed(day="2020-01-01", tag="old")

        self.run_cli("auto")
        self.assertIn("2020-01-01", self.insight.buffer_days())


class TestConsentText(AutoTestCase):
    def test_setup_with_hourly_says_the_machine_uploads_on_its_own(self):
        # The consent record has to describe what will actually happen. A text
        # promising manual handover on a machine that uploads hourly is not a
        # weaker consent record, it is a false one.
        _, output = self.run_cli("init", "--yes", "--email",
                                 "canh@seta-international.vn")
        self.assertNotIn("upload on its own", output)

        shutil.rmtree(self.home, ignore_errors=True)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            import argparse
            self.insight.cmd_init(argparse.Namespace(
                yes=True, force=True, endpoint=None, no_endpoint=False,
                email="canh@seta-international.vn", hourly=True))
        self.assertIn("upload on its own", buffer.getvalue())
        self.assertIn("schedule --off", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
