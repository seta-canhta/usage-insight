# Weekly report

Turns the NDJSON event stream into a weekly report — Markdown, HTML, or JSON.

```bash
# most recent COMPLETE week from a directory of poller/emitter output
python3 weekly_report.py --input ~/.aiep/telemetry/shipped/ 

# a specific week, as a shareable self-contained page
python3 weekly_report.py --input events.ndjson --week 2026-W33 \
  --format html --out report.html

# machine-readable, for a dashboard or a Confluence job
cat events.ndjson | python3 weekly_report.py --format json
```

| Flag | Meaning |
|---|---|
| `--input PATH...` | Files or directories (`*.ndjson` is scanned recursively). Defaults to stdin |
| `--week` | `YYYY-Www`, or a `YYYY-MM-DD` date that is resolved to its ISO week |
| `--weeks N` | Trailing weeks shown in the trend table (default 4) |
| `--format` | `md` (default), `html`, `json` |
| `--out PATH` | Output file; otherwise stdout |
| `--min-group N` | Suppress percentages below this denominator (default 5) |

Stdlib only. No network, no dependencies.

---

## Sections

1. **Adoption** — runs, active people, agents and models in use
2. **Output acceptance** — the funnel and the acceptance rate
3. **Speed** — review and merge lead time, run duration, CI duration (median and p85)
4. **Quality** — merged/declined, decline rate, reverts, comments per 100 lines, gates
5. **Test execution** — AIO runs by outcome, pass rate, automated share, defects raised
6. **Cost** — tokens, cache share, cost per accepted output, and the cost basis
7. **Reliability** — run success, tool failures, retries, dependency failures, CI
8. **Human involvement** — turn kinds and manual intervention rate
9. **Trend** — the trailing weeks
10. **Data quality** — malformed lines, link-method distribution, event types present

**Section 9 is the one to read for a management view.** A single week is a
snapshot and invites over-reading; the trend table is where a real change becomes
visible. It carries PRs merged, decline rate, merge lead time, tests executed,
test pass rate, defects raised and tokens, week over week. A dash means the
source produced nothing that week and is not a zero.

---

## Design decisions worth knowing before you read a number

**Absent is not zero.** A section with no source events says so and renders nothing.
It never prints `0`. An unmeasured test suite must not look like a passing one, and a
week with no telemetry must not look like a week with no AI usage.

**Percentages are suppressed below `--min-group` (default 5)** and the raw count is
shown instead. A "100% acceptance rate" over two outputs is noise that reads as a
finding. The same rule is the k-anonymity control for anything crossing a person
boundary (design §11.5).

**Blocked and skipped stay in the test pass-rate denominator; never-run rows stay
out.** A blocked test is one that could not be run, and dropping it flatters the
number. Rows AIO seeded at "Not Run" when a cycle was created are cycle planning,
not test activity — 8,078 of 25,125 rows on the first real run.

**Median and p85, never the mean.** Lead-time distributions are long-tailed by
construction; a mean hides exactly the tail that hurts.

**Durations scale to their magnitude.** `71s`, `76m`, `2.0h`, `10.0d`. Fixed hours
would render a 12-second merge as `0.0h` and hide the most interesting thing in the
data — a pull request merged before anyone could have read it. That is a real pattern
in this organisation's history, not a hypothetical.

**Approval and clarification are not interventions.** `manual_intervention_rate`
counts `correction` and `rejection` only. The agents are *designed* to ask before
architectural choices, breaking changes and security trade-offs; counting those would
penalise correct behaviour (design §8.11).

**The default week is the most recent *complete* one.** A partial week always looks
like a decline.

**The trend table is context, not a finding.** Four weeks is not a trend — read the
slope over 8+ weeks.

---

## What this report deliberately does not contain

No ROI. No monetary "value delivered". No counterfactual "time saved" headline. No
AI-vs-human comparison. No per-person table.

AI is applied to essentially all work here, so **there is no non-AI control group** and
no attribution is possible. Any such number would be a scenario model presented as a
measurement, and it would not survive a competent challenge (design §8.16, §9.1
Decision 2).

The economic metric is **cost per accepted output** — both terms measured. The
comparisons that *are* valid are between AI configurations: agent vs agent, model vs
model, skill on vs off. Those change decisions; a ratio with a percent sign does not.

The JSON output declares this explicitly in `excluded_by_design`, so a downstream
dashboard cannot quietly reintroduce it.

A per-person view is out of scope until the governance statement in design §11.5 is
signed off. That constraint is organisational, not technical.

---

## Cost is notional, not spend

GitHub Copilot bills per seat plus a premium-request allowance, **not per token**. A
per-token figure therefore represents the economic weight of the tokens consumed, not
an invoice line.

Correct for: comparing agents, comparing models, trending cost per accepted output.

Wrong for: chargeback, budget reconciliation, or any number handed to finance as
spend. Use seat count × seat price for those, allocated per person-month (AR-8).

The report prints the `cost_basis` mix and warns when any of it is `modelled`.

---

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

24 tests, stdlib only, no network and no on-disk fixtures. They cover ISO-week
boundaries, duration formatting, malformed-line accounting, dedup by `event_id`,
the intervention-kind rule, acceptance-denominator handling, HTML escaping and
self-containment, and the assertion that no forbidden metric is ever rendered as a
value.

---

# people_workbook.py — the named-person exception

`weekly_report.py` is person-blind on purpose (design §11.5). `people_workbook.py`
is the deliberate exception: an Excel workbook for a lead tracking specific
engineers they are directly supporting, with those engineers named explicitly on
the command line.

```bash
python3 people_workbook.py \
  --person "A Name=<atlassian-account-id>" \
  --person "Another Name=<atlassian-account-id>" \
  --input ../../../reports/exports --since 2026-06-21 \
  --out ../../../reports/ai-work-tracking.xlsx
```

Any number of `--person` flags is fine; two is just the smallest useful case.
Account ids are Atlassian `accountId` values — the same key Jira, Bitbucket and
AIO all use. Look one up with
`GET /rest/api/3/user/search?query=<name or email>`.

It takes an **allow-list**, never a "top N by output" query — there is no way to
ask it who the most productive person is, and that is the point. Anyone who
appears only as a reviewer or co-assignee is written as their opaque account id.

Sheets: Summary · Jira Issues · Jira Transitions · Pull Requests · Reviews Given ·
Reverts · Test Cycles · Test Runs · Coverage & Gaps.

**Test execution matters most for the QA half of a team.** A workbook built only
from Bitbucket makes a QA engineer look inactive while they are running hundreds
of test cases; the AIO sheets are what stop that misreading. Execution is
credited to `executed_by_person_id`, never to the assignee or the cycle owner —
being the standing assignee on a case someone else ran is not work you did, and
it is counted in a separate column.

`not_run` rows stay out of every pass-rate denominator (see `poll_aio.py`), while
blocked and skipped stay **in** it — a blocked test is one that could not be run,
and dropping it flatters the number.

**The rule the tests exist to protect:** `0` and `no data` are different cells.
`0` means measured and zero; `no data` means there was nothing to measure from.
Collapsing the two would have hidden the most interesting number in the first
real run — 32 pull requests merged with zero reviewers.

Optional `--source NAME|STATUS|COUNT|DETAIL` and `--gap MISSING|REASON` rows
populate the Coverage sheet, so the file states what it could *not* see rather
than leaving the reader to assume it saw everything.

Needs `openpyxl` (`weekly_report.py` remains stdlib-only). 22 tests.
