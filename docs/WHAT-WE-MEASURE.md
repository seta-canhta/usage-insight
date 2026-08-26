# AI Effectiveness — what we measure

*Measured 2026-08-24; local source replaced 2026-08-26. Project PRJ · 427 work
items · 19 people · one pilot machine.*

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
| 9 | **AI Cost per Accepted Output** | AI Cost ÷ Accepted outputs | ↓ | Copilot telemetry ÷ outputs merged without rework. **Tokens and premium requests now measured; the division is still not permitted** — see below | tokens: yes |
| 10 | **AI ROI** | (Value − AI Cost) ÷ AI Cost | ↑ | Cost is measurable. **Value is a management definition**, not a measurement | — |

**AI adoption: 22%** — 93 of 427 items carry an AI tag, up from 2 in April to 54 in
July, across 19 people. AI-tagged work is accepted at first review 94.5% of the time
against 92.7% for the rest.

---

## What changed on 2026-08-24

> Read as a record of that day. The span source described below was retired on
> 2026-08-26 and three of its statements no longer hold — see the next section.

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

## What changed on 2026-08-26

The local source was replaced. Copilot's OTel span exporter is retired; usage now
comes from Copilot CLI's own session journal at
`~/.copilot/session-state/<id>/events.jsonl`. Four things got better, one got
narrower, and both halves belong in the same section.

**Nothing has to be switched on.** The exporter was a per-machine VS Code setting,
and a machine where it never landed collected *nothing* while looking exactly like
a machine having a quiet month. The journal is written whether or not anything is
watching. Measured 2026-08-26 on one real tree of 22 journals: **2,935 contract
events**, with **zero absolute paths and zero usernames** in the output.

**Tool failures are measured for the first time.** Under the span source
`tool.call.status` was structurally NULL, so the weekly report's tool-failure count
was always `0` **by construction, not by measurement** — a zero nobody had ever
observed. The journal carries a real verdict: **2,000 ok, 62 error**.

**Gate verdicts are real, and their absence is honest.** Copilot's bash tool appends
its exit code to the output it returns, matched anchored at the end so a command that
merely prints the phrase cannot forge a result. **~88%** of shell-command gates now
carry a real verdict. The other ~12% are NULL — still running, or output truncated —
and a **missing verdict is not a pass**.

**Repositories are discovered, not registered.** `session.start` records the git
root, so `scan` walks every tree Copilot has worked in: **7 found with nothing
registered**.

**And what it costs.** The journal covers the **Copilot CLI / agent surface only**.
The VS Code Chat panel and inline completions write nothing to it and are now
**unmeasured**; pilot usage is mixed across all three. Every read publishes a
`coverage` block naming the uncovered surfaces, and no figure derived from this
source may be read without it. A surface nobody measured is not a surface nobody
used.

Two of the 22 sessions ended without a clean shutdown and therefore record no usage
totals at all. Their tokens are **unknowable, not zero** — and `0` is the one value
they must never be given.

---

## The confidence ceiling on metric 9

`CONTRACT.md` §2.4: *only `method='explicit'` rows may feed cost-per-output.*

Joining a Copilot session to an agent run currently earns **`heuristic` 0.8** — the
run's window contains the session's events, and the agent name agrees from two
independent sources. That is enough to report tokens by agent. It is **not** enough
to price an individual output.

`explicit` requires the agent to emit `run.bound` naming its own
`copilot_session_id`, and no agent does. Until then the honest position is: we can
say what a week of AI work cost in tokens and premium requests, and we cannot say
what one accepted output cost.

**A second ceiling arrived with the journal, and it is a real loss.** The span source
emitted one `model.call` per API call, each individually timestamped, so a
time-windowed join placed tokens on the sub-agent that spent them. The journal does
not: it totals usage per session in `session.shutdown`, so one aggregate `model.call`
per (session, model) is all there is. Tokens per session, per model, per person, per
repository and per week are exact and unaffected — and so are tokens by agent where
the session ran one agent, which is the common case on the CLI surface. Tokens
attributed to **one run inside a multi-run session** are not available at all, and
such runs report `cost_usd = NULL` rather than a share. Apportioning a measured total
across runs by time or call count is synthesising a join key with arithmetic on top.

**Tokens are a weight; premium requests are the bill.** Copilot charges per seat plus
premium requests, never per token, so a token-derived `cost_usd` is an economic weight
— right for comparing agents and configurations, wrong to hand a finance team as
spend. The two are reported alongside each other and never blended: one is measured
and dimensionless, the other modelled and in dollars. The seat component is a contract
term and is not visible from any client, so any total presented as spend must say it
is excluded.

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
| Copilot CLI's session journal | tokens, premium requests, model, agent name, skill, tool success/failure, gate verdicts, per-edit lines, git context | one pilot machine · **CLI/agent surface only** |
| Platform agents | which agent, which ticket, which phase | one pilot machine |

Work counts as AI-assisted when it carries at least one of four markers the platform
applies as it happens — `PLANNED_BY_COPILOT`, `AUTH_BY_COPILOT`, `GEN_BY_COPILOT`,
`REVIEW_BY_COPILOT`. Nothing is reconstructed afterwards.

Review outcomes come from Jira, not code review: only 5 of 102 pull requests carried
a formal review, because this team reviews through Jira.

Coverage cannot yet be split by priority — priority lives in Jira and the test tool
uses a separate scale.

---

## What we refuse to measure

These are decisions, not gaps. Each was taken because the alternative produces a
number that flatters the programme and cannot survive a competent challenge.
Reopen the design before adding any of them back.

| Refused | Because |
|---|---|
| **ROI** | `value_gained` rests on `time_saved_hours`, `defects_prevented` and `incidents_avoided`, none of which is measurable here. The economic metric is **cost per accepted output** — both terms measured, no counterfactual. |
| **Any monetary "value delivered" field** | A currency figure attached to an output is a modelled assumption wearing the costume of a measurement. The contract forbids emitting one. |
| **Time saved, and any counterfactual** | AI is applied to essentially all work, so there is no non-AI control group. "How long would this have taken manually?" is biased upward, unfalsifiable, and a vanity metric. |
| **AI vs human comparison** | The non-AI cohort does not exist. Unmarked commits are overwhelmingly AI-assisted work where the marker was not applied — **"marker absent" is not "human"**. |
| **Individual leaderboards** | Rankings change behaviour before they measure it. See the governance conditions below. |

**The valid comparisons are inside AI**, and the schema already carries every
dimension for them: agent vs agent, model vs model, skill on vs off,
`agent_version` before vs after a prompt change, task class vs task class. Those
are queries, not builds. They are observational, so stratify by task class and
PR-size band and require **n ≥ 20 per arm** before reporting a difference.

### If individual metrics are ever built

Measuring individuals is legitimate for capability development and unacceptable
as covert performance surveillance, and the difference is **entirely
governance**. All of the following, or none of it:

- Declared purpose in writing **before** collection — capability development and
  platform improvement. Not performance management, compensation, or redundancy
  selection.
- **No surprise.** Engineers see their own data on day one, in the same view
  their manager sees. Nothing about a person exists that the person cannot see.
- **k-anonymity ≥ 5** on every aggregate crossing a person boundary; smaller
  cells show counts, never rates.
- **Directional, never absolute.** "Independent completion rose 11pp over six
  months" is a development signal. "Person X is 4th of 12" is a ranking.
- A high intervention rate reads as an **agent-quality** signal first: if an
  agent needs correction 60% of the time, the finding is about the agent.
- Right to context — any person-level figure carries a way to annotate it.
- Works-council / employment-law review first, in any jurisdiction requiring it.

**Absent from every dashboard regardless:** person-vs-person leaderboards of
runs, cost or acceptance; individual intervention rankings; and raw prompts,
code or artifact content at any access level.

### Before adding a metric

1. Is it in the metric dictionary? If not, say why it belongs there.
2. Are both its numerator and denominator *measured*, or is one assumed?
3. Does it require a control group that does not exist?
4. Does every dimension it uses have < 100 distinct values?
5. Would you show it, unchanged, to the person it describes?

A "no" to 2, 3 or 5 means the metric is an opinion. Publish it as one, or not at
all.

## Rules

| | |
|---|---|
| No content is stored | no prompts, no code, no text — counts and categories only |
| Missing is never shown as zero | an unconnected source is marked as such, so a gap never reads as a bad result |
| Self-reported work is never counted | it would rank people by how carefully they write reports |
| Nothing synthesises a join | where two sources cannot be linked with evidence, the answer is "cannot attribute" |
| Not a performance record | collection on individual machines is voluntary and reversible |
| An unmeasured surface is not an unused one | the Chat panel and inline completions write no journal; every read names what it could not see |
