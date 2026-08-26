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
| 4 | no signal | — | same denominator |
| 5 | partial | — | AIO case ↔ Bitbucket merge join not built |
| 6 | out of scope | — | counterfactual; needs a controlled manual arm |
| 7 | **live** | 66.6% (8,653 / 12,983) | 13 cycles, 8,028 automated, 557 defects |
| 8 | not measurable | — | AIO keeps one run per case per cycle and overwrites on re-execution. Verified: 0 case+cycle pairs have more than one run. Comparing across cycles counts fixes and regressions as flakes |
| 9 | blocked | — | 0 `AI-Run-Id` events, 0 AI telemetry in August |
| 10 | out of scope | — | "value" is not observable |

**One is live. One is reportable with a caveat. Two are permanently out of
scope. The rest are blocked.**

An earlier draft claimed three were live. Two of those were wrong, which is why
this file exists separately from the code.

## What blocks the rest

**No AI telemetry in August, from any source.** Not thin, zero.

| Source | Newest data |
|---|---|
| `~/.copilot/session-state` on the reference machine | 2026-06-26 |
| VS Code `chatSessions` on the reference machine | 2026-02-05 |
| the two sampled QA machines | no `session-state` at all |

Verified against file mtimes. The cause is that no Copilot CLI session has been
run on those machines, not that the journal is unwritable. Every metric with AI
in its numerator therefore has no numerator.

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
