# Pipeline

Measurement system for the AI Engineering Platform: what the agents do, what it costs,
and whether the output is kept.

> **`emit.py` is not in this repository.** It runs inside the agent flow and stays in
> `ai-engineering-platform`, which is what ships to engineers. It is documented here
> because it produces the correlation stream this pipeline reads — see
> `docs/ARCHITECTURE.md` for why the split falls where it does.

- **Design**: `ai-engineering-platform/docs/spikes/ai-effectiveness-observability.md`
- **Schema contract**: [`schema/CONTRACT.md`](schema/CONTRACT.md) — the single source of
  truth. Every component conforms to it. Change it there, bump `schema_version`, never
  redefine a structure locally.

---

## 1. Architecture

Two independent event streams are joined in the warehouse. Neither can answer the
question alone, and that is the central fact of the design (§6.5).

```
  ┌─ CORRELATION STREAM ────────────────────────────────────────────────┐
  │  agents (.agent.md)  ── emit.py ──►  ~/.aiep/telemetry/*.ndjson      │
  │    run.started · run.phase.* · output.generated · gate.evaluated     │
  │    scm.pr.created · run.completed                                    │
  │  git hooks (prepare-commit-msg / post-commit) ──► AI-Run-Id trailer  │
  │  KNOWS: jira key, PR id, file paths, gate results, commit SHA        │
  │  CANNOT SEE: tokens, model latency, tool calls                       │
  └──────────────────────────────────────────────────────────────────────┘
                                    │
                       run.bound  ⭐ the bridge event
                  {run_id, gen_ai.conversation.id, jira_issue_key}
                                    │
  ┌─ OTel STREAM ───────────────────┴───────────────────────────────────┐
  │  GitHub Copilot native OTLP export (configuration, not code — §4.4a) │
  │    model.call · tool.call, keyed by gen_ai.conversation.id           │
  │  KNOWS: tokens, model id, latency, retries, tool calls               │
  │  CANNOT SEE: jira key, PR id, artifact identity, acceptance          │
  └──────────────────────────────────────────────────────────────────────┘

                    ┌──────────────┴──────────────┐
   POLLERS ────────►│          COLLECTOR          │◄──── both streams
   Bitbucket PRs    │  validate · reject unknown  │
   CI pipelines     │  event_type · reject        │
   Jira transitions │  forbidden attributes ·     │
   (outcomes the    │  dedup on event_id · stamp  │
   client never     │  ingested_at                │
   sees)            └──────────────┬──────────────┘
                                   ▼
                        BigQuery  raw.* ─► core.* ─► marts.*
                        (append-only)  (transform  (dashboards)
                                        enforces
                                        attribution
                                        rules AR-1..AR-9)
                                   ▼
                        Looker Studio · dq.dq_findings
```

Three non-negotiables carried by every component (CONTRACT §1):

1. **Never store content.** No prompts, responses, source, diffs, error bodies or raw
   emails. Hashes, counts, bounded enums and file paths only. (§11.3)
2. **Fail open.** Telemetry must never block, slow or fail an agent run. Client-side
   errors are swallowed and logged locally.
3. **Append-only and idempotent.** `event_id` is the dedup key; corrections are new
   events, never updates.

---

## 2. Component map

| Directory | One line |
|---|---|
| `schema/` | `CONTRACT.md` — envelope, event-type enum, attribution rules, table names. Read first. |
| `quickwins/` | Standalone scripts that produce real numbers **today**, before any of the below exists. Start here. |
| `report/` | Weekly report generator — Markdown, HTML or JSON from the NDJSON stream. Stdlib only. See `report/README.md` |
| `hooks/` | `prepare-commit-msg` / `post-commit` — append `AI-Run-Id` trailers, emit `scm.commit`. Never touches the commit subject. |
| `collector/` | Validates, deduplicates and lands events into `raw.*`. Rejects unknown `event_type` and forbidden attribute names. |
| `pollers/` | Bitbucket, CI and Jira pull jobs for the outcomes a client never sees — review, merge, decline, revert, pipeline, transition. |
| `sql/` | BigQuery DDL and the `raw → core → marts` transforms, plus the `dq.dq_findings` checks. |
| `tests/` | Contract and transform tests. |

The emitter CLI itself is `emit.py` at the root of this directory. The subset the
instrumented agents use:

```
emit.py run-start --agent <name> --agent-file <path> --jira <KEY> --model <id> --mode <jira_driven|direct_plan>
                  [--trace <trc_…> --parent-run <run_…>]      # sub-agent: join the caller's trace
emit.py phase     --run <id> --phase <name> --start           # run.phase.started
emit.py phase     --run <id> --phase <name> --status <ok|failed|skipped> --ms <n>   # run.phase.completed
emit.py output    --run <id> --type <code|test|spec|mock|doc|csv|config> --path <p> --added <n> --removed <n>
emit.py gate      --run <id> --gate <build|test|lint|secrets|coverage> --status <pass|fail|skipped> --score <n> --attempt <i>
emit.py pr        --run <id> --pr-id <id> [--ai-marker]       # --ai-marker: title carries "[Authored By Copilot]"
emit.py run-end   --run <id> --status <completed|failed|timeout>
```

`run-start` prints JSON `{"run_id": …, "trace_id": …}`. A supervisor stores both in
`workflow-context.json`; each sub-agent passes them back as `--trace` / `--parent-run`,
so one user-initiated workflow is one trace. `emit.py` also carries `run-bound`,
`human-turn`, `commit`, `ship` and `status` subcommands — see `emit.py --help`.

Every call fails open and returns 0 even on error; `--strict` (tests only) surfaces
failures instead.

### Instrumented agents

| Agent | Emission points |
|---|---|
| `agents/development/developer.implementer.agent.md` | `run-start` (phase_1_init) · `output` per file (phase_2) · `gate` per gate per attempt (phase_3) · `pr` (phase_6 step_2) · `run-end` (phase_7) |
| `agents/qualdev/supervisor-test-spec.agent.md` | `run-start` (initialize_workflow, threads trace to sub-agents) · `phase` around phases 0, 2, 3, 4, 4.5, 5, 6, 6.2, 7 · `run-end` (finalize_workflow) |
| `agents/qualdev/test-executor-committer.agent.md` | `run-start` (Step 1) · `gate` from the cucumber report (Step 4) · `run-end` (Step 8) |

Each of those files states the non-blocking rule once, near the top, under `telemetry`.

---

## 3. Setup order

Do these in order. Each step is useful on its own; nothing later is required for
anything earlier to pay off.

| # | Step | Why here |
|---|---|---|
| 0 | **`quickwins/retain_metrics.sh`** wired into the end of each workflow | Design §14.1 item 4. The agents already compute metrics and delete them. Starts a time series today. |
| 1 | **Enable Copilot OTel export** for one developer (§4.4a, §14.1 item 5) | Configuration, not code. Determines whether cost is *measured* or *modelled* everywhere downstream, so it gates the design of everything after it. |
| 2 | **`emit.py`** + local NDJSON buffer at `~/.aiep/telemetry/` | Nothing can be emitted until it exists. Ships locally first, so it works offline and on VPN failure. |
| 3 | **`hooks/`** — `prepare-commit-msg` trailers | The deterministic SCM anchor. Does **not** touch the commit subject, so the existing `[AUTH_BY_COPILOT] [{TICKET}]` convention and all 43 marked commits stay valid. |
| 4 | **Agent instrumentation** (already applied to the three agents above) | Supplies the jira key, PR id and artifact identity that OTel cannot see. |
| 5 | **`collector/` + `sql/` raw tables** | First point at which events are durable and queryable. |
| 6 | **`pollers/`** — Bitbucket first | Acceptance is an *outcome*; no client-side event can observe a merge, a decline or a revert. |
| 7 | **`sql/` core + marts**, then dashboards | Only meaningful once ~6–8 weeks of events exist (§9.1). |
| 8 | **Individual capability metrics (§7.6)** | **Last, and only after the §11.5 governance statement is signed off.** The constraint is organisational, not technical. |

Prerequisites, credentials and open questions (OQ-1…OQ-7) are in design §12.3.

---

## 4. What this deliberately does NOT measure

These are decisions, not gaps. Each was taken because the alternative produces a number
that flatters the programme and cannot survive a competent challenge. Do not add them
back without reopening the design.

**No ROI.** Design **§8.16** retains the formula for reference and marks it *not
recommended for publication*. `value_gained` rests entirely on `time_saved_hours`,
`defects_prevented` and `incidents_avoided`, none of which is measurable under §9.1
Decision 2. The economic metric is **cost per accepted output** (§8.12) — both terms
directly measured, no counterfactual anywhere.

**No monetary "value delivered" field.** CONTRACT §1 rule 6 forbids emitting one. Any
currency figure attached to an output would be a modelled assumption wearing the
costume of a measurement.

**No counterfactual, and no time-saved headline.** Design **§9.1 Decision 2**: AI is
applied to essentially all work, so there is no non-AI control group and no honest way
to answer "how much of this is because of AI". "How long would this have taken
manually?" is systematically biased upward, unfalsifiable, and a vanity metric. §8.10
productivity improvement is demoted to a directional trend.

**No AI-vs-human comparison.** The non-AI cohort does not exist. Unmarked commits and
PRs are overwhelmingly AI-assisted work where the marker was not applied — "marker
absent" is not "human". The real control groups are *inside* AI and the schema already
carries every dimension for them: agent vs agent, model vs model, skill on vs off,
`agent_version` before vs after a prompt change, task class vs task class (§9.1). Those
are queries, not builds. They are observational, so stratify by task class and PR-size
band and require **n ≥ 20 per arm** before reporting a difference.

**No individual leaderboards.** Design **§11.5**: measuring individuals is legitimate
for capability development and unacceptable as covert performance surveillance, and the
difference is entirely governance. Therefore:

- Declared purpose in writing before collection — capability development and platform
  improvement. **Not** performance management, compensation, or redundancy selection.
- No surprise: engineers see their own data on day one, in the same view their manager
  sees. Nothing about a person exists that the person cannot see.
- **k-anonymity ≥ 5** on every aggregate crossing a person boundary; smaller cells show
  counts, not rates.
- Capability metrics are **directional, never absolute**. "Independent completion rose
  11pp over 6 months" is a development signal. "Person X is 4th of 12" is a ranking,
  and rankings change behaviour before they measure it.
- A high intervention rate reads as an **agent-quality** signal first: if an agent needs
  correction 60% of the time, the finding is about the agent.
- Right to context — any person-level figure carries a mechanism to annotate it.
- Works-council / employment-law review before Phase 2 in any jurisdiction requiring it.

**Also deliberately absent from every dashboard** (§10.5): person-vs-person
leaderboards of runs, cost or acceptance; individual manual-intervention rankings; and
raw prompts, code or artifact content at any access level.

---

## 5. If you are about to add a metric

1. Is it in the metric dictionary (§7)? If not, say why it belongs there.
2. Are both its numerator and denominator *measured*, or is one of them assumed?
3. Does it require a control group that does not exist?
4. Does every dimension it uses have < 100 distinct values (CONTRACT §1 rule 5)?
5. Would you be willing to show it, unchanged, to the person it describes?

A "no" to 2, 3 or 5 means the metric is an opinion. Publish it as one, or not at all.
