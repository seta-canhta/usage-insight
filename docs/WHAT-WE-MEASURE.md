# AI Effectiveness — what we measure

*Measured 2026-08-24. Project PRJ · 427 work items · 19 people · one pilot machine.*

Metric names and formulas are the ones requested. **"How we measure it" states what
is actually computed** — where that differs from the requested formula, the
difference is called out rather than smoothed over.

| # | Metric | Requested formula | Target | How we measure it | Now |
|---|---|---|:--:|---|---:|
| 1 | **Automation Output** | # scripts created & completed | ↑ | Git: one artifact per file an AI-marked commit touched, typed from its path. Only commits carrying a marker or an `AI-Run-Id` count | 57 |
| 2 | **Automation Coverage** | Automated ÷ Total eligible | ↑ | Test tool: automated ÷ all live cases in the cycle, matching its own dashboard | 97.8% |
| 3 | **First-Pass Acceptance** | Accepted first review ÷ Total reviewed | ↑ | Jira: items moving forward on their first exit from review, not back. **Jira workflow review, not code review** | 93.1% |
| 4 | **Rework Rate** | Scripts requiring rework ÷ Total scripts | ↓ | Jira: items reopened at least once ÷ items reviewed. **Denominator is work items, not scripts** | 19.7% |
| 5 | **Automation Lead Time** | Ready − Start | ↓ | Case created → its script merged. **Blocked**: no test case carries a work-item key | — |
| 6 | **Productivity Gain** | (AI − Manual) ÷ Manual | ↑ | Jira: median days created → resolved, AI-tagged vs rest, per item type | 7.3 vs 25.0 d |
| 7 | **Execution Rate** | Executed ÷ Planned | ↑ | Test tool: runs with a result ÷ runs planned. Not-run rows stay in the denominator | 95.6% |
| 8 | **Flaky Test Rate** | Flaky ÷ Total tests | ↓ | Result changing between runs of the same cycle on the same code. **Blocked**: re-runs overwrite | — |
| 9 | **AI Cost per Accepted Output** | AI Cost ÷ Accepted outputs | ↓ | Copilot telemetry ÷ outputs merged without rework. **Tokens now measured; the division is not yet permitted** — see below | tokens: yes |
| 10 | **AI ROI** | (Value − AI Cost) ÷ AI Cost | ↑ | Cost is measurable. **Value is a management definition**, not a measurement | — |

**AI adoption: 22%** — 93 of 427 items carry an AI tag, up from 2 in April to 54 in
July, across 19 people. AI-tagged work is accepted at first review 94.5% of the time
against 92.7% for the rest.

---

## What changed on 2026-08-24

Copilot's telemetry was switched on for one machine and a real agent run was
measured end to end. Three things moved.

**Metric 9 is no longer blocked at the source.** Tokens, model, latency and cache
reads all arrive. One run produced:

| | |
|---|---:|
| Model calls | 7 |
| Input tokens | 211,481 |
| Cached input | 185,856 |
| Output tokens | 3,124 |
| Reasoning tokens | 1,271 |
| Tool calls | 21 |

What is still missing is not the cost side but the **denominator**: pricing, and a
link firm enough to charge a run for an output. See "the confidence ceiling" below.

**Which agent ran arrives from Copilot itself.** `copilot_chat.mode_name` carries the
agent name on the `invoke_agent` span, and it does so whether or not the agent
remembers to emit anything. The design assumed this could only come from the
emitter; it was wrong, and the two sources now corroborate each other.

**Artifacts and gates are derived, not awaited.** On the measured run the agent
emitted `run.started` and `run.completed` and nothing between — `phases_completed: 0`.
Rather than wait for the wiring to be obeyed, artifacts come from git (files an
AI-marked commit touched) and gates from terminal spans (a command that runs pytest,
eslint or tsc *is* a gate evaluation). Gate `status` stays NULL: the verdict lives in
the tool result, which is content and stays out, so "it ran" is what we know.

---

## The confidence ceiling on metric 9

`CONTRACT.md` §2.4: *only `method='explicit'` rows may feed cost-per-output.*

Joining a Copilot conversation to an agent run currently earns **`heuristic` 0.8** —
the run's window contains every span, and the agent name agrees from two independent
sources. That is enough to report tokens by agent. It is **not** enough to price an
individual output.

`explicit` requires the agent to emit `run.bound` naming its own conversation id, and
no agent does. Until then the honest position is: we can say what a week of AI work
cost in tokens, and we cannot say what one accepted output cost.

That limit is a property of the evidence, not a gap in the code, and the linker
reports it in a field rather than leaving a reader to re-derive the rule.

---

## Read with care

| | |
|---|---|
| **6 · 7.3 vs 25.0 days** | Two self-selected groups — people reach for AI when the work suits it. A signal, not proof of cause. The ratio also changes with the reading (70% less elapsed time, or 3.4× throughput), so quote the two medians, never a multiplier |
| **2 · 97.8%** | Matches the test tool's own dashboard exactly, but counts a ticked field. **0 of 4,248 cases link to a real script.** Measures form-filling, not automation |
| **7 · 99.0% pass rate** | A 0.6% failure rate is too good. Either tests aren't stressing the product, or failures aren't recorded |
| **9 · 185,856 of 211,481 input tokens were cache reads** | Roughly 88%. Any cost model that prices cached and fresh input the same will be wrong by close to an order of magnitude |

A 2.6% flaky rate was previously reported and is **withdrawn** — it compared results
across different cycles, which measures change, not stability.

---

## Sources

Read automatically through each system's API, or from a file the tool writes itself.
Nothing is typed in by hand.

| System | Gives us | Volume |
|---|---|---|
| Jira | transitions, review outcomes, reopens, priority, AI tags | 2,014 transitions · 427 items |
| Bitbucket | pull requests, merges, reverts, files and lines changed | 102 PRs · 9 reverts |
| Test management | runs, results, cycles, case inventory, automation status | 17,047 runs · 4,248 cases |
| GitHub Copilot | tokens, model, latency, tool calls, agent name | one pilot machine |
| Platform agents | which agent, which ticket, which phase | one pilot machine |

Work counts as AI-assisted when it carries at least one of four markers the platform
applies as it happens — `PLANNED_BY_COPILOT`, `AUTH_BY_COPILOT`, `GEN_BY_COPILOT`,
`REVIEW_BY_COPILOT`. Nothing is reconstructed afterwards.

Review outcomes come from Jira, not code review: only 5 of 102 pull requests carried
a formal review, because this team reviews through Jira.

Coverage cannot yet be split by priority — priority lives in Jira and the test tool
uses a separate scale.

---

## Rules

| | |
|---|---|
| No content is stored | no prompts, no code, no text — counts and categories only |
| Missing is never shown as zero | an unconnected source is marked as such, so a gap never reads as a bad result |
| Self-reported work is never counted | it would rank people by how carefully they write reports |
| Nothing synthesises a join | where two sources cannot be linked with evidence, the answer is "cannot attribute" |
| Not a performance record | collection on individual machines is voluntary and reversible |
