---
name: weekly-report
description: Write the weekly AI-effectiveness report for a period already pulled into reports/<name>/. Use when asked to produce, write or update a weekly or monthly report, a per-person report, or a report for a named window like 2026-W35 or "24 and 25 Aug only".
---

# Writing the weekly report

The numbers are already computed. `report/weekly_report.py` does every
aggregation and is tested; this skill is about turning its output into the
document somebody reads, and about the things a generator cannot know.

**Never compute a metric by hand.** If a number is not in the generator's
output, it is not in the report. A figure you derived in your head is
indistinguishable from a measured one once it is in Markdown, and this project
has already shipped two wrong figures that way.

**The report is structured on the ten metrics, not on the surfaces the data
came from.** See §8. A report organised by data source reads as an engineering
log; the customer asked for ten metrics and that is the shape they get.

## 1. Check what arrived

```bash
ls reports/<name>/exports/          # poller and laptop NDJSON
ls reports/<name>/daily/            # the daily report, if one was dropped in
cat reports/<name>/roster.txt       # who was expected
```

If `exports/` is empty, stop and say so. Do not write a report from nothing.

**An empty `exports/` does not mean the source was empty.** `admin.py cmd_pull`
builds its work list from `BITBUCKET_REPOS`, `JIRA_PROJECT_KEYS` and
`AIO_PROJECTS` (falling back to the Jira keys). Unset means *silently skipped*,
never *failed* — no file, no error, nothing in the run log. Check `.env` for the
variable before concluding a source had no data, and name which of the two it
was in the report. Measured 2026-08-27: `.env` carried only `INSIGHT_*` and
`AIO_*`, so Jira and Bitbucket were skipped and their exports on disk were 3 and
6 days stale. A stale export is not an empty week — date-range every export you
did not pull yourself:

```bash
python3 -c "import json,sys; ds=sorted((json.loads(l)['event_time'] or '')[:10]
  for l in open(sys.argv[1]) if l.strip()); print(ds[0], '->', ds[-1])" \
  reports/<name>/exports/<file>.ndjson
```

**Bundles are filed by `window_start`, not by the days their events cover.** A
backfill lands in the folder of the week it *started*, so
`pull --week <name>` alone silently misses it. Measured 2026-W34:
`insight backfill --since 2026-08-01` put that engineer's whole month —
including every W34 event — in `bundles/2026-W31/`, and pulling W34 returned
zero bundles for a week that had three days of activity. Pull a span into one
inbox and let the generator filter:

```bash
for n in 28 29 30 31 32 33 34 35 36; do
  python3 importers/pull.py --week 2026-W$n \
    --inbox reports/<name>/inbox --roster reports/<name>/roster.txt
done
python3 importers/bundle.py --inbox reports/<name>/inbox \
  --out reports/<name>/exports/laptops.ndjson --state reports/<name>/bundles.json
```

`weekly_report.py` filters on `event_time` against `--week`, so the extra weeks
drop out harmlessly.

**Everything you fetch lands under `reports/<name>/`** — poller NDJSON and
`laptops.ndjson` in `exports/`, raw bundles in `inbox/`, and the
`GET /v1/people` and `GET /v1/bundles?week=` responses you cite. The report
asserts who was expected and what arrived; that evidence belongs on disk beside
it. A re-run should not have to re-hit the network, and `reports/` is
gitignored, which is what makes it the right home.

## 2. Scope the pull to the people the report is about

**A week pull fetches everyone's bundles, not just your subjects'.** For a
report about named people, the inbox will fill with data belonging to people who
are not in it. Measured 2026-08-27 on a two-person report: 41 bundles pulled, 28
of them belonging to a third account and an unattributed machine.

Split by machine, keep only your subjects, and delete the rest — from `inbox/`,
from any listing JSON you saved, and from `roster.txt`. Someone reading the
report directory should not find third parties in it, and being asked "why is
this person in my report" is a fair question with no good answer.

## 3. Generate

```bash
python3 report/weekly_report.py \
  --input reports/<name>/exports/ \
  --week <name> \
  --out reports/<name>/weekly-<name>.md
```

For a month, run it per ISO week and write one file per week. The generator's
window is a week; a month-long window would silently mix them.

**The generator has no day-range option, and `--week 2026-08-24` resolves to
that date's whole ISO week.** For a sub-week window — "24 and 25 Aug only" — or
for one person, **filter the input and let the generator aggregate**:

```bash
python3 -c "import json,sys
D={'2026-08-24','2026-08-25'}
[sys.stdout.write(l) for l in open(sys.argv[1])
 if l.strip() and json.loads(l)['event_time'][:10] in D]" \
  reports/<name>/exports/laptops.ndjson > /tmp/window.ndjson

python3 report/weekly_report.py --input /tmp/window.ndjson --week 2026-W35 \
  --scope-note "2026-08-24 and 2026-08-25 only — Ngoc and Linh." \
  --out reports/<name>/scoped.md
```

Filtering rows is not computing by hand; the aggregation still belongs to the
generator. **Always pass `--scope-note`** — a scoped report that does not say so
gets read as the whole team's week. And say in the document that no figure from
a partial window is comparable with a seven-day one.

Run `--format json` alongside the Markdown. Quote from the JSON; it is the same
aggregation without the prose, and it stops you re-typing a number wrongly.

## 4. Read it before you present it

Open the output and check these, in order. Each has been wrong before.

| Check | What to look for |
|---|---|
| **Empty vs zero** | A section saying "no `model.call` events" is *absent data*, not a zero. Never summarise it as "no AI usage this week" |
| **Active people** | See the box below — this one is worse than it looks |
| **Coverage** | A stock, not a flow — the position on the day it was pulled, not something that happened this week. Measured **by cycle**, never over the case estate — see §5a |
| **Coverage across two windows** | Two coverage figures with different denominators are not a trend. Measured: 90.9% of 4,584 cases one week, 57.4% of the 340 cases snapshotted on a two-day window the next. Same metric name, not the same population. Never plot them together |
| **n=** | Any median over fewer than 5 samples is noise. The generator suppresses percentages below 5; do not quote the raw count as a rate |
| **Link completeness** | `Explicit-link completeness 0.0%` means no cost metric is admissible. Say it |
| **Trend** | A dash is "the source produced nothing", never zero |
| **Enrolled but silent** | "Machine enrolled, no bundle received" is its own state — distinct from "not on the roster" and from "sent a bundle containing zero events". Report which one. Never render it as no AI usage |
| **A surface that omits a record type** | `AI runs 0` next to 75 `model.call` events means `vscode-copilot-chat` emits no `run.*` at all, not that nobody ran anything. Check whether the event type exists in the stream before reading its count as a measurement |
| **Copilot CLI** | `copilot_read.py` printing "no Copilot session journals" means the CLI surface is **unmeasured**. A `.copilot` tree holding only `logs/process-*.log` and `ide/*.lock` has no journals. Do not read it as no CLI usage |

> **`Active people` is not a person count.** `weekly_report.py:496` builds the
> set as `person_id or person_email_hash`. Laptop events carry **null for both**,
> so `None` is added as a member: a single-person, laptop-only run still reports
> `people: 1`, and a run mixing surfaces reports Jira and AIO actors plus one
> null bucket. Measured 2026-W34: `Active people 24` against exactly **one**
> person shipping laptop telemetry. Never quote this row as a headcount.

## 5a. Coverage is measured by cycle, not over the case estate

Metric 2's denominator is the cases in the **test cycles being delivered**, not
every case in the project. The cycle is the delivery record and the pull
request is not (`CONTRACT.md` §3 row 22).

Measured 2026-08-27 on IML — the same data, two readings:

| | cases | automated | |
|---|---:|---:|---|
| **By cycle** — the 7 cycles that ran | 8,564 | 7,976 | **93.1%** |
| By estate — the P3 slice of all 10,742 | 1,291 | 294 | 22.8% |

The estate reading looks like a crisis and is mostly a backlog nobody has
triaged: **3,695 of 5,183 P3 cases have no automation status set at all** and
appear in no cycle. Counting an unset field as "not automated" measures how
diligently the field gets filled in — the same mistake as counting from the
daily sync's "AI Usage" column.

So report per cycle, and name it. `IML-CY-207` at 97.2% of 3,858 cases and
`IML-CY-202` at 0% of 230 are both true and say different things — the second
is a manual suite, not a failure. Lead with the weighted total across cycles,
list the cycles beneath it, and if the estate figure appears at all, label it
**backlog**, never coverage.

Compute it from `test.run.completed`: distinct `test_case_key` per
`test_cycle_key`, and the share of those whose `is_automated` is true. That
also gives you the cycle's window, failures and defects for free, and
`executed_by_person_id` says which of your people worked it.

## 5. The daily report is the delivery record — lead with it

**This is the most valuable file in the folder and the easiest one to
under-use.** It is the only source that says what the engineers actually
produced. The event stream says 4,001 tests ran; the daily sheet says *one
engineer converted 75 scenarios from UI to API, fixed 20 unstable tests and cut
a suite from 59 cases to 46*. The second is what a PM is reading for.

**Extract it as a numbers table, one row per output, with the count in its own
column.** Not prose bullets. Every figure in these sheets is countable — cases
converted, cases resolved, defects raised, scenarios pending, suites reduced,
schedulers added — and prose hides them. Give each engineer a table and put it
in **section 1**, above the metrics.

Derive the obvious totals and gaps: "75 done, 120 pending, 250+ identified" is
three numbers from the sheet plus the observation that they do not reconcile —
which is exactly the kind of thing a PM wants surfaced. Where the sheet's own
arithmetic disagrees with itself, report both and say the discrepancy is in the
source. Never silently correct it.

**What "never count from it" actually forbids.** The rule is about the
**"AI Usage" column** — a free-text form field that read `None` on 20 of 21
entries, *including days where the same person's machine recorded 40 prompts*.
Counting adoption from it would rank people by how carefully they fill in forms.
It does **not** mean the sheet's delivery figures are unusable: those are the
team's own record of their work, and burying them under a "self-reported, not
counted" disclaimer throws away the report's whole value.

So: label the section's source once — *"Source: team daily sync sheet"* — keep
the delivery numbers out of the ten-metric table, and put them front and centre
everywhere else.

Practical notes on the workbook:

- Sheets are named `DDMMYYYY` (`21082026`, `24082026`), one per day, and **not
  every working day has one**. Say how many days of the window the sheet
  actually covers — measured 2026-W34, it was 1 of 5.
- `openpyxl` is often not installed. Read the file with `zipfile` +
  `xml.etree` against `xl/worksheets/sheet*.xml` and `xl/sharedStrings.xml`
  rather than adding a dependency.
- Save the extract you used into `reports/<name>/daily/` beside the workbook.

Practical notes on the workbook:

- Sheets are named `DDMMYYYY` (`21082026`, `24082026`), one per day, and **not
  every working day has one**. Say how many days of the window the mapping
  actually covers — measured 2026-W34, it was 1 of 5.
- `openpyxl` is often not installed. Read the file with `zipfile` +
  `xml.etree` against `xl/worksheets/sheet*.xml` and `xl/sharedStrings.xml`
  rather than adding a dependency.
- Save the extract you used into `reports/<name>/daily/` beside the workbook.

## 6. Check the keys before you quote anything joined to a ticket

`extract_jira_key` accepts anything key-shaped when it gets no allow-list, and
"passes `projects=`" is not proof the guard was on — the value passed can be
`None`. Measured 2026-W34, from call sites that all looked correct:
`fix/AUG-20` → `AUG-20` (a date), `UI-JUN-30` → `JUN-30`, and fabricated keys
outnumbered real ones **45 to 9** in a live export. Count the project prefixes
in any export you did not pull yourself:

```bash
python3 -c "import json,sys,collections; print(collections.Counter(
  (json.loads(l).get('context') or {}).get('jira_project_key')
  for l in open(sys.argv[1]) if l.strip()))" reports/<name>/exports/<file>.ndjson
```

Real keys are `IML`, `APR`, `AERLABS`, `IOTA3`. Anything else — month
abbreviations especially — is fabricated and must be NULLed, with the original
kept. AR-1.

**The AIO key space is separate and matters more here.** `IML-TC-5` is a test
case and `IML-CY-199` a cycle; for a QA team the cycle is the delivery record
and the pull request is not (CONTRACT.md §3 row 22). They live in
`context.test_case_key` / `context.test_cycle_key`, not in `jira_issue_key`,
and `TC-*` or `CY-*` appearing as a *project prefix* is the old bug, not a
test key — before 2026-08-27 `IML-TC-5` was mined for the false ticket `TC-5`.

## 7. Per-person reporting: what the join does and does not allow

Two different questions, and conflating them wastes a lot of time.

**Attributing laptop events to a person — possible, and does not need the event
actor.** `GET /v1/bundles?week=` returns `email` and `machine` per object: the
address the endpoint **authenticated**. Machine prefix → person is therefore a
real key, not an inference, even when every event inside carries
`actor.person_id = null`. Split the inbox by machine prefix and run
`importers/bundle.py` per person.

```bash
python3 -c "
import sys; sys.path.insert(0,'.')
from importers.pull import list_week
import os,json,collections
ep,tok=os.environ['INSIGHT_ENDPOINT'],os.environ['INSIGHT_ADMIN_TOKEN']
m=collections.defaultdict(set)
for n in range(28,37):
    for o in list_week(ep,f'2026-W{n}',tok,60):
        m[str(o['machine'])[:8]].add(o.get('email'))
print(json.dumps({k:sorted(v) for k,v in m.items()},indent=1))"
```

Do not take the machine count from `GET /v1/people`: measured 2026-08-27 it
reported `fingerprints: 1` for a person the bundle listing showed on **two**
machines. The listing is the authority; flag the disagreement for an operator.

**Joining that person's AI use to their tests, PRs or tickets — not possible
without the identities file.** AIO, Bitbucket and Jira key on Atlassian
accountIds and join to each other perfectly; laptop events share not one id with
them. The fix is `importers/pull.py --identities`, a file of `email accountId`
lines. Without it, metrics 1–5, 7 and 8 stay project-level however much laptop
data arrives.

So the correct shape is: **per-person AI engagement figures, project-level
outcome metrics, and an explicit sentence saying why the two cannot be
multiplied together.** Not "no per-person section".

**Never promote a candidate accountId to a key.** You will find convergences —
an accountId that reviewed the PR someone's daily entry names, on the day it
names. Measured 2026-08-27: one accountId matched a person's sheet on four
independent points. That is worth **writing down as unconfirmed, for an operator
to verify against Jira**, and it is still an inference from narrative text.
AR-1: nothing in the report may be computed from it. Equally, check the
convergence you assume — a second candidate looked obvious and **failed**: the
sole AERLABS executor turned out to have written 13 results in a two-second bulk
write, not the hand verification the sheet described.

## 8. Structure the report on the ten metrics

`docs/METRICS.md` holds the current status of all ten — one live, one with a
caveat, two permanently out of scope, the rest blocked. **Read it before
claiming any metric is reportable**, and update it if this run changes the
picture.

Lead with a table of the ten, in order, with a column per person and one for the
project. Then, and only then, supporting measurement.

**Give the nearest measured equivalent where a metric is blocked, and mark it
`proxy`.** A page of "not measurable" is useless to a PM; a proxy with its
denominator is not. A proxy is never reported as the metric, and never enters
the metric's column — merged PR lead time is not Automation Lead Time, and a
failure rate is not a flaky rate.

Every `—` gets one line saying which it is: absent, structurally unavailable, or
excluded by design. And every report ends with **what to change to unblock the
rest**, ordered by payoff, marking which items are configuration and which need
a process change. The identities file is usually first: it is four lines and it
applies retroactively to bundles already on disk.

## 9. Write it as a standalone document for a PM

The reader is the customer's project manager. They have no context on this
system, will not open a second file, and are deciding what to do next. Assume
nothing carries over between reports — each one stands alone.

**Cut every internal name.** No field names (`premium_requests`,
`has_automation_key`, `AI-Run-Id`, `person_id`), no file paths, no module names,
no event types, no footnote-marker soup. Say what the thing *is*:
"`premium_requests` is NULL" becomes "no cost data available"; "the identities
file" becomes "a file mapping each engineer's work email to their Atlassian
account ID"; "`vscode-copilot-chat` emits no `run.*`" becomes nothing at all —
the reader does not need it. Name AIO as "the test management system" the first
time it appears.

**One caveat, stated once.** The recurring failure is repeating the same
explanation in the metric table, in the notes, and again in the footnotes. Put
every caveat in a single closing "How to read this report" list, and let the
table point at it with one plain status word — *Reportable* / *Not measurable* /
*Needs person link* / *Excluded by design*.

**Keep the section count low.** Bottom line → the ten metrics → the period's
concrete numbers → AI usage → what was delivered → what's blocking → how to
read. Seven sections, no nesting beyond one level.

**Lead with a bottom line.** Three short paragraphs: what is reportable, what is
not and the single reason why, and any collection gap affecting a named person.
A PM who reads only that paragraph should still be able to act.

**Make the blockers a decision table** — action, what it unlocks, effort — and
mark which items are configuration (do this week) and which need the customer to
change how the team works (needs your decision). "Not measurable" without a
route to measurable is a complaint, not a report.

**Do not use a bare `—` as the not-measured marker in a customer table.** It
reads as a dash, a zero, or a typo. Use `n/a` with one bold line under the table:
*"n/a means not measured — it never means zero."*

**Every self-reported figure carries the label in the section heading**, not in
a footnote — *"Source: the team's daily sync sheet. Self-reported, not counted
toward any metric above."*

## What never goes in

- ROI, or any monetary "value delivered". Not observable here.
- An AI-vs-human comparison. There is no control group.
- Anything that reads as individual performance, or that invites two people's
  rows to be read against each other. Collection is voluntary and reversible,
  which is exactly what makes it useless as an audit trail — and one person's
  missing week is almost always a collection gap, not an adoption gap. Say so
  in that person's row, every time.
- A number without its denominator.
- A third party who is not a subject of the report (§2).
