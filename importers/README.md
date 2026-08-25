# importers

Two things arrive by hand every week, and both are validated on the way in.

| | What | Emits |
|---|---|---|
| `bundle.py` | weekly bundles from `./insight pack` on engineers' machines | contract events + a coverage report |
| `daily_report.py` | the team's daily Excel | a mapping table + trail completeness — **no events** |

```bash
python3 importers/bundle.py --inbox inbox/ --out events.ndjson \
    --state state/bundles.json --coverage-out coverage.json

python3 importers/daily_report.py --file inbox/daily.xlsx \
    --events exports/jira-PRJ.ndjson --events exports/aio-PRJ.ndjson \
    --out mapping.json
```

## Why the daily report emits no events

It is self-reported, written after the fact, by the people being measured.
Counting from it would rank people by how carefully they write reports —
someone who works hard and logs little looks worse than someone who works less
and logs everything, and nothing in the output would show that this happened.

So it produces evidence instead of figures:

- **A mapping table.** AIO test cases carry no Jira key — 0 of 4,248 — which
  blocks Automation Lead Time and any split of coverage by priority. Where the
  report links an item to a test case *and both machine trails agree something
  happened around that day*, that is evidence for the link. Pairs nothing
  corroborates are kept, marked `reported_only`, and never mixed in at the same
  confidence.
- **Trail completeness.** What share of reported work left a machine-readable
  trace. A gap is a question, not a verdict: either work is not reaching Jira,
  or the report overstates. Both are worth knowing.

The `note` column is read and discarded. Free text written by a person is
content, and `CONTRACT.md` §1.1 admits no exception for content that happens to
be convenient.

## Rejection is loud

A bundle that fails its checksum is rejected **whole** — half a bundle is worse
than none, because the events land, the week looks covered, and nobody can tell
the rest is missing. A spreadsheet row that will not parse is reported with its
row number so it can be fixed at source, never coerced into a guess.

A rejected bundle contributes no coverage either, or its week would read as
measured when nothing was measured.

## Coverage is the point of `bundle.py`

With hand-collected data a gap is the normal case — someone forgets, someone is
on leave, someone joins mid-quarter. Coverage is what lets a report tell a
measured zero from an absent one, so it is a first-class output rather than a
diagnostic. An empty bundle still counts as a covered week.

The checksum catches truncation and corruption. It is **not** tamper-evidence:
an engineer can read and edit their own bundle before handing it over, which is
what makes the collection consensual. A voluntary record, never an audit trail.

## `pull.py` — fetching a week

```bash
export INSIGHT_ENDPOINT=https://aeris-insight.seta-international.com
export INSIGHT_ADMIN_TOKEN=...

python3 pull.py --week 2026-W34 --inbox inbox/ --roster roster.txt
python3 bundle.py --inbox inbox/ --out events.ndjson --state state/bundles.json
```

It stops at `inbox/` on purpose. `bundle.py` already parses, checksums,
re-checks the allow-list and dedupes, and a bundle that arrived over HTTP has
earned none of those exemptions — `pull.py` only replaces the person who used to
save attachments into a folder.

**Always pass `--roster`.** When bundles arrived by email a missing week was
visible: no email came. Over HTTP it is silence, and silence reads as zero.
`bundle.py`'s coverage report cannot help here by construction — it is derived
from bundles that arrived, so someone who has never sent one is invisible to it.
Only a roster knows who was expected.

The roster is the same people as the server's `INSIGHT_ALLOWED`, one work email
per line. Emails stay on this side: nothing written into `inbox/` carries one.

## `watch.py` — noticing silence

```bash
python3 watch.py --roster roster.txt          # once a day, from cron
python3 watch.py --roster roster.txt --dry-run
```

Posts to ntfy.sh when someone stops reporting. Configured entirely in `.env`;
see `.env.example` and `docs/TRANSPORT.md`.

The unit is a **working day**, not an hour. `insight auto` uploads nothing in an
hour where nothing changed, so hourly misses are not signal — but an idle day
still uploads one empty bundle, so a working day with nothing at all means the
collection is broken rather than that the person was quiet.

It stays silent about nights, weekends, an outage it has already reported, and
anyone new to the roster who has not had time to set up yet. Each of those is a
reason someone would mute the channel, and a muted channel is worse than none.
