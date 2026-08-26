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

## 4. Fold in the daily report

If `reports/<name>/daily/` holds a file, it is a **mapping** source: person →
team → ticket. Use it to say who worked on what.

**Never count from it.** Its "AI Usage" column is a form field — measured, it
read `None` on 20 of 21 entries. Counting from it would rank people by how
carefully they fill in forms. Mapping only, and say that in the report if you
use it at all.

## 5. Say what is missing

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
