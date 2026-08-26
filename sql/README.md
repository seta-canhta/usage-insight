# AI Telemetry — Warehouse Layer (BigQuery)

The warehouse half of the AI effectiveness observability platform: raw landing zone,
conformed core, dashboard mart, data-quality checks, and the metric views.

**Binding references — this directory implements them, it does not redefine them:**

| Document | Role |
|---|---|
| [`../schema/CONTRACT.md`](../schema/CONTRACT.md) | **Single source of truth.** Envelope, event types, cost derivation, acceptance state machine, attribution rules, table names. |
| [`../../../docs/spikes/ai-effectiveness-observability.md`] (`ai-engineering-platform`: `docs/spikes/ai-effectiveness-observability.md`) | Design rationale: §6.4 derived tables, §8 formulas, §9.3 attribution, §9.4 DQ catalogue, §11 storage/retention/access/privacy. |
| [`../../../skills/bigquery/SKILL.md`] (`ai-engineering-platform`: `skills/bigquery/SKILL.md`) | BigQuery execution conventions for this org (`bq_tool.py`). |

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
| 1 | `01_raw.sql` | DDL — `raw.ai_run_event` (the one live stream), plus `raw.otel_span` / `raw.v_otel_span_tokens` kept **frozen** for pre-cutover reprocessing | once, then on schema change |
| 2 | `02_dims.sql` | DDL + seed — `core.dim_person`, `core.dim_model_pricing` (effective-dated, **placeholder rates**), `core.dim_agent_version`, `core.dim_task_benchmark` | once, then on schema change |
| 3 | `03_core_fct.sql` | DDL — `core.fct_ai_run`, `fct_ai_output`, `fct_pull_request`, `fct_ci_run`, `fct_jira_issue` | once, then on schema change |
| 4 | `04_transform_run.sql` | **raw → `core.fct_ai_run`.** Binds runs to session usage, refuses to attribute a multi-run session, and computes cost | **hourly** |
| 5 | `05_transform_output.sql` | raw + Bitbucket facts → `core.fct_ai_output`. Runs the acceptance state machine | **nightly** |
| 6 | `06_marts.sql` | DDL + rebuild — `marts.agg_daily_person_agent` with the k-anonymity guard, plus the one-row `marts.dim_grain_cutover` | **nightly**, after 5 |
| 7 | `07_dq_checks.sql` | DDL + DQ-1…DQ-16 (plus DQ-6b, DQ-6c, DQ-17, DQ-RET and the ten contract-1.1.0 guards) → `dq.dq_findings` | **hourly** (safe to run all together) |
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

**One live stream, one frozen one.**

- **`raw.ai_run_event`** — the *correlation* stream from `emit.py`, the git hooks, the
  pollers and — since contract 1.1.0 — `cli/copilot_read.py`, which reads Copilot CLI's
  own session journal. Knows `jira_issue_key`, `person_id`, `agent_name`, outputs,
  gates, PR ids, commit SHAs **and now tokens**: `model.call` arrives here.
- **`raw.otel_span`** — the *OTel* stream. ⚠ **LEGACY and FROZEN.** Nothing writes it.
  It is kept, not dropped, because rows inside the 90-day window are the only way to
  price a pre-cutover run; every reader is marked LEGACY and gated on `cutover_date`.
  Drop it once the last pre-cutover partition has expired.

`raw.otel_span` has **no `total_tokens` column, deliberately.** Copilot emits no
`gen_ai.usage.total_tokens`. Total is derived exactly once, in
`raw.v_otel_span_tokens`, as `input + output` — cached-input is a subset of input and
reasoning is a subset of output, so adding either would double-count. The same rule
holds for the journal path, plus one more: `cache_write_tokens` is **not** a term of
the CONTRACT §4 cost formula and is never added into `total_tokens`.

`finish_reason` and `retry_count` were **removed** from `raw.v_otel_span_tokens` in
1.1.0: neither is recorded by the journal, and a nullable column that is always NULL
is an invitation to `SUM()` it and publish a zero. That is not hypothetical — see the
retry-rate note below.

⚠ **`run_id` and `trace_id` on `raw.ai_run_event` are NULLABLE**, and both relaxations
are load-bearing. Journal `model.call` events carry `run_id = null` by contract, and
the git scanner carries `trace_id = null` on any commit with no `AI-Trace-Id` trailer.
Declared `NOT NULL`, those rows do not get dropped — the load fails. `trace_id` also
carries **two namespaces** now (`trc_<uuid4hex>` from the emitter, a Copilot session id
from the journal reader); they are disjoint by construction and
`core.fct_ai_run.trace_id_namespace` names which.

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
effective-dated pricing join, sets `cost_basis`, and — new in 1.1.0 — **refuses to
compute a cost at all** for a run that shared its Copilot session with other runs.

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

Ten more were added with contract 1.1.0, because the source switch created several
ways for a number to be wrong while looking right:

| Check | Why |
|---|---|
| `DQ-SESSION` | A session with no `session.shutdown` recorded **no** usage — unknowable, not zero (2 of 22 on the reference machine). Also flags usage that arrived outside the 21-day rebuild window. |
| `DQ-GATE` | ~12% of gate evaluations carry no verdict. They leave the pass-rate denominator, and this is the trace that they did. |
| `DQ-COV` | The journal covers `copilot-cli` **only**. Emits an `info` finding **every day**, unconditionally — a check that only speaks up on failure cannot tell you a whole surface has been unmeasured all along. |
| `DQ-GRAIN` | The trend-honesty guard: a row on the wrong branch, a mart row mixing grains, or `dim_grain_cutover` disagreeing with the data. |
| `DQ-ATTR` | A multi-run session with a non-NULL cost — a direct `CONTRACT.md` §3 breach. Should be structurally impossible; checked anyway, because the temptation to "just fill in the cost column" is the strongest failure mode in the system. |
| `DQ-TOOL` | Tool-status coverage, plus any `status` value outside the `ok`/`error` enum — which is exactly how `tool_error_count` came to be permanently zero. |
| `DQ-BILL` | Scans view definitions for `premium_requests` in an arithmetic expression with a `_usd` column (§4.1). Heuristic, and deliberately biased towards false positives. |
| `DQ-MODEL` | A `model_id` on the wire with no price-book row. DQ-6 catches this at run grain, after the damage; this catches it where the fix is one row. |
| `DQ-PATH` | An absolute `file_path` reaching the warehouse. The client guards it; this verifies rather than trusts. The finding deliberately reports the **shape**, never the path — quoting it would copy the identifier into `dq_findings`. |
| `DQ-LINK` | What share of measured tokens the `link_method = 'explicit'` gate excludes from the cost-per-output views, so an empty view is explicable rather than mysterious. |

`DQ-11` (duplicate events) was **retuned**, not added. Journal `event_id`s are
deterministic and an open session legitimately re-emits every event on each hourly
read, so duplicates are the expected steady state — 2,614 of 2,935 on a second pass.
Left as one finding per duplicated event it wrote thousands of correct, unactionable
rows a day, which trains people to ignore `dq_findings` entirely. It now reports the
rate once, and keeps a per-event `critical` for the genuine fault: one `event_id`
carrying two different payloads.

`DQ-13` (late arrival) now **excludes `model.call`**. That event is written at
`session.shutdown` and legitimately arrives days after the run it covers. That is a
grain, not a late arrival; `DQ-SESSION(b)` asks the question that actually matters —
did it arrive too late for the transform to attach it.

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
| **`marts.v_session_usage`** | **CONTRACT §3/§4.1 — usage at the grain it is true at** |
| **`marts.v_config_comparison`** | **§9.1 — the primary decision-making query** |

---

## The bind: runs ⇄ usage

This is the heart of the whole system and the thing most likely to be broken by a
well-meaning edit. It lives in `04_transform_run.sql`.

**The bridge** is one event type: `run.bound` (`CONTRACT.md` §3, event #2), now
carrying `copilot_session_id` — the `~/.copilot/session-state/<id>` directory name.
The transform reads `COALESCE($.copilot_session_id, $.otel_conversation_id)` so a
client that has not upgraded still binds; reading only one name binds nothing from
half the fleet, and reading only the old name binds nothing at all from 1.1.0.

For a run read from a journal there is no `run.bound` at all: `cli/copilot_read.py`
writes the session id into `trace_id`, and the transform recovers it from there.

**Why a naive join on the session id is wrong — still true.** A Copilot session is not
a run. One session routinely hosts several — sequential invocations, a `/resume`
(which is a second invocation, not a continuation), or a supervisor plus nine
sub-agents. Joining on session id alone is many-to-many and would attribute every
token in the session to every run in it, silently.

### ⚠ And in 1.1.0 the fix that worked before stopped working for tokens

The span source stamped **every API call** with its own timestamp, so binding on
(session id AND time window) placed each call on the run that made it. The journal does
not: it totals usage per session in `session.shutdown`, so **one `model.call` now
carries a whole session's tokens for one model**, stamped at shutdown.

Applying the window join to it would charge every token in a multi-run session to
whichever run happened to be open when the session ended. `CONTRACT.md` §3 settles it:

> A session that hosted more than one run must report `cost_usd = NULL` for its
> constituent runs rather than a share of the total. §2.4 already forbids synthesising
> a join key; apportioning a measured total across runs by time or by call count is the
> same offence wearing arithmetic.

So the transform computes `session_run_count` and, where it exceeds 1, withholds the
**whole usage block** — not just the cost — and sets `cost_attributable = FALSE`.
Charging one run a session's tokens is the same error whether or not a price is
attached to them.

**Expect run-level cost coverage to fall sharply.** On the reference machine most
sessions are multi-run (38 resumes across 22 sessions, plus 13 sub-agent runs), so this
is the common case. That fall is the truth becoming visible, and
`runs_cost_not_attributable_count` in the mart is there so it can be read as a
measurement change rather than a collection failure.

**The tokens are not lost.** `marts.v_session_usage` publishes them at (session ×
model) grain, which `CONTRACT.md` §3 confirms is valid — "tokens and premium requests
per session, per model, per person, per repository, per week. Every §6 Cost figure is
built from these and is unaffected."

### The window join, which survives for everything else

`tool.call`, `gate.evaluated`, `output.generated` and `human.turn` are one journal
record each with their own timestamp, and `cli/copilot_read.py` already stamps a
`run_id` on them, so the transform simply groups by `run_id`. The window machinery
below is now **legacy**, serving pre-cutover spans only:

1. Build each bound run's **active window**: `[started_at − 5 min, terminal_at + 5 min]`,
   capped at 24 hours for a run with no terminal event (matching the DQ-2 orphan window,
   so an abandoned run cannot keep absorbing tokens from whatever the engineer does next).
2. Join `raw.v_otel_span_tokens` to that window on `conversation_id` **and** time.
3. Force one-span-to-one-run with
   `QUALIFY ROW_NUMBER() OVER (PARTITION BY span_id ORDER BY run.started_at DESC, run_id ASC) = 1`.

The `PARTITION BY span_id` is the load-bearing part: it makes double-counting
*structurally impossible* rather than merely unlikely. The `started_at DESC` tie-break
assigns a span to the **most recently started** run whose window contains it, so
sequential runs each get their own tokens and a supervisor's calls land on the
**sub-agent**; AR-4 then rolls the supervisor up by `trace_id` instead of re-counting.
`run_id ASC` is a secondary tie-break purely for determinism across re-runs.

Cost is computed **per call** on that path and **per (session, model)** on the journal
path — `CONTRACT.md` §4 accepts the looser grain explicitly, because "inventing a finer
one would be a guess with a decimal point on it". If **any** call or model in the unit
cannot be priced, the whole unit's `cost_usd` is `NULL`: summing only the priceable part
produces a number that looks complete and is systematically too low.

---

## ⚠ The grain cutover — read this before comparing anything across 2026-08-26

`04_transform_run.sql` declares `cutover_date`. Runs started before it take usage from
OTel spans at **per-call** grain; runs on or after it take usage from the Copilot
journal at **per-session-model** grain. The two branches are gated on that date so no
row is ever processed by both.

A date alone is not enough to mark the boundary. `04` rebuilds a 21-day trailing window
and `06` rebuilds 45 days, so a date **smears** across every rebuild instead of marking
anything. The boundary is therefore carried **in the data**:

| Where | What |
|---|---|
| `core.fct_ai_run.usage_grain` | `per_call` \| `per_session_model` \| `none` |
| `core.fct_ai_run.usage_source` | `otel_span` \| `copilot_journal` \| `emitter_estimate` \| `none` |
| `marts.agg_daily_person_agent.usage_grain_mixed` | a row aggregating both grains |
| **`marts.dim_grain_cutover`** | **one row every dashboard joins to draw the annotation** |
| `DQ-GRAIN` | reports a row on the wrong branch, a mixed mart row away from the boundary, or a `dim_grain_cutover` date that disagrees with the data |

Retention makes this long-lived rather than a fortnight's problem: `fct_*` keeps 396
days and `agg_daily_person_agent` keeps **1130**, so three years of executive trend
cross this line.

### What changes at the boundary, and must be announced

| Column / metric | Before | After |
|---|---|---|
| `tool_error_count` | **structurally 0** — the transform tested `status = 'failed'`, a value the CONTRACT §3 enum has never contained | measured (62 errors in 2,062 calls) |
| `gate_pass_count` / `gate_fail_count` | both 0 — the span source carried no gate status | real verdicts on ~88% of gates; the other ~12% land in `gate_unknown_count` |
| `retry_rate_pct` | **0.0%, always** — an always-NULL numerator divided by `run_count` | `NULL` — the metric is retired, nothing records retries |
| run-level cost coverage | most runs priced | most runs `NULL`, because most sessions host several runs |
| `model_call_count` | one per API call | one per (session, model). The real call count is the new `request_count` |
| measured surfaces | `vscode-copilot-chat` + `copilot-cli` | `copilot-cli` only. The others are **unmeasured, not zero** — `DQ-COV` says so daily |
| — | — | new: `premium_requests` (the unit Copilot actually bills), `nano_aiu`, `cache_write_tokens` |

---

## ⚠ `premium_requests` is measured; `cost_usd` is modelled. Never blend them.

`CONTRACT.md` §4.1: Copilot charges **per seat plus premium requests**, never per
token. So `cost_usd` — derived from vendor list prices — is an *economic weight*, right
for comparing agents, models and configurations, and wrong to hand a finance team as
spend. `premium_requests` is the measured count of the unit actually billed.

They are carried **side by side** on `fct_ai_run`, on the mart and in
`v_cost_per_accepted_output`, and never summed together or into `total_cost_usd`: "one
is measured and dimensionless, the other is modelled and in dollars, and a single
number carrying both would be defensible as neither." `DQ-BILL` scans
`INFORMATION_SCHEMA.VIEWS.view_definition` for anyone who tries.

`premium_requests` is **NUMERIC, not INT64**. `requests.cost` is fractional by model
tier — a premium request can cost 0.33 — and an INT64 column truncates that to 0,
standing a measured zero in for real billed usage.

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
| Raw | `raw.ai_run_event`, `raw.otel_span` (frozen) | **90 days** | `01_raw.sql` |
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

`marts.v_session_usage` is the same case for a different table: it reads
`raw.ai_run_event`, which is restricted to the AI Platform Owner and data engineers. It
**must** be an authorised view. It projects a session id, a model id, token counts and
billing counts — no content, no file paths, no email hashes — and the grant goes on the
view, never on `raw.*`.

⚠ That view has a **90-day horizon**, because `raw.*` expires at 90 days while the mart
keeps 1130. It is not a substitute for a `core.fct_ai_session` table with 396-day
retention; that table is the right answer and is deliberately **not** built here, since
a half-built fact table and transform would be worse than a stated gap. Roll the view
into a mart before the raw partitions expire if a long trend is needed.

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

**There is no session fact table.** `marts.v_session_usage` covers the grain but only
for 90 days; see the note under Access control. Stated, not hidden.

**There is no per-run apportionment of session usage, and there must never be.**
`CONTRACT.md` §3 forbids it and `DQ-ATTR` reports it. Splitting a session total across
its runs by elapsed time or by call count is the §2.4 synthesised join key with
arithmetic on top, and it would look entirely reasonable in a code review.

**The economic metric is `marts.v_cost_per_accepted_output`** — both terms measured, no
counterfactual anywhere. If leadership asks for ROI, the answer is the pair of questions
a decision can actually turn on: *is cost per accepted output falling, and is acceptance
rising*, sliced by `marts.v_config_comparison`.

`time_saved` is likewise not computed anywhere. `core.dim_task_benchmark` exists solely so
`DQ-12` has a lookup to check; its presence is not licence to reintroduce the metric.
