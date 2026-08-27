#!/usr/bin/env python3
"""ai_usage_sheets.py -- the AI-usage half of the per-person workbook.

`people_workbook.py` reads Jira, Bitbucket and AIO and is thorough on delivery,
but it carries nothing from the laptop stream: no prompts, no sessions, no
tokens, no cost. That is the "are they using AI effectively" half of the
question this project exists to answer, so this adds it to the same file.

    python3 report/people_workbook.py --person ... --out W.xlsx
    python3 report/ai_usage_sheets.py W.xlsx \
        --person "Ngoc Nguyen=5bee..." --person "Linh Hoang=712020:28cc..." \
        --input reports/2026-08/workbook-input \
        --weeks 2026-W31..2026-W35 --full-weeks 2026-W32..2026-W34 \
        --price "claude-sonnet-4.6=3.0/15.0" --price "claude-opus-4.6=5.0/25.0"

Run it AFTER the generator -- the generator writes the file from scratch and
would drop these sheets.

Three rules it keeps, because each one has already produced a wrong number here:

* **Prices are passed in, never hardcoded.** CONTRACT.md §4: cost is derived
  against *dated* pricing and is never asserted by a client. A rate baked into
  this file would be a client asserting a price, and would silently restate
  history the day a vendor changes one. A model with no `--price` is counted
  and left unpriced rather than guessed at.
* **Coverage is measured by cycle, not over the case estate.** The cycle is
  the delivery record (CONTRACT.md §3 row 22). Measured 2026-08-27 on IML:
  7,976 of 8,564 cases across the seven cycles that ran are automated (93.1%),
  where the same data read over the whole 10,742-case estate puts P3 at 22.8%
  -- a backlog nobody has triaged, not a delivery figure.
* **Partial weeks are labelled, and the change column skips them.** A Mon-Thu
  window against a full week misreads every volume metric.

Requires ``openpyxl``, like the workbook it augments. Everything else is stdlib.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import glob
import json
import os
import statistics
import sys

try:
    from openpyxl import load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:  # pragma: no cover - dependency guard
    sys.exit("ai_usage_sheets.py needs openpyxl:  python3 -m pip install openpyxl")


def parse_person(value):
    name, _, account = value.partition("=")
    if not name.strip() or not account.strip():
        raise argparse.ArgumentTypeError(
            "--person takes NAME=ACCOUNT_ID, e.g. \"Ngoc Nguyen=5bee...\"")
    return name.strip(), account.strip()


def parse_price(value):
    model, _, rates = value.partition("=")
    inp, _, out = rates.partition("/")
    try:
        return model.strip(), (float(inp), float(out))
    except ValueError:
        raise argparse.ArgumentTypeError(
            "--price takes MODEL=IN/OUT per 1M tokens, e.g. "
            "\"claude-sonnet-4.6=3.0/15.0\"")


def week_span(value):
    """``2026-W31..2026-W35`` -> every ISO week between, inclusive."""
    start, _, end = value.partition("..")
    if not end:
        return [start]
    def key(w):
        y, _, n = w.partition("-W")
        return int(y), int(n)
    (y0, n0), (y1, n1) = key(start), key(end)
    out = []
    y, n = y0, n0
    while (y, n) <= (y1, n1):
        out.append("%d-W%02d" % (y, n))
        n += 1
        if n > datetime.date(y, 12, 28).isocalendar()[1]:
            y, n = y + 1, 1
    return out


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def iso_week(ts):
    if not ts or len(ts) < 10:
        return None
    try:
        d = datetime.date.fromisoformat(ts[:10])
    except ValueError:
        return None
    y, w, _ = d.isocalendar()
    return "%d-W%02d" % (y, w)


def rows(paths):
    for path in paths:
        if os.path.isdir(path):
            found = sorted(glob.glob(os.path.join(path, "**", "*.ndjson"),
                                     recursive=True))
        else:
            found = [path]
        for one in found:
            with open(one, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            continue


def collect(inputs, people, weeks, prices):
    """Every figure these sheets render, counted from the event stream."""
    names = list(people.values())
    z = lambda: {n: {w: collections.Counter() for w in weeks} for n in names}
    ai, bb = z(), z()
    days = {n: {w: set() for w in weeks} for n in names}
    sess = {n: {w: set() for w in weeks} for n in names}
    toks = {n: {w: [] for w in weeks} for n in names}
    merge = {n: {w: [] for w in weeks} for n in names}
    cycles = collections.defaultdict(
        lambda: {"cases": set(), "auto": set(), "folders": collections.Counter(),
                 "first": None, "last": None, "runs": 0, "failed": 0,
                 "defects": 0, "people": collections.Counter()})
    estate = collections.defaultdict(collections.Counter)

    for e in rows(inputs):
        who = people.get((e.get("actor") or {}).get("person_id"))
        t, a = e.get("event_type"), (e.get("attributes") or {})
        w = iso_week(e.get("event_time"))

        if t == "test.case.snapshot":
            pri = a.get("priority") or "unset"
            st = a.get("automation_status")
            estate[pri]["total"] += 1
            estate[pri][{None: "unset", "Automated": "automated",
                         "To Be Automated": "to_be_automated",
                         "Manual": "manual",
                         "In Progress": "in_progress"}.get(st, "other")] += 1
            continue

        if t == "test.run.completed":
            key, case = a.get("test_cycle_key"), a.get("test_case_key")
            if not key:
                continue
            c = cycles[key]
            c["runs"] += 1
            if case:
                c["cases"].add(case)
                if a.get("is_automated"):
                    c["auto"].add(case)
            if a.get("folder_name"):
                c["folders"][a["folder_name"]] += 1
            if (a.get("status_category") or "").lower() == "failed":
                c["failed"] += 1
            c["defects"] += a.get("defect_count") or 0
            runner = a.get("executed_by_person_id")
            if runner:
                c["people"][people.get(runner, "other")] += 1
            when = a.get("executed_at") or ""
            if when:
                c["first"] = min(c["first"] or when, when)
                c["last"] = max(c["last"] or when, when)
            continue

        if not who or w not in weeks:
            continue

        if t in ("human.turn", "model.call", "tool.call", "scm.commit",
                 "output.generated"):
            ai[who][w][t] += 1
            days[who][w].add(e["event_time"][:10])
            if e.get("trace_id"):
                sess[who][w].add(e["trace_id"])
            if t == "model.call":
                i, o, m = (a.get("input_tokens"), a.get("output_tokens"),
                           a.get("model_id"))
                if isinstance(i, int) and isinstance(o, int):
                    toks[who][w].append((i, o, m))
            continue

        if t.startswith("scm.") or t == "scm.revert":
            bb[who][w][t] += 1
            if t == "scm.pr.merged":
                for src, dst in (("automation_scripts_added", "scripts_added"),
                                 ("automation_scripts_modified", "scripts_modified"),
                                 ("lines_added", "lines_added"),
                                 ("lines_removed", "lines_removed"),
                                 ("commit_count", "commits")):
                    bb[who][w][dst] += a.get(src) or 0
                if isinstance(a.get("merge_lead_time_ms"), (int, float)):
                    merge[who][w].append(a["merge_lead_time_ms"])

    cov = {}
    for key, c in cycles.items():
        n = len(c["cases"])
        cov[key] = {
            "cases": n, "automated": len(c["auto"]),
            "pct": round(100.0 * len(c["auto"]) / n, 1) if n else None,
            "runs": c["runs"], "failed": c["failed"], "defects": c["defects"],
            "folder": c["folders"].most_common(1)[0][0] if c["folders"] else None,
            "first": (c["first"] or "")[:10], "last": (c["last"] or "")[:10],
            "ours": {k: v for k, v in c["people"].items() if k in names},
        }

    cost = {}
    for n in names:
        cost[n] = {}
        for w in weeks:
            calls = ai[n][w]["model.call"]
            priced = [x for x in toks[n][w] if x[2] in prices]
            if not calls:
                cost[n][w] = None
                continue
            if not priced:
                # Calls happened and none could be priced. Absent, not zero.
                cost[n][w] = {"measured": None, "modelled": None, "n": 0,
                              "calls": calls, "unpriced": len(toks[n][w])}
                continue
            measured = sum(i / 1e6 * prices[m][0] + o / 1e6 * prices[m][1]
                           for i, o, m in priced)
            med_i = statistics.median([x[0] for x in priced])
            med_o = statistics.median([x[1] for x in priced])
            mix = collections.Counter(x[2] for x in priced).most_common(1)[0][0]
            per = med_i / 1e6 * prices[mix][0] + med_o / 1e6 * prices[mix][1]
            cost[n][w] = {
                "measured": round(measured, 2),
                "modelled": round(measured + per * (calls - len(priced)), 2),
                "n": len(priced), "calls": calls,
                "unpriced": len(toks[n][w]) - len(priced)}

    return {"ai": ai, "bb": bb, "days": days, "sessions": sess, "tokens": toks,
            "merge": merge, "coverage_by_cycle": cov, "estate": dict(estate),
            "cost": cost, "names": names}


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

HEAD = Font(bold=True, color="FFFFFF")
FILL = PatternFill("solid", fgColor="1F3864")
GRP = Font(bold=True)
GRPF = PatternFill("solid", fgColor="DDEBF7")
NOTE = Font(italic=True, size=9, color="555555")


def fresh(wb, title):
    if title in wb.sheetnames:
        del wb[title]
    return wb.create_sheet(title)


def head(ws, cols, widths):
    for i, c in enumerate(cols, 1):
        cell = ws.cell(row=1, column=i, value=c)
        cell.font, cell.fill = HEAD, FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(row=2, column=1)


def pct(a, b):
    return None if not b else round(100.0 * a / b, 1)


def notes(ws, row, lines):
    for line in lines:
        ws.cell(row=row, column=1, value=line).font = NOTE
        row += 1
    return row


def render(wb, data, weeks, full_weeks, window_label):
    names = data["names"]
    ai, bb, cost = data["ai"], data["bb"], data["cost"]

    def change(series):
        """First to last across FULL weeks only -- partials misread volume."""
        i0, i1 = weeks.index(full_weeks[0]), weeks.index(full_weeks[-1])
        a, b = series[i0], series[i1]
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            return None
        return None if not a else round(100.0 * (b - a) / a, 1)

    # ---------------------------------------------------------- AI Usage
    ws = fresh(wb, "AI Usage")
    head(ws, ["Person", "Week", "Window", "Prompts", "Model calls", "Tool calls",
              "Sessions", "Active days", "Prompts/active day", "Action rate %",
              "Priced calls", "Token cost $ (modelled)", "Cost/prompt $"],
         [16, 10, 24, 9, 11, 10, 9, 11, 17, 12, 11, 20, 12])
    r = 2
    for n in names:
        for w in weeks:
            prompts = ai[n][w]["human.turn"]
            if not prompts:
                continue
            d = len(data["days"][n][w])
            c = cost[n].get(w) or {}
            m = c.get("modelled")
            for col, val in enumerate([
                    n, w, window_label.get(w, "full week"), prompts,
                    ai[n][w]["model.call"], ai[n][w]["tool.call"],
                    len(data["sessions"][n][w]), d,
                    round(prompts / d, 1) if d else None,
                    pct(ai[n][w]["tool.call"], prompts), c.get("n"), m,
                    round(m / prompts, 3) if m and prompts else None], 1):
                ws.cell(row=r, column=col, value=val)
            r += 1
    notes(ws, r + 1, [
        "Action rate = tool calls / prompts. It separates the assistant DOING "
        "work (reading, editing, running) from ANSWERING questions.",
        "Token cost is MODELLED, not measured: this surface records tokens on a "
        "minority of calls, so the rest are projected from the per-week median "
        "of the priced ones. List price, no cache discount.",
        "It is NOT the invoice. Copilot bills per seat plus premium requests, "
        "and premium_requests is NULL here. Read it as an economic weight.",
        "A blank cell is a quantity with no measurement, never a zero.",
    ])

    # ------------------------------------------------------- Ten Metrics
    ws = fresh(wb, "Ten Metrics")
    head(ws, ["#", "Metric / sub-measure", "Want", "Person"] +
         [w[-3:] for w in weeks] +
         ["%s->%s" % (full_weeks[0][-3:], full_weeks[-1][-3:]), "Note"],
         [5, 40, 6, 20] + [10] * len(weeks) + [10, 64])
    r = 2

    def group(num, name):
        nonlocal r
        for col in range(1, 12):
            ws.cell(row=r, column=col).fill = GRPF
        ws.cell(row=r, column=1, value=num).font = GRP
        ws.cell(row=r, column=2, value=name).font = GRP
        r += 1

    def line(sub, want, person, series, note=""):
        nonlocal r
        ws.cell(row=r, column=2, value="   " + sub)
        ws.cell(row=r, column=3, value=want)
        ws.cell(row=r, column=4, value=person)
        for i, v in enumerate(series):
            ws.cell(row=r, column=5 + i,
                    value=v if v is not None else "no data")
        d = change(series)
        ws.cell(row=r, column=5 + len(weeks),
                value=("%+.0f%%" % d) if d is not None else "-")
        ws.cell(row=r, column=6 + len(weeks), value=note)
        r += 1

    def free(text, note=""):
        nonlocal r
        ws.cell(row=r, column=2, value="   " + text)
        if note:
            ws.cell(row=r, column=6 + len(weeks), value=note)
        r += 1

    group(1, "Automation Output")
    for n in names:
        line("Automation scripts added", "up", n,
             [bb[n][w]["scripts_added"] for w in weeks],
             "Spec / step-definition / feature files added in merged PRs")
        line("Automation scripts modified", "up", n,
             [bb[n][w]["scripts_modified"] for w in weeks])
        line("PRs merged", "up", n,
             [bb[n][w]["scm.pr.merged"] for w in weeks])
        line("AI outputs written (files)", "up", n,
             [ai[n][w]["output.generated"] for w in weeks],
             "Files the assistant itself wrote, from the laptop stream")

    group(2, "Automation Coverage  (BY CYCLE)")
    cov = data["coverage_by_cycle"]
    tc = sum(c["cases"] for c in cov.values())
    ta = sum(c["automated"] for c in cov.values())
    ws.cell(row=r, column=2, value="   All delivered cycles")
    ws.cell(row=r, column=3, value="up")
    ws.cell(row=r, column=5,
            value="%.1f%% (%d / %d)" % (100.0 * ta / tc, ta, tc) if tc else "no data")
    ws.cell(row=r, column=6 + len(weeks),
            value="The cycle is the delivery record (CONTRACT.md 3 row 22), so "
                  "'eligible' means the cases in the cycles actually being run "
                  "-- not every case that has ever existed in the project.")
    r += 1
    for key in sorted(cov, key=lambda k: -cov[k]["cases"]):
        c = cov[key]
        ws.cell(row=r, column=2, value="      %s  (%s)" % (key, c["folder"] or "-"))
        ws.cell(row=r, column=4,
                value=", ".join("%s %d" % kv for kv in sorted(c["ours"].items()))
                or "neither")
        ws.cell(row=r, column=5, value="%.1f%% (%d / %d)"
                % (c["pct"], c["automated"], c["cases"]))
        ws.cell(row=r, column=6 + len(weeks),
                value="%s to %s - %d runs, %d failed, %d defects"
                      % (c["first"], c["last"], c["runs"], c["failed"], c["defects"]))
        r += 1
    est = data["estate"]
    if est:
        free("Backlog view (whole case estate)",
             "A DIFFERENT question, kept separate on purpose: "
             + "; ".join("%s %d automated / %d to-be-automated / %d no status"
                         % (k, est[k].get("automated", 0),
                            est[k].get("to_be_automated", 0), est[k].get("unset", 0))
                         for k in ("High", "Medium", "Low") if k in est)
             + ". Reading this as coverage makes an untriaged backlog look like "
               "a delivery failure. Label it backlog, never coverage.")
    r += 1

    group(3, "First-Pass Acceptance Rate")
    for n in names:
        line("Reviews given", "up", n,
             [bb[n][w]["scm.pr.reviewed"] for w in weeks],
             "No rate is meaningful at this denominator -- state the counts")

    group(4, "Rework Rate")
    for n in names:
        rev = [bb[n][w]["scm.revert"] for w in weeks]
        mer = [bb[n][w]["scm.pr.merged"] for w in weeks]
        line("Reverts", "down", n, rev,
             "A revert is a signal to read, not a defect count")
        line("Reverts / PRs merged %", "down", n,
             [pct(a, b) for a, b in zip(rev, mer)])

    group(5, "Automation Lead Time")
    for n in names:
        line("Median PR merge lead time (h)", "down", n,
             [round(statistics.median(data["merge"][n][w]) / 3600000.0, 2)
              if data["merge"][n][w] else None for w in weeks],
             "Nearest available proxy. The test-case -> merge link is not built, "
             "so this is NOT Automation Lead Time as the metric defines it")

    group(6, "Productivity Gain")
    free("excluded",
         "The formula needs a manual control group. None exists and none is "
         "planned. Do not report a number here.")
    r += 1

    group(7, "Execution Rate")
    for n in names:
        line("AI prompts", "up", n, [ai[n][w]["human.turn"] for w in weeks],
             "Laptop stream -- the AI side of execution")
        line("Active days", "up", n,
             [len(data["days"][n][w]) for w in weeks])
    free("Test runs executed, per cycle",
         "; ".join("%s %d" % (k, cov[k]["runs"])
                   for k in sorted(cov, key=lambda x: -cov[x]["runs"]))
         + ". Volume follows cycle scheduling, not effort.")
    r += 1

    group(8, "Flaky Test Rate")
    free("Failed runs, per cycle",
         "; ".join("%s %d" % (k, cov[k]["failed"])
                   for k in sorted(cov, key=lambda x: -cov[x]["failed"]))
         + ". NOT flakiness: AIO keeps one run per case per cycle and overwrites "
           "on re-run, so an intermittent test leaves no trace. The metric as "
           "defined is unmeasurable from this source.")
    r += 1

    group(9, "AI Cost per Accepted Output")
    for n in names:
        line("Token cost $ (modelled)", "down", n,
             [(cost[n].get(w) or {}).get("modelled") for w in weeks],
             "Numerator only. Method and caveats on the AI Usage sheet.")
    free("Denominator: accepted outputs",
         "Does not exist. CONTRACT.md 2.4 admits only link.method='explicit' to "
         "the cost metrics, and this surface emits run_id null on every event, "
         "so no row qualifies. The figures above are an economic weight, not "
         "this metric.")
    r += 1

    group(10, "AI ROI")
    free("excluded",
         "Value delivered is not observable from any source wired up here.")

    # ------------------------------------------------------- Productivity
    ws = fresh(wb, "Productivity")
    head(ws, ["Person", "Week", "Window", "Prompts/active day", "Action rate %",
              "Commits", "Commits/prompt", "Lines changed (PRs)",
              "Scripts touched", "Scripts/prompt", "PRs merged",
              "Cost $/PR merged", "Cost $/script touched"],
         [16, 10, 24, 17, 12, 9, 14, 18, 15, 14, 10, 16, 20])
    r = 2
    for n in names:
        for w in weeks:
            prompts = ai[n][w]["human.turn"]
            merged = bb[n][w]["scm.pr.merged"]
            if not prompts and not merged:
                continue
            d = len(data["days"][n][w])
            commits = ai[n][w]["scm.commit"]
            scripts = bb[n][w]["scripts_added"] + bb[n][w]["scripts_modified"]
            lines = bb[n][w]["lines_added"] + bb[n][w]["lines_removed"]
            c = (cost[n].get(w) or {}).get("modelled")
            for col, val in enumerate([
                    n, w, window_label.get(w, "full week"),
                    round(prompts / d, 1) if d else None,
                    pct(ai[n][w]["tool.call"], prompts), commits,
                    round(commits / prompts, 2) if prompts else None,
                    lines, scripts,
                    round(scripts / prompts, 2) if prompts else None, merged,
                    round(c / merged, 2) if c and merged else None,
                    round(c / scripts, 2) if c and scripts else None], 1):
                ws.cell(row=r, column=col, value=val)
            r += 1
    notes(ws, r + 1, [
        "These are RATIOS OF MEASURED THINGS. None of them is metric 6 "
        "(Productivity Gain), which needs a control group and stays excluded.",
        "Commits come from the laptop git scan and PR lines from the SCM "
        "poller: different populations, deliberately never summed.",
        "Cost per unit uses the modelled token cost -- a direction of travel, "
        "not an invoice.",
        "A blank cell is a quantity with no denominator that week, never a zero.",
    ])

    order = ["Summary", "Ten Metrics", "AI Usage", "Productivity"]
    wb._sheets = ([wb[t] for t in order if t in wb.sheetnames] +
                  [s for s in wb._sheets if s.title not in order])


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Add AI-usage, ten-metric and productivity sheets to a "
                    "workbook written by people_workbook.py.")
    p.add_argument("workbook", help="the .xlsx to augment, in place")
    p.add_argument("--person", type=parse_person, action="append", required=True,
                   metavar="NAME=ACCOUNT_ID",
                   help="Repeatable. Only named people appear in the file.")
    p.add_argument("--input", nargs="+", required=True,
                   help="NDJSON files or directories.")
    p.add_argument("--weeks", type=week_span, required=True,
                   metavar="YYYY-Www..YYYY-Www", help="Week span to render.")
    p.add_argument("--full-weeks", type=week_span, metavar="YYYY-Www..YYYY-Www",
                   help="The weeks that are complete. The change column spans "
                        "these only, so a partial week never misreads volume. "
                        "Defaults to every week given.")
    p.add_argument("--price", type=parse_price, action="append", default=[],
                   metavar="MODEL=IN/OUT",
                   help="Repeatable, per 1M tokens. A model with no price is "
                        "counted and left unpriced, never guessed.")
    p.add_argument("--partial", action="append", default=[],
                   metavar="YYYY-Www=REASON",
                   help="Repeatable. Labels a week's Window column.")
    args = p.parse_args(argv)

    if not os.path.exists(args.workbook):
        raise SystemExit("no such workbook: %s -- run people_workbook.py first"
                         % args.workbook)

    people = {acct: name for name, acct in args.person}
    prices = dict(args.price)
    weeks = args.weeks
    full = args.full_weeks or weeks
    missing = [w for w in full if w not in weeks]
    if missing:
        raise SystemExit("--full-weeks names weeks outside --weeks: %s"
                         % ", ".join(missing))
    labels = {}
    for item in args.partial:
        w, _, why = item.partition("=")
        labels[w.strip()] = why.strip() or "partial week"

    data = collect(args.input, people, weeks, prices)
    wb = load_workbook(args.workbook)
    render(wb, data, weeks, full, labels)
    wb.save(args.workbook)

    print(json.dumps({
        "msg": "ai_sheets_written", "workbook": args.workbook,
        "people": sorted(people.values()), "weeks": len(weeks),
        "cycles": len(data["coverage_by_cycle"]),
        "priced_models": sorted(prices),
        "unpriced_calls": sum((data["cost"][n][w] or {}).get("unpriced", 0)
                              for n in data["names"] for w in weeks
                              if data["cost"][n].get(w)),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
