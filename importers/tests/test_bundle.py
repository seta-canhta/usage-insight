"""Tests for the bundle importer.

    python3 -m pytest importers/tests -q
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importers import bundle as bundle_mod  # noqa: E402


def make_bundle(path, events, machine="m1", window=("2026-08-03T00:00:00Z",
                                                    "2026-08-09T00:00:00Z"),
                corrupt=False, declared_count=None):
    body = "".join(json.dumps(e, sort_keys=True) + "\n" for e in events)
    manifest = {
        "format": bundle_mod.BUNDLE_FORMAT,
        "schema_version": bundle_mod.common.SCHEMA_VERSION,
        "machine_id": machine,
        "packed_at": "2026-08-10T00:00:00Z",
        "window_start": window[0],
        "window_end": window[1],
        "event_count": declared_count if declared_count is not None else len(events),
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"_manifest": manifest}, sort_keys=True) + "\n")
        handle.write(body + ("tampered\n" if corrupt else ""))
    return manifest


def event(event_id, event_type="scm.commit"):
    return {"event_id": event_id, "event_type": event_type,
            "event_time": "2026-08-05T00:00:00Z", "ingested_at": None,
            "attributes": {"commit_sha": event_id, "lines_added": 1,
                           "lines_removed": 0, "has_ai_marker": False}}


class InboxTestCase(unittest.TestCase):

    def setUp(self):
        self.inbox = tempfile.mkdtemp(prefix="inbox-")
        self.addCleanup(shutil.rmtree, self.inbox, True)

    def path(self, name):
        return os.path.join(self.inbox, name)


class TestParsing(InboxTestCase):

    def test_a_clean_bundle_parses(self):
        make_bundle(self.path("a.ndjson"), [event("evt_1"), event("evt_2")])
        manifest, events = bundle_mod.parse_bundle(self.path("a.ndjson"))
        self.assertEqual(manifest["machine_id"], "m1")
        self.assertEqual(len(events), 2)

    def test_a_corrupted_bundle_is_rejected_whole(self):
        # Half a bundle is worse than none: the events land, the week looks
        # covered, and nobody can tell the rest is missing.
        make_bundle(self.path("a.ndjson"), [event("evt_1")], corrupt=True)
        with self.assertRaises(bundle_mod.BundleError) as ctx:
            bundle_mod.parse_bundle(self.path("a.ndjson"))
        self.assertIn("checksum", str(ctx.exception))

    def test_a_manifest_that_undercounts_is_rejected(self):
        make_bundle(self.path("a.ndjson"), [event("evt_1"), event("evt_2")],
                    declared_count=1)
        with self.assertRaises(bundle_mod.BundleError) as ctx:
            bundle_mod.parse_bundle(self.path("a.ndjson"))
        self.assertIn("declares 1", str(ctx.exception))

    def test_an_unknown_format_is_rejected(self):
        make_bundle(self.path("a.ndjson"), [event("evt_1")])
        with open(self.path("a.ndjson"), "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        payload = json.loads(lines[0])
        payload["_manifest"]["format"] = "something-else/9"
        lines[0] = json.dumps(payload, sort_keys=True) + "\n"
        with open(self.path("a.ndjson"), "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        with self.assertRaises(bundle_mod.BundleError):
            bundle_mod.parse_bundle(self.path("a.ndjson"))

    def test_a_file_with_no_manifest_is_rejected(self):
        with open(self.path("a.ndjson"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps(event("evt_1")) + "\n")
        with self.assertRaises(bundle_mod.BundleError):
            bundle_mod.parse_bundle(self.path("a.ndjson"))


class TestAllowList(InboxTestCase):

    def test_an_event_carrying_content_is_dropped_not_imported(self):
        bad = event("evt_bad")
        bad["attributes"]["prompt_text"] = "whatever the user typed"
        make_bundle(self.path("a.ndjson"), [event("evt_ok"), bad])
        result = bundle_mod.import_inbox(self.inbox)
        stored = json.dumps(result["events"])
        self.assertNotIn("prompt_text", stored)
        self.assertNotIn("whatever the user typed", stored)
        self.assertEqual(len(result["events"]), 1)
        self.assertTrue(result["rejected"])


class TestImport(InboxTestCase):

    def test_the_same_bundle_twice_imports_once(self):
        make_bundle(self.path("a.ndjson"), [event("evt_1")])
        make_bundle(self.path("b.ndjson"), [event("evt_1")], machine="m2")
        result = bundle_mod.import_inbox(self.inbox)
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["duplicates"], 1)

    def test_state_carries_dedup_across_runs(self):
        make_bundle(self.path("a.ndjson"), [event("evt_1")])
        first = bundle_mod.import_inbox(self.inbox)
        second = bundle_mod.import_inbox(self.inbox, {
            "event_ids": first["event_ids"], "coverage": first["coverage"]})
        self.assertEqual(len(second["events"]), 0)
        self.assertEqual(second["duplicates"], 1)

    def test_ingested_at_is_stamped_on_arrival(self):
        # event_time is when it happened on a machine whose clock we do not
        # control; ingested_at is ours. Never sort by one alone.
        make_bundle(self.path("a.ndjson"), [event("evt_1")])
        result = bundle_mod.import_inbox(self.inbox)
        self.assertTrue(result["events"][0]["ingested_at"])

    def test_a_rejected_bundle_contributes_no_coverage(self):
        # Otherwise its week reads as measured when nothing was measured.
        make_bundle(self.path("a.ndjson"), [event("evt_1")], corrupt=True)
        result = bundle_mod.import_inbox(self.inbox)
        self.assertEqual(result["coverage"], {})
        self.assertEqual(result["files_rejected"], ["a.ndjson"])


class TestCoverage(InboxTestCase):

    def test_an_empty_bundle_still_counts_as_a_covered_week(self):
        # A quiet week is a measured zero. It must not look like a week nobody
        # reported.
        make_bundle(self.path("a.ndjson"), [])
        result = bundle_mod.import_inbox(self.inbox)
        report = bundle_mod.coverage_report(result["coverage"])
        self.assertEqual(report["machine_weeks_covered"], 1)
        self.assertEqual(report["by_machine"]["m1"]["events"], 0)

    def test_missing_weeks_inside_a_machine_span_are_named(self):
        make_bundle(self.path("a.ndjson"), [event("evt_1")], machine="m1",
                    window=("2026-08-03T00:00:00Z", "2026-08-03T00:00:00Z"))
        make_bundle(self.path("b.ndjson"), [event("evt_2")], machine="m1",
                    window=("2026-08-24T00:00:00Z", "2026-08-24T00:00:00Z"))
        make_bundle(self.path("c.ndjson"), [event("evt_3")], machine="m2",
                    window=("2026-08-10T00:00:00Z", "2026-08-10T00:00:00Z"))
        make_bundle(self.path("d.ndjson"), [event("evt_4")], machine="m2",
                    window=("2026-08-17T00:00:00Z", "2026-08-17T00:00:00Z"))
        result = bundle_mod.import_inbox(self.inbox)
        report = bundle_mod.coverage_report(result["coverage"])
        missing = report["by_machine"]["m1"]["weeks_missing_within_span"]
        self.assertEqual(missing, ["2026-W33", "2026-W34"])
        self.assertEqual(
            report["by_machine"]["m2"]["weeks_missing_within_span"], [])

    def test_a_window_spanning_weeks_covers_each_of_them(self):
        make_bundle(self.path("a.ndjson"), [event("evt_1")],
                    window=("2026-08-03T00:00:00Z", "2026-08-17T00:00:00Z"))
        result = bundle_mod.import_inbox(self.inbox)
        self.assertEqual(result["coverage"]["m1"]["weeks"],
                         ["2026-W32", "2026-W33", "2026-W34"])

    def test_a_bundle_with_no_window_claims_no_coverage(self):
        make_bundle(self.path("a.ndjson"), [event("evt_1")], window=(None, None))
        result = bundle_mod.import_inbox(self.inbox)
        self.assertEqual(result["coverage"]["m1"]["weeks"], [])


class TestEndToEnd(InboxTestCase):

    def test_a_packed_bundle_from_the_client_imports(self):
        # The two sides agree through the contract, not through shared code
        # paths. If pack and import ever drift, this is where it shows.
        repo_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        sys.path.insert(0, os.path.join(repo_root, "cli"))
        home = tempfile.mkdtemp(prefix="insight-e2e-")
        self.addCleanup(shutil.rmtree, home, True)
        os.environ["SETA_INSIGHT_HOME"] = home
        self.addCleanup(os.environ.pop, "SETA_INSIGHT_HOME", None)
        for name in ("insight",):
            sys.modules.pop(name, None)
        import insight

        with contextlib.redirect_stdout(io.StringIO()):
            insight.main(["init", "--yes"])
            insight.append_events([event("evt_from_client")])
            insight.main(["pack"])
        packed = os.listdir(insight.REPORTS_DIR)[0]
        shutil.copy(os.path.join(insight.REPORTS_DIR, packed),
                    self.path("handed-over.ndjson"))

        result = bundle_mod.import_inbox(self.inbox)
        self.assertEqual(result["rejected"], [])
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["event_id"], "evt_from_client")


if __name__ == "__main__":
    unittest.main(verbosity=2)
