# CLAUDE.md — what this project is for

## The goal

**This system exists to measure ten metrics. They are the foundation. Everything
else is supplementary.**

Any work here is judged by whether it moves one of these ten closer to being
reportable and true. A metric that is easy to compute but is not on this list is
supporting evidence, not the point — and must never displace one of the ten in a
report, a schema decision, or a sprint.

| # | Metric | Measures | Formula | Direction |
|---|---|---|---|---|
| 1 | **Automation Output** | automation scripts AI produced | scripts created & completed | ↑ |
| 2 | **Automation Coverage** | share of eligible cases automated | automated / total eligible, **per test cycle** | ↑ |
| 3 | **First-Pass Acceptance Rate** | script quality at first review | accepted on first review / total reviewed | ↑ |
| 4 | **Rework Rate** | how much AI output must be fixed | scripts requiring rework / total scripts | ↓ |
| 5 | **Automation Lead Time** | test case → working automation | ready − start | ↓ |
| 6 | **Productivity Gain** | speed-up from AI | (AI − manual) / manual | ↑ |
| 7 | **Execution Rate** | automation actually being run | executed / planned | ↑ |
| 8 | **Flaky Test Rate** | automation stability | flaky / total | ↓ |
| 9 | **AI Cost per Accepted Output** | cost of one usable output | AI cost / accepted outputs | ↓ |
| 10 | **AI ROI** | value created against cost | (value − AI cost) / AI cost | ↑ |

The subject is **QA test automation**, not software delivery in general. When a
design choice could serve either, it serves this.

## Status

Measured figures live in [`docs/METRICS.md`](docs/METRICS.md) — one metric is
live, one is reportable with a caveat, two are permanently out of scope, and the
rest are blocked. Read it before quoting a number: two of those figures were
marked live in an earlier draft of *this* file and were wrong.

**Metric 2 is measured by cycle, never over the case estate.** "Eligible"
means the cases in the test cycles actually being delivered — the cycle is the
delivery record and the pull request is not (`schema/CONTRACT.md` §3 row 22).
The two readings disagree enormously and the estate one is the misleading one.
Measured 2026-08-27 on IML: across the seven cycles that ran, **7,976 of 8,564
cases are automated — 93.1%**, with the two large regression cycles at 95.7%
and 97.2%. Read over the whole 10,742-case estate the same data puts P3 at
22.8%, which sounds like a crisis and is mostly a backlog nobody has triaged:
3,695 of 5,183 P3 cases have no automation status set at all and appear in no
cycle. Counting those as "not automated" measures how diligently a field is
filled in, which is the same error as counting from the daily sync sheet.

The estate view still answers a real question — *what is in the backlog* — and
may be reported as that, clearly labelled, never as coverage.

## Rules that outrank convenience

These come from `schema/CONTRACT.md` and `docs/METRICS.md`. They have each
already prevented a wrong number from shipping.

- **Never synthesise a join key** (AR-1). Measured: `extract_jira_key` invented
  `AUG-25` (a date) from `fix/AUG-25`; fabricated keys outnumbered real ones
  14 to 5. Always pass `projects=` — real keys here are `IML`, `APR`, `AERLABS`,
  `IOTA3`. The AIO test key space (`IML-TC-5`, `IML-CY-199`) is separate and
  is read by `extract_test_keys`; it lands in `context.test_case_key` /
  `context.test_cycle_key`, never in `jira_issue_key`.
- **Absent is never zero.** An unmeasured quantity and a measured zero must not
  render the same. VS Code stores no token counts, so those fields are NULL
  forever, not 0.
- **Never count from self-reported data.** The daily sync sheet is a *mapping*
  source (person → team → ticket) only. Its "AI Usage" column reads `None` on
  20 of 21 entries; that is a form field, not an adoption measurement.
- **Never store content.** Prompts, responses, diffs, source. Readers name what
  they keep (`KEEP`) and never inspect the rest — an exclusion list cannot keep
  pace with a format that gains keys without asking.
- **Cost is never client-emitted.** Derived in the warehouse against dated
  pricing. `premium_requests` is a measured count and is fine; `cost_usd` is a
  valuation and is not.
- **Repo-relative paths only.** An absolute path carries the username.

## How collection is triggered

**On Copilot activity, not on a clock.** `insight setup` installs
`~/.copilot/hooks/seta-insight.json`; Copilot runs `insight hook` before each
tool call; the hook returns in milliseconds and detaches a collector.

Two different clocks, deliberately:

| | interval | why |
|---|---|---|
| collect | 10 min debounce | evidence perishes — 24 of 27 VS Code workspace folders and 6 of 7 `gitRoot`s were already deleted when read after the fact |
| upload | 60 min batch | shipping per trigger would put dozens of nearly-empty objects a day on the endpoint |

Nothing is lost by batching: `pack` is idempotent over the day, and a bundle
that misses a window is shipped whole by the next. The stamp is written on
upload **success** only, so a failed upload leaves the next run due.

The hourly timer stays as a fallback for machines where the hook cannot be
installed. Note that a hook cannot be triggered from outside — a laptop takes no
inbound commands. What it gives instead is data that is already fresh, so
nobody needs to trigger it.

## Layout

Libraries and services are separate, and the shared library belongs to neither
of its consumers:

```
common/          the shared library — build_event, Config, HttpClient,
                 extract_jira_key, hash_email, ssl_context
cli/             the local client, shipped as a single zipapp
pollers/         Jira, Bitbucket, AIO, CI
importers/       weekly bundles, daily reconciliation
report/          weekly Markdown, per-person workbooks
collector/       the ingest service — the attribute allow-list, enforced
server/          the collection endpoint — S3 behind one PUT
schema/          CONTRACT.md, the single source of truth
sql/             the warehouse
tools/           build_pyz.py, and diagnostics/ that belong to no metric
```

`common/` sits at the root, not inside `pollers/`, because `cli/`, `importers/`,
`report/` and `collector/` all depend on it. The zipapp mirrors this layout
exactly, so a checkout and an installed binary resolve imports the same way.

What each module is for:

| | |
|---|---|
| `cli/copilot_read.py` | Copilot CLI journal — premium requests, cache split, per-session totals |
| `cli/vscode_read.py` | VS Code Copilot Chat (`.jsonl`) — per-call latency, tool names, prompt/output tokens |
| `cli/rtk_read.py` | `rtk`, a second token source present on the pilot machines only |
| `pollers/poll_aio.py` | AIO TCMS — metrics 2, 7, 8 |
| `pollers/poll_bitbucket.py` | PRs, reviews, automation-script diffstat — metrics 1, 3, 4 |
| `pollers/poll_jira.py` | tickets, AI labels, transitions — metric 5 |
| `common/` | the shared library every other package depends on |
| `schema/CONTRACT.md` | single source of truth; it wins over any code |

## Docs

Four files, and there is no fifth. What it is (`README.md`), how to install it
(`docs/INSTALL.md`), what it measures (`docs/METRICS.md`), how to run the
pipeline (`docs/OPERATE.md`), plus `schema/CONTRACT.md` for field-level truth.
Anything that reads as design narrative or an investigation log belongs in a
commit message, not a new markdown file.

`insight help` carries the user-facing version of the same thing, in the tool.

## Testing

```bash
for s in pollers report collector cli importers server; do
  python3 -m unittest discover -s $s/tests
done
python3 -m unittest discover -s tests        # admin.py
```

All suites must pass before anything ships.
