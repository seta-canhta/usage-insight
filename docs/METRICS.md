# The ten metrics

This system exists to measure these. Everything else is supporting evidence.

| # | Metric | Formula | Direction |
|---|---|---|---|
| 1 | Automation Output | scripts created and completed | ↑ |
| 2 | Automation Coverage | automated / total eligible | ↑ |
| 3 | First-Pass Acceptance Rate | accepted on first review / reviewed | ↑ |
| 4 | Rework Rate | scripts requiring rework / total | ↓ |
| 5 | Automation Lead Time | ready − start | ↓ |
| 6 | Productivity Gain | (AI − manual) / manual | ↑ |
| 7 | Execution Rate | executed / planned | ↑ |
| 8 | Flaky Test Rate | flaky / total | ↓ |
| 9 | AI Cost per Accepted Output | AI cost / accepted outputs | ↓ |
| 10 | AI ROI | (value − AI cost) / AI cost | ↑ |

The subject is QA test automation, not software delivery in general.

## What is actually reportable

Window: August 2026 onward. Measured 2026-08-26.

| # | State | Figure | Why |
|---|---|---|---|
| 1 | partial | 195 scripts changed across 35 merged PRs | only 2 of 35 carry an AI marker |
| 2 | declared, not verified | 80.2% (1,483 / 1,848) | `has_automation_key` is false on every case, so this measures a dropdown, not whether automation exists. 1,150 cases have no status and are excluded, not counted as un-automated |
| 3 | no signal | — | 5 review events on 41 PRs |
| 4 | no signal | — | same denominator. One of its two blockers was removed 2026-08-27: `lines_changed_after_first_review`, the numerator §5 defines and `sql/08_metrics.sql` sums, had no emitter at all, so `v_rework_rate` was averaging over an empty column. It is now measured per commit against its first parent, split at `first_review_at`. The remaining blocker is the one above — 5 review events on 41 PRs, and a rework figure needs a review to be after |
| 5 | partial | — | AIO case ↔ Bitbucket merge join not built |
| 6 | out of scope | — | counterfactual; needs a controlled manual arm |
| 7 | **live** | 66.6% (8,653 / 12,983) | 13 cycles, 8,028 automated, 557 defects |
| 8 | not measurable | — | AIO keeps one run per case per cycle and overwrites on re-execution. Verified: 0 case+cycle pairs have more than one run. Comparing across cycles counts fixes and regressions as flakes |
| 9 | blocked | — | 0 `AI-Run-Id` events. AI telemetry now exists (935 laptop events, 16 August days, measured 2026-08-26) but carries no cost input: `premium_requests` is NULL on 427 of 427 model calls |
| 10 | out of scope | — | "value" is not observable |

**One is live. One is reportable with a caveat. Two are permanently out of
scope. The rest are blocked.**

An earlier draft claimed three were live. Two of those were wrong, which is why
this file exists separately from the code.

## What nothing could be attributed to, until 2026-08-27

Measured on the W34 export, and the reason several of the rows above say "no
signal" rather than a number:

| | Measured |
|---|---|
| Laptop events carrying a person id | 0 of 935 |
| Laptop events carrying any ticket | 0 of 935 |
| Distinct branch names on those laptops | 4, none containing a key |
| Real prompts naming a ticket | 1 in 5,036 |
| Key-shaped decoys in those prompts | 234 (`UTF-8`, `SHA-256`, `CVE-2024`, `GPT-4`) |
| Watchtower branch names naming an AIO test key | 0 of 82 |
| AIO cases with `has_automation_key` | 0 of 4,512 (4,165 marked "Automated") |
| AIO runs carrying any context key | 0 of 8,539 |
| `AI-Run-Id` trailers, i.e. `link.method='explicit'` | 0 of 121 |

AIO runs, AIO cases and Bitbucket all key on the same Atlassian accountIds and
join to each other perfectly. The laptop side shared **not one id** with any of
them, so the question "what did this person's AI use produce" had no join to
make in any direction.

Four of those rows were fixable in code and were fixed: AIO events now carry
their own case and cycle keys; the extractor can read that key space at all
(and no longer mines `IML-TC-5` for the false ticket `TC-5`); prompts and PR
fields are scanned for it; and `importers/pull.py --identities` supplies the
accountId the endpoint already authenticated. The report's §10 now prints a
**Joinability** table per surface, so a zero here is visible rather than
inferred.

Two are not code. Branch names carry no test key and `has_automation_key` is
unset on the whole estate — until one of those changes, no amount of parsing
reaches a test case from a laptop, and case-level attribution stays out of
reach. Person-level and week-level attribution is now possible; case-level is
not.

## What blocks the rest

**AI telemetry now arrives, and still has no cost input.** Superseded
2026-08-26: this section previously read "No AI telemetry in August, from any
source. Not thin, zero." That was measured before collection reached any QA
machine, and it is no longer true.

Measured 2026-08-26 from the collection endpoint, `reports/2026-W34/exports/`:

| | Measured |
|---|---|
| Laptop events, August 2026 | 935 |
| Distinct August days with activity | 16 |
| Machines reporting | 7 |
| Surfaces | `vscode-copilot-chat` (913), `headless` (22) |

What has *not* changed is the numerator for metric 9. Of 427 August
`model.call` events, `premium_requests` is NULL on **427** and `input_tokens`
on **371** — VS Code Copilot Chat does not record either. `run.*` events,
populated `run_id`s and AI commit markers remain at **0**, so there is still no
`AI-Run-Id` and no admissible cost input. Absent is not zero: a report that
renders these as 0 is claiming a measurement nobody made.

**Scope is one repository out of 32 active in August**, and the Jira project
with the most AI attribution is not the one being polled:

| Project | Issues updated in Aug | `AUTH_BY_COPILOT` | `PLANNED_BY_COPILOT` |
|---|---:|---:|---:|
| AERLABS | 719 | 100 | 157 |
| IML | 185 | 24 | 44 |
| APR | 55 | 0 | 0 |

**Review practice produces no signal.** Metrics 3 and 4 cannot be built from a
process that records 5 reviews on 41 PRs.

## To unblock, in order

1. Poll AERLABS and the 32 August-active repositories, not just
   `wt-playwrite-taf`.
2. Get collection onto the QA machines. `insight vscode` must run daily: 24 of
   27 VS Code workspace folders were already deleted when read after the fact.
3. Turn on AI markers — `AI-Run-Id` trailers and `[AUTH_BY_COPILOT]`. Currently
   zero anywhere. This is the only route to metric 9.
4. Build the AIO case ↔ Bitbucket merge join for metric 5.

## Rules that keep these honest

- **Never synthesise a join key.** Measured: key extraction invented `AUG-25`
  (a date) from `fix/AUG-25`, and fabricated keys outnumbered real ones 14 to 5.
  Always pass `projects=`. Real keys are `IML`, `APR`, `AERLABS`.
- **Absent is never zero.** VS Code stores no token counts for some calls. Those
  fields are NULL forever, not 0.
- **Never count from self-reported data.** The daily sync sheet is a mapping
  source only. Its "AI Usage" column reads `None` on 20 of 21 entries.
- **Cost is never client-emitted.** `premium_requests` is a measured count and
  is fine. `cost_usd` is a valuation, derived in the warehouse.
- **`REVIEWED_BY_COPILOT` is an unrecognised variant** of `REVIEW_BY_COPILOT`,
  on 6 issues. `reviewed_by_ai` reads 0 where it should read 6. Not changed
  unilaterally; needs a decision on whether to accept the variant.
