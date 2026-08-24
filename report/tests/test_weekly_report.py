"""Tests for weekly_report.py — stdlib unittest, no network, no fixtures on disk."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "weekly_report", os.path.join(os.path.dirname(_HERE), "weekly_report.py")
)
wr = importlib.util.module_from_spec(_SPEC)
sys.modules["weekly_report"] = wr
_SPEC.loader.exec_module(wr)


def event(kind, when, attrs=None, run_id="run_1", link="explicit", **kw):
    base = {
        "schema_version": "1.0.0",
        "event_id": kw.pop("event_id", f"evt_{kind}_{when}_{run_id}"),
        "event_type": kind,
        "event_time": when,
        "trace_id": "trc_1",
        "run_id": run_id,
        "actor": {"person_id": kw.pop("person", "p1"), "person_email_hash": "h1"},
        "context": {"jira_issue_key": "PRJ-1", "jira_project_key": "PRJ",
                    "repo_full_name": "ws/repo"},
        "agent": {"agent_name": kw.pop("agent", "A"), "surface": "headless"},
        "attributes": attrs or {},
        "link": {"method": link, "confidence": 1.0},
    }
    base.update(kw)
    return base


class TestTimeHelpers(unittest.TestCase):
    def test_iso_week_label(self):
        self.assertEqual(wr.iso_week(datetime(2026, 8, 12, tzinfo=timezone.utc)), "2026-W33")

    def test_week_bounds_is_monday_to_monday(self):
        start, end = wr.week_bounds("2026-W33")
        self.assertEqual(start.weekday(), 0)
        self.assertEqual((end - start), timedelta(days=7))
        self.assertEqual(start.strftime("%Y-%m-%d"), "2026-08-10")

    def test_previous_weeks_are_ordered_oldest_first(self):
        self.assertEqual(wr.previous_weeks("2026-W33", 3),
                         ["2026-W30", "2026-W31", "2026-W32"])

    def test_parse_ts_handles_z_suffix_and_naive(self):
        self.assertIsNotNone(wr.parse_ts("2026-08-12T10:00:00Z"))
        self.assertIsNotNone(wr.parse_ts("2026-08-12T10:00:00"))
        self.assertIsNone(wr.parse_ts("not a date"))
        self.assertIsNone(wr.parse_ts(None))


class TestFormatting(unittest.TestCase):
    def test_duration_scales_to_magnitude(self):
        # The whole point: a 12-second merge must not render as "0.0h".
        self.assertEqual(wr.fmt_duration(12_000), "12s")
        self.assertEqual(wr.fmt_duration(71_000), "71s")
        self.assertEqual(wr.fmt_duration(600_000), "10m")
        # 4533679 ms = 75.6 min, below the 90-minute cutoff, so minutes are clearer.
        self.assertEqual(wr.fmt_duration(4_533_679), "76m")
        self.assertEqual(wr.fmt_duration(7_200_000), "2.0h")
        self.assertEqual(wr.fmt_duration(864_000_000), "10.0d")
        self.assertEqual(wr.fmt_duration(None), "—")

    def test_percentile_and_median_return_none_on_empty(self):
        self.assertIsNone(wr.percentile([], 0.85))
        self.assertIsNone(wr.median([]))
        self.assertEqual(wr.percentile([1, 2, 3, 4, 5], 0.0), 1)
        self.assertEqual(wr.percentile([1, 2, 3, 4, 5], 1.0), 5)

    def test_ratio_suppressed_below_min_group(self):
        self.assertIsNone(wr.ratio(2, 2, 5))
        self.assertIsNone(wr.ratio(0, 0, 5))
        self.assertAlmostEqual(wr.ratio(5, 10, 5), 50.0)

    def test_fmt_pct_shows_n_when_suppressed(self):
        self.assertEqual(wr.fmt_pct(None, 3), "n=3")
        self.assertEqual(wr.fmt_pct(50.0), "50.0%")


class TestLoading(unittest.TestCase):
    def test_malformed_lines_are_counted_not_dropped_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.ndjson")
            with open(path, "w") as fh:
                fh.write(json.dumps(event("scm.pr.merged", "2026-08-12T10:00:00Z")) + "\n")
                fh.write("{not json\n")
                fh.write(json.dumps({"no_event_type": 1}) + "\n")
                fh.write(json.dumps(event("scm.pr.merged", "bad-timestamp",
                                          event_id="evt_bad")) + "\n")
            events, stats = wr.load_events([path])
        self.assertEqual(len(events), 1)
        self.assertEqual(stats["malformed"], 2)
        self.assertEqual(stats["no_timestamp"], 1)

    def test_duplicate_event_ids_are_deduped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.ndjson")
            same = event("scm.pr.merged", "2026-08-12T10:00:00Z", event_id="evt_same")
            with open(path, "w") as fh:
                fh.write(json.dumps(same) + "\n")
                fh.write(json.dumps(same) + "\n")
            events, _ = wr.load_events([path])
        self.assertEqual(len(events), 1)

    def test_directory_input_is_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a.ndjson", "b.ndjson", "ignore.txt"):
                with open(os.path.join(tmp, name), "w") as fh:
                    fh.write(json.dumps(event("scm.pr.merged", "2026-08-12T10:00:00Z",
                                              event_id=f"evt_{name}")) + "\n")
            events, stats = wr.load_events([tmp])
        self.assertEqual(stats["files"], 2)
        self.assertEqual(len(events), 2)


class TestAggregation(unittest.TestCase):
    def setUp(self):
        self.week = "2026-W33"
        self.t = "2026-08-12T10:00:00Z"

    def agg(self, events, min_group=5):
        return wr.aggregate(events, min_group)[self.week]

    def test_approval_and_clarification_are_not_interventions(self):
        # Designed gates must not be counted as manual intervention (design 8.11).
        evs = [
            event("human.turn", self.t, {"turn_kind": "approval"}, run_id="r1",
                  event_id="e1"),
            event("human.turn", self.t, {"turn_kind": "clarification"}, run_id="r2",
                  event_id="e2"),
            event("human.turn", self.t, {"turn_kind": "correction"}, run_id="r3",
                  event_id="e3"),
        ]
        a = self.agg(evs)
        self.assertEqual(a.runs_with_correction, {"r3"})
        self.assertEqual(a.human_turns["approval"], 1)

    def test_acceptance_rate_excludes_in_flight_from_denominator(self):
        evs = [
            event("output.generated", self.t, {"acceptance_state": s},
                  event_id=f"e{i}")
            for i, s in enumerate(
                ["accepted"] * 6 + ["reworked", "rejected"] + ["in_flight"] * 4
            )
        ]
        a = self.agg(evs)
        self.assertEqual(a.judged_outputs, 8)
        self.assertAlmostEqual(a.acceptance_rate(), 75.0)

    def test_cost_per_accepted_is_none_below_min_group(self):
        evs = [
            event("output.generated", self.t, {"acceptance_state": "accepted"},
                  event_id="e1"),
            event("model.call", self.t,
                  {"input_tokens": 100, "output_tokens": 10, "cost_usd": 1.0,
                   "cost_basis": "measured", "model_id": "m"}, event_id="e2"),
        ]
        a = self.agg(evs)
        self.assertIsNone(a.cost_per_accepted())

    def test_tokens_and_cache_accumulate(self):
        evs = [
            event("model.call", self.t,
                  {"input_tokens": 20392, "output_tokens": 11,
                   "cached_input_tokens": 10305, "model_id": "claude-sonnet-4.6"},
                  event_id="e1"),
            event("model.call", self.t,
                  {"input_tokens": 254, "output_tokens": 9,
                   "cached_input_tokens": 0, "model_id": "gpt-4o-mini-2024-07-18"},
                  event_id="e2"),
        ]
        a = self.agg(evs)
        self.assertEqual(a.input_tokens, 20646)
        self.assertEqual(a.output_tokens, 20)
        self.assertEqual(a.cached_tokens, 10305)
        self.assertEqual(a.total_tokens, 20666)
        self.assertEqual(len(a.runs_by_model), 2)

    def test_events_land_in_the_right_iso_week(self):
        evs = [
            event("scm.pr.merged", "2026-08-09T23:59:00Z", {"pr_id": 1}, event_id="e1"),
            event("scm.pr.merged", "2026-08-10T00:01:00Z", {"pr_id": 2}, event_id="e2"),
        ]
        weeks = wr.aggregate(evs, 5)
        self.assertEqual(weeks["2026-W32"].prs_merged, 1)
        self.assertEqual(weeks["2026-W33"].prs_merged, 1)


class TestRendering(unittest.TestCase):
    def _report(self, evs, fmt="md", min_group=5):
        weeks = wr.aggregate(evs, min_group)
        stats = {"files": 1, "lines": len(evs), "malformed": 0, "no_timestamp": 0}
        if fmt == "json":
            return wr.render_json("2026-W33", weeks.get("2026-W33"), stats)
        return wr.render_markdown("2026-W33", weeks, [], stats, min_group)

    def test_absent_data_reads_as_absent_not_zero(self):
        evs = [event("scm.pr.merged", "2026-08-12T10:00:00Z", {"pr_id": 1})]
        body = self._report(evs)
        self.assertIn("No `model.call` events", body)
        self.assertIn("An absent measurement is not a measurement of zero", body)

    def test_forbidden_metrics_never_appear_as_values(self):
        evs = [event("scm.pr.merged", "2026-08-12T10:00:00Z", {"pr_id": 1})]
        body = self._report(evs)
        lowered = body.lower()
        # The words may appear in the explanatory footer, but never as a metric row.
        self.assertNotIn("| roi ", lowered)
        self.assertNotIn("value delivered |", lowered)
        self.assertNotIn("time saved |", lowered)

    def test_json_declares_what_is_excluded(self):
        evs = [event("scm.pr.merged", "2026-08-12T10:00:00Z", {"pr_id": 1})]
        payload = json.loads(self._report(evs, fmt="json"))
        self.assertIn("roi", payload["excluded_by_design"])
        self.assertIn("ai_vs_human_comparison", payload["excluded_by_design"])

    def test_empty_week_does_not_crash(self):
        body = wr.render_markdown("2026-W33", {}, [], {"files": 0, "lines": 0,
                                                       "malformed": 0,
                                                       "no_timestamp": 0}, 5)
        self.assertIn("No events found", body)

    def test_html_is_self_contained(self):
        evs = [event("scm.pr.merged", "2026-08-12T10:00:00Z", {"pr_id": 1})]
        md = self._report(evs)
        html = wr.render_html(md, "2026-W33")
        self.assertTrue(html.startswith("<!doctype html>"))
        # No external assets: a strict CSP or an offline reader must still render it.
        for bad in ("http://", "https://", "<script"):
            self.assertNotIn(bad, html)

    def test_html_escapes_content(self):
        md = "# T\n\n| A | B |\n|---|---|\n| <script>x</script> | y |\n"
        html = wr.render_html(md, "w")
        self.assertNotIn("<script>x</script>", html)
        self.assertIn("&lt;script&gt;", html)


class TestWeekResolution(unittest.TestCase):
    def test_date_is_resolved_to_its_iso_week(self):
        self.assertEqual(wr.resolve_week("2026-08-12", {}), "2026-W33")

    def test_default_skips_the_week_in_progress(self):
        now = datetime.now(timezone.utc)
        this_week = wr.iso_week(now)
        last_week = wr.iso_week(now - timedelta(days=7))
        weeks = {this_week: None, last_week: None}
        # A partial week always looks like a decline, so it is not the default.
        self.assertEqual(wr.resolve_week(None, weeks), last_week)


class TestAutomationMetrics(unittest.TestCase):
    """Metrics 1 and 2: Automation Output (a flow) and Coverage (a stock)."""

    T = "2026-08-12T10:00:00Z"

    def snapshot(self, key, status, when=None, archived=False, eid=None):
        return event("test.case.snapshot", when or self.T, {
            "test_case_key": key, "automation_status": status,
            "is_archived": archived, "folder_name": "Regression",
            "priority": "High", "test_case_status": "Published",
            "script_type": "Classic", "has_automation_key": False,
            "automation_owner_person_id": None,
            "created_at": self.T, "updated_at": when or self.T,
        }, event_id=eid or f"evt_case_{key}_{when or self.T}")

    def test_headline_coverage_matches_the_aio_dashboard_definition(self):
        # AIO's own tile shows Total - Automated as "Non Automated", so Manual,
        # In Progress and Not Assigned all sit in the denominator. Reporting a
        # stricter number under the same name is how two dashboards end up three
        # points apart with nobody able to say which is right.
        evs = ([self.snapshot(f"TC-a{i}", "Automated") for i in range(6)] +
               [self.snapshot(f"TC-b{i}", "To Be Automated") for i in range(2)] +
               [self.snapshot(f"TC-c{i}", None) for i in range(2)])
        inv = wr.inventory_coverage(evs)
        self.assertEqual(inv["counts"]["live"], 10)
        self.assertEqual(inv["coverage_pct"], 60.0)          # 6 / 10
        self.assertEqual(inv["coverage_pct_classified"], 75.0)  # 6 / 8
        self.assertEqual(inv["counts"]["unset"], 2)

    def test_manual_and_in_progress_count_against_coverage(self):
        evs = [self.snapshot("TC-1", "Automated"),
               self.snapshot("TC-2", "Manual"),
               self.snapshot("TC-3", "In Progress"),
               self.snapshot("TC-4", "To Be Automated")]
        inv = wr.inventory_coverage(evs)
        self.assertEqual(inv["coverage_pct"], 25.0)
        self.assertEqual(inv["counts"]["manual"], 1)
        self.assertEqual(inv["counts"]["in progress"], 1)

    def test_only_the_latest_snapshot_of_a_case_counts(self):
        evs = [self.snapshot("TC-1", "To Be Automated", "2026-07-01T10:00:00Z",
                             eid="old"),
               self.snapshot("TC-1", "Automated", "2026-08-12T10:00:00Z",
                             eid="new")]
        inv = wr.inventory_coverage(evs)
        self.assertEqual(inv["cases"], 1)
        self.assertEqual(inv["counts"]["automated"], 1)
        self.assertEqual(inv["counts"]["to be automated"], 0)

    def test_archived_cases_leave_the_estate(self):
        evs = [self.snapshot("TC-1", "Automated"),
               self.snapshot("TC-2", "To Be Automated", archived=True)]
        inv = wr.inventory_coverage(evs)
        self.assertEqual(inv["counts"]["archived"], 1)
        self.assertEqual(inv["counts"]["live"], 1)
        self.assertEqual(inv["known"], 1)

    def test_no_snapshots_yields_no_percentage_rather_than_zero(self):
        inv = wr.inventory_coverage([event("scm.pr.merged", self.T, {"pr_id": 1})])
        self.assertIsNone(inv["coverage_pct"])
        self.assertEqual(inv["cases"], 0)

    def test_coverage_is_reported_as_a_position_not_a_week(self):
        # Snapshots are stamped with the case's updated_at, so a weekly bucket
        # would answer "of the cases edited last week, how many are automated".
        evs = [self.snapshot("TC-1", "Automated", "2026-01-05T10:00:00Z"),
               self.snapshot("TC-2", "Automated", "2026-08-12T10:00:00Z")]
        inv = wr.inventory_coverage(evs)
        self.assertEqual(inv["cases"], 2)
        self.assertTrue(inv["as_at"].startswith("2026-08-12"))
        body = wr.render_markdown(
            "2026-W33", wr.aggregate(evs, 5), [],
            {"files": 1, "lines": 2, "malformed": 0, "no_timestamp": 0}, 5, inv)
        self.assertIn("not a weekly figure", body)
        self.assertIn("stock, not a flow", body)

    def test_unreliable_coverage_is_flagged(self):
        evs = ([self.snapshot(f"TC-a{i}", "Automated") for i in range(5)] +
               [self.snapshot(f"TC-c{i}", None) for i in range(40)])
        inv = wr.inventory_coverage(evs)
        body = wr.render_markdown(
            "2026-W33", wr.aggregate(evs, 5), [],
            {"files": 1, "lines": 1, "malformed": 0, "no_timestamp": 0}, 5, inv)
        self.assertIn("provisional", body)

    def test_scripts_created_counts_only_merged_prs(self):
        evs = [
            event("scm.pr.merged", self.T, {
                "pr_id": 1, "automation_scripts_added": 3,
                "automation_scripts_modified": 5,
                "automation_files_by_kind": {"feature": {"added": 3}},
            }, event_id="m"),
            event("scm.pr.declined", self.T, {
                "pr_id": 2, "automation_scripts_added": 99,
            }, event_id="d"),
        ]
        week = wr.aggregate(evs, 5)["2026-W33"]
        # A declined PR shipped nothing; counting it would inflate output with
        # work that was explicitly rejected.
        self.assertEqual(week.scripts_added, 3)
        self.assertEqual(week.scripts_modified, 5)

    def test_other_is_not_listed_as_an_automation_kind(self):
        evs = [event("scm.pr.merged", self.T, {
            "pr_id": 1, "automation_scripts_added": 1,
            "automation_files_by_kind": {"feature": {"added": 1},
                                         "other": {"added": 40}},
        })]
        inv = wr.inventory_coverage(evs)
        body = wr.render_markdown(
            "2026-W33", wr.aggregate(evs, 5), [],
            {"files": 1, "lines": 1, "malformed": 0, "no_timestamp": 0}, 5, inv)
        self.assertIn("created by kind", body)
        self.assertNotIn("other 40", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
