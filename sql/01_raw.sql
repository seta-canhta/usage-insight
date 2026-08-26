-- =====================================================================================
-- 01_raw.sql — RAW landing zone
-- =====================================================================================
-- Contract:  schema/CONTRACT.md  §2 (envelope), §3 (event types),
--            §7 (table names / partition / cluster)
-- Design:    docs/spikes/ai-effectiveness-observability.md §4.4a, §6.2, §11.1, §11.2
--
-- Two streams land here. As of contract 1.1.0 only ONE of them is still written.
--
--   1. raw.ai_run_event  — the CORRELATION stream. THE ONLY LIVE STREAM.
--        Produced by emit.py, the git hooks, the pollers, and — new in 1.1.0 —
--        cli/copilot_read.py, which reads Copilot CLI's own session journal at
--        ~/.copilot/session-state/<id>/events.jsonl. It knows about Jira keys, PR
--        ids, commit SHAs, outputs, gates AND, since 1.1.0, tokens: `model.call`
--        events now arrive here rather than on a span stream.
--
--   2. raw.otel_span     — the OTel stream. ⚠ LEGACY AND FROZEN as of 1.1.0.
--        Produced by the GitHub Copilot OTLP exporter (design §4.4a), which this
--        platform no longer enables or reads. NOT DROPPED: rows already landed are
--        inside the 90-day window and are what pre-cutover reprocessing prices
--        against. Every reader of it is marked LEGACY below and in
--        04_transform_run.sql, and is gated on the cutover date declared there.
--
-- The bridge is still one event type, `run.bound`, but the id it carries was renamed
-- in 1.1.0: attributes.copilot_session_id (the ~/.copilot/session-state/<id>
-- directory name), with attributes.otel_conversation_id still accepted so a client
-- that has not upgraded keeps ingesting. See 04_transform_run.sql.
--
-- ⚠ SURFACE COVERAGE CHANGED IN 1.1.0. The journal covers `copilot-cli` ONLY.
-- `vscode-copilot-chat` and inline completions write nothing to it and are now
-- UNMEASURED, not zero. A fall in measured tokens across the cutover is a fall in
-- coverage, not in usage. DQ-COV in 07_dq_checks.sql exists to say so out loud.
--
-- Retention: 90 days (design §11.2), enforced by partition_expiration_days so expiry
-- is a platform guarantee rather than a cron job someone can forget.
--
-- Non-negotiable (CONTRACT §1): append-only, never UPDATE, never DELETE. Corrections
-- are new events. `event_id` is the dedup key.
--
-- Substitute ${PROJECT_ID} before running. No project id is confirmed yet — do not
-- hardcode one.
-- =====================================================================================

CREATE SCHEMA IF NOT EXISTS `${PROJECT_ID}.raw`
OPTIONS (
  location    = 'EU',
  description = 'AI telemetry raw landing zone. Append-only. 90-day retention (design §11.2). Access: AI Platform Owner + data engineers only (design §11.4).'
);


-- -------------------------------------------------------------------------------------
-- raw.ai_run_event — the correlation stream
-- -------------------------------------------------------------------------------------
-- Envelope fields (CONTRACT §2) are FLATTENED to top-level typed columns rather than
-- kept as STRUCTs, for two reasons:
--   (a) BigQuery clustering keys must be top-level columns, and CONTRACT §7 mandates
--       CLUSTER BY person_id, agent_name;
--   (b) every downstream transform filters on these, so typing them buys both schema
--       enforcement and partition/cluster pruning.
--
-- Event-type-specific payload (CONTRACT §3) stays in `attributes JSON` because it is
-- a discriminated union across 21 event types. Typing it would mean 21 nullable
-- column groups and a migration every time an event type is added.
--
-- CONTRACT §3 forbidden attribute names are rejected at the collector, not here. This
-- table trusts the collector; DQ checks in 07_dq_checks.sql verify that trust.
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.raw.ai_run_event`
(
  -- ---- envelope: identity and time (CONTRACT §2) ----
  schema_version     STRING    NOT NULL OPTIONS (description = 'Semver of the contract this event was emitted against, e.g. "1.0.0". Collector rejects unknown majors.'),
  event_id           STRING    NOT NULL OPTIONS (description = 'evt_<uuid4hex>. DEDUP KEY (CONTRACT §1.3). Re-delivery must be idempotent — see the MERGE pattern in 04_transform_run.sql.'),
  event_type         STRING    NOT NULL OPTIONS (description = 'Closed enum, CONTRACT §3: run.started | run.bound | run.phase.started | run.phase.completed | model.call | tool.call | human.turn | output.generated | gate.evaluated | run.completed | run.failed | run.timeout | run.abandoned | scm.commit | scm.pr.created | scm.pr.reviewed | scm.pr.merged | scm.pr.declined | scm.revert | ci.pipeline.completed | jira.transition | test.run.completed | test.case.snapshot. Since 1.1.0 model.call, tool.call, gate.evaluated, output.generated and human.turn may also arrive from the Copilot session journal — see the grain warning on model.call in CONTRACT §3.'),
  event_time         TIMESTAMP NOT NULL OPTIONS (description = 'When it happened on the client, UTC. PARTITION KEY. Client clocks drift — DQ-10 clamps skew > 5 min.'),
  ingested_at        TIMESTAMP          OPTIONS (description = 'Set by the collector, never by the client (CONTRACT §2). NULL only for events replayed by a backfill that bypassed the collector.'),

  -- ---- envelope: correlation ids (CONTRACT §2, design §5.2) ----
  -- ⚠ NULLABLE since 1.1.0, and both relaxations are load-bearing. Declared NOT NULL
  -- this table ingested NOTHING from two of its four producers:
  --   * `model.call` from the session journal carries run_id = NULL by contract
  --     (CONTRACT §2.4 forbids synthesising one to force a join), as do poller events
  --     for any commit with no AI-Run-Id trailer;
  --   * scm.commit from cli/insight.py carries trace_id = trailers['ai-trace-id'],
  --     which is absent on every commit a human wrote.
  -- A NOT NULL column does not drop those rows, it fails the load — so the failure
  -- mode was total, not partial. Found by reading the producers against this DDL,
  -- 2026-08-26, before either had been run against a project.
  --
  -- ⚠ trace_id NOW CARRIES TWO NAMESPACES, and this is a deliberate, documented
  -- overload rather than an accident. The emitter mints `trc_<uuid4hex>`;
  -- cli/copilot_read.py fills it with the Copilot **session id** (the
  -- session-state directory name), because a session is exactly what trace_id
  -- means here — one user-initiated workflow, shared by a supervisor and all its
  -- sub-agents, which is what AR-4 rolls up on. A separate `copilot_session_id`
  -- column was considered and rejected: nothing that writes this table can populate
  -- one (the collector's envelope allow-list is closed and lives in
  -- collector/main.py), so the column would be permanently NULL — the exact defect
  -- this file warns about elsewhere. The two namespaces are structurally disjoint
  -- (`trc_` prefix vs a bare session directory name), so no GROUP BY can silently
  -- merge two workflows; 04_transform_run.sql derives copilot_session_id from this
  -- column and publishes `trace_id_namespace` beside it so the overload is visible
  -- in the data and not only in this comment.
  trace_id           STRING             OPTIONS (description = 'One user-initiated workflow. TWO NAMESPACES: trc_<uuid4hex> from the emitter, or a Copilot CLI session id from cli/copilot_read.py (1.1.0). Disjoint by construction; core.fct_ai_run.trace_id_namespace names which. A supervisor and all its sub-agents share one trace_id (AR-4 rolls up on this). NULL where the producer had none — e.g. a commit with no AI-Trace-Id trailer.'),
  run_id             STRING             OPTIONS (description = 'run_<uuid4hex>. One agent invocation — the atom of AI measurement. NULL on `model.call` from the session journal (that event is per session×model, not per run) and on poller events with no AI-Run-Id trailer. CONTRACT §2.4 forbids synthesising one.'),
  parent_run_id      STRING             OPTIONS (description = 'Sub-agent -> supervisor edge. NULL for a root run. Builds the run tree.'),
  span_id            STRING             OPTIONS (description = 'spn_<uuid4hex>. One phase within a run. NOT the OTel span id — see raw.otel_span.span_id.'),
  workflow_id        STRING             OPTIONS (description = 'Legacy bridge, wf-{JIRA}-{YYYYMMDD}, e.g. "wf-PRJ-6383-20260819". Human-readable link to workflow-context.json artifacts.'),

  -- ---- envelope: actor (CONTRACT §2.1) ----
  person_id          STRING             OPTIONS (description = 'Atlassian accountId. CANONICAL PERSON KEY. Never a git display name (design §9.4 documents the identity-collision evidence). CLUSTER KEY.'),
  person_email_hash  STRING             OPTIONS (description = 'sha256(salt + lower(git_author_email)), hex. NEVER the raw email (CONTRACT §1.1, design §11.3).'),
  team_id            STRING             OPTIONS (description = 'From the org directory. NULL until OQ-6 resolves — team dashboards are blocked on this.'),
  role               STRING             OPTIONS (description = 'Bounded enum: dev | qa | devops | po | lead.'),

  -- ---- envelope: context (CONTRACT §2.2) ----
  jira_issue_key     STRING             OPTIONS (description = 'Matches ^[A-Z][A-Z0-9]+-\\d+$. The spine of the whole model. For QualDev runs this is the FEATURE ticket after AR-3 resolution, not qd_jira_key.'),
  jira_project_key   STRING             OPTIONS (description = 'Derived from jira_issue_key. Bounded dimension (CONTRACT §1.5).'),
  repo_full_name     STRING             OPTIONS (description = '{workspace}/{repo_slug} parsed from git remote origin.'),
  branch_name        STRING             OPTIONS (description = 'e.g. feature/PRJ-6383-rate-limit. High cardinality — payload only, never a metric label (CONTRACT §1.5).'),
  product_profile    STRING             OPTIONS (description = 'Bounded enum: watchtower | automotive | ... From workflow-context.json.target_profile.'),
  environment        STRING             OPTIONS (description = 'Bounded enum: dev | sit | pre | prd | local.'),

  -- ---- envelope: agent (CONTRACT §2.3) ----
  agent_name         STRING             OPTIONS (description = 'From .agent.md frontmatter `name`, e.g. "Platform Developer 2.0". CLUSTER KEY. Bounded dimension.'),
  agent_version      STRING             OPTIONS (description = 'Short git SHA of the .agent.md at load time. Answers "which version of the agent produced this" (design §9.1 config comparison).'),
  skill_name         STRING             OPTIONS (description = 'Skill loaded for this run, if any.'),
  skill_version      STRING             OPTIONS (description = 'Short git SHA of the SKILL.md at load time.'),
  surface            STRING             OPTIONS (description = 'Bounded enum: vscode-copilot-chat | copilot-cli | headless | unknown. Determines whether OTel spans exist for this run at all.'),

  -- ---- envelope: link provenance (CONTRACT §2.4, design §5.3) ----
  link_method        STRING             OPTIONS (description = 'explicit | heuristic | marker_only. CONTRACT §2.4: only explicit rows may feed cost-per-output metrics.'),
  link_confidence    FLOAT64            OPTIONS (description = '0.0-1.0. explicit => 1.0. Heuristic L2 joins carry < 1.0 and are excluded from money metrics.'),

  -- ---- event-type-specific payload (CONTRACT §3) ----
  attributes         JSON               OPTIONS (description = 'Event-type-specific attributes per CONTRACT §3. Discriminated on event_type. Contains NO content — no prompts, responses, code, diffs, emails, or error bodies (CONTRACT §1.1). Forbidden key names are rejected by the collector.')
)
PARTITION BY DATE(event_time)
CLUSTER BY person_id, agent_name
OPTIONS (
  partition_expiration_days = 90,
  description = 'CORRELATION STREAM. One row per telemetry event from emit.py / git hooks / pollers. Append-only, event_id is the dedup key. Partition DATE(event_time), cluster person_id, agent_name (CONTRACT §7). 90-day retention (design §11.2). RESTRICTED: AI Platform Owner + data engineers only (design §11.4).',
  labels = [('layer', 'raw'), ('domain', 'ai-telemetry'), ('pii', 'pseudonymous')]
);


-- -------------------------------------------------------------------------------------
-- raw.otel_span — the OpenTelemetry GenAI stream  ⚠ LEGACY / FROZEN (contract 1.1.0)
-- -------------------------------------------------------------------------------------
-- ⚠⚠ NOTHING WRITES THIS TABLE ANY MORE. Kept, not dropped, deliberately.
--
-- 1.1.0 replaced the span stream with Copilot CLI's own session journal
-- (cli/copilot_read.py). The exporter is no longer enabled — cli/vscode_setup.py
-- actively removes the setting — so no new row will arrive.
--
-- It is NOT dropped because the rows already here are inside the 90-day retention
-- window and are the ONLY source that can price a pre-cutover run. Dropping it would
-- make the eight weeks before the switch unreprocessable, which is a far larger loss
-- than the storage. Every reader is marked LEGACY and 04_transform_run.sql gates the
-- whole OTel branch on `started_at < cutover_date` so a run is never priced from both
-- sources.
--
-- When the last pre-cutover partition expires (90 days after the cutover date in
-- 04_transform_run.sql) this table and its view can be dropped outright, and the
-- LEGACY branches in 04, 07 and 08 with them.
--
-- Source: GitHub Copilot Chat (VS Code) and Copilot CLI native OTLP exporter
-- (design §4.4a). This is a shipped product feature, not something this repo builds.
-- Span tree emitted by the exporter:
--
--     invoke_agent   (whole orchestration)
--        └── chat            (one LLM call — this is where gen_ai.usage.* lives)
--        └── execute_tool    (one tool / MCP invocation)
--        └── execute_hook
--
-- ⚠ CRITICAL TOKEN MODELLING NOTE (design §4.4a, verified 2026-08-19):
--   Copilot emits gen_ai.usage.input_tokens and gen_ai.usage.output_tokens, plus
--   cached-input and reasoning tokens. It does NOT emit gen_ai.usage.total_tokens.
--   Therefore total_tokens is deliberately ABSENT from this table — storing a
--   nullable total_tokens column would invite someone to SUM() a column that is
--   always NULL and silently report zero. Total is DERIVED, and only ever as:
--       total_tokens = COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)
--   Cached-input and reasoning tokens are NOT added into the total: cached input is
--   a subset of input that is billed at a different rate (CONTRACT §4 prices it as a
--   separate term), and reasoning tokens are a subset of output on the models that
--   report them. Adding either would double-count. The canonical derivation is the
--   view raw.v_otel_span_tokens below — use it, do not re-derive by hand.
--
-- Privacy: Copilot's exporter captures no prompt content, responses, or tool
-- arguments unless captureContent is explicitly enabled. Design decision (§4.4a,
-- §11.3): keep it OFF, enforced by enterprise-managed settings.
--
-- This table has NO person_id and NO jira_issue_key, by construction. OTel does not
-- know them. That is exactly the correlation problem 04_transform_run.sql solves.
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.raw.otel_span`
(
  -- ---- OTel span identity ----
  span_id                 STRING    NOT NULL OPTIONS (description = 'OTel span id (16 hex chars). NOT the same namespace as raw.ai_run_event.span_id.'),
  trace_id                STRING    NOT NULL OPTIONS (description = 'OTel trace id (32 hex chars). NOT the same namespace as raw.ai_run_event.trace_id — the correlation stream mints its own trc_ ids. Never join these two directly.'),
  parent_span_id          STRING             OPTIONS (description = 'Parent OTel span. NULL for the root invoke_agent span.'),
  span_name               STRING             OPTIONS (description = 'Span name as emitted, e.g. "chat gpt-5.3-codex".'),

  -- ---- timing ----
  start_time              TIMESTAMP NOT NULL OPTIONS (description = 'Span start, UTC. PARTITION KEY (CONTRACT §7).'),
  end_time                TIMESTAMP          OPTIONS (description = 'Span end, UTC. NULL for a span whose end was never exported (crashed client).'),
  duration_ms             INT64              OPTIONS (description = 'Span duration in milliseconds. Kept explicitly rather than derived so a NULL end_time does not silently become a zero duration.'),

  -- ---- gen_ai semantic convention: identity ----
  gen_ai_operation_name   STRING             OPTIONS (description = 'gen_ai.operation.name. Bounded enum: invoke_agent | chat | execute_tool | embeddings. THE span-type discriminator — token attributes are only meaningful on "chat".'),
  gen_ai_system           STRING             OPTIONS (description = 'gen_ai.system, e.g. "github.copilot". Bounded.'),
  gen_ai_agent_name       STRING             OPTIONS (description = 'gen_ai.agent.name as reported by the runtime. Compare against raw.ai_run_event.agent_name to detect drift between the declared agent and the one actually invoked.'),
  conversation_id         STRING             OPTIONS (description = 'gen_ai.conversation.id. ⭐ CLUSTER KEY (CONTRACT §7) and THE ONLY join key back to the correlation stream, via the run.bound event. Everything in 04_transform_run.sql hangs off this column.'),

  -- ---- gen_ai semantic convention: model ----
  gen_ai_request_model    STRING             OPTIONS (description = 'gen_ai.request.model — the model the client ASKED for.'),
  gen_ai_response_model   STRING             OPTIONS (description = 'gen_ai.response.model — the model that actually ANSWERED. Prefer this for pricing: a request may be silently routed to a different model, and the bill follows the responder.'),

  -- ---- gen_ai semantic convention: usage (design §4.4a) ----
  input_tokens            INT64              OPTIONS (description = 'gen_ai.usage.input_tokens. Includes the cached portion — cached_input_tokens is a SUBSET of this, not an addition.'),
  output_tokens           INT64              OPTIONS (description = 'gen_ai.usage.output_tokens.'),
  cached_input_tokens     INT64              OPTIONS (description = 'Cached-input tokens. Subset of input_tokens, billed at the cheaper cached rate (CONTRACT §4). NULL if the model/runtime does not report caching.'),
  reasoning_tokens        INT64              OPTIONS (description = 'Reasoning tokens. Subset of output_tokens on models that report them. Reported for visibility; NOT added into total (see the header note).'),
  -- NOTE: there is deliberately NO total_tokens column. Copilot does not emit
  -- gen_ai.usage.total_tokens. Use raw.v_otel_span_tokens.total_tokens instead.

  -- ---- gen_ai semantic convention: tools ----
  gen_ai_tool_name        STRING             OPTIONS (description = 'gen_ai.tool.name. Populated only on execute_tool spans. Potentially unbounded across MCP servers — DQ-15 guards its use as a metric dimension.'),
  gen_ai_tool_type        STRING             OPTIONS (description = 'gen_ai.tool.type where emitted. Bounded: mcp | terminal | file | http | other.'),

  -- ---- outcome ----
  status_code             STRING             OPTIONS (description = 'OTel span status: OK | ERROR | UNSET.'),
  error_class             STRING             OPTIONS (description = 'Bounded exception-type enum only. NEVER an error message body or stack trace (CONTRACT §1.1, design §11.3).'),
  finish_reason           STRING             OPTIONS (description = 'gen_ai.response.finish_reasons, first element. Bounded: stop | length | tool_calls | content_filter | error.'),
  retry_count             INT64              OPTIONS (description = 'Retries observed for this call, where the runtime reports them.'),

  -- ---- resource / provenance ----
  service_name            STRING             OPTIONS (description = 'OTel service.name, e.g. "github.copilot.chat" or "copilot-cli". Distinguishes the surface.'),
  service_version         STRING             OPTIONS (description = 'Client version. Matters: gen_ai.usage.* population varies by client version (design §4.4a [A]).'),
  resource_attributes     JSON               OPTIONS (description = 'Full OTel resource attributes as exported. Retained for forensics when a semantic-convention field moves between versions.'),
  attributes              JSON               OPTIONS (description = 'Full span attributes as exported, including any gen_ai.* key not promoted to a typed column above. Content capture is OFF by design (§4.4a) so this holds no prompts or tool arguments.'),

  ingested_at             TIMESTAMP          OPTIONS (description = 'Set by the OTel collector on receipt.')
)
PARTITION BY DATE(start_time)
CLUSTER BY conversation_id
OPTIONS (
  partition_expiration_days = 90,
  description = '⚠ LEGACY / FROZEN as of contract 1.1.0 — nothing writes this table any more; the source is now Copilot CLI session journals landing in raw.ai_run_event. Retained, not dropped: rows inside the 90-day window are the only way to price a pre-cutover run. OTEL STREAM, one row per OpenTelemetry GenAI span (design §4.4a). Partition DATE(start_time), cluster conversation_id (CONTRACT §7). NOTE: no total_tokens column — Copilot does not emit gen_ai.usage.total_tokens; use raw.v_otel_span_tokens.',
  labels = [('layer', 'raw'), ('domain', 'ai-telemetry'), ('pii', 'none'), ('lifecycle', 'legacy-frozen')]
);


-- -------------------------------------------------------------------------------------
-- raw.v_otel_span_tokens — CANONICAL token derivation  ⚠ LEGACY (contract 1.1.0)
-- -------------------------------------------------------------------------------------
-- Reads the frozen raw.otel_span. Its only remaining consumer is the pre-cutover
-- branch of 04_transform_run.sql. New usage arrives as `model.call` events on
-- raw.ai_run_event at (session × model) grain — see CONTRACT §3 and §4.
--
-- The single place total_tokens is computed FOR THE SPAN SOURCE. Every downstream
-- consumer must read this view rather than re-deriving, so the "Copilot emits no
-- total_tokens" fact is encoded exactly once.
--
-- ⚠ `finish_reason` and `retry_count` were REMOVED from this view in 1.1.0. Neither is
-- recorded by the journal, so post-cutover both are permanently NULL — and a nullable
-- column that is always NULL is an invitation to SUM() it and publish a zero. That is
-- not hypothetical: `retry_rate_pct` in 06_marts.sql and 08_metrics.sql read exactly
-- 0.0% for as long as the column existed and was COALESCE'd to 0. They are still on
-- raw.otel_span for forensics; they are no longer on the path anything aggregates.
--
-- Rules encoded here:
--   * total = input + output. Cached-input is a subset of input; reasoning is a subset
--     of output. Neither is added.
--   * A span with BOTH input and output NULL yields total_tokens = NULL, not 0.
--     A run with unknown token usage must not look free (same principle as CONTRACT §4
--     on unpriced models).
--   * Only gen_ai.operation.name = 'chat' spans carry usage. Others are passed through
--     with NULL usage so the view can still be used as a general span reader.
-- -------------------------------------------------------------------------------------
CREATE OR REPLACE VIEW `${PROJECT_ID}.raw.v_otel_span_tokens`
OPTIONS (
  description = '⚠ LEGACY (1.1.0): reads the frozen raw.otel_span and serves only the pre-cutover branch of 04_transform_run.sql. Canonical token derivation for the span source. total_tokens = input + output (Copilot emits no gen_ai.usage.total_tokens, design §4.4a). NULL in, NULL out — never 0. finish_reason and retry_count are NOT projected: the journal never records them and an always-NULL column gets summed to a false zero.'
)
AS
SELECT
  s.span_id,
  s.trace_id,
  s.parent_span_id,
  s.start_time,
  s.end_time,
  s.duration_ms,
  s.conversation_id,
  s.gen_ai_operation_name,
  s.gen_ai_agent_name,
  s.gen_ai_request_model,
  s.gen_ai_response_model,
  -- Pricing follows the model that actually answered; fall back to the requested model
  -- when the runtime does not report a response model.
  COALESCE(s.gen_ai_response_model, s.gen_ai_request_model) AS effective_model_id,
  s.input_tokens,
  s.output_tokens,
  s.cached_input_tokens,
  s.reasoning_tokens,
  -- Derived total. NULL-preserving: if neither side was reported we do not know the
  -- usage, and pretending it is zero would understate cost.
  CASE
    WHEN s.input_tokens IS NULL AND s.output_tokens IS NULL THEN NULL
    ELSE COALESCE(s.input_tokens, 0) + COALESCE(s.output_tokens, 0)
  END AS total_tokens,
  s.gen_ai_tool_name,
  s.gen_ai_tool_type,
  s.status_code,
  s.error_class,
  -- finish_reason and retry_count deliberately NOT projected — see the header note.
  s.service_name,
  s.service_version,
  s.ingested_at
FROM `${PROJECT_ID}.raw.otel_span` AS s;


-- -------------------------------------------------------------------------------------
-- MIGRATION — for a project where raw.ai_run_event ALREADY EXISTS
-- -------------------------------------------------------------------------------------
-- The DDL above is CREATE TABLE IF NOT EXISTS, so it is a no-op against an existing
-- table and the 1.1.0 nullability relaxation would NOT be applied. Run this once, by
-- hand, before the first 1.1.0 bundle arrives — otherwise every journal `model.call`
-- and every trailer-less commit is rejected at load:
--
--   ALTER TABLE `${PROJECT_ID}.raw.ai_run_event` ALTER COLUMN run_id   DROP NOT NULL;
--   ALTER TABLE `${PROJECT_ID}.raw.ai_run_event` ALTER COLUMN trace_id DROP NOT NULL;
--
-- Dropping a NOT NULL constraint is a metadata-only change in BigQuery: no rewrite, no
-- backfill, and it cannot fail on existing data. The reverse is not true, which is
-- why the constraint is not re-tightened anywhere.
-- -------------------------------------------------------------------------------------
