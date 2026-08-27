#!/usr/bin/env python3
"""The snapshot the /insights and /activities screens draw.

One source of truth for the numbers: this imports ``ai_usage_sheets.collect``
rather than re-deriving anything, so a figure on the screen and the same figure
in the workbook cannot drift apart. If they ever disagree, one of them is not
reading this file.

Why a snapshot and not a live query. The screens want Jira, AIO test and
Bitbucket alongside the Copilot telemetry, and three of those four need
credentials that deliberately do not exist on the box that serves the page --
it runs untrusted workflow code. So the pull happens where the credentials are,
the derived figures are written here, and the file that ships carries counts and
no secrets: no tokens, no emails, no paths, no prompt or response text.

The shape follows one rule throughout, the same one the workbook follows:
**absent is not zero**. A measured zero is ``0`` and an unmeasured quantity is
``null``, and the pages draw those two differently.
"""

import argparse
import datetime
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ai_usage_sheets as sheets  # noqa: E402


SCHEMA = 1

#: Validated for colour vision as a pair -- dE 24.7 protan, 33.6 normal --
#: which is why the pages take the hues from here rather than picking their
#: own. Two people, two hues, assigned in fixed order and never cycled.
SERIES = ("#2A78D6", "#EB6834")


def week_label(week, last_seen=None):
    """`3-9 Aug`, clipped to the last day anything was actually recorded.

    A part week that advertises dates nobody has data for reads as a week of
    silence rather than a week that has not finished yet.
    """
    year, num = int(week[:4]), int(week[-2:])
    monday = datetime.date.fromisocalendar(year, num, 1)
    sunday = monday + datetime.timedelta(days=6)
    if last_seen and sunday.isoformat() > last_seen:
        sunday = datetime.date.fromisoformat(last_seen)
    if monday.month == sunday.month:
        span = "%d–%d %s" % (monday.day, sunday.day, sunday.strftime("%b"))
    else:
        span = "%d %s – %d %s" % (monday.day, monday.strftime("%b"),
                                       sunday.day, sunday.strftime("%b"))
    return span, monday.isoformat(), sunday.isoformat(), (sunday - monday).days + 1


def series(data, weeks, names, get):
    """`{person: [v, ...]}` aligned to `weeks`, one entry per person."""
    return {n: [get(n, w) for w in weeks] for n in names}


def build(data, weeks, full_weeks, window_label, roles, pronouns):
    """Everything both screens draw, in one object."""
    names = data["names"]
    ai, bb, cost = data["ai"], data["bb"], data["cost"]
    wkp, wk = data["week_people"], data["week"]
    last_seen = max((d for n in names for w in weeks
                     for d in data["days"][n][w]), default=None)

    week_rows = []
    for w in weeks:
        span, mon, sun, days = week_label(w, last_seen)
        week_rows.append({
            "week": w, "short": w[-3:], "label": span,
            "from": mon, "to": sun, "days": days,
            "partial": w in window_label,
            "note": window_label.get(w),
            # Volume may only be compared across whole weeks. The pages read
            # this flag rather than each re-deciding, because deciding it twice
            # is how a part week ends up in a trend.
            "full": w in full_weeks})

    people = [{"name": n,
               "role": roles.get(n),
               "pronouns": pronouns(n).subj,
               "colour": SERIES[i % len(SERIES)]}
              for i, n in enumerate(names)]

    def per(key, source):
        return series(data, weeks, names, lambda n, w: source[n][w][key])

    def qa(key):
        return series(data, weeks, names,
                      lambda n, w: wkp.get(w, {}).get(n, {}).get(key, 0))

    def rate(a, b, source):
        return series(data, weeks, names,
                      lambda n, w: sheets.pct(source[n][w][a], source[n][w][b]))

    def money(key):
        return series(data, weeks, names,
                      lambda n, w: (cost[n].get(w) or {}).get(key))

    def scripts(n, w):
        return bb[n][w]["scripts_added"] + bb[n][w]["scripts_modified"]

    # ---- coverage, by cycle and never over the estate --------------------
    cov = data["coverage_by_cycle"]
    cycles = [{"key": k,
               "area": cov[k]["folder"],
               "cases": cov[k]["cases"],
               "automated": cov[k]["automated"],
               "pct": cov[k]["pct"],
               "runs": cov[k]["runs"],
               "failed": cov[k]["failed"],
               "defects": cov[k]["defects"],
               "from": cov[k]["first"], "to": cov[k]["last"],
               "ours": cov[k]["ours"]}
              for k in sorted(cov, key=lambda x: -cov[x]["cases"])]
    tot = sum(c["cases"] for c in cycles)
    auto = sum(c["automated"] for c in cycles)

    coverage = {
        "cases": tot, "automated": auto,
        "pct": round(100.0 * auto / tot, 1) if tot else None,
        "cycles": cycles,
        "basis": "test cycles delivered",
        "note": "Counted across the test cycles actually being run, which is "
                "the work in flight. Read over every test case ever written "
                "the same data says 22.8%, and most of that gap is a backlog "
                "nobody has triaged -- a queue, not a score."}

    estate = {k: dict(v) for k, v in (data["estate"] or {}).items()}

    # ---- the ten ---------------------------------------------------------
    #
    # Every one of the ten is present, including the ones that cannot be
    # measured. A screen that silently drops metric 6 and metric 10 lets a
    # reader believe eight is all there ever were.
    metrics = [
        {"n": 1, "name": "Automation Output", "want": "up", "status": "live",
         "measures": [
             {"label": "Automation scripts added", "unit": "count",
              "series": per("scripts_added", bb),
              "note": "New test script files in the code changes they "
                      "delivered"},
             {"label": "Automation scripts modified", "unit": "count",
              "series": per("scripts_modified", bb)},
             {"label": "Changes delivered", "unit": "count",
              "series": per("scm.pr.merged", bb)},
             {"label": "AI outputs written", "unit": "count",
              "series": per("output.generated", ai),
              "note": "Files AI wrote directly, rather than a person typing "
                      "them"}]},
        {"n": 2, "name": "Automation Coverage", "want": "up", "status": "live",
         "headline": {"value": coverage["pct"], "unit": "percent",
                      "of": "%d of %d cases in the cycles being run"
                            % (auto, tot) if tot else None},
         "by_cycle": cycles,
         "measures": [],
         "note": coverage["note"]},
        {"n": 3, "name": "First-Pass Acceptance Rate", "want": "up",
         "status": "partial",
         "measures": [
             {"label": "Reviews given", "unit": "count",
              "series": per("scm.pr.reviewed", bb)}],
         "note": "Too few reviews to turn into a percentage. The counts are "
                 "the honest answer; a rate off five reviews would not be."},
        {"n": 4, "name": "Rework Rate", "want": "down", "status": "partial",
         "measures": [
             {"label": "Changes rolled back", "unit": "count",
              "series": per("scm.revert", bb),
              "note": "A rollback is a signal to read, not a defect count"},
             {"label": "Rollbacks per change delivered", "unit": "percent",
              "series": series(data, weeks, names,
                               lambda n, w: sheets.pct(
                                   bb[n][w]["scm.revert"],
                                   bb[n][w]["scm.pr.merged"]))}]},
        {"n": 5, "name": "Automation Lead Time", "want": "down",
         "status": "partial",
         "measures": [
             {"label": "Median time to merge (hours)", "unit": "hours",
              "series": series(
                  data, weeks, names,
                  lambda n, w: round(
                      sheets.statistics.median(data["merge"][n][w])
                      / 3600000.0, 2) if data["merge"][n][w] else None)}],
         "note": "The closest thing available. Nothing links a test to the "
                 "change that automated it, so this is time-to-merge, not "
                 "time-to-automate."},
        {"n": 6, "name": "Productivity Gain", "want": "up",
         "status": "impossible", "measures": [],
         "note": "This needs the same work done without AI to compare "
                 "against, and there is none. Anyone quoting a speed-up "
                 "percentage is guessing."},
        {"n": 7, "name": "Execution Rate", "want": "up", "status": "partial",
         "measures": [
             {"label": "Tests run", "unit": "count", "series": qa("runs")},
             {"label": "Times they asked AI for help", "unit": "count",
              "series": per("human.turn", ai)},
             {"label": "Days they used AI", "unit": "count",
              "series": series(data, weeks, names,
                               lambda n, w: len(data["days"][n][w]))}],
         "note": "Run volume follows cycle scheduling, not effort. There is "
                 "no plan to divide by, so this is what was executed, not a "
                 "share of what was planned."},
        {"n": 8, "name": "Flaky Test Rate", "want": "down",
         "status": "impossible",
         "measures": [
             {"label": "Failed runs", "unit": "count", "series": qa("failed")}],
         "note": "A failure count, not a flakiness rate. The test tool keeps "
                 "only the latest result for each test in each cycle, so a "
                 "test that fails then passes leaves no trace at all. "
                 "Flakiness cannot be measured from this source."},
        {"n": 9, "name": "AI Cost per Accepted Output", "want": "down",
         "status": "partial",
         "measures": [
             {"label": "AI cost, estimated", "unit": "usd",
              "series": money("modelled")},
             {"label": "Cost per test script worked on", "unit": "usd",
              "series": series(
                  data, weeks, names,
                  lambda n, w: (round((cost[n].get(w) or {}).get("modelled")
                                      / scripts(n, w), 2)
                                if (cost[n].get(w) or {}).get("modelled")
                                and scripts(n, w) else None))},
             {"label": "Cost per change delivered", "unit": "usd",
              "series": series(
                  data, weeks, names,
                  lambda n, w: (round((cost[n].get(w) or {}).get("modelled")
                                      / bb[n][w]["scm.pr.merged"], 2)
                                if (cost[n].get(w) or {}).get("modelled")
                                and bb[n][w]["scm.pr.merged"] else None))}],
         "note": "The cost is an estimate against published list prices, and "
                 "it is not the bill -- Copilot charges a monthly fee per "
                 "person plus usage, and neither is visible here. Nothing "
                 "records which AI conversation produced which script, so "
                 "cost splits by person and week but not by finished piece "
                 "of work."},
        {"n": 10, "name": "AI ROI", "want": "up", "status": "impossible",
         "measures": [],
         "note": "The value of the work delivered is not recorded anywhere, "
                 "so the numerator does not exist."},
    ]

    # A measure with no direction of its own takes the metric's. Leaving it
    # unset renders every trend as "not measured", which is a lie about the
    # data rather than a caution about it.
    for metric in metrics:
        for measure in metric["measures"]:
            measure.setdefault("want", metric["want"])

    # ---- what a QA engineer's day actually consists of --------------------
    #
    # Grouped by the job being done, not by which system happened to record it.
    # A QA engineer chases a bug and files it, designs a case, runs it, writes
    # the script that runs it next time, gets that script reviewed and merged,
    # and asks AI for help throughout. Six groups, in that order, because that
    # is the order the work happens in -- and because a flat list of eighteen
    # counters is a list nobody reads.
    #
    # `attributed` is the load-bearing flag. Where it is false the source
    # records no author, so the numbers are the whole project's and the member
    # filter must not touch them. Splitting them would mean inventing an author.
    def m(key, label, want=None, unit="count", note=None, source=None):
        row = {"key": key, "label": label, "unit": unit,
               "series": (source or qa)(key)}
        if want:
            row["want"] = want
        if note:
            row["note"] = note
        return row

    groups = [
        {"id": "finding",
         "name": "Finding and reporting problems",
         "why": "The core of the job: what they found, and what they told the "
                "developers about it.",
         "attributed": True,
         "measures": [
             m("raised_bug", "Bugs raised for developers", "up",
               note="Counted once per issue by who reported it, not once per "
                    "status change -- otherwise this ranks people by how much "
                    "a ticket moved."),
             m("raised_task", "Tasks raised", "up"),
             m("defects", "Defects logged against their test runs"),
             m("failed", "Failures found while running tests",
               note="A failure found is the job working, not a fault. It is "
                    "not counted as a bad outcome anywhere on this screen."),
         ]},
        {"id": "design",
         "name": "Designing the tests",
         "why": "Writing and revising the test cases themselves.",
         "attributed": False,
         "measures": [
             {"key": "cases_created", "label": "Test cases created",
              "unit": "count", "want": "up",
              "series": [wk.get(w, {}).get("cases_created", 0) for w in weeks]},
             {"key": "cases_updated", "label": "Test cases edited",
              "unit": "count",
              "note": "The LAST edit only -- a case changed three times in a "
                      "week counts once.",
              "series": [wk.get(w, {}).get("cases_updated", 0) for w in weeks]},
         ],
         "note": "The test tool records no author on a test case, so this "
                 "group is the whole project's work and the member filter "
                 "does not apply to it. Splitting it would mean inventing an "
                 "author."},
        {"id": "running",
         "name": "Running the tests",
         "why": "Execution: by hand and by automation.",
         "attributed": True,
         "measures": [
             m("runs", "Tests run", "up"),
             m("passed", "Of those, passed"),
             m("failed", "Of those, failed"),
             m("automated", "Of those, run by automation", "up",
               note="Manual against automated is the test tool's own flag, "
                    "not a guess made here."),
             {"key": "manual_runs", "label": "Of those, run by hand",
              "unit": "count",
              "series": series(data, weeks, names,
                               lambda n, w: (wkp.get(w, {}).get(n, {})
                                             .get("runs", 0)
                                             - wkp.get(w, {}).get(n, {})
                                             .get("automated", 0)))},
         ]},
        {"id": "automating",
         "name": "Building the automation",
         "why": "Turning a test that a person runs into a test that runs "
                "itself.",
         "attributed": True,
         "measures": [
             {"key": "scripts_added", "label": "Test scripts added", "unit":
              "count", "want": "up", "series": per("scripts_added", bb),
              "note": "New test script files in the code changes they "
                      "delivered."},
             {"key": "scripts_modified", "label": "Test scripts modified",
              "unit": "count", "want": "up",
              "series": per("scripts_modified", bb)},
             {"key": "commits", "label": "Commits", "unit": "count",
              "series": per("scm.commit", ai)},
             {"key": "lines", "label": "Lines of code changed", "unit": "count",
              "series": series(data, weeks, names,
                               lambda n, w: bb[n][w]["lines_added"]
                               + bb[n][w]["lines_removed"])},
         ]},
        {"id": "delivering",
         "name": "Getting the changes in",
         "why": "Review and merge -- the part that is other people as much as "
                "them.",
         "attributed": True,
         "measures": [
             {"key": "merged", "label": "Changes delivered", "unit": "count",
              "want": "up", "series": per("scm.pr.merged", bb)},
             {"key": "reviewed", "label": "Changes reviewed for others",
              "unit": "count", "want": "up",
              "series": per("scm.pr.reviewed", bb)},
             {"key": "reverted", "label": "Changes rolled back", "unit":
              "count", "want": "down", "series": per("scm.revert", bb),
              "note": "A rollback is a signal to read, not a defect count."},
             {"key": "lead_time", "label": "Median time to merge (hours)",
              "unit": "hours", "want": "down",
              "series": series(
                  data, weeks, names,
                  lambda n, w: round(
                      sheets.statistics.median(data["merge"][n][w])
                      / 3600000.0, 2) if data["merge"][n][w] else None)},
         ]},
        {"id": "ai",
         "name": "Working with AI",
         "why": "How much they leaned on it, and what that came to.",
         "attributed": True,
         "measures": [
             {"key": "prompts", "label": "Times they asked AI for help",
              "unit": "count", "series": per("human.turn", ai)},
             {"key": "sessions", "label": "Separate AI conversations",
              "unit": "count",
              "series": series(data, weeks, names,
                               lambda n, w: len(data["sessions"][n][w]))},
             {"key": "active_days", "label": "Days they used AI",
              "unit": "count",
              "series": series(data, weeks, names,
                               lambda n, w: len(data["days"][n][w]))},
             {"key": "action_rate",
              "label": "How often AI did the work, not just answered",
              "unit": "percent", "want": "up",
              "series": rate("tool.call", "human.turn", ai),
              "note": "The share of questions where AI went and read, changed "
                      "or ran something, rather than only replying."},
             {"key": "cost", "label": "AI cost, estimated", "unit": "usd",
              "want": "down", "series": money("modelled"),
              "note": "An estimate against published list prices, not the "
                      "bill. Copilot charges a monthly fee per person plus "
                      "usage and neither is visible here."},
         ]},
    ]

    activity = {
        "groups": groups,
        "project": {
            "cases_created": [wk.get(w, {}).get("cases_created", 0)
                              for w in weeks],
            "cases_updated": [wk.get(w, {}).get("cases_updated", 0)
                              for w in weeks],
            "cycles": [len(data["week_cycles"].get(w, [])) for w in weeks],
        },
        "cycles_by_person": {
            n: sorted(({"key": c["key"], "area": c["area"],
                        "runs": c["ours"][n], "pct": c["pct"]}
                       for c in cycles if c["ours"].get(n)),
                      key=lambda x: -x["runs"])
            for n in names},
    }

    return {
        "schema": SCHEMA,
        "generated_at": datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": {"from": week_rows[0]["from"] if week_rows else None,
                   "to": last_seen,
                   "weeks": [w["week"] for w in weeks_of(week_rows)],
                   "full_weeks": list(full_weeks)},
        "weeks": week_rows,
        "people": people,
        "metrics": metrics,
        "coverage": coverage,
        "estate": estate,
        "activity": activity,
        "sources": ["Jira", "AIO test", "Bitbucket", "Copilot"],
        "gaps": [m["note"] for m in metrics if m["status"] == "impossible"],
    }


def weeks_of(week_rows):
    return week_rows


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Write the JSON snapshot the /insights and /activities "
                    "screens read.")
    p.add_argument("--person", type=sheets.parse_person, action="append",
                   required=True, metavar="NAME=ACCOUNT_ID",
                   help="Repeatable. Only named people appear in the file.")
    p.add_argument("--role", action="append", default=[], metavar="NAME=ROLE",
                   help="Repeatable. What this person does, in a reader's "
                        "words -- shown beside their name.")
    p.add_argument("--pronouns", type=sheets.parse_pronouns, action="append",
                   default=[], metavar="NAME=she",
                   help="Repeatable. Never inferred from a name; anyone "
                        "unnamed stays they/them.")
    p.add_argument("--input", nargs="+", required=True,
                   help="NDJSON files or directories.")
    p.add_argument("--weeks", type=sheets.week_span, required=True,
                   metavar="YYYY-Www..YYYY-Www")
    p.add_argument("--full-weeks", type=sheets.week_span,
                   metavar="YYYY-Www..YYYY-Www",
                   help="The weeks that are complete. Volume is only compared "
                        "across these. Defaults to every week given.")
    p.add_argument("--price", type=sheets.parse_price, action="append",
                   default=[], metavar="MODEL=IN/OUT",
                   help="Repeatable, per 1M tokens. A model with no price is "
                        "counted and left unpriced, never guessed.")
    p.add_argument("--partial", action="append", default=[],
                   metavar="YYYY-Www=REASON")
    p.add_argument("--out", required=True, help="Where to write the JSON.")
    args = p.parse_args(argv)

    people = {acct: name for name, acct in args.person}
    weeks = args.weeks
    full = args.full_weeks or weeks
    missing = [w for w in full if w not in weeks]
    if missing:
        raise SystemExit("--full-weeks names weeks outside --weeks: %s"
                         % ", ".join(missing))

    said = dict(args.pronouns)
    unknown = [n for n in said if n not in people.values()]
    if unknown:
        raise SystemExit("--pronouns names someone not in --person: %s"
                         % ", ".join(sorted(unknown)))
    default = sheets.Pronouns()
    pron = lambda n: said.get(n, default)

    roles = {}
    for item in args.role:
        name, _, role = item.partition("=")
        if name.strip() not in people.values():
            raise SystemExit("--role names someone not in --person: %s"
                             % name.strip())
        roles[name.strip()] = role.strip()

    labels = {}
    for item in args.partial:
        w, _, why = item.partition("=")
        labels[w.strip()] = why.strip() or "part week"

    data = sheets.collect(args.input, people, weeks, dict(args.price))
    payload = build(data, weeks, full, labels, roles, pron)

    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
    os.replace(tmp, args.out)

    print(json.dumps({
        "msg": "snapshot_written", "out": args.out,
        "bytes": os.path.getsize(args.out),
        "people": sorted(people.values()), "weeks": len(weeks),
        "cycles": len(payload["coverage"]["cycles"]),
        "metrics_live": sum(1 for m in payload["metrics"]
                            if m["status"] == "live"),
        "metrics_impossible": sum(1 for m in payload["metrics"]
                                  if m["status"] == "impossible"),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
