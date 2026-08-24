# AI Telemetry — Warehouse Layer (BigQuery)

The warehouse half of the AI effectiveness observability platform: raw landing zone,
conformed core, dashboard mart, data-quality checks, and the metric views.

**Binding references — this directory implements them, it does not redefine them:**

| Document | Role |
|---|---|
| [`../schema/CONTRACT.md`](../schema/CONTRACT.md) | **Single source of truth.** Envelope, event types, cost derivation, acceptance state machine, attribution rules, table names. |
| [`../../../docs/spikes/ai-effectiveness-observability.md`](../../../docs/spikes/ai-effectiveness-observability.md) | Design rationale: §6.4 derived tables, §8 formulas, §9.3 attribution, §9.4 DQ catalogue, §11 storage/retention/access/privacy. |
| [`../../../skills/bigquery/SKILL.md`](../../../skills/bigquery/SKILL.md) | BigQuery execution conventions for this org (`bq_tool.py`). |

If a change is needed, **change `CONTRACT.md` first and bump `schema_version`** — then
change these files. Never the other way round.

---

## Before you run anything

Every file uses `${PROJECT_ID}` as a substitutable placeholder. **No GCP project id is
confirmed for this platform.** Nothing in this directory has been executed against a
real project.

```bash
export PROJECT_ID="your-gcp-project"

# Substitute and apply one file
sed "s/\${PROJECT_ID}/${PROJECT_ID}/g" 01_raw.sql | bq query --use_legacy_sql=false
```

Dataset `location` is set to `EU` in the `CREATE SCHEMA` statements. Change it in
`01_raw.sql`, `02_dims.sql`, `06_marts.sql` and `07_dq_checks.sql` **before first
apply** — a dataset's location is immutable once created.

---

## Execution order

Order matters: later files reference tables created by earlier ones.

| # | File | What it does | When to run |
|---|---|---|---|
| 1 | `01_raw.sql` | DDL — `raw.ai_run_event` (correlation stream), `raw.otel_span` (OTel stream), and `raw.v_otel_span_tokens` (canonical token derivation) | once, then on schema change |
| 2 | `02_dims.sql` | DDL + seed — `core.dim_person`, `core.dim_model_pricing` (effective-dated, **placeholder rates**), `core.dim_agent_version`, `core.dim_task_benchmark` | once, then on schema change |
| 3 | `03_core_fct.sql` | DDL — `core.fct_ai_run`, `fct_ai_output`, `fct_pull_request`, `fct_ci_run`, `fct_jira_issue` | once, then on schema change |
| 4 | `04_transform_run.sql` | **raw → `core.fct_ai_run`.** Binds the correlation stream to the OTel stream and computes cost | **hourly** |
| 5 | `05_transform_output.sql` | raw + Bitbucket facts → `core.fct_ai_output`. Runs the acceptance state machine | **nightly** |
| 6 | `06_marts.sql` | DDL + rebuild — `marts.agg_daily_person_agent` with the k-anonymity guard | **nightly**, after 5 |
| 7 | `07_dq_checks.sql` | DDL + DQ-1…DQ-16 (plus DQ-6b, DQ-6c, DQ-17) → `dq.dq_findings` | **hourly** (safe to run all together) |
| 8 | `08_metrics.sql` | Metric views in `marts`, plus the supporting `core.fct_test_case` table | once, then on definition change |
| — | `09_set_model_price.sql` | **Run on demand, not in sequence.** Populates or updates one model's rates: closes the current price window and opens a new one, never updating in place. Read its billing note before quoting any cost as spend | on price change |

Files 4–7 are **idempotent**. Re-running them over the same window produces the same
result; they `MERGE` on a primary key or delete-and-reinsert a bounded trailing window.
Files 1–3 and 8 use `CREATE ... IF NOT EXISTS` / `CREATE OR REPLACE VIEW`.

`09_set_model_price.sql` is **not** idempotent and is **not** part of the numbered
sequence — it mutates the price book. Run it once per model per price change. Until it
has been run at least once per model, `dim_model_pricing` holds placeholder rows with
NULL rates: `cost_usd` resolves to NULL (never 0) and DQ-6 fires on every run. That is
deliberate — an unpriced model must be loudly unpriced, not silently free.

**One-time bootstrap:**

```bash
for f in 01_raw.sql 02_dims.sql 03_core_fct.sql 08_metrics.sql; do
  sed "s/\${PROJECT_ID}/${PROJECT_ID}/g" "$f" | bq query --use_legacy_sql=false
done
```

> ⚠️ `02_dims.sql` contains an `INSERT` seeding `dim_model_pricing`. It is **not**
> idempotent — re-running it inserts duplicate placeholder rows, which `DQ-6b` will
> then correctly report as overlapping pricing windows. Run the seed once.

**Scheduled jobs** (BigQuery scheduled queries, or Cloud Scheduler → the same SQL):

```
hourly   04_transform_run.sql
hourly   07_dq_checks.sql
nightly  05_transform_output.sql  →  06_marts.sql   (in that order)
```

---

## What each file actually does

### `01_raw.sql` — the landing zone

Two streams that know nothing about each other:

- **`raw.ai_run_event`** — the *correlation* stream from `emit.py`, the git hooks, and
  the pollers. Knows `jira_issue_key`, `person_id`, `agent_name`, outputs, gates, PR
  ids, commit SHAs. **Knows nothing about tokens.**
- **`raw.otel_span`** — the *OTel* stream from GitHub Copilot's native OTLP exporter
  (design §4.4a). Knows tokens, models, latency, tool calls, `gen_ai.conversation.id`.
  **Knows nothing about Jira, PRs, or commits.**

`raw.otel_span` has **no `total_tokens` column, deliberately.** Copilot does not emit
`gen_ai.usage.total_tokens`. Total is derived exactly once, in
`raw.v_otel_span_tokens`, as `input + output` — cached-input is a subset of input and
reasoning is a subset of output, so adding either would double-count. **Read the view;
do not re-derive.**

### `02_dims.sql` — dimensions

- **`core.dim_person`** — ⚠️ **access-restricted** (see below). The identity map that
  makes per-person metrics safe at all; design §9.4 measured the collision problem on
  this very repository.
- **`core.dim_model_pricing`** — effective-dated so historical cost stays reproducible
  when prices change. **Seeded with `GPT-5.3-Codex` and `Claude Sonnet 4.6` at NULL
  rates flagged `is_placeholder = TRUE`.** See the warning below.
- **`core.dim_agent_version`** — the registry that makes the design §9.1 config
  comparisons possible.
- **`core.dim_task_benchmark`** — supporting lookup so `DQ-12` has something to check.
  No metric reads it; `time_saved` is deliberately not computed.

### `03_core_fct.sql` — facts

Five fact tables at 13-month retention. Every row carries `link_method` and
`link_confidence`; dashboards default to `explicit` and widening that filter is a
deliberate, visible act.

### `04_transform_run.sql` — the join that makes the system work

See **The bind** below. Also computes cost per `CONTRACT.md` §4 through the
effective-dated pricing join, and sets `cost_basis`.

### `05_transform_output.sql` — the acceptance state machine

Implements `CONTRACT.md` §5 exactly, including the 7-day maturity window and the 30-day
revert window, and the design §8.9 split between post-review **rework** and pre-review
**auto-fix cycles**. Enforces AR-1, AR-3, AR-4, AR-5, AR-7, AR-9.

### `06_marts.sql` — the dashboard feed

`marts.agg_daily_person_agent`, grain day × person × agent × project, 37-month
retention, with the k-anonymity guard.

### `07_dq_checks.sql` — data quality

`dq.dq_findings` plus one query per check, DQ-1 through DQ-16 from design §9.4, plus
four invariant guards this design needed and the catalogue did not have:

| Extra | Why |
|---|---|
| `DQ-6b` | Overlapping `dim_model_pricing` validity windows — the cost join would match both rows and silently double-count. |
| `DQ-6c` | Cost derived from a `is_placeholder = TRUE` price row — not chargeback-safe. |
| `DQ-17` | The `run.bound` bridge failed. Three sub-cases that look identical on a dashboard (cost = `NULL`) and have completely different fixes. |
| `DQ-RET` | The `dq_retention` check design §11.2 calls for: asserts every partitioned table still carries the `partition_expiration_days` the policy requires. Run **weekly**. |

Checks **report**; they never repair. Repair is the transform's job, and keeping the two
apart is what makes the findings trustworthy as an audit trail.

### `08_metrics.sql` — the metric views

Each view carries its design section and the exact formula in its header comment, so the
definition travels with the query.

| View | Design § |
|---|---|
| `marts.v_ai_acceptance_rate` | §8.7 — explicit links only, with sensitivity at 0.10 / 0.25 / 0.50 |
| `marts.v_rework_rate` | §8.9 |
| `marts.v_cost_per_accepted_output` | §8.12 — **the economic metric** |
| `marts.v_pr_lead_time` | §8.14 — median **and** p85, raw and per 100 changed lines |
| `marts.v_asset_reuse_rate` / `marts.v_run_reuse_rate` | §8.15 — asset-centric and run-centric |
| `marts.v_manual_intervention_rate` | §8.11 — **excluding** `approval` and `clarification` |
| `marts.v_automation_coverage` | §8.8 |
| `marts.v_run_reliability` / `marts.v_tool_error_rate` | §7.8 |
| **`marts.v_config_comparison`** | **§9.1 — the primary decision-making query** |

---

## The bind: correlation stream ⇄ OTel stream

This is the heart of the whole system and the thing most likely to be broken by a
well-meaning edit. It lives in `04_transform_run.sql`, `span_binding` CTE.

**The bridge** is one event type: `run.bound` (`CONTRACT.md` §3, event #2). `emit.py`
captures the active Copilot conversation id at run start and emits one row carrying
`{run_id, otel_conversation_id, jira_issue_key}`.

**Why a naive join on `conversation_id` is wrong.** A Copilot conversation is a chat
*session*, not a run. One session routinely hosts several runs — sequential
invocations, or a supervisor plus nine sub-agents. Joining on conversation id alone is
many-to-many and would attribute every token in the session to every run in it,
inflating cost by the number of runs and doing so silently.

**The strategy actually used:**

1. Build each bound run's **active window**: `[started_at − 5 min, terminal_at + 5 min]`,
   capped at 24 hours for a run with no terminal event (matching the DQ-2 orphan window,
   so an abandoned run cannot keep absorbing tokens from whatever the engineer does next).
2. Join `raw.v_otel_span_tokens` to that window on `conversation_id` **and** time.
3. Force one-span-to-one-run with
   `QUALIFY ROW_NUMBER() OVER (PARTITION BY span_id ORDER BY run.started_at DESC, run_id ASC) = 1`.

The `PARTITION BY span_id` is the load-bearing part: it makes double-counting
*structurally impossible* rather than merely unlikely. The `started_at DESC` tie-break
assigns a span to the **most recently started** run whose window contains it, so:

- sequential runs in one session each get their own tokens;
- for a supervisor + sub-agent, the tokens land on the **sub-agent** — the run that
  actually issued the call. AR-4 then rolls the supervisor up by `trace_id` instead of
  re-counting.

`run_id ASC` is a secondary tie-break purely for determinism across re-runs.

Cost is then computed **per call**, not per run: `CONTRACT.md` §4 states the formula at
call grain, and a run can legitimately span two models or straddle a midnight price
change. If **any** call in a run cannot be priced, the run's `cost_usd` is `NULL` —
summing only the priceable calls would produce a number that looks complete and is
systematically too low.

---

## ⚠️ Model pricing is seeded with PLACEHOLDERS

`02_dims.sql` seeds `dim_model_pricing` with the two models declared in this repo's
agent frontmatter — `GPT-5.3-Codex` and `Claude Sonnet 4.6` — at **NULL rates**, flagged
`is_placeholder = TRUE`.

The rates are NULL on purpose. A fabricated but plausible rate propagates to a
dashboard, gets screenshotted into a steering deck, and is indistinguishable from a real
figure. A NULL rate propagates to `cost_usd = NULL` and fires `DQ-6`, which is loud and
correct — and it exercises the `CONTRACT.md` §4 "never 0" path from day one.

**Before publishing any cost figure:**

1. Obtain the real per-1k rates from the vendor pricing pages.
2. **Close** the placeholder rows: set `effective_to` to the day before the real rate
   starts.
3. `INSERT` new rows with `is_placeholder = FALSE` and a populated `source_url`.

Do **not** `UPDATE` the rate columns of the placeholder rows in place — that silently
restates every historical cost (design §8.4).

---

## Retention (design §11.2)

Enforced by table-level `partition_expiration_days`, so expiry is a platform guarantee
rather than a cron job someone can forget. `DQ-RET` at the end of `07_dq_checks.sql` is
the `dq_retention` check design §11.2 asks for — schedule it weekly.

| Layer | Table(s) | Retention | Set in |
|---|---|---|---|
| Raw | `raw.ai_run_event`, `raw.otel_span` | **90 days** | `01_raw.sql` |
| Core facts | `core.fct_*` | **396 days** (13 months) | `03_core_fct.sql`, `08_metrics.sql` |
| Mart | `marts.agg_daily_person_agent` | **1130 days** (37 months) | `06_marts.sql` |
| DQ | `dq.dq_findings` | **180 days** | `07_dq_checks.sql` |
| Dimensions | `core.dim_*` | **no expiry** | `02_dims.sql` |

Rationale: raw at 90 days matches the retention already chosen in the legacy
`qa-metrics-tracker.yaml`; 13 months on facts enables year-on-year comparison with one
month of overlap; 37 months on the mart supports three-year executive trends on cheap,
aggregated, low-sensitivity data.

Dimensions are **not** expired — expiring them would break the reproducibility of the
historical facts that reference them. The one exception is `dim_person`, whose retention
is *life of employment + 30 days* and is therefore the **only table in the warehouse
subject to manual deletion**; everything else is append-only with platform-enforced
expiry.

---

## Access control (design §11.4)

| Layer | Who | Mechanism |
|---|---|---|
| `raw.*` | AI Platform Owner + data engineers only | Dataset IAM |
| **`core.dim_person`** | **Named data stewards ONLY** | **Table-level IAM (not dataset-inherited), Data Access audit logging on, quarterly access review** |
| `core.fct_*` | Engineering leads, QA leads, DevOps | Dataset IAM |
| Own-data view | Every engineer | Authorised view filtering `person_id` to the session user |
| Team view | Managers, own reports only | Row-level security on `team_id` |
| `marts.*` | All engineering | Dataset IAM — this is why the k-guard lives in the mart, not in a dashboard filter |
| Executive view | Leadership | Aggregated only; no person grain reaches it |

`core.dim_person` is the only place in the warehouse where a pseudonymous hash can be
turned back into a human being. It is the one table whose IAM must be set explicitly and
reviewed.

`marts.v_manual_intervention_rate` reads `dim_person` for `tenure_start_date`. It **must
be created as an authorised view** so mart readers get the bounded `tenure_bucket`
without being granted read on the identity map. Never widen IAM on `dim_person` to make
a view work.

Service accounts for the pollers hold **read-only** scopes on Jira/Bitbucket/CI; the
collector service account can write only to Pub/Sub.

---

## Privacy controls built into this layer

- **No content, anywhere** (`CONTRACT.md` §1.1, design §11.3). Hashes, counts, bounded
  enums, and file paths only. No prompts, responses, source, diffs, error bodies, or raw
  email addresses. Column descriptions state this per column so it survives a schema
  migration.
- **k-anonymity ≥ 5** (design §11.5, §9.5) in `marts.agg_daily_person_agent` and in every
  person-grain metric view. Groups below the threshold **expose their counts** but have
  every ratio and percentage column set to `NULL`. Do not reconstruct a suppressed rate
  by dividing the exposed counts — re-aggregate to a coarser grain and re-apply the test
  there.
- **Completeness is published, not hidden** (design §9.4). `explicit_link_pct` and
  `runs_with_tokens_pct` are deliberately *not* k-guarded: they describe the pipeline,
  not a person, and a dashboard that hides its own gaps is worse than no dashboard.
- **Interpretation rule carried in the data.** `v_manual_intervention_rate` emits
  `tenure_bucket` and an `interpretation_rule` string, because §8.11 makes segmenting by
  tenure mandatory: a high rate on a junior engineer is a signal about **agent quality**,
  not about the person.

---

## ⛔ Deliberately not implemented

**There is no ROI view and no monetary "value delivered" metric.** `CONTRACT.md` §1.6,
design §8.16, and design §9.1 Decision 2 forbid publishing one.

The reason is measurability, not squeamishness. AI is applied to essentially all work, so
there is no non-AI control group; `value_gained` rests entirely on `time_saved_hours`,
`defects_prevented`, and `incidents_avoided`, none of which is measurable, estimable from
this data, or falsifiable. Publishing it produces a number that flatters the programme
and cannot survive a competent challenge.

**The economic metric is `marts.v_cost_per_accepted_output`** — both terms measured, no
counterfactual anywhere. If leadership asks for ROI, the answer is the pair of questions
a decision can actually turn on: *is cost per accepted output falling, and is acceptance
rising*, sliced by `marts.v_config_comparison`.

`time_saved` is likewise not computed anywhere. `core.dim_task_benchmark` exists solely so
`DQ-12` has a lookup to check; its presence is not licence to reintroduce the metric.
