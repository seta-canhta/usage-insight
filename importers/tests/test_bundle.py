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
                corrupt=False, declared_count=None, sources=None):
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
    if sources is not None:
        manifest["sources"] = sources
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


NOTHING = {"repos": 0, "copilot": False, "vscode": False, "agent": False}
SOMETHING = {"repos": 2, "copilot": False, "vscode": False, "agent": False}


class MeasuredZeroTests(unittest.TestCase):
    """A zero is only a zero if something was watching.

    The bug this exists for: a machine with no repository registered uploads a
    well-formed bundle declaring its window and no events -- identical, from
    here, to a genuinely quiet day. Averaged in as a zero it says the person
    did no work, which is a wrong answer rather than a missing one.
    """

    def test_events_make_it_measured_whatever_the_sources_say(self):
        self.assertTrue(bundle_mod.measured(
            {"event_count": 3, "sources": NOTHING}))

    def test_a_zero_from_a_configured_machine_is_a_real_zero(self):
        self.assertTrue(bundle_mod.measured(
            {"event_count": 0, "sources": SOMETHING}))

    def test_a_zero_from_a_machine_with_nothing_configured_is_not(self):
        self.assertFalse(bundle_mod.measured(
            {"event_count": 0, "sources": NOTHING}))

    def test_any_single_source_is_enough(self):
        for key in ("repos", "copilot", "vscode", "agent"):
            sources = dict(NOTHING)
            sources[key] = 1 if key == "repos" else True
            self.assertTrue(bundle_mod.measured(
                {"event_count": 0, "sources": sources}), key)

    def test_a_bundle_from_before_this_existed_is_left_alone(self):
        # No `sources` key at all. Guessing the other way would retroactively
        # erase real weeks that were genuinely quiet.
        self.assertTrue(bundle_mod.measured({"event_count": 0}))
        self.assertTrue(bundle_mod.measured({"event_count": 0, "sources": None}))


class UnmeasuredCoverageTests(InboxTestCase):

    def test_a_machine_measuring_nothing_is_named(self):
        make_bundle(os.path.join(self.inbox, "a.ndjson"), [],
                    machine="idle-but-configured", sources=SOMETHING)
        make_bundle(os.path.join(self.inbox, "b.ndjson"), [],
                    machine="never-set-up", sources=NOTHING)
        report = bundle_mod.coverage_report(
            bundle_mod.import_inbox(self.inbox)["coverage"])
        self.assertEqual(report["machines_measuring_nothing"], ["never-set-up"])

    def test_a_machine_that_measured_once_is_not_named(self):
        # Somebody who finished the setup on Wednesday has Monday's empty
        # bundles on file. They are reporting properly now.
        make_bundle(os.path.join(self.inbox, "mon.ndjson"), [],
                    machine="m9", sources=NOTHING)
        make_bundle(os.path.join(self.inbox, "wed.ndjson"), [event("e1")],
                    machine="m9", sources=SOMETHING)
        report = bundle_mod.coverage_report(
            bundle_mod.import_inbox(self.inbox)["coverage"])
        self.assertEqual(report["machines_measuring_nothing"], [])

    def test_unmeasured_bundles_are_counted_per_machine(self):
        for name in ("a", "b"):
            make_bundle(os.path.join(self.inbox, name + ".ndjson"), [],
                        machine="m9", sources=NOTHING)
        report = bundle_mod.coverage_report(
            bundle_mod.import_inbox(self.inbox)["coverage"])
        self.assertEqual(report["by_machine"]["m9"]["unmeasured_bundles"], 2)

    def test_the_week_is_still_counted_as_covered(self):
        # The machine did report. Dropping its week would turn one wrong
        # reading into a different wrong reading.
        make_bundle(os.path.join(self.inbox, "a.ndjson"), [],
                    machine="m9", sources=NOTHING)
        report = bundle_mod.coverage_report(
            bundle_mod.import_inbox(self.inbox)["coverage"])
        self.assertEqual(report["machine_weeks_covered"], 1)

    def test_state_written_before_this_existed_still_loads(self):
        old_state = {"event_ids": [],
                     "coverage": {"m9": {"weeks": ["2026-W32"], "bundles": 1,
                                         "events": 0}}}
        make_bundle(os.path.join(self.inbox, "a.ndjson"), [],
                    machine="m9", sources=NOTHING)
        result = bundle_mod.import_inbox(self.inbox, old_state)
        self.assertEqual(result["coverage"]["m9"]["unmeasured"], 1)


if __name__ == "__main__":
    unittest.main()


class TestPersonIdentityIsStamped(InboxTestCase):
    """Laptop events joined to nothing until 2026-08-27.

    Measured that week: 935 laptop events, `person_id` null on every one and
    `person_email_hash` salted with a `uuid4()` generated at `init` -- so two
    machines of one person hash differently and the server cannot recompute
    either. Meanwhile AIO runs, AIO cases and Bitbucket all key on the same
    Atlassian accountIds and join to each other perfectly. The laptop side
    shared not one id with any of them.

    A laptop cannot know its own accountId; that is a Jira fact. The endpoint
    authenticated the upload, so `pull.py` turns the address into an id and
    leaves it here -- the address itself never enters the event path (§1.1).
    """

    def _actor(self, person_id=None):
        return {"person_id": person_id, "person_email_hash": "abc",
                "team_id": None, "role": None}

    def _laptop_event(self, event_id, person_id=None):
        found = event(event_id)
        found["actor"] = self._actor(person_id)
        return found

    def _identities(self, mapping):
        with open(self.path(bundle_mod.IDENTITIES), "w", encoding="utf-8") as h:
            json.dump(mapping, h)

    def test_an_event_gets_the_accountid_of_whoever_uploaded_it(self):
        make_bundle(self.path("a.ndjson"), [self._laptop_event("evt_1")])
        self._identities({"a.ndjson": "712020:abc"})
        result = bundle_mod.import_inbox(self.inbox)
        self.assertEqual(result["events"][0]["actor"]["person_id"], "712020:abc")

    def test_an_unmapped_bundle_stays_null_rather_than_guessed(self):
        make_bundle(self.path("a.ndjson"), [self._laptop_event("evt_1")])
        self._identities({"someone-else.ndjson": "712020:abc"})
        result = bundle_mod.import_inbox(self.inbox)
        self.assertIsNone(result["events"][0]["actor"]["person_id"])

    def test_a_client_that_already_knows_is_not_overwritten(self):
        # Fill, never overwrite. The stamp is the weaker signal of the two.
        make_bundle(self.path("a.ndjson"),
                    [self._laptop_event("evt_1", person_id="712020:real")])
        self._identities({"a.ndjson": "712020:other"})
        result = bundle_mod.import_inbox(self.inbox)
        self.assertEqual(result["events"][0]["actor"]["person_id"], "712020:real")

    def test_no_identities_file_is_not_an_error(self):
        # Every pull that predates this, and every deployment without a map.
        make_bundle(self.path("a.ndjson"), [self._laptop_event("evt_1")])
        result = bundle_mod.import_inbox(self.inbox)
        self.assertIsNone(result["events"][0]["actor"]["person_id"])

    def test_the_identities_file_is_not_imported_as_a_bundle(self):
        make_bundle(self.path("a.ndjson"), [self._laptop_event("evt_1")])
        self._identities({"a.ndjson": "712020:abc"})
        result = bundle_mod.import_inbox(self.inbox)
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["rejected"], [])
