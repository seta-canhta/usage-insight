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
    surface = kw.pop("surface", "headless")
    skill = kw.pop("skill", None)
    base = {
        "schema_version": kw.pop("schema_version", "1.0.0"),
        "event_id": kw.pop("event_id", f"evt_{kind}_{when}_{run_id}"),
        "event_type": kind,
        "event_time": when,
        "trace_id": "trc_1",
        "run_id": run_id,
        "actor": {"person_id": kw.pop("person", "p1"), "person_email_hash": "h1"},
        "context": {"jira_issue_key": "PRJ-1", "jira_project_key": "PRJ",
                    "repo_full_name": "ws/repo"},
        "agent": {"agent_name": kw.pop("agent", "A"), "surface": surface,
                  "skill_name": skill},
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


class TestFalseZeros(unittest.TestCase):
    """Unknown is `—`/`null`, never 0.

    Every assertion here is against a *specific* zero this report used to print.
    They assert the em dash or the JSON null, never the absence of the digit,
    because "0" legitimately appears elsewhere on the page.
    """

    T = "2026-08-12T10:00:00Z"

    def report(self, evs, fmt="md", min_group=5, trail=()):
        weeks = wr.aggregate(evs, min_group)
        stats = {"files": 1, "lines": len(evs), "malformed": 0, "no_timestamp": 0}
        if fmt == "json":
            return wr.render_json("2026-W33", weeks.get("2026-W33"), stats)
        return wr.render_markdown("2026-W33", weeks, list(trail), stats, min_group)

    def usage(self, **over):
        attrs = {"model_id": "claude-sonnet-4.6", "input_tokens": 2_251_096,
                 "output_tokens": 22_512, "cached_input_tokens": 1_998_030,
                 "cache_write_tokens": 0, "reasoning_tokens": 0,
                 "request_count": 48, "premium_requests": 1, "nano_aiu": None,
                 "latency_ms": 805_479, "retry_count": None, "finish_reason": None,
                 # Context levels, carried only for a single-model session.
                 "tool_definitions_tokens": 19_127, "system_tokens": 11_635,
                 "conversation_tokens": 40_564}
        attrs.update(over)
        return attrs

    # -- cost --------------------------------------------------------------

    def test_cost_renders_a_dash_not_zero_dollars(self):
        # CONTRACT §4 keeps cost_usd off the client allow-list, so it never
        # arrives. `| Cost | $0.00 |` was printed every week for that reason.
        evs = [event("model.call", self.T, self.usage(), event_id="m1")]
        body = self.report(evs)
        self.assertIn("| Token cost (modelled) | — | derived in the warehouse",
                      body)
        self.assertNotIn("$0.00", body)
        self.assertNotIn("| Cost |", body)

    def test_cost_usd_is_null_in_json_not_zero(self):
        evs = [event("model.call", self.T, self.usage(), event_id="m1")]
        payload = json.loads(self.report(evs, fmt="json"))
        self.assertIsNone(payload["cost"]["cost_usd"])
        self.assertIsNone(payload["cost"]["cost_per_accepted_output"])
        self.assertIsNone(payload["cost"]["seat_cost_usd"])

    def test_a_supplied_cost_still_renders(self):
        # The dash means "nobody supplied one", not "this field is banned".
        evs = [event("model.call", self.T,
                     self.usage(cost_usd=1.25, cost_basis="modelled"),
                     event_id="m1")]
        agg = wr.aggregate(evs, 5)["2026-W33"]
        self.assertAlmostEqual(agg.cost_usd, 1.25)
        self.assertIn("$1.25", self.report(evs))

    # -- retries -----------------------------------------------------------

    def test_retries_are_none_not_zero_when_the_source_omits_them(self):
        evs = [event("model.call", self.T, self.usage(), event_id="m1"),
               event("tool.call", self.T, {"status": "ok", "tool_kind": "file"},
                     event_id="t1")]
        agg = wr.aggregate(evs, 5)["2026-W33"]
        self.assertIsNone(agg.retries)
        body = self.report(evs)
        self.assertIn("| Model retries | — | `retry_count` is not recorded", body)
        self.assertNotIn("| Model retries | 0 |", body)
        payload = json.loads(self.report(evs, fmt="json"))
        self.assertIsNone(payload["reliability"]["retries"])

    def test_a_real_retry_count_is_counted(self):
        evs = [event("model.call", self.T, self.usage(retry_count=3), event_id="m1")]
        self.assertEqual(wr.aggregate(evs, 5)["2026-W33"].retries, 3)

    def test_a_measured_zero_retry_count_is_zero_not_a_dash(self):
        # The rule is symmetrical: a source that says "0 retries" is measuring.
        evs = [event("model.call", self.T, self.usage(retry_count=0), event_id="m1")]
        self.assertEqual(wr.aggregate(evs, 5)["2026-W33"].retries, 0)

    # -- tool status -------------------------------------------------------

    def test_null_tool_status_is_not_pooled_with_success(self):
        evs = [
            event("tool.call", self.T, {"status": "ok", "tool_kind": "file"},
                  event_id="t1"),
            event("tool.call", self.T, {"status": "error", "tool_kind": "terminal",
                                        "error_class": "permission_denied"},
                  event_id="t2"),
            event("tool.call", self.T, {"status": None, "tool_kind": "terminal"},
                  event_id="t3"),
        ]
        agg = wr.aggregate(evs, 2)["2026-W33"]
        self.assertEqual((agg.tool_ok, agg.tool_error, agg.tool_status_unknown),
                         (1, 1, 1))
        # error / (ok + error) — the unknown is in neither term.
        self.assertAlmostEqual(agg.tool_error_rate(), 50.0)
        payload = json.loads(self.report(evs, fmt="json", min_group=2))
        self.assertEqual(payload["reliability"]["tool_status_unknown"], 1)
        self.assertEqual(payload["reliability"]["tool_error"], 1)

    def test_tool_rows_render_without_any_terminal_run_event(self):
        # Tool activity exists independently of run.completed. Nesting these
        # rows under a terminal-run guard hid every one of them.
        evs = [event("tool.call", self.T, {"status": "ok", "tool_kind": "file"},
                     event_id=f"t{i}") for i in range(6)]
        body = self.report(evs)
        self.assertIn("No terminal run events", body)
        self.assertIn("**Tool calls**", body)
        self.assertIn("| Tool calls | 6 | |", body)

    def test_tool_name_is_never_used_as_a_dimension(self):
        # CONTRACT §1 rule 5: < 100 distinct values. `tool_name` is unbounded
        # across MCP servers, so it must not appear as a breakdown.
        evs = [event("tool.call", self.T,
                     {"status": "ok", "tool_kind": "mcp",
                      "tool_name": "mcp__atlassian__searchJiraIssuesUsingJql"},
                     event_id="t1")]
        body = self.report(evs)
        self.assertNotIn("searchJiraIssuesUsingJql", body)
        self.assertIn("| mcp | 1 |", body)

    # -- gates -------------------------------------------------------------

    def gate(self, name, status, attempt, eid, trace="trc_1"):
        return event("gate.evaluated", self.T,
                     {"gate_name": name, "status": status, "attempt_index": attempt,
                      "quality_score": None, "coverage_pct": None},
                     event_id=eid, trace_id=trace)

    def test_gate_null_verdict_gets_a_column_not_a_none_row(self):
        evs = ([self.gate("build", "pass", i, f"gp{i}") for i in range(5)]
               + [self.gate("build", "fail", 5 + i, f"gf{i}") for i in range(2)]
               + [self.gate("build", None, 7 + i, f"gu{i}") for i in range(3)])
        agg = wr.aggregate(evs, 5)["2026-W33"]
        self.assertEqual(dict(agg.gates["build"]),
                         {"pass": 5, "fail": 2, "unknown": 3})
        # 5 / (5 + 2) — the three unknowns are excluded from the denominator.
        self.assertAlmostEqual(agg.gate_pass_rate("build"), 100 * 5 / 7)
        body = self.report(evs)
        self.assertIn("| Gate | Pass | Fail | Unknown | Pass rate |", body)
        self.assertIn("| build | 5 | 2 | 3 | 71.4% |", body)
        # The old key rendered `| build | None | 3 |`.
        self.assertNotIn("| build | None |", body)
        self.assertNotIn(":None", body)

    def test_gate_unknown_is_null_in_json_and_out_of_the_rate(self):
        evs = ([self.gate("test", "pass", i, f"p{i}") for i in range(5)]
               + [self.gate("test", None, 5, "u0")])
        payload = json.loads(self.report(evs, fmt="json"))
        gates = payload["quality"]["gates"]["test"]
        self.assertEqual(gates["unknown"], 1)
        self.assertEqual(gates["pass"], 5)
        self.assertEqual(gates["fail"], 0)
        self.assertAlmostEqual(gates["pass_rate_pct"], 100.0)

    def test_gate_retry_depth_is_the_first_pass_per_session(self):
        # Two sessions: one passes on its third go, one first time. Re-running
        # an already-green gate is not a retry and must not extend the depth.
        evs = [
            self.gate("test", "fail", 0, "a0", trace="s1"),
            self.gate("test", "fail", 1, "a1", trace="s1"),
            self.gate("test", "pass", 2, "a2", trace="s1"),
            self.gate("test", "pass", 3, "a3", trace="s1"),
            self.gate("test", "pass", 0, "b0", trace="s2"),
        ]
        agg = wr.aggregate(evs, 5)["2026-W33"]
        self.assertEqual(sorted(agg.gate_first_pass["test"]), [0, 2])
        self.assertIn("Gate retry depth", self.report(evs))

    def test_gate_that_never_passed_contributes_no_retry_depth(self):
        evs = [self.gate("build", "fail", i, f"f{i}") for i in range(3)]
        agg = wr.aggregate(evs, 5)["2026-W33"]
        self.assertEqual(agg.gate_first_pass.get("build"), None)
        body = self.report(evs)
        self.assertIn("| build | n=0 | — | 0 |", body)

    # -- section 6 ---------------------------------------------------------

    def test_premium_requests_are_the_headline_not_dollars(self):
        evs = ([event("model.call", self.T, self.usage(premium_requests=7),
                      event_id="m1")]
               + [event("output.generated", self.T,
                        {"acceptance_state": "accepted"}, event_id=f"o{i}")
                  for i in range(5)])
        agg = wr.aggregate(evs, 5)["2026-W33"]
        self.assertAlmostEqual(agg.premium_per_accepted(), 7 / 5)
        body = self.report(evs)
        self.assertIn("**Premium requests per accepted output**", body)
        self.assertNotIn("Cost per accepted output", body)
        payload = json.loads(self.report(evs, fmt="json"))
        self.assertAlmostEqual(
            payload["cost"]["premium_requests_per_accepted_output"], 1.4)

    def test_api_requests_are_never_divided_into_premium_requests(self):
        # Measured: 107 calls -> cost 2; 12 calls -> cost 1. The ratio is
        # Copilot's pricing policy, not a fact about anyone's work.
        evs = [event("model.call", self.T,
                     self.usage(request_count=107, premium_requests=2),
                     event_id="m1")]
        body = self.report(evs)
        self.assertIn("| API requests | 107 | `requests.count`", body)
        self.assertIn("| **Premium requests** | 2 |", body)
        self.assertIn("**Premium requests are not API requests.**", body)
        # 107 / 2 = 53.5 must appear nowhere.
        self.assertNotIn("53.5", body)

    def test_cache_share_is_a_share_of_input_tokens(self):
        evs = [event("model.call", self.T, self.usage(
            input_tokens=1000, cached_input_tokens=940, cache_write_tokens=10,
            output_tokens=100), event_id="m1")]
        agg = wr.aggregate(evs, 5)["2026-W33"]
        self.assertAlmostEqual(agg.cached_share, 94.0)
        # in + out, not in + out + cached: the cache figures are already inside
        # input_tokens (CONTRACT §3 row 5).
        self.assertEqual(agg.total_tokens, 1100)

    def test_empty_cost_section_does_not_name_the_otel_export(self):
        evs = [event("scm.pr.merged", self.T, {"pr_id": 1})]
        body = self.report(evs)
        self.assertIn("No `model.call` events", body)
        self.assertNotIn("OTel", body)
        self.assertIn("session-state", body)

    # -- section 3, 8, 9, 10 ----------------------------------------------

    def test_model_api_time_is_labelled_as_api_time_not_wall_clock(self):
        evs = [event("model.call", self.T, self.usage(latency_ms=76_000),
                     event_id="m1")]
        body = self.report(evs)
        self.assertIn("AI session API time", body)
        self.assertIn("not wall clock", body)
        # It must not sit inside the PR lead-time table.
        speed = body.split("## 3. Speed")[1].split("## 4.")[0]
        first_table = speed.split("**Model API time**")[0]
        self.assertIn("PR merge lead time", first_table)
        self.assertNotIn("AI session API time", first_table)

    def test_approvals_are_a_count_and_the_blind_spot_is_named(self):
        evs = [event("human.turn", self.T, {"turn_kind": "approval"},
                     run_id=f"r{i}", event_id=f"h{i}") for i in range(151)]
        body = self.report(evs)
        self.assertIn("**Approvals granted** 151", body)
        self.assertNotIn("Approval rate", body)
        self.assertIn("silently rewrites it", body)
        payload = json.loads(self.report(evs, fmt="json"))
        self.assertEqual(payload["human"]["approvals_granted"], 151)

    def test_adoption_separates_sessions_resumes_and_subagents(self):
        evs = [
            event("run.started", self.T, {"input_source": "start"},
                  run_id="r1", event_id="s1", trace_id="sess-a",
                  surface="copilot-cli", skill="executing-plans"),
            event("run.started", self.T, {"input_source": "resume"},
                  run_id="r2", event_id="s2", trace_id="sess-a",
                  surface="copilot-cli", skill="executing-plans"),
            event("run.started", self.T, {"input_source": "subagent"},
                  run_id="r3", event_id="s3", trace_id="sess-a",
                  surface="copilot-cli", parent_run_id="r1",
                  agent="general-purpose", skill="brainstorming"),
        ]
        agg = wr.aggregate(evs, 5)["2026-W33"]
        self.assertEqual(dict(agg.runs_by_input_source),
                         {"start": 1, "resume": 1, "subagent": 1})
        self.assertEqual(len(agg.sessions), 1)
        self.assertEqual(agg.skills, {"executing-plans", "brainstorming"})
        body = self.report(evs)
        self.assertIn("| ...sub-agent invocations | 1 |", body)
        self.assertIn("| Sessions | 1 |", body)
        # The agent table must not read as a league table.
        self.assertNotIn("**Runs by agent**", body)
        self.assertIn("not a comparison", body)

    def test_runs_without_a_terminal_event_are_named_not_failed(self):
        evs = [event("run.started", self.T, {"input_source": "start"},
                     run_id="r1", event_id="s1"),
               event("run.started", self.T, {"input_source": "start"},
                     run_id="r2", event_id="s2"),
               event("run.completed", self.T, {"duration_ms": 1000},
                     run_id="r1", event_id="c1")]
        agg = wr.aggregate(evs, 5)["2026-W33"]
        self.assertEqual(agg.runs_without_terminal, 1)
        body = self.report(evs)
        self.assertIn("| Runs with no terminal event | 1 |", body)
        # One terminal event, so the success denominator is 1 — not 2.
        self.assertEqual(sum(agg.runs_by_status.values()), 1)

    def test_trend_marks_a_source_change(self):
        prior = [event("run.started", "2026-08-05T10:00:00Z",
                       {"input_source": "start"}, run_id="p1", event_id="p1",
                       surface="vscode-copilot-chat", schema_version="1.0.0")]
        now = [event("run.started", self.T, {"input_source": "start"},
                     run_id="n1", event_id="n1",
                     surface="copilot-cli", schema_version="1.1.0")]
        body = self.report(prior + now, trail=["2026-W32"])
        trend = body.split("## 9. Trend")[1]
        self.assertIn("‡", trend)
        self.assertIn("a step change may be a source change", trend)
        self.assertIn("vscode-copilot-chat", trend)
        # Poller rows are unaffected by which client is installed.
        for line in trend.splitlines():
            if line.startswith("| PRs merged "):
                self.assertNotIn("‡", line)

    def test_trend_does_not_mark_a_stable_source(self):
        evs = [event("run.started", "2026-08-05T10:00:00Z",
                     {"input_source": "start"}, run_id="p1", event_id="p1",
                     surface="copilot-cli", schema_version="1.1.0"),
               event("run.started", self.T, {"input_source": "start"},
                     run_id="n1", event_id="n1",
                     surface="copilot-cli", schema_version="1.1.0")]
        trend = self.report(evs, trail=["2026-W32"]).split("## 9. Trend")[1]
        self.assertNotIn("‡", trend)

    def test_null_audit_names_every_field_rendered_as_a_dash(self):
        evs = [event("model.call", self.T, self.usage(), event_id="m1")]
        body = self.report(evs)
        dq = body.split("## 10. Data quality")[1]
        for field in ("retry_count", "finish_reason", "tool.call.duration_ms",
                      "cost_usd"):
            self.assertIn(field, dq)
        payload = json.loads(self.report(evs, fmt="json"))
        self.assertIn("model.call.retry_count",
                      payload["data_quality"]["null_not_zero"])

    def test_refusals_are_published_not_merely_omitted(self):
        doc = wr.__doc__
        for phrase in ("Time to approve a permission",
                       "Permission denial rate",
                       "assistant.message.outputTokens",
                       "Tokens per agent or sub-agent",
                       "post_review_change_ratio"):
            self.assertIn(phrase, doc)
        payload = json.loads(self.report(
            [event("model.call", self.T, self.usage(), event_id="m1")],
            fmt="json"))
        for name in ("time_to_approve_a_permission", "permission_denial_rate",
                     "per_run_cost_from_output_tokens",
                     "tokens_per_agent_or_subagent", "repeated_edit_as_rework"):
            self.assertIn(name, payload["excluded_by_design"])

    def test_k_anonymity_suppression_still_applies_to_the_new_rates(self):
        # Four tool calls is under the default k of 5, so the rate is suppressed
        # and the raw denominator is shown instead.
        evs = [event("tool.call", self.T, {"status": "ok", "tool_kind": "file"},
                     event_id=f"t{i}") for i in range(4)]
        agg = wr.aggregate(evs, 5)["2026-W33"]
        self.assertIsNone(agg.tool_error_rate())
        self.assertIn("| **Tool error rate** | n=4 |", self.report(evs))


class TestContextComposition(TestFalseZeros):
    """§6 context levels: `tool_definitions_tokens` and the two rows that make
    it a composition rather than one number without a whole.

    These are LEVELS. Everything here exists to stop the report presenting one
    as spend, as a weekly total, or as something that can be multiplied by a
    request count.
    """

    def sessions(self, *triples):
        """One `model.call` per session, each carrying one context composition."""
        return [
            event("model.call", self.T,
                  self.usage(tool_definitions_tokens=defs, system_tokens=system,
                             conversation_tokens=conversation),
                  event_id=f"m{i}", trace_id=f"sess-{i}")
            for i, (defs, system, conversation) in enumerate(triples)
        ]

    def test_context_is_a_level_per_session_never_a_weekly_total(self):
        evs = self.sessions((20_000, 10_000, 30_000), (20_000, 10_000, 30_000))
        agg = wr.aggregate(evs, 5)["2026-W33"]
        self.assertEqual(agg.context_tool_defs, [20_000, 20_000])
        self.assertEqual(agg.context_total, [60_000, 60_000])
        body = self.report(evs)
        self.assertIn("**level per session**, not a total for the week", body)
        self.assertIn("| Tool definitions | 20,000 | 20,000 | 2 |", body)
        # The sums a total would produce must appear nowhere.
        self.assertNotIn("40,000", body)
        self.assertNotIn("120,000", body)

    def test_the_median_and_the_range_are_both_shown(self):
        # The spread is the finding: a level that steps does not drift.
        evs = self.sessions((14_318, 11_000, 20_000), (32_122, 11_000, 20_000),
                            (34_219, 11_000, 20_000))
        body = self.report(evs)
        self.assertIn("| Tool definitions | 32,122 | 14,318 – 34,219 | 3 |", body)

    def test_tool_definitions_carry_the_mcp_framing(self):
        body = self.report(self.sessions((19_127, 11_635, 40_564)))
        self.assertIn("**Tool definitions are what connecting an MCP server "
                      "costs.**", body)
        self.assertIn("paid on every request", body)

    def test_context_is_null_not_zero_for_a_multi_model_session(self):
        evs = [event("model.call", self.T,
                     self.usage(tool_definitions_tokens=None, system_tokens=None,
                                conversation_tokens=None),
                     event_id="m1", trace_id="sess-multi")]
        agg = wr.aggregate(evs, 5)["2026-W33"]
        self.assertEqual(agg.context_unknown, 1)
        self.assertEqual(agg.context_tool_defs, [])
        self.assertIsNone(wr.median(agg.context_tool_defs))
        body = self.report(evs)
        self.assertIn("| Tool definitions | — | — | — |", body)
        self.assertIn("| Sessions with no context recorded | — | | 1 |", body)
        self.assertIn("more than one model", body)
        self.assertNotIn("| Tool definitions | 0 |", body)

    def test_multi_model_nulls_are_named_in_the_data_quality_audit(self):
        evs = [event("model.call", self.T,
                     self.usage(tool_definitions_tokens=None, system_tokens=None,
                                conversation_tokens=None),
                     event_id="m1", trace_id="sess-multi")]
        dq = self.report(evs).split("## 10. Data quality")[1]
        self.assertIn("tool_definitions_tokens", dq)
        self.assertIn("one context, not one per model", dq)
        self.assertIn("| Sessions with no context recorded | 1 |", dq)
        payload = json.loads(self.report(evs, fmt="json"))
        self.assertIn("model.call.tool_definitions_tokens and the other two "
                      "context levels",
                      payload["data_quality"]["null_not_zero"])

    def test_a_partial_composition_is_refused_whole(self):
        # Two of three does not add up, and the whole point of the other two
        # rows is that the composition adds up.
        evs = [event("model.call", self.T,
                     self.usage(conversation_tokens=None),
                     event_id="m1", trace_id="sess-partial")]
        agg = wr.aggregate(evs, 5)["2026-W33"]
        self.assertEqual(agg.context_tool_defs, [])
        self.assertEqual(agg.context_total, [])
        self.assertEqual(agg.context_unknown, 1)

    def test_share_is_the_median_of_per_session_shares(self):
        # Not the median tool-definition level over the median context: those
        # two medians can come from sessions that never coexisted.
        evs = self.sessions(*[(25, 25, 50)] * 4 + [(90, 5, 5)])
        agg = wr.aggregate(evs, 5)["2026-W33"]
        self.assertAlmostEqual(agg.tool_definition_share(), 25.0)

    def test_share_is_suppressed_below_min_group(self):
        evs = self.sessions((20_000, 10_000, 30_000))
        agg = wr.aggregate(evs, 5)["2026-W33"]
        self.assertIsNone(agg.tool_definition_share())
        self.assertIn("| ...tool definitions' share | n=1 | | 1 |",
                      self.report(evs))

    def test_json_offers_medians_and_no_total_key(self):
        evs = self.sessions((19_127, 11_635, 40_564), (32_122, 11_861, 75_623))
        context = json.loads(self.report(evs, fmt="json"))["cost"]["context"]
        self.assertEqual(context["tool_definitions_tokens_min"], 19_127)
        self.assertEqual(context["tool_definitions_tokens_max"], 32_122)
        self.assertEqual(context["sessions_measured"], 2)
        self.assertEqual(context["sessions_context_null_multi_model"], 0)
        self.assertIn("never multiply by request_count", context["basis"])
        # No key a dashboard could pick up and sum by accident.
        for key in context:
            self.assertNotIn("total", key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
