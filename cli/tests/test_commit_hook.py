"""The prepare-commit-msg hook, which wrote nothing for as long as it existed.

`AI-Run-Id` was present on **0 of 121** pull requests in the 2026-W34 export.
CONTRACT.md §2.4 makes that trailer the only thing earning
`link.method='explicit'`, and `explicit` the only method admissible to the cost
metrics -- so metrics 9 and 10 were held shut by a hook that was installed by
default, correct in every line, and searching a directory that does not exist
on the machines it was installed on.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "hooks", "prepare-commit-msg")
_SPEC = importlib.util.spec_from_loader(
    "commit_hook", importlib.machinery.SourceFileLoader("commit_hook", HOOK))
hook = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(hook)


def _now(offset_seconds=0):
    return (datetime.now(timezone.utc)
            + timedelta(seconds=offset_seconds)).isoformat().replace(
                "+00:00", "Z")


class HookTestCase(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="hook-home-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.buffer = os.path.join(self.home, "buffer")
        os.makedirs(self.buffer)
        self._prev = os.environ.get("SETA_INSIGHT_HOME")
        os.environ["SETA_INSIGHT_HOME"] = self.home
        self.addCleanup(self._restore)

    def _restore(self):
        if self._prev is None:
            os.environ.pop("SETA_INSIGHT_HOME", None)
        else:
            os.environ["SETA_INSIGHT_HOME"] = self._prev

    def write_run(self, run_id="run_1", at=None, closed=False, name="a.ndjson"):
        events = [{
            "event_type": "run.started", "run_id": run_id,
            "event_time": at or _now(-60), "trace_id": "trc_1",
            "agent": {"agent_name": "copilot.cli"},
            "attributes": {"model_declared_id": "gpt-5"},
        }]
        if closed:
            events.append({"event_type": "run.completed", "run_id": run_id,
                           "event_time": _now()})
        with open(os.path.join(self.buffer, name), "w", encoding="utf-8") as h:
            for event in events:
                h.write(json.dumps(event) + "\n")

    def read(self, path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def commit_message(self, text="feat: add a thing\n"):
        path = os.path.join(self.home, "COMMIT_EDITMSG")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path


class TestItLooksWhereTheRunsActuallyAre(HookTestCase):

    def test_a_copilot_cli_run_in_insights_own_buffer_is_found(self):
        # The whole bug. `~/.aiep/telemetry` is the platform agent's buffer and
        # does not exist on the pilot machines; their runs come from the
        # Copilot CLI journal into insight's buffer, which was never searched.
        self.write_run()
        self.assertIsNotNone(hook.latest_open_run())

    def test_the_trailer_reaches_the_message(self):
        self.write_run()
        path = self.commit_message()
        hook.main(["prepare-commit-msg", path])
        body = self.read(path)
        self.assertIn("AI-Run-Id: run_1", body)
        self.assertIn("AI-Trace-Id: trc_1", body)
        self.assertIn("AI-Agent: copilot.cli", body)

    def test_the_subject_is_never_touched(self):
        self.write_run()
        path = self.commit_message()
        hook.main(["prepare-commit-msg", path])
        self.assertEqual(
            self.read(path).splitlines()[0],
            "feat: add a thing")

    def test_no_open_run_writes_nothing(self):
        path = self.commit_message()
        hook.main(["prepare-commit-msg", path])
        self.assertNotIn("AI-Run-Id", self.read(path))

    def test_a_closed_run_is_not_attached(self):
        self.write_run(closed=True)
        self.assertIsNone(hook.latest_open_run())


class TestStalenessIsActuallyEnforced(HookTestCase):
    """`MAX_AGE_SECONDS` was declared, commented, and never read."""

    def test_a_run_older_than_the_limit_is_not_attached(self):
        # Left open on Friday; this is Monday's commit. The file says twice
        # that a wrong join is worse than a missing one.
        self.write_run(at=_now(-(hook.MAX_AGE_SECONDS + 600)))
        self.assertIsNone(hook.latest_open_run())

    def test_a_recent_run_still_is(self):
        self.write_run(at=_now(-60))
        self.assertIsNotNone(hook.latest_open_run())

    def test_an_unparseable_start_time_counts_as_stale(self):
        # Not evidence that it started recently.
        self.write_run(at="not a timestamp")
        self.assertIsNone(hook.latest_open_run())


class TestItNeverBreaksACommit(HookTestCase):

    def test_a_merge_is_left_alone(self):
        self.write_run()
        path = self.commit_message("Merge branch 'main'\n")
        hook.main(["prepare-commit-msg", path, "merge"])
        self.assertNotIn("AI-Run-Id", self.read(path))

    def test_an_already_stamped_message_is_not_stamped_twice(self):
        self.write_run()
        path = self.commit_message("feat: x\n\nAI-Run-Id: run_old\n")
        hook.main(["prepare-commit-msg", path])
        body = self.read(path)
        self.assertEqual(body.count("AI-Run-Id"), 1)
        self.assertIn("run_old", body)

    def test_an_unreadable_buffer_file_does_not_stop_the_search(self):
        self.write_run(name="good.ndjson")
        bad = os.path.join(self.buffer, "bad.ndjson")
        with open(bad, "w") as handle:
            handle.write("{not json\n")
        self.assertIsNotNone(hook.latest_open_run())

    def test_it_exits_zero_even_when_everything_is_wrong(self):
        done = subprocess.run([sys.executable, HOOK, "/nonexistent/path"],
                              capture_output=True)
        self.assertEqual(done.returncode, 0)


if __name__ == "__main__":
    unittest.main()
