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
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class AutoTestCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="insight-auto-")
        os.environ["SETA_INSIGHT_HOME"] = self.home
        os.environ["SETA_INSIGHT_ENDPOINT"] = "https://endpoint.test"
        # Every local source is pointed at the sandbox, not at this machine.
        # Without this, `auto` read the developer's real VS Code chat store --
        # 937 sessions on the machine this was written on -- once per test:
        # four minutes of suite time, and tests touching data they have no
        # business reading. A sandbox that leaks into $HOME is not a sandbox.
        os.environ["COPILOT_HOME"] = os.path.join(self.home, "copilot")
        os.environ["VSCODE_HOME"] = os.path.join(self.home, "vscode")
        sys.modules.pop("insight", None)
        sys.modules.pop("vscode_read", None)
        sys.modules.pop("copilot_read", None)
        import insight
        self.insight = insight
        self.addCleanup(shutil.rmtree, self.home, True)
        for name in ("SETA_INSIGHT_HOME", "SETA_INSIGHT_ENDPOINT",
                     "COPILOT_HOME", "VSCODE_HOME"):
            self.addCleanup(os.environ.pop, name, None)

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

    def fake_ship(self, receipts, fail=False):
        """Replace the network with something that records what it was given."""
        import ship as ship_mod
        sent = []

        def ship_bundle(path, endpoint, token=None, previous_token=None, **kw):
            if fail:
                raise ship_mod.ShipError("endpoint unreachable (test)")
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

    def expire_ship_stamp(self):
        """Push the upload stamp far enough back that a run is due."""
        path = self.insight.SHIP_STAMP
        if os.path.exists(path):
            old = time.time() - self.insight.SHIP_MIN_INTERVAL_S - 60
            os.utime(path, (old, old))

    def test_an_unchanged_hour_leaves_no_bundle_behind(self):
        self.init()
        self.seed()
        self.fake_ship({})
        self.run_cli("auto")
        before = len(os.listdir(self.insight.REPORTS_DIR))
        self.run_cli("auto")
        self.assertEqual(len(os.listdir(self.insight.REPORTS_DIR)), before)

    def test_new_events_are_uploaded_on_the_next_due_run(self):
        # Uploads are batched: collection runs on Copilot activity, which on a
        # busy machine is often, and shipping each trigger would put dozens of
        # nearly-empty objects a day on the endpoint. So the second run sends
        # only once its interval has passed -- and it does send, whole.
        self.init()
        self.seed(tag="a")
        sent = self.fake_ship({})
        self.run_cli("auto")
        self.seed(tag="b")
        self.expire_ship_stamp()
        self.run_cli("auto")
        self.assertEqual(len(sent), 2)
        self.assertNotEqual(sent[0]["content_sha256"], sent[1]["content_sha256"])

    def test_a_second_run_inside_the_interval_is_held_not_dropped(self):
        # Held, not lost: the bundle stays on disk and the next due run ships
        # the day whole, because `pack` re-seals the same day.
        self.init()
        self.seed(tag="a")
        sent = self.fake_ship({})
        self.run_cli("auto")
        self.assertEqual(len(sent), 1)

        self.seed(tag="b")
        self.run_cli("auto")                     # inside the interval
        self.assertEqual(len(sent), 1, "shipped inside the batching interval")
        self.assertEqual(self.log_lines()[-1]["event"], "batched")

        self.expire_ship_stamp()
        self.run_cli("auto")
        self.assertEqual(len(sent), 2, "the held bundle never went")
        self.assertNotEqual(sent[0]["content_sha256"], sent[1]["content_sha256"])

    def test_a_failed_upload_leaves_the_next_run_due(self):
        # The stamp is written on success only. A machine whose endpoint was
        # down must retry on the next trigger, not wait out an interval it
        # never used.
        self.init()
        self.seed(tag="a")
        self.fake_ship({}, fail=True)
        self.run_cli("auto")
        self.assertTrue(self.insight.ship_due())

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

        self.addCleanup(setattr, self.insight, "cmd_copilot", self.insight.cmd_copilot)
        self.insight.cmd_copilot = explode

        self.run_cli("auto")
        self.assertEqual(len(sent), 1)
        self.assertTrue(any("copilot" in p for p in self.log_lines()[-1]["problems"]))

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


class TestTheCopilotHook(AutoTestCase):
    """Collection fires on activity, and must never be felt.

    The hook sits in the latency path of somebody's editor: `PreToolUse` runs
    before every tool call, and there were 2,062 of those in 22 measured
    sessions. It has a 5s budget it should never approach.
    """

    def fake_spawn(self):
        """Record what would be detached, without detaching it.

        Spawning for real leaves an orphan collector per test. What matters
        here is that the hook hands the work off rather than doing it.
        """
        import subprocess as sp
        spawned = []

        def popen(argv, **kw):
            spawned.append((argv, kw))
            return unittest.mock.Mock()

        self.addCleanup(setattr, sp, "Popen", sp.Popen)
        sp.Popen = popen
        return spawned

    def test_the_hook_returns_without_collecting(self):
        self.init()
        self.seed()
        sent = self.fake_ship({})
        spawned = self.fake_spawn()
        code, _ = self.run_cli("hook")
        self.assertEqual(code, 0)
        # Handed off: the work does not happen inside this call.
        self.assertEqual(len(sent), 0)
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][0][-1], "auto")
        # Detached, or a slow collector would hold the editor open.
        self.assertTrue(spawned[0][1].get("start_new_session"))

    def test_a_second_hook_inside_the_debounce_does_nothing(self):
        # Otherwise one Copilot session spawns a collector per tool call.
        self.init()
        self.fake_spawn()
        self.run_cli("hook")
        first = os.path.getmtime(self.insight.HOOK_STAMP)
        self.run_cli("hook")
        self.assertEqual(os.path.getmtime(self.insight.HOOK_STAMP), first)

    def test_the_stamp_is_written_before_the_run_not_after(self):
        # If collection hangs or dies, the next tool call must not immediately
        # start another one.
        self.init()
        self.fake_spawn()
        self.run_cli("hook")
        self.assertTrue(os.path.exists(self.insight.HOOK_STAMP))

    def test_an_uninitialised_machine_is_silent(self):
        # Someone may install the hook and never run setup. That must cost
        # nothing and say nothing.
        code, out = self.run_cli("hook")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "")

    def test_now_collects_in_the_foreground(self):
        self.init()
        self.seed()
        sent = self.fake_ship({})
        self.run_cli("hook", "--now")
        self.assertEqual(len(sent), 1)

    def test_installing_the_hook_keeps_somebody_elses(self):
        # Both pilot machines already carry `rtk-rewrite.json`. Copilot reads
        # the directory, so ours is its own file and theirs is untouched.
        hooks = os.path.join(self.insight.COPILOT_ROOT, "hooks")
        os.makedirs(hooks, exist_ok=True)
        theirs = os.path.join(hooks, "rtk-rewrite.json")
        with open(theirs, "w", encoding="utf-8") as handle:
            handle.write('{"version": 1}')
        self.insight.install_copilot_hook()
        self.assertTrue(os.path.exists(theirs))
        self.assertTrue(os.path.exists(self.insight.COPILOT_HOOK_PATH))

    def test_the_hook_is_registered_in_both_spellings(self):
        # `rtk`, demonstrably accepted in the field, registers `PreToolUse` and
        # `preToolUse`. Matching that shape beats a cleaner one that might be
        # ignored in silence.
        self.insight.install_copilot_hook()
        with open(self.insight.COPILOT_HOOK_PATH, encoding="utf-8") as handle:
            body = json.load(handle)
        self.assertIn("PreToolUse", body["hooks"])
        self.assertIn("preToolUse", body["hooks"])

    def test_installing_twice_is_a_no_op(self):
        self.insight.install_copilot_hook()
        self.assertEqual(self.insight.install_copilot_hook()["status"],
                         "already installed")
