"""Tests for the daily Excel importer.

    python3 -m unittest discover -s importers/tests
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importers import daily_report as daily  # noqa: E402

HEADER = ["date", "person", "jira_key", "test_case_key", "activity",
          "ai_used", "ai_agent", "hours", "note"]


def row(date="2026-08-12", person="dev@example.com", jira="PRJ-6384",
        case="PRJ-TC-2891", activity="automate", ai="yes",
        agent="test-script-generator", hours="2", note=""):
    return [date, person, jira, case, activity, ai, agent, hours, note]


class TestParsing(unittest.TestCase):

    def parse(self, table, salt="s"):
        return daily.parse_rows(table, salt)

    def test_a_clean_sheet_parses(self):
        rows, problems = self.parse([HEADER, row()])
        self.assertEqual(problems, [])
        self.assertEqual(rows[0].jira_key, "PRJ-6384")
        self.assertEqual(rows[0].test_case_key, "PRJ-TC-2891")
        self.assertTrue(rows[0].ai_used)

    def test_a_missing_required_column_stops_the_file(self):
        header = [h for h in HEADER if h != "jira_key"]
        rows, problems = self.parse([header, row()[:3] + row()[4:]])
        self.assertEqual(rows, [])
        self.assertIn("jira_key", problems[0]["problem"])

    def test_a_bad_row_is_reported_with_its_number_not_dropped(self):
        # The row number is the whole point: it makes the problem fixable at
        # source rather than an anonymous count.
        rows, problems = self.parse(
            [HEADER, row(), row(jira="the ticket from yesterday"), row()])
        self.assertEqual(len(rows), 2)
        self.assertEqual(problems[0]["row"], 3)
        self.assertIn("jira_key", problems[0]["problem"])

    def test_every_fault_in_a_row_is_reported_at_once(self):
        # Reporting one fault per pass would mean as many correction rounds as
        # there are faults.
        _, problems = self.parse(
            [HEADER, row(date="12/08/2026", activity="did stuff", ai="maybe")])
        for expected in ("date", "activity", "ai_used"):
            self.assertIn(expected, problems[0]["problem"])

    def test_a_blank_spacer_row_is_not_an_error(self):
        rows, problems = self.parse([HEADER, row(), [None] * 9, row()])
        self.assertEqual(len(rows), 2)
        self.assertEqual(problems, [])

    def test_the_test_case_column_may_be_empty(self):
        rows, problems = self.parse([HEADER, row(case="")])
        self.assertEqual(problems, [])
        self.assertIsNone(rows[0].test_case_key)

    def test_an_unknown_activity_is_rejected_rather_than_bucketed(self):
        # A free-text activity would break the column as a dimension, quietly.
        _, problems = self.parse([HEADER, row(activity="automation work")])
        self.assertIn("activity", problems[0]["problem"])


class TestPrivacy(unittest.TestCase):

    def test_the_note_column_is_never_kept(self):
        # Free text written by a person is content, and CONTRACT.md admits no
        # exception for content that happens to be convenient.
        rows, _ = daily.parse_rows(
            [HEADER, row(note="spent the morning stuck on the VPN")], "s")
        self.assertNotIn("VPN", json.dumps(rows[0].as_dict()))
        self.assertNotIn("note", rows[0].as_dict())

    def test_the_email_is_hashed(self):
        rows, _ = daily.parse_rows([HEADER, row(person="qa1@example.com")], "s")
        self.assertNotIn("qa1@example.com", json.dumps(rows[0].as_dict()))
        self.assertTrue(rows[0].person_hash)

    def test_the_salt_changes_the_hash(self):
        one, _ = daily.parse_rows([HEADER, row()], "salt-a")
        two, _ = daily.parse_rows([HEADER, row()], "salt-b")
        self.assertNotEqual(one[0].person_hash, two[0].person_hash)


class TestReconcile(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="daily-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def events(self, name, records):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        return path

    def jira_event(self, key, day):
        return {"event_type": "jira.transition", "event_time": day + "T09:00:00Z",
                "context": {"jira_issue_key": key}, "attributes": {}}

    def aio_event(self, case, day):
        return {"event_type": "test.run.completed", "event_time": day + "T09:00:00Z",
                "context": {}, "attributes": {"test_case_key": case}}

    def test_agreement_on_both_trails_is_corroborated(self):
        rows, _ = daily.parse_rows([HEADER, row()], "s")
        trails = daily.index_events([
            self.events("j.ndjson", [self.jira_event("PRJ-6384", "2026-08-12")]),
            self.events("a.ndjson", [self.aio_event("PRJ-TC-2891", "2026-08-12")]),
        ])
        report = daily.reconcile(rows, trails)
        self.assertEqual(report["mappings"][0]["confidence"], "corroborated")
        self.assertEqual(report["corroborated_mappings"], 1)

    def test_a_pair_nothing_corroborates_is_kept_at_lower_confidence(self):
        # Kept, because it is still a lead. Separated, because a table mixing
        # corroborated and uncorroborated rows at one confidence is not
        # trustworthy at any.
        rows, _ = daily.parse_rows([HEADER, row()], "s")
        report = daily.reconcile(rows, {"jira": {}, "cases": {}})
        self.assertEqual(report["mappings"][0]["confidence"], "reported_only")
        self.assertEqual(report["corroborated_mappings"], 0)

    def test_a_trail_a_day_either_side_still_corroborates(self):
        # Reports are written at the end of a day or the morning after. Exact
        # day matching makes the metric read near zero for reasons unrelated to
        # whether the work left a trace.
        rows, _ = daily.parse_rows([HEADER, row(date="2026-08-12")], "s")
        trails = daily.index_events([
            self.events("j.ndjson", [self.jira_event("PRJ-6384", "2026-08-13")]),
            self.events("a.ndjson", [self.aio_event("PRJ-TC-2891", "2026-08-11")]),
        ])
        report = daily.reconcile(rows, trails)
        self.assertEqual(report["mappings"][0]["confidence"], "corroborated")

    def test_the_tolerance_can_be_turned_off(self):
        rows, _ = daily.parse_rows([HEADER, row(date="2026-08-12")], "s")
        trails = daily.index_events([
            self.events("j.ndjson", [self.jira_event("PRJ-6384", "2026-08-13")]),
            self.events("a.ndjson", [self.aio_event("PRJ-TC-2891", "2026-08-13")]),
        ])
        report = daily.reconcile(rows, trails, tolerance=0)
        self.assertEqual(report["mappings"][0]["confidence"], "reported_only")

    def test_activity_well_outside_the_window_does_not_corroborate(self):
        rows, _ = daily.parse_rows([HEADER, row(date="2026-08-12")], "s")
        trails = daily.index_events([
            self.events("j.ndjson", [self.jira_event("PRJ-6384", "2026-07-01")]),
            self.events("a.ndjson", [self.aio_event("PRJ-TC-2891", "2026-07-01")]),
        ])
        report = daily.reconcile(rows, trails)
        self.assertEqual(report["mappings"][0]["confidence"], "reported_only")

    def test_trail_completeness_counts_reported_work_with_no_trace(self):
        # Not automatically a bad report -- equally often work that never
        # reached Jira. The number exists so somebody asks which.
        rows, _ = daily.parse_rows(
            [HEADER, row(jira="PRJ-1"), row(jira="PRJ-2")], "s")
        trails = daily.index_events([
            self.events("j.ndjson", [self.jira_event("PRJ-1", "2026-08-12")])])
        completeness = daily.reconcile(rows, trails)["trail_completeness"]
        self.assertEqual(completeness["rows_with_a_jira_trail"], 1)
        self.assertEqual(completeness["rows_without_a_jira_trail"], 1)
        self.assertEqual(completeness["pct"], 50.0)

    def test_repeated_observations_of_a_pair_are_counted_once(self):
        rows, _ = daily.parse_rows(
            [HEADER, row(date="2026-08-12"), row(date="2026-08-13")], "s")
        report = daily.reconcile(rows, {"jira": {}, "cases": {}})
        self.assertEqual(len(report["mappings"]), 1)
        self.assertEqual(report["mappings"][0]["observations"], 2)
        self.assertEqual(report["mappings"][0]["days"],
                         ["2026-08-12", "2026-08-13"])

    def test_the_reported_agent_names_are_surfaced(self):
        # Until the emitter is wired this column is the only answer to "which
        # agent are they using", and afterwards it is what the emitter is
        # checked against.
        rows, _ = daily.parse_rows(
            [HEADER, row(agent="test-script-generator"),
             row(agent="architect.planner")], "s")
        report = daily.reconcile(rows, {"jira": {}, "cases": {}})
        self.assertEqual(report["ai_agents_reported"],
                         ["architect.planner", "test-script-generator"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
