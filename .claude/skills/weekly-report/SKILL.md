---
name: weekly-report
description: Write the weekly AI-effectiveness report for a period already pulled into reports/<name>/. Use when asked to produce, write or update a weekly or monthly report, or when handed a period name like 2026-W35.
---

# Writing the weekly report

The numbers are already computed. `report/weekly_report.py` does every
aggregation and is tested; this skill is about turning its output into the
document somebody reads, and about the things a generator cannot know.

**Never compute a metric by hand.** If a number is not in the generator's
output, it is not in the report. A figure you derived in your head is
indistinguishable from a measured one once it is in Markdown, and this project
has already shipped two wrong figures that way.

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
was in the report. Measured 2026-W34: all three unset, so every poller was
skipped including AIO, which had a working token the whole time.

**Bundles are filed by `window_start`, not by the days their events cover.** A
backfill lands in the folder of the week it *started*, so
`pull --week <name>` alone silently misses it. Measured 2026-W34:
`insight backfill --since 2026-08-01` put that engineer's whole month —
including every W34 event — in `bundles/2026-W31/`, and pulling W34 returned
zero bundles for a week that had three days of activity. Pull a span into one
inbox, run `importers/bundle.py` over it, and let the generator filter:

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
`GET /v1/people` and `GET /v1/bundles?week=` responses you cite as
`exports/people.json` and `exports/bundles-<week>.json`. The report asserts who
was expected and what arrived; that evidence belongs on disk beside it. A
re-run should not have to re-hit the network, and `reports/` is gitignored,
which is what makes it the right home.

## 2. Generate

```bash
python3 report/weekly_report.py \
  --input reports/<name>/exports/ \
  --week <name> \
  --out reports/<name>/weekly-<name>.md
```

For a month, run it per ISO week and write one file per week. The generator's
window is a week; a month-long window would silently mix them.

## 3. Read it before you present it

Open the output and check these, in order. Each has been wrong before.

| Check | What to look for |
|---|---|
| **Empty vs zero** | A section saying "no `model.call` events" is *absent data*, not a zero. Never summarise it as "no AI usage this week" |
| **Active people** | Counts Jira and Bitbucket actors, not AI users. It sits under "AI runs: 0" and invites the wrong reading. Say which it is |
| **Coverage** | A stock, not a flow. It is the estate position on the day it was pulled, not something that happened this week |
| **n=** | Any median over fewer than 5 samples is noise. The generator suppresses percentages below 5; do not quote the raw count as a rate |
| **Link completeness** | `Explicit-link completeness 0.0%` means no cost metric is admissible. Say it |
| **Trend** | A dash is "the source produced nothing", never zero |
| **Enrolled but silent** | "Machine enrolled, no bundle received" is its own state — distinct from "not on the roster" and from "sent a bundle containing zero events". Report which one. Never render it as no AI usage |
| **A surface that omits a record type** | `AI runs 0` next to 75 `model.call` events means `vscode-copilot-chat` emits no `run.*` at all, not that nobody ran anything. Check whether the event type exists in the stream before reading its count as a measurement |

## 4. Fold in the daily report

If `reports/<name>/daily/` holds a file, it is a **mapping** source: person →
team → ticket. Use it to say who worked on what.

**Never count from it.** Its "AI Usage" column is a form field — measured, it
read `None` on 20 of 21 entries. Counting from it would rank people by how
carefully they fill in forms. Mapping only, and say that in the report if you
use it at all.

## 5. Check the keys before you quote anything joined to a ticket

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

## 6. Read the joinability table before quoting any per-person figure

§10 of the report carries a **Joinability** table: for each surface, the share
of events that name a person, a Jira issue, a test case, a test cycle and a
repository. Read it first, because it decides which questions the week can
answer at all.

A surface at **0% on person** cannot be joined to anything: not to the test
runs that person executed, not to their pull requests. That was the state on
2026-08-26 — AIO and Bitbucket carried a person on every event and joined to
each other perfectly, while all 935 laptop events carried none, so no AI usage
could be attributed to any test, any ticket or anyone. The fix is
`importers/pull.py --identities`, a file of `email accountId` lines; without it
laptop events stay unattributed however many arrive.

So: if the laptop surface shows 0% person, do **not** write a per-person
section. Say the join is missing and name the file that supplies it.

## 7. Say what is missing

End with what could not be measured and why, naming the source. `docs/METRICS.md`
holds the current status of all ten metrics — one live, one with a caveat, two
permanently out of scope, the rest blocked. Read it before claiming any metric
is reportable, and update it if this run changes the picture.

## What never goes in

- ROI, or any monetary "value delivered". Not observable here.
- An AI-vs-human comparison. There is no control group.
- Anything that reads as individual performance. The collection is voluntary
  and reversible, which is exactly what makes it useless as an audit trail.
- A number without its denominator.
