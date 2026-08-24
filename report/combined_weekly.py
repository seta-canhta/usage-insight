#!/usr/bin/env python3
"""combined_weekly.py -- one Markdown file: named people plus the project.

Produces the single document a lead actually circulates: a weekly view of the
engineers they are supporting, followed by the project-level position, in one
file rather than a workbook plus two HTML reports.

    python3 combined_weekly.py \
        --person "A Name=<atlassian-account-id>" \
        --person "Another Name=<atlassian-account-id>" \
        --input ../../../reports/exports \
        --release-input ../../../reports/exports/release-26.8 \
        --release-label "26.8" \
        --since 2026-06-21 \
        --out ../../../reports/weekly-combined.md

Reuse, not reimplementation
---------------------------
Every number here is computed by code that already exists and is already tested:
person-level aggregation by ``people_workbook.py``, project-level aggregation and
automation coverage by ``weekly_report.py``. This module only *renders*. That
split is deliberate -- rendering can differ between outputs without consequence,
but two copies of an aggregation drift, and a drifted metric is worse than a
missing one because it looks authoritative.

Stdlib only. Unlike ``people_workbook.py`` it needs no openpyxl.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


wr = _load("weekly_report")
pw = _load("people_workbook")


# --------------------------------------------------------------------------
# rendering helpers
# --------------------------------------------------------------------------

def cell(value: Any) -> str:
    """Render one table cell, keeping 'no data' distinct from zero."""
    if value is None:
        return "no data"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]],
          align_right_from: int = 1) -> List[str]:
    if not rows:
        return []
    out = ["| " + " | ".join(headers) + " |"]
    out.append("|" + "|".join(
        "---:" if i >= align_right_from else "---" for i in range(len(headers))
    ) + "|")
    for row in rows:
        out.append("| " + " | ".join(cell(v) for v in row) + " |")
    out.append("")
    return out


def pct(value: Optional[float]) -> str:
    """One decimal place, always. Raw ratios arrive as 9.090909090909092."""
    if value is None:
        return "no data"
    if isinstance(value, str):
        return value
    return f"{round(float(value), 1)}%"


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------

def section_at_a_glance(data: Dict[str, "pw.PersonData"],
                        trends: Dict[str, Dict[str, Any]], label: str,
                        window_desc: str, names: Sequence[str]) -> List[str]:
    """Two tables: the reported week, then the whole window.

    Keeping them apart matters. An earlier draft put window totals under a
    heading that named a single week, which is the most direct way to make a
    reader believe someone merged 53 pull requests in five days.
    """
    out = ["## 1. At a glance", "", f"**This week — {label}**", ""]
    week_rows = []
    for metric in pw.TREND_METRICS:
        values = [(trends[n].get(label) or {}).get(metric) for n in names]
        if all(v in (None, 0) for v in values):
            continue          # nothing happened; the row would be noise
        week_rows.append([metric] + values)
    if week_rows:
        out += table(["Metric"] + list(names), week_rows)
    else:
        out += ["_No activity recorded for either person in this week._", ""]

    out.append(f"**Whole window — {window_desc}**")
    out.append("")
    rows: List[List[Any]] = []

    def add(label: str, fn, basis=None, note: str = "") -> None:
        cells: List[Any] = []
        for name in names:
            person = data[name]
            cells.append(None if (basis and not basis(person)) else fn(person))
        rows.append([label] + cells + [note])

    has_prs = lambda p: bool(p.prs)                       # noqa: E731
    has_tests = lambda p: bool(p.test_runs)               # noqa: E731
    has_issues = lambda p: bool(p.issues)                 # noqa: E731
    owns_cases = lambda p: bool(p.owned_cases)            # noqa: E731

    add("Issues raised", lambda p: sum(1 for r in p.issues.values()
                                       if "reporter" in r["roles"]), has_issues)
    add("Issues resolved", lambda p: sum(
        1 for r in p.issues.values()
        if "assignee" in r["roles"] and r["issue"].get("resolved_at")), has_issues)
    add("PRs merged", lambda p: sum(1 for v in p.prs.values()
                                    if v["event_type"] == "scm.pr.merged"), has_prs)
    add("Automation scripts created",
        lambda p: sum(v["attrs"].get("automation_scripts_added") or 0
                      for v in p.prs.values()), has_prs,
        ".feature / step-definition / spec files added")
    add("PRs with a reviewer",
        lambda p: sum(1 for v in p.prs.values()
                      if (v["attrs"].get("reviewer_count") or 0) > 0), has_prs,
        "A zero here is a real zero")
    add("Median merge lead time (h)", lambda p: pw.med(
        [pw.ms_to_hours(v["attrs"].get("merge_lead_time_ms"))
         for v in p.prs.values()]), has_prs, "Median, never the mean")
    add("Test runs executed", lambda p: pw.test_totals(p)["executed"], has_tests,
        "Excludes rows AIO seeded at Not Run")
    add("Test pass rate", lambda p: (
        pct(round(100 * pw.test_totals(p)["passed"] / pw.test_totals(p)["executed"], 1))
        if pw.test_totals(p)["executed"] else None), has_tests,
        "Blocked and skipped stay in the denominator")
    add("Defects raised from runs",
        lambda p: sum(r["attrs"].get("defect_count") or 0 for r in p.test_runs),
        has_tests)
    def person_coverage(p: "pw.PersonData") -> Optional[str]:
        totals = pw.coverage_totals(p)
        if not totals["known"]:
            return None
        value = round(100 * totals["automated"] / totals["known"], 1)
        if totals["status_unset"] > totals["known"]:
            # A clean "100%" over a denominator smaller than the pile of
            # unclassified cases is arithmetically right and tells you nothing.
            return (f"{value}% ⚠️ ({totals['status_unset']} of {totals['owned']} "
                    f"cases have no status)")
        return f"{value}%"

    add("Automation coverage (their cases)", person_coverage, owns_cases,
        "Cases with no status set are excluded from the rate")

    out += table(["Metric"] + list(names) + ["Note"], rows)
    out.append("> These two do different jobs — read **down** the columns, not "
               "across them. One engineer's work lands in Jira and AIO, the "
               "other's in Bitbucket; putting them side by side invites a "
               "comparison the data does not support.")
    out.append("")
    return out


def section_trend(trends: Dict[str, Dict[str, Any]], weeks: Sequence[str],
                  names: Sequence[str], partial: Optional[str]) -> List[str]:
    out = ["## 2. Weekly trend", ""]
    if not weeks:
        out += ["_Not enough history to draw a trend._", ""]
        return out
    shown = list(weeks)[-8:]
    for metric in pw.TREND_METRICS:
        rows = []
        for name in names:
            rows.append([name] + [
                (trends[name].get(w) or {}).get(metric) for w in shown])
        out.append(f"**{metric}**")
        out.append("")
        out += table(["Person"] + [w[-3:] for w in shown], rows)
    out.append(f"> Weeks shown are ISO weeks; the current week"
               f"{f' ({partial})' if partial else ''} is excluded because a "
               f"part-finished week always reads as a decline. A `no data` cell "
               f"is a week with nothing to divide by — it is not a zero. Four or "
               f"five weeks is context, not a trend; read the slope over eight.")
    out.append("")
    return out


def section_person(name: str, person: "pw.PersonData", index: int) -> List[str]:
    out = [f"## {index}. {name}", ""]

    if person.prs:
        merged = [v for v in person.prs.values()
                  if v["event_type"] == "scm.pr.merged"]
        leads = [pw.ms_to_hours(v["attrs"].get("merge_lead_time_ms"))
                 for v in person.prs.values()]
        reviewed = sum(1 for v in person.prs.values()
                       if (v["attrs"].get("reviewer_count") or 0) > 0)
        out.append("**Pull requests**")
        out.append("")
        out += table(["Metric", "Value"], [
            ["Authored", len(person.prs)],
            ["Merged", len(merged)],
            ["Declined", sum(1 for v in person.prs.values()
                             if v["event_type"] == "scm.pr.declined")],
            ["With at least one reviewer", reviewed],
            ["Median merge lead time (h)", pw.med(leads)],
            ["p85 merge lead time (h)", pw.p85(leads)],
            ["Automation scripts created",
             sum(v["attrs"].get("automation_scripts_added") or 0
                 for v in person.prs.values())],
            ["Automation scripts modified",
             sum(v["attrs"].get("automation_scripts_modified") or 0
                 for v in person.prs.values())],
            ["Lines added", sum(v["attrs"].get("lines_added") or 0
                                for v in person.prs.values())],
            ["Lines removed", sum(v["attrs"].get("lines_removed") or 0
                                  for v in person.prs.values())],
            ["Reverts touching their commits", len(person.reverts)],
        ])
        if reviewed == 0 and merged:
            out.append(f"> ⚠️ **{len(merged)} pull requests merged, none of them "
                       f"reviewed.** That is a measured zero, not missing data, "
                       f"and it is a finding about how the repository is used "
                       f"rather than about this engineer. It also means "
                       f"first-pass acceptance and rework rate cannot be "
                       f"computed at all — there is no first review to measure.")
            out.append("")

    if person.test_runs:
        totals = pw.test_totals(person)
        cycles = sorted({r["attrs"].get("test_cycle_key")
                         for r in person.test_runs
                         if r["attrs"].get("test_cycle_key")})
        out.append("**Test execution**")
        out.append("")
        out += table(["Metric", "Value"], [
            ["Runs executed", totals["executed"]],
            ["...passed", totals["passed"]],
            ["...failed", totals["failed"]],
            ["...blocked", totals["blocked"]],
            ["Pass rate", pct(round(100 * totals["passed"] / totals["executed"], 1))
             if totals["executed"] else None],
            ["Automated runs", sum(1 for r in person.test_runs
                                   if r["attrs"].get("is_automated"))],
            ["Defects raised", sum(r["attrs"].get("defect_count") or 0
                                   for r in person.test_runs)],
            ["Cycles worked in", len(cycles)],
            ["Cases assigned but run by someone else", len(person.test_assigned)],
        ])
        if cycles:
            out.append(f"Cycles: {', '.join(cycles)}")
            out.append("")

    if person.issues:
        assigned = [r for r in person.issues.values() if "assignee" in r["roles"]]
        raised = [r for r in person.issues.values() if "reporter" in r["roles"]]
        out.append("**Jira**")
        out.append("")
        out += table(["Metric", "Value"], [
            ["Issues assigned", len(assigned)],
            ["Issues raised", len(raised)],
            ["Resolved", sum(1 for r in assigned if r["issue"].get("resolved_at"))],
            ["Bugs assigned", sum(1 for r in assigned
                                  if r["issue"].get("issue_type") == "Bug")],
            ["Carrying an AI label",
             sum(1 for r in person.issues.values()
                 if r["attribution"].get("has_ai_labels"))],
            # Drift is reported next to the AI count, never folded into it: each
            # unrecognised marker is AI work the figure above is missing.
            ["Carrying an UNRECOGNISED AI label",
             sum(1 for r in person.issues.values()
                 if r["attribution"].get("has_ai_label_drift"))],
            ["Median issue age at transition (h)",
             pw.med([t["age_hours"] for t in person.transitions])],
        ])
    return out


def coverage_rows(inventory: Dict[str, Any]) -> List[List[Any]]:
    rows = []
    for entry in inventory.get("by_priority") or []:
        counts = entry["counts"]
        rows.append([entry["label"], counts["live"], counts["automated"],
                     counts["to be automated"], counts["unset"],
                     pct(entry["coverage_pct"])])
    return rows


def section_project(week, inventory: Dict[str, Any], index: int,
                    heading: str, note: str) -> List[str]:
    out = [f"## {index}. {heading}", ""]
    if note:
        out += [f"> {note}", ""]

    has_delivery = week is not None and (
        week.prs_merged or week.prs_declined or week.scripts_added
        or week.scripts_modified or week.prs_reverted)
    if has_delivery:
        closed = week.prs_merged + week.prs_declined
        out.append("**Delivery — this week**")
        out.append("")
        out += table(["Metric", "Value"], [
            ["PRs merged", week.prs_merged],
            ["PRs declined", week.prs_declined],
            ["Decline rate", pct(week.pr_decline_rate()) if closed else None],
            ["Median merge lead time",
             wr.fmt_duration(wr.median(week.merge_lead_ms))],
            ["p85 merge lead time",
             wr.fmt_duration(wr.percentile(week.merge_lead_ms, 0.85))],
            ["Automation scripts created", week.scripts_added],
            ["Automation scripts modified", week.scripts_modified],
            ["Reverts", week.prs_reverted],
            ["Active people", len(week.people)],
        ])

    if week is not None and week.tests_by_category:
        out.append("**Test execution — this week**")
        out.append("")
        out += table(["Metric", "Value"], [
            ["Runs executed", week.tests_executed],
            ["...passed", week.tests_by_category["passed"]],
            ["...failed", week.tests_by_category["failed"]],
            ["...blocked", week.tests_by_category["blocked"]],
            ["Pass rate", pct(week.test_pass_rate())],
            ["Automated share",
             pct(wr.ratio(week.tests_automated, week.tests_executed, 5))],
            ["Defects raised", week.test_defects],
            ["Cycles touched", len(week.test_cycles)],
        ])

    if inventory and inventory.get("cases"):
        counts, known = inventory["counts"], inventory["known"]
        out.append(f"**Automation coverage — position as at "
                   f"{(inventory.get('as_at') or '')[:10]}**")
        out.append("")
        out += table(["Metric", "Value"], [
            ["Coverage (AIO dashboard formula)", pct(inventory["coverage_pct"])],
            ["...automated", counts["automated"]],
            ["...to be automated", counts["to be automated"]],
            ["...manual", counts["manual"]],
            ["...in progress", counts["in progress"]],
            ["...not assigned", counts["unset"]],
            ["Live cases in scope", counts["live"]],
            ["Of triaged cases only", pct(inventory["coverage_pct_classified"])],
        ])
        rows = coverage_rows(inventory)
        if len(rows) > 1:
            out += table(["Priority", "Cases", "Automated", "To be automated",
                          "Not assigned", "Coverage"], rows)
        out.append("> **Coverage is automated / every live case in scope** — the "
                   "same formula as the AIO \"Regression Test Automation "
                   "Coverage\" dashboard, whose own tile reports Total − "
                   "Automated as \"Non Automated\". Manual, In Progress and Not "
                   "Assigned therefore all count against you. The triaged-only "
                   "figure is given underneath because it answers a different "
                   "question: of what has been looked at, how much is done.")
        out.append("")
        out.append("> Coverage is a **stock, not a flow** — the state of the test "
                   "estate at a moment, so it is a position rather than a weekly "
                   "figure. The priority split uses AIO's own High / Medium / Low "
                   "scale; AIO has no P1/P2/P3 field, and the AIO dashboard does "
                   "not split by priority at all.")
        out.append("")
    return out


def section_coverage(index: int, coverage_path: Optional[str]) -> List[str]:
    """How much of reality the figures cover.

    Local collection is hand-delivered, so a missing week is the ordinary case
    rather than a fault -- somebody forgets, somebody is on leave, somebody
    joins mid-quarter. Rendering those weeks as activity of zero shows a team
    getting worse when it is only getting quieter, so every aggregate above has
    to be read against this section.
    """
    out = [f"## {index}. How much of this is covered", ""]

    if not coverage_path or not os.path.exists(coverage_path):
        out += [
            "**Local collection has not started.** Copilot usage, agent runs "
            "and commit-time run ids are all still absent, which is why the "
            "cost metrics have no figure. Everything above comes from Jira, "
            "Bitbucket and AIO, which are read directly and are complete for "
            "the window.",
            "",
            "This is a stated absence, not a zero. See `docs/WHAT-WE-MEASURE.md`.",
            "",
        ]
        return out

    with open(coverage_path, "r", encoding="utf-8") as fh:
        report = json.load(fh)

    seen = report.get("weeks_seen") or []
    if seen:
        machine_weeks = report["machine_weeks_covered"]
        machines = report["machines"]
        span = seen[0] if len(seen) == 1 else f"{seen[0]} to {seen[-1]}"
        out.append(
            f"**{machine_weeks} machine-week{'' if machine_weeks == 1 else 's'}** "
            f"across {machines} machine{'' if machines == 1 else 's'}, {span}.")
    else:
        out.append("**No bundles have been handed over yet.**")
    out.append("")

    rows = []
    for machine, entry in sorted(report.get("by_machine", {}).items()):
        missing = entry["weeks_missing_within_span"]
        rows.append([
            machine[:8],
            len(entry["weeks_covered"]),
            entry["bundles"],
            f"{entry['events']:,}",
            # Named, not counted. "3 weeks missing" invites a shrug; the weeks
            # themselves invite a question about what happened in them.
            ", ".join(missing) if missing else "—",
        ])
    out += table(["Machine", "Weeks", "Bundles", "Events", "Weeks missing"],
                 rows, align_right_from=1)
    out.append("> A week with no bundle is **absent**, not zero. A bundle "
               "carrying no events is a measured zero, and the two are counted "
               "differently everywhere in this report.")
    out.append("")
    return out


def section_gaps(index: int, gaps: Sequence[str]) -> List[str]:
    out = [f"## {index}. What this report cannot answer", ""]
    # align_right_from is past the last column: both columns here are prose, and
    # right-aligned prose is unreadable.
    out += table(["Metric", "Why"], [g.split("|", 1) for g in gaps],
                 align_right_from=2)
    out.append("> No ROI, no time-saved, no AI-versus-human split. Every task "
               "here uses AI, so there is no control group; any such figure "
               "would be a scenario model wearing the clothes of a measurement.")
    out.append("")
    return out


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

# Reviewed against the 2026-08-24 measurements. Two entries were removed rather
# than reworded: first-pass acceptance and rework rate were listed here as
# having no denominator because pull requests merge without review -- true of
# Bitbucket, and beside the point, because this team's review happens in the
# Jira workflow. Both are now reported figures. A gap list that keeps naming
# solved problems trains readers to skip it.
DEFAULT_GAPS = (
    "Automation lead time|AIO test cases carry no Jira key -- 0 of 4,248 -- so a "
    "case cannot be traced to the work that created it. The daily-report "
    "reconciliation builds that mapping; until it has run, there is no figure.",
    "Flaky test rate|AIO stores one result per case per cycle and overwrites on "
    "re-run, so no re-run history exists. A previously reported 2.6% compared "
    "results across different cycles -- different code -- and is withdrawn.",
    "Token usage and cost per accepted output|Copilot reports its own usage and "
    "that reporting is switched off. Turning it on is configuration on developer "
    "machines; `emit.py` then supplies the accepted-output denominator.",
    "Automation output attributable to AI|Scripts added are counted, but not "
    "which of them AI produced. Needs the same telemetry as the cost metric.",
    "CI pass rate and build duration|CI is Jenkins, not Bitbucket Pipelines. "
    "Needs the Jenkins API or the commit-status poller.",
    "Time actually spent|Jira worklogs and AIO effort are both essentially "
    "unfilled. Nothing else observes effort.",
    "Return on AI spend|Requires a monetary value for delivered work, which is a "
    "management definition rather than a measurement. The cost side arrives with "
    "the Copilot telemetry; the value side is a decision.",
)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="One Markdown weekly report: named people plus the project.")
    ap.add_argument("--person", action="append", required=True,
                    metavar="NAME=ACCOUNT_ID")
    ap.add_argument("--input", nargs="+", required=True,
                    help="NDJSON files or directories for the project view.")
    ap.add_argument("--release-input", nargs="*", default=[],
                    help="Optional second input set for a release-scoped section.")
    ap.add_argument("--release-label", default="",
                    help="Release name, e.g. 26.8.")
    ap.add_argument("--since", help="YYYY-MM-DD window start.")
    ap.add_argument("--week", help="ISO week (YYYY-Www) or a date. Defaults to "
                                   "the most recent complete week.")
    ap.add_argument("--min-group", type=int, default=5)
    ap.add_argument("--scope-note", default="")
    ap.add_argument("--coverage", help="coverage.json from importers/bundle.py. "
                    "Absent renders as 'local collection has not started', "
                    "never as zero.")
    ap.add_argument("--out", help="Output .md path (default: stdout).")
    args = ap.parse_args(argv)

    people: Dict[str, str] = {}
    for spec in args.person:
        if "=" not in spec:
            print(f"error: --person needs NAME=ACCOUNT_ID, got: {spec}",
                  file=sys.stderr)
            return 2
        name, _, account = spec.partition("=")
        people[name.strip()] = account.strip()
    names = list(people)

    events, stats = pw.load_events(args.input)
    if not events:
        print("error: no events found in the given input", file=sys.stderr)
        return 2
    since = pw.parse_ts(args.since + "T00:00:00Z") if args.since else None

    data = pw.collect(events, people, since)
    trends = {n: pw.weekly_trend(p, since) for n, p in data.items()}
    all_weeks = pw.week_range(w for t in trends.values() for w in t)
    current_week = pw.iso_week(datetime.now(timezone.utc))
    complete = [w for w in all_weeks if w != current_week]

    weeks = wr.aggregate(events, args.min_group)
    label = wr.resolve_week(args.week, weeks)
    inventory = wr.inventory_coverage(events)

    out: List[str] = [f"# Weekly report · {label}", ""]
    if args.scope_note:
        out += [f"> **Scope: {args.scope_note}**", ""]
    start, end = wr.week_bounds(label)
    out.append(f"**Reported week** {start:%Y-%m-%d} → "
               f"{(end - timedelta(days=1)):%Y-%m-%d} (UTC) · "
               f"**People** {', '.join(names)} · "
               f"**Events read** {len(events):,}")
    out.append("")
    out.append("Section 1 gives the reported week first, then the whole window. "
               "Sections 3 onward are **window** totals unless a heading says "
               "otherwise — a single week is too small to read a person by.")
    out.append("")

    window_desc = (f"{args.since} → today" if args.since
                   else "all data supplied")
    out += section_at_a_glance(data, trends, label, window_desc, names)
    out += section_trend(trends, complete, names, current_week)

    index = 3
    for name in names:
        out += section_person(name, data[name], index)
        index += 1

    out += section_project(
        weeks.get(label), inventory, index,
        "Project — business as usual",
        "Everyone in the project, not only the two people above. Automation "
        "coverage here is the P1+P2 commitment.")
    index += 1

    if args.release_input:
        rel_events, _ = pw.load_events(args.release_input)
        if rel_events:
            rel_weeks = wr.aggregate(rel_events, args.min_group)
            out += section_project(
                rel_weeks.get(label), wr.inventory_coverage(rel_events), index,
                f"Release {args.release_label}".strip(),
                f"Scoped to the cycles belonging to release "
                f"{args.release_label}. A release under test has to cover P1, P2 "
                f"**and** P3, so no priority filter is applied here — which is "
                f"why its coverage figure is not comparable with the one above.")
            index += 1

    out += section_coverage(index, args.coverage)
    index += 1

    out += section_gaps(index, DEFAULT_GAPS)
    out.append("---")
    out.append("")
    out.append(f"Generated from {stats['files']} NDJSON files, "
               f"{stats['lines']:,} lines, {stats['malformed']} malformed, "
               f"{stats['duplicates']} duplicate event ids dropped.")
    out.append("")

    body = "\n".join(out)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".",
                    exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(body)
        print(f"wrote {args.out} ({len(body):,} bytes)")
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
