# Trail gaps — what is missing and what it costs to add

A **trail** is a machine-readable mark that work leaves behind. Without one, the work
happened but cannot be counted.

**Design rule: a trail is only worth adding if it is a by-product of work already being
done.** A trail that costs an engineer a separate action will be skipped under pressure,
and a trail that is skipped under pressure produces a metric that measures compliance
rather than work.

---

## The gap table, cheapest first

| # | Missing trail | Blocks | Trail to add | Who writes it | Marginal cost |
|---|---|---|---|---|---|
| G1 | Agent run — **0 events ever emitted** | which agent, which task, burn | wire `emit.py` into the 11 remaining agent definitions | us, once | **zero** |
| G2 | Commit ↔ AI run | separating AI output from human output | `AI-Run-Id:` git trailer written by the agent | the agent | **zero** |
| G3 | `GEN_BY_COPILOT` unreadable | QA-side AI attribution | add it to `AI_COMMIT_MARKER_RE`; add the matching Jira label to the QA agents | us + agent | **zero** (bug fix) |
| G4 | Token / cost | metrics 9, 10 | `COPILOT_OTEL_ENABLED=1` per developer | developer, once | **~10 min** |
| G5 | Daily report unstructured | metrics 1, 2, 5 | fix the Excel columns (see `IMPORT-SPEC.md`) | **already written daily** | **~zero** |
| G6 | AIO case ↔ Jira issue — 0/4,248 | P1/P2 split, metric 5 | Jira key on the AIO case | QA at case design | 5s/case + 4,248 backfill |
| G7 | AIO case ↔ script file — 0/4,248 | verifying metric 2 | automation key = repo path | QA when automating | 10s/case + backfill |
| G8 | `REVIEW_BY_COPILOT` never applied | AI review quality | the external review system must tag back | **outside our control** — see below |
| G9 | Test rerun history — 0 pairs | metric 8 flaky | publish each execution instead of overwriting | CI / AIO config | depends on CI |

G1–G5 are essentially free. G6–G7 are the ones that cost real human time. G8 depends
on a system we do not own.

---

## G8 — the review trail is owned by someone else

`REVIEW_BY_COPILOT` means: **the team triggers an AI code review in a different
system, and that system tags the ticket back when it finishes.**

Consequences:

1. We cannot instrument it. No AIEP agent applies it, and adding one would not help —
   the trigger is elsewhere.
2. We can only **consume** it, which makes the tag-back a hard dependency. If that
   system tags inconsistently, the metric silently under-reports and looks like
   "AI review is rarely used" rather than "the tag is unreliable".
3. Before anything is built on it, verify the tag-back actually fires. Today it has
   fired **zero** times in 427 issues, which is either no adoption or no tagging —
   and those two need different fixes.

**Open question — must be answered before Phase 3:** which system, who owns it, and
does it apply the label on every completed review or only on request?

What it unlocks once reliable: pairing `REVIEW_BY_COPILOT` against the Jira first-pass
and reopen rates already computed in `FINDINGS.md §7` — i.e. do AI-reviewed issues
bounce back less often than the 19.7% baseline.

---

## The marker set, closed

Four markers exist in the flow. Treat this as a closed enum; anything else is drift.

| Marker | Form | Meaning |
|---|---|---|
| `PLANNED_BY_COPILOT` | Jira label | AI produced the plan |
| `AUTH_BY_COPILOT` | Jira label + commit prefix | AI authored the change |
| `GEN_BY_COPILOT` | commit prefix (Jira label **to be added**) | AI generated test artifacts |
| `REVIEW_BY_COPILOT` | Jira label (**nothing applies it yet**) | AI reviewed the change |

Three drifted variants are already in the data — `PLANNER_BY_COPILOT`,
`DEV_BY_COPILOT`, `COPILOT_TESTING` — and each one silently subtracts from the AI
figure. Agents must validate against this enum **before** applying a label rather than
after, and the poller must count unrecognised `*_BY_COPILOT` labels as a data-quality
signal instead of dropping them.

---

## Self-reported trails: use for mapping, never for counting

The daily Excel report is the cheapest trail available because it is already being
written. It is also **self-reported, retrospective, and human**.

- Use it to **build** the AIO ↔ Jira mapping and to **cross-check** machine trails.
- Never use it to **count** output.

Counting from self-report produces a metric that ranks people by how carefully they
write reports. Someone who works hard and logs little will look worse than someone who
works less and logs everything — and that failure is invisible in the output, which is
what makes it dangerous.

The reconciliation is the real value:

```
Excel:  person X worked on PRJ-6384 and PRJ-TC-2891 on 2026-08-12
Jira:   PRJ-6384 transitioned on 2026-08-12          ✓
AIO:    PRJ-TC-2891 has a run on 2026-08-12          ✓
     → mapping PRJ-6384 ↔ PRJ-TC-2891, high confidence
```

Agreement across three sources builds the mapping G6 needs without editing 4,248
cases by hand. Disagreement is itself a finding — either work is not reaching Jira, or
the report overstates. Both are worth surfacing, and neither should be silently
smoothed over.

This also yields a metric not in the original ten:

> **Trail completeness** — the share of self-reported work that left a machine-readable
> trace. It measures the measurement system, and it should be reported alongside every
> other figure so a reader knows how much of reality the numbers cover.
