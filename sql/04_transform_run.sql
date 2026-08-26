-- =====================================================================================
-- 04_transform_run.sql — raw -> core.fct_ai_run
-- =====================================================================================
-- Contract:  schema/CONTRACT.md §2 (envelope), §3 (event types),
--            §4 (cost derivation), §6 (attribution AR-3, AR-4), §7 (tables)
-- Design:    docs/spikes/ai-effectiveness-observability.md §4.4a, §5.3, §6.5, §8.1,
--            §8.4, §8.11, §9.3
--
-- Cadence: hourly. Idempotent — safe to re-run over the same window any number of
-- times. Reprocessing restates history; §9.5 requires restatement to be VISIBLE, which
-- is what transformed_at is for.
--
-- ┌───────────────────────────────────────────────────────────────────────────────────┐
-- │  READ THIS BEFORE CHANGING ANYTHING BELOW.                                        │
-- │                                                                                   │
-- │  THE CORRELATION PROBLEM (design §4.4a, §6.5) — AND HOW 1.1.0 CHANGED IT          │
-- │                                                                                   │
-- │  BEFORE THE CUTOVER there were two streams, neither sufficient:                   │
-- │                                                                                   │
-- │    raw.ai_run_event   knew  run_id, jira_issue_key, person_id, agent_name,        │
-- │    (emit.py + hooks)        outputs, gates, PR ids, commit SHAs                   │
-- │                      lacked TOKENS. The agent never saw them.                     │
-- │                                                                                   │
-- │    raw.otel_span      knew  tokens, model, latency, tool calls,                   │
-- │    (Copilot OTLP)           gen_ai.conversation.id                                │
-- │                      lacked jira key, person, PR, commit                          │
-- │                                                                                   │
-- │  AFTER THE CUTOVER there is one stream. cli/copilot_read.py reads Copilot CLI's   │
-- │  own session journal and emits `model.call`, `tool.call`, `gate.evaluated`,       │
-- │  `output.generated` and `human.turn` onto raw.ai_run_event like any other         │
-- │  source. raw.otel_span is frozen (see 01_raw.sql) and read ONLY by the legacy     │
-- │  branch below, gated on `started_at < cutover_date`.                              │
-- │                                                                                   │
-- │  THE BRIDGE is still one event type: `run.bound` (CONTRACT §3, event #2), now     │
-- │  carrying `copilot_session_id`. The old attribute name is still read — see the    │
-- │  COALESCE in `run_bound` and the note there.                                      │
-- │                                                                                   │
-- │  ⚠ WHY A NAIVE JOIN ON THE SESSION ID IS WRONG — STILL TRUE                       │
-- │                                                                                   │
-- │  A Copilot session is not a run. One session routinely hosts several:             │
-- │     - the engineer invokes an agent, it finishes, they invoke another;            │
-- │     - `/resume` reopens it, which is a second invocation, not a continuation      │
-- │       (measured 2026-08-26: 38 resumes against 22 starts);                        │
-- │     - a supervisor invokes nine sub-agents, ALL inside one session.               │
-- │                                                                                   │
-- │  So a plain equijoin on session id is many-to-many and would multiply every       │
-- │  token in the session by the number of runs in it, silently.                      │
-- │                                                                                   │
-- │  ⭐⭐ AND THE FIX THAT WORKED BEFORE DOES NOT WORK FOR `model.call` ANY MORE.      │
-- │                                                                                   │
-- │  The span source stamped every API call with its own timestamp, so binding on     │
-- │  (session id AND time window) placed each call on the run that made it. The       │
-- │  journal does not: it totals usage per session in `session.shutdown`, so one      │
-- │  `model.call` now carries the WHOLE SESSION's tokens for one model, stamped at    │
-- │  shutdown. Applying the window join to it would charge every token in a           │
-- │  multi-run session to whichever run happened to be open when the session ended.   │
-- │                                                                                   │
-- │  CONTRACT §3 settles it: a session that hosted more than one run reports          │
-- │  `cost_usd = NULL` for its constituent runs. Apportioning a measured total by     │
-- │  time or by call count is §2.4's forbidden synthesised join key with arithmetic   │
-- │  on top. `session_run_count` and `cost_attributable` below carry that decision    │
-- │  into the data; the session-grain totals, which ARE valid, live in                │
-- │  marts.v_session_usage.                                                           │
-- │                                                                                   │
-- │  The window join SURVIVES for tool.call / gate.evaluated / output.generated /     │
-- │  human.turn — one journal record each, each with its own timestamp — but          │
-- │  cli/copilot_read.py already stamps `run_id` on those, so the bind happens on     │
-- │  the client and this file simply groups by run_id. The window machinery below is  │
-- │  now LEGACY, serving pre-cutover spans only.                                      │
-- │                                                                                   │
-- │  THE PRE-CUTOVER FIX, in the `span_binding` CTE: bind on conversation id AND      │
-- │  time, then force one-span-to-one-run with QUALIFY ROW_NUMBER() = 1 partitioned   │
-- │  by span_id. A span goes to the MOST RECENTLY STARTED run whose window contains   │
-- │  it, so sequential runs each get their own tokens and a supervisor's calls land   │
-- │  on the SUB-AGENT. AR-4 rolls the supervisor up by trace_id, never by             │
-- │  re-counting. The partition-by-span_id guarantee is the load-bearing part: it     │
-- │  makes double-counting structurally impossible rather than merely unlikely.       │
-- └───────────────────────────────────────────────────────────────────────────────────┘
--
-- Substitute ${PROJECT_ID}. Run after 01_raw.sql, 02_dims.sql, 03_core_fct.sql.
-- =====================================================================================

-- ⭐ THE GRAIN CUTOVER. On and after this date usage is read from Copilot CLI session
-- journals at (session × model) grain; before it, from OTel spans at per-call grain.
-- The two branches below are gated on it so NO ROW IS EVER PROCESSED BY BOTH — a run
-- that took usage from both sources would carry a number that is neither measurement.
--
-- This has to be a date AND a pair of columns, not a date alone. This file rebuilds a
-- trailing window and 06_marts.sql rebuilds 45 days, so a date on its own SMEARS the
-- boundary across every rebuild instead of marking it: a reader looking at a chart
-- cannot tell which rows were measured which way. `usage_grain` and `usage_source` on
-- every fact row, plus marts.dim_grain_cutover for dashboards to join, is what makes
-- the boundary legible. Retention makes this long-lived, not a fortnight's problem:
-- fct_* keeps 396 days and marts.agg_daily_person_agent keeps 1130, so three years of
-- executive trend cross this line.
DECLARE cutover_date DATE DEFAULT DATE '2026-08-26';

-- Reprocessing window. WIDENED from 8 to 21 days in 1.1.0.
--
-- 8 days was right for the span source: DQ-13 accepts events up to 7 days late, so
-- anything shorter would build a run row from a partial event set and never revisit
-- it. It is NOT right for the journal. A session's `model.call` is written at
-- `session.shutdown`, which can be days after the `run.started` that opened it — a
-- session left open over a weekend, or one the engineer resumes across a fortnight.
-- At 8 days the usage arrived after its run had left the window and was merged onto
-- nothing at all.
--
-- 21 = 7 (DQ-13 late arrival) + 14 (session lifetime allowance). The 14 is a POLICY
-- CHOICE, not a measurement — nothing here has observed the true distribution of
-- session lifetimes. DQ-SESSION in 07_dq_checks.sql reports any session whose usage
-- landed outside the window, so the choice is falsifiable rather than assumed.
DECLARE window_days INT64 DEFAULT 21;

-- Grace applied around a run's active window when binding OTel spans. Client clocks
-- drift (DQ-10 measures it) and the exporter flushes asynchronously, so a span may be
-- stamped slightly outside the run it genuinely belongs to.
DECLARE bind_grace_seconds INT64 DEFAULT 300;

-- Cap on how long a run with no terminal event is considered "active" for binding.
-- Matches the DQ-2 orphan-run window: after 24h the run is abandoned and must stop
-- absorbing tokens from whatever the engineer does next in the same chat session.
DECLARE max_open_run_hours INT64 DEFAULT 24;


MERGE `${PROJECT_ID}.core.fct_ai_run` AS tgt
USING (

  -- ===================================================================================
  -- STEP 1 — deduplicate the correlation stream
  -- ===================================================================================
  -- CONTRACT §1.3: event_id is the dedup key and re-delivery must be safe. The
  -- collector is at-least-once, so duplicates are expected, not exceptional.
  -- Keeping the EARLIEST ingested_at makes the transform deterministic across re-runs.
  WITH dedup_event AS (
    SELECT *
    FROM `${PROJECT_ID}.raw.ai_run_event`
    WHERE DATE(event_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL window_days DAY)
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY event_id
      ORDER BY COALESCE(ingested_at, event_time) ASC
    ) = 1
  ),

  -- ===================================================================================
  -- STEP 2 — the runs in scope, and their identity/context/agent dimensions
  -- ===================================================================================
  -- run.started is authoritative for everything in the envelope. Later events repeat
  -- the envelope, but a mid-run context change (e.g. the branch is renamed) must not
  -- retroactively rewrite the run's dimensions.
  run_started AS (
    SELECT
      run_id,
      ANY_VALUE(trace_id)          AS trace_id,
      ANY_VALUE(parent_run_id)     AS parent_run_id,
      ANY_VALUE(workflow_id)       AS workflow_id,
      MIN(event_time)              AS started_at,
      ANY_VALUE(person_id)         AS person_id,
      ANY_VALUE(person_email_hash) AS person_email_hash,
      ANY_VALUE(team_id)           AS team_id,
      ANY_VALUE(role)              AS role,
      ANY_VALUE(jira_issue_key)    AS jira_issue_key,
      ANY_VALUE(jira_project_key)  AS jira_project_key,
      ANY_VALUE(repo_full_name)    AS repo_full_name,
      ANY_VALUE(branch_name)       AS branch_name,
      ANY_VALUE(product_profile)   AS product_profile,
      ANY_VALUE(environment)       AS environment,
      ANY_VALUE(agent_name)        AS agent_name,
      ANY_VALUE(agent_version)     AS agent_version,
      ANY_VALUE(skill_name)        AS skill_name,
      ANY_VALUE(skill_version)     AS skill_version,
      ANY_VALUE(surface)           AS surface,
      ANY_VALUE(link_method)       AS link_method,
      ANY_VALUE(link_confidence)   AS link_confidence,
      ANY_VALUE(schema_version)    AS schema_version,
      ANY_VALUE(JSON_VALUE(attributes, '$.invocation_mode'))   AS invocation_mode,
      ANY_VALUE(JSON_VALUE(attributes, '$.model_declared_id')) AS model_declared_id,
      ANY_VALUE(JSON_VALUE(attributes, '$.input_source'))      AS input_source
    FROM dedup_event
    WHERE event_type = 'run.started'
    GROUP BY run_id
  ),

  -- ===================================================================================
  -- STEP 3 — terminal event (CONTRACT §3 events 10-13)
  -- ===================================================================================
  -- Exactly one terminal event is expected per run. If several arrive (a retry that
  -- re-emitted, or the DQ job marking abandoned a run that later completed), the
  -- EARLIEST wins: the run really did end when it first said it did, and a later
  -- run.abandoned is the DQ job being wrong, not the run resurrecting.
  run_terminal AS (
    SELECT
      run_id,
      event_type   AS terminal_event_type,
      event_time   AS ended_at,
      SAFE_CAST(JSON_VALUE(attributes, '$.duration_ms') AS INT64)      AS duration_ms,
      SAFE_CAST(JSON_VALUE(attributes, '$.phases_completed') AS INT64) AS phases_completed,
      JSON_VALUE(attributes, '$.failure_class')                        AS failure_class,
      JSON_VALUE(attributes, '$.dependency_failed')                    AS dependency_failed,
      JSON_VALUE(attributes, '$.timeout_policy')                       AS timeout_policy
    FROM dedup_event
    WHERE event_type IN ('run.completed', 'run.failed', 'run.timeout', 'run.abandoned')
    QUALIFY ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY event_time ASC) = 1
  ),

  -- ===================================================================================
  -- STEP 4 — ⭐ THE BRIDGE: run.bound
  -- ===================================================================================
  -- CONTRACT §3 event #2. One row per run carrying gen_ai.conversation.id.
  --
  -- AR-3 is applied here as well: run.bound carries the jira_issue_key that emit.py
  -- resolved. Where the QualDev delivery ticket (qd_jira_key) is present it is kept
  -- SEPARATELY as delivery_ticket_key and metrics attribute to the FEATURE ticket.
  -- Without this, supervisor-test-spec runs — which comment on both tickets — would
  -- be counted twice.
  --
  -- FIRST bind wins. A session id is captured once at run start; a second run.bound
  -- for the same run means the emitter re-bound after a client restart, and
  -- re-pointing an already-priced run at a different session would silently restate
  -- its cost.
  --
  -- ⚠ COALESCE(new, old) IS LOAD-BEARING. CONTRACT §3 row 2 renamed this attribute
  -- from `otel_conversation_id` to `copilot_session_id` in 1.1.0, and the collector
  -- deliberately still accepts the old name so a client that has not upgraded keeps
  -- ingesting. Reading only the new name here would bind NOTHING from those clients;
  -- reading only the old name — which is what this CTE did until 2026-08-26,
  -- including in the `IS NOT NULL` filter — meant NO 1.1.0 run.bound event bound at
  -- all, because the filter dropped every row before the SELECT could look at it.
  -- Found by diffing this file against collector/main.py's ATTRIBUTE_ALLOWLIST.
  run_bound AS (
    SELECT
      run_id,
      COALESCE(JSON_VALUE(attributes, '$.copilot_session_id'),
               JSON_VALUE(attributes, '$.otel_conversation_id')) AS copilot_session_id,
      JSON_VALUE(attributes, '$.jira_issue_key')       AS bound_jira_issue_key,
      JSON_VALUE(attributes, '$.qd_jira_key')          AS delivery_ticket_key,
      event_time                                       AS bound_at
    FROM dedup_event
    WHERE event_type = 'run.bound'
      AND COALESCE(JSON_VALUE(attributes, '$.copilot_session_id'),
                   JSON_VALUE(attributes, '$.otel_conversation_id')) IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY event_time ASC) = 1
  ),

  -- ===================================================================================
  -- STEP 4b — every run's session id, and how many runs share it
  -- ===================================================================================
  -- Two producers, one column. emit.py binds explicitly through run.bound; the journal
  -- reader writes the session id into `trace_id`, because a Copilot session IS the
  -- "one user-initiated workflow" that trace_id means (see the namespace note in
  -- 01_raw.sql). The two id namespaces are disjoint — `trc_` prefix vs a bare session
  -- directory name — so this discrimination is exact, not a heuristic.
  run_session AS (
    SELECT
      s.run_id,
      COALESCE(
        b.copilot_session_id,
        IF(s.trace_id IS NOT NULL AND NOT STARTS_WITH(s.trace_id, 'trc_'),
           s.trace_id, NULL)
      ) AS copilot_session_id,
      CASE
        WHEN s.trace_id IS NULL                     THEN 'unknown'
        WHEN STARTS_WITH(s.trace_id, 'trc_')        THEN 'emitter_trace'
        ELSE 'copilot_session'
      END AS trace_id_namespace
    FROM run_started AS s
    LEFT JOIN run_bound AS b USING (run_id)
  ),

  -- ⭐ THE NUMBER THAT DECIDES WHETHER SESSION-GRAIN USAGE MAY BE ATTRIBUTED AT ALL.
  -- CONTRACT §3: a session that hosted more than one run reports cost_usd = NULL for
  -- its constituent runs. On the reference machine most sessions are multi-run (38
  -- resumes across 22 sessions, plus 13 sub-agents), so this is the common case, not
  -- the exception, and run-level cost coverage falls sharply because of it.
  --
  -- ⚠ COUNTED WITHIN THE REBUILD WINDOW ONLY. A session whose first run started
  -- before the window and whose shutdown lands inside it will under-count its runs
  -- and could wrongly look attributable. That is what widening window_days to 21
  -- buys, and DQ-SESSION reports the residual rather than leaving it to trust.
  session_counts AS (
    SELECT
      copilot_session_id,
      COUNT(DISTINCT run_id) AS session_run_count
    FROM run_session
    WHERE copilot_session_id IS NOT NULL
    GROUP BY copilot_session_id
  ),

  -- ===================================================================================
  -- STEP 5 — each bound run's ACTIVE WINDOW    ⚠ LEGACY (pre-cutover spans only)
  -- ===================================================================================
  -- The interval during which a span in this conversation may be attributed to this
  -- run. Open runs are capped at max_open_run_hours so an abandoned run cannot keep
  -- absorbing tokens from every later run in the same chat session.
  --
  -- ⭐ THE HARD CUTOVER GUARD. `started_at < cutover_ts` is what makes it structurally
  -- impossible for one run to take usage from both sources. Without it a run straddling
  -- the switch could pick up spans AND a journal session total, and the resulting
  -- number would be neither measurement — the failure mode being guarded against is
  -- not double-counting so much as UNFALSIFIABILITY: nothing downstream could tell
  -- what the number was. DQ-GRAIN checks the invariant from the other side.
  run_window AS (
    SELECT
      s.run_id,
      s.trace_id,
      rs.copilot_session_id,
      s.started_at,
      TIMESTAMP_SUB(s.started_at, INTERVAL bind_grace_seconds SECOND) AS bind_from,
      TIMESTAMP_ADD(
        COALESCE(
          t.ended_at,
          TIMESTAMP_ADD(s.started_at, INTERVAL max_open_run_hours HOUR)
        ),
        INTERVAL bind_grace_seconds SECOND
      ) AS bind_to
    FROM run_started AS s
    JOIN run_session AS rs USING (run_id)
    LEFT JOIN run_terminal AS t USING (run_id)
    -- INNER on a resolved session id: an unbound run has no token data.
    WHERE rs.copilot_session_id IS NOT NULL
      AND s.started_at < TIMESTAMP(cutover_date)   -- ⭐ legacy branch only
  ),

  -- ===================================================================================
  -- STEP 6 — ⭐⭐ THE BIND ITSELF: OTel span -> run
  -- ===================================================================================
  -- This is the single most important join in the warehouse.
  --
  --   JOIN CONDITION   conversation id equality AND the span starts inside the run's
  --                    active window.
  --   DISAMBIGUATION   QUALIFY ROW_NUMBER() OVER (PARTITION BY span_id ...) = 1.
  --                    PARTITION BY span_id — not by run — is what makes it
  --                    IMPOSSIBLE for one span's tokens to be counted by two runs.
  --   TIE-BREAK        latest started_at first. Where a supervisor and a sub-agent
  --                    windows overlap, the tokens go to the sub-agent — the run that
  --                    actually issued the call. AR-4 then rolls the supervisor up by
  --                    trace_id instead of re-counting.
  --                    Secondary tie-break on run_id keeps the choice deterministic
  --                    across re-runs when two runs start in the same microsecond.
  --
  -- The span partition filter is widened by one day on each side: a span can be
  -- exported shortly after midnight for a run that started before it, and vice versa.
  --
  -- ⚠ LEGACY. raw.otel_span is frozen; this CTE returns nothing for any run started on
  -- or after cutover_date, because run_window is empty for those runs.
  span_binding AS (
    SELECT
      w.run_id,
      sp.span_id,
      sp.trace_id            AS otel_trace_id,
      sp.gen_ai_operation_name,
      sp.effective_model_id,
      sp.input_tokens,
      sp.output_tokens,
      sp.cached_input_tokens,
      sp.reasoning_tokens,
      sp.total_tokens,
      sp.status_code,
      -- sp.retry_count is deliberately NOT read. The metric it fed is retired — see
      -- the note on retry in STEP 8 — and the column no longer exists on
      -- raw.v_otel_span_tokens.
      sp.gen_ai_agent_name,
      sp.start_time
    FROM `${PROJECT_ID}.raw.v_otel_span_tokens` AS sp
    JOIN run_window AS w
      ON  sp.conversation_id = w.copilot_session_id
      AND sp.start_time     >= w.bind_from
      AND sp.start_time     <= w.bind_to
    WHERE DATE(sp.start_time)
            BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL (window_days + 1) DAY)
                AND DATE_ADD(CURRENT_DATE(), INTERVAL 1 DAY)
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY sp.span_id
      ORDER BY w.started_at DESC, w.run_id ASC
    ) = 1
  ),

  -- ===================================================================================
  -- STEP 7 — price each model call individually (CONTRACT §4)  ⚠ LEGACY branch
  -- ===================================================================================
  -- Pre-cutover only. The 1.1.0 pricing of journal usage is STEP 7b, at (session ×
  -- model) grain, and CONTRACT §4 accepts that looser grain explicitly: "inventing a
  -- finer one would be a guess with a decimal point on it".
  --
  -- Pricing is per CALL, not per run, for two reasons:
  --   (a) CONTRACT §4 states the formula at call grain;
  --   (b) a single run can legitimately span two models (fallback routing) or straddle
  --       a price change at midnight. Pricing the run as a whole on one rate would be
  --       wrong in both cases.
  --
  -- ⭐ THE EFFECTIVE-DATED JOIN. Exactly as CONTRACT §4 specifies:
  --       ON  p.model_id = <model of the call>
  --       AND DATE(call time) BETWEEN p.effective_from
  --                               AND COALESCE(p.effective_to, DATE '9999-12-31')
  --   A LEFT JOIN, deliberately: an unpriced model must produce a row with NULL cost
  --   plus a DQ-6 finding, NOT vanish from the fact table.
  --
  -- Cached-input tokens are a SUBSET of input_tokens billed at a cheaper rate, so the
  -- input term is charged on (input - cached) at the full rate and the cached
  -- remainder at the cached rate. Charging the full input_tokens AND the cached tokens
  -- again would bill the cached portion twice.
  priced_call AS (
    SELECT
      b.run_id,
      b.span_id,
      b.effective_model_id,
      b.input_tokens,
      b.output_tokens,
      b.cached_input_tokens,
      b.reasoning_tokens,
      b.total_tokens,
      p.effective_from AS pricing_effective_from,
      p.is_placeholder AS pricing_is_placeholder,

      -- Is this call priceable at all? Both base rates must exist; the cached rate is
      -- only required when there are cached tokens to charge.
      (   p.input_per_1k_usd  IS NOT NULL
      AND p.output_per_1k_usd IS NOT NULL
      AND (COALESCE(b.cached_input_tokens, 0) = 0 OR p.cached_input_per_1k_usd IS NOT NULL)
      ) AS is_priceable,

      CASE
        WHEN p.input_per_1k_usd IS NULL OR p.output_per_1k_usd IS NULL THEN NULL
        WHEN COALESCE(b.cached_input_tokens, 0) > 0
             AND p.cached_input_per_1k_usd IS NULL THEN NULL
        -- CONTRACT §4 cost formula.
        ELSE
            ( SAFE_CAST(GREATEST(COALESCE(b.input_tokens, 0)
                                 - COALESCE(b.cached_input_tokens, 0), 0) AS NUMERIC)
              / 1000 * p.input_per_1k_usd )
          + ( SAFE_CAST(COALESCE(b.output_tokens, 0) AS NUMERIC)
              / 1000 * p.output_per_1k_usd )
          + ( SAFE_CAST(COALESCE(b.cached_input_tokens, 0) AS NUMERIC)
              / 1000 * COALESCE(p.cached_input_per_1k_usd, 0) )
      END AS call_cost_usd
    FROM span_binding AS b
    LEFT JOIN `${PROJECT_ID}.core.dim_model_pricing` AS p
      ON  p.model_id = b.effective_model_id
      AND DATE(b.start_time) BETWEEN p.effective_from
                                 AND COALESCE(p.effective_to, DATE '9999-12-31')
    WHERE b.gen_ai_operation_name = 'chat'   -- only LLM calls carry usage and cost
  ),

  -- ===================================================================================
  -- STEP 8 — roll the OTel side up to run grain   ⚠ LEGACY (pre-cutover spans only)
  -- ===================================================================================
  -- ⚠ `retry_count` USED TO BE AGGREGATED HERE as SUM(COALESCE(retry_count, 0)), and
  -- that COALESCE is why §7.8 retry_rate_pct read exactly 0.0% and was published as a
  -- measurement. The span runtime rarely reported retries and the journal does not
  -- report them at all, so the column was overwhelmingly NULL — and COALESCE turned
  -- "nobody counted" into "there were none". The metric is retired rather than kept
  -- alive on a frozen source: only the emitter-estimate path (STEP 9) still carries a
  -- retry figure, and every consumer now divides by a known-retry denominator so an
  -- unmeasured rate renders NULL. Removed 2026-08-26.
  otel_rollup AS (
    SELECT
      run_id,
      ANY_VALUE(otel_trace_id)                                                   AS otel_trace_id,
      COUNT(*)                                                                   AS otel_span_count,
      COUNTIF(gen_ai_operation_name = 'chat')                                    AS model_call_count,
      COUNTIF(gen_ai_operation_name = 'execute_tool')                            AS tool_call_count,
      COUNTIF(gen_ai_operation_name = 'execute_tool' AND status_code = 'ERROR')  AS tool_error_count,

      -- NULL-preserving sums: a run whose usage was never reported must show NULL,
      -- not 0. SUM() over all-NULL input already returns NULL, which is what we want;
      -- COALESCE here would silently manufacture a free run.
      SUM(IF(gen_ai_operation_name = 'chat', input_tokens,        NULL)) AS input_tokens,
      SUM(IF(gen_ai_operation_name = 'chat', output_tokens,       NULL)) AS output_tokens,
      SUM(IF(gen_ai_operation_name = 'chat', cached_input_tokens, NULL)) AS cached_input_tokens,
      SUM(IF(gen_ai_operation_name = 'chat', reasoning_tokens,    NULL)) AS reasoning_tokens,
      SUM(IF(gen_ai_operation_name = 'chat', total_tokens,        NULL)) AS total_tokens,

      -- The model this run is reported under: the one that answered the most calls.
      -- APPROX_TOP_COUNT over the chat spans only. Approximate, and acceptable here
      -- for one reason only: there are many spans per run, so it really is a mode.
      -- It is NOT acceptable over the journal source, where there is exactly one row
      -- per model and "top 1 of N singletons" is an arbitrary pick wearing the name
      -- of a statistic — STEP 8b orders by tokens instead.
      (SELECT value
       FROM UNNEST(APPROX_TOP_COUNT(
              IF(gen_ai_operation_name = 'chat', effective_model_id, NULL), 1))
      )                                                                          AS model_id,
      COUNT(DISTINCT IF(gen_ai_operation_name = 'chat', effective_model_id, NULL)) AS distinct_model_count,
      ANY_VALUE(gen_ai_agent_name)                                               AS otel_agent_name
    FROM span_binding
    GROUP BY run_id
  ),

  cost_rollup AS (
    SELECT
      run_id,
      -- ⚠ CONTRACT §4: an unpriced model yields NULL, NEVER 0.
      -- If ANY call in the run could not be priced, the RUN's cost is unknown. Summing
      -- only the priceable calls would produce a number that looks complete and is
      -- systematically too low — the single most dangerous failure mode in a cost
      -- system, because nothing about the output signals that it is partial.
      IF(LOGICAL_AND(is_priceable), SUM(call_cost_usd), NULL) AS cost_usd,
      LOGICAL_OR(NOT is_priceable)                            AS has_unpriced_call,
      LOGICAL_OR(COALESCE(pricing_is_placeholder, FALSE))     AS cost_is_placeholder,
      MIN(pricing_effective_from)                             AS pricing_effective_from
    FROM priced_call
    GROUP BY run_id
  ),

  -- ===================================================================================
  -- STEP 7b — ⭐⭐ THE JOURNAL USAGE PATH (contract 1.1.0)
  -- ===================================================================================
  -- One `model.call` per (session, model), carrying the WHOLE session's usage for that
  -- model and stamped at `session.shutdown`. It has no run_id, deliberately and by
  -- contract: CONTRACT §2.4 forbids synthesising one, and cli/copilot_read.py records
  -- in its own comment that stamping the open run "would charge one agent for what the
  -- others spent".
  --
  -- ⚠ `e.run_id IS NULL` IS THE DISCRIMINATOR, and it is why STEP 9 below now filters
  -- on `run_id IS NOT NULL`. Both CTEs read `model.call`; without the split, STEP 9's
  -- `GROUP BY run_id` collapsed every journal event into a single NULL-keyed row that
  -- then joined to nothing, so the entire usage side of the warehouse was empty and
  -- nothing said why.
  journal_call AS (
    SELECT
      e.trace_id                                                            AS copilot_session_id,
      e.event_time                                                          AS shutdown_at,
      JSON_VALUE(e.attributes, '$.model_id')                                AS model_id,
      SAFE_CAST(JSON_VALUE(e.attributes, '$.input_tokens')        AS INT64) AS input_tokens,
      SAFE_CAST(JSON_VALUE(e.attributes, '$.output_tokens')       AS INT64) AS output_tokens,
      SAFE_CAST(JSON_VALUE(e.attributes, '$.cached_input_tokens') AS INT64) AS cached_input_tokens,
      SAFE_CAST(JSON_VALUE(e.attributes, '$.cache_write_tokens')  AS INT64) AS cache_write_tokens,
      SAFE_CAST(JSON_VALUE(e.attributes, '$.reasoning_tokens')    AS INT64) AS reasoning_tokens,
      SAFE_CAST(JSON_VALUE(e.attributes, '$.request_count')       AS INT64) AS request_count,
      -- ⚠ NUMERIC, NOT INT64. `requests.cost` is fractional per model tier — a
      -- premium request can cost 0.33 — and an INT64 cast truncates that to 0,
      -- standing a measured zero in for real billed usage. CONTRACT §4.1 makes this
      -- the BILLING unit, so a truncated one is worse than an absent one.
      SAFE_CAST(JSON_VALUE(e.attributes, '$.premium_requests')  AS NUMERIC) AS premium_requests,
      SAFE_CAST(JSON_VALUE(e.attributes, '$.nano_aiu')            AS INT64) AS nano_aiu
      -- ⚠ DELIBERATELY NOT CARRIED YET: `tool_definitions_tokens`, `system_tokens`
      -- and `conversation_tokens`, added to CONTRACT §3 row 5 on 2026-08-26. They are
      -- a LEVEL, not a total — what the model was carrying at the end of the session
      -- and paid for on every request in it — so SUM()ing them across runs or days,
      -- which is what every rollup in this warehouse does to a token column, would
      -- produce a number with no referent. Carrying them needs a column type that a
      -- mart cannot casually add up; adding them to the existing token block would
      -- guarantee somebody did. Tracked, not dropped.
    FROM dedup_event AS e
    WHERE e.event_type = 'model.call'
      AND e.run_id  IS NULL          -- the session-grain shape; see above
      AND e.trace_id IS NOT NULL     -- no session id, no way to reach a run
      AND DATE(e.event_time) >= cutover_date   -- ⭐ journal branch only
  ),

  -- Priced at (session, model). CONTRACT §4: "Since 1.1.0 'per call' means per
  -- (session, model), because that is the grain the journal records ... A session that
  -- straddles a price change is priced at the boundary it ends on, and a session using
  -- two models is priced per model, which is exact."
  --
  -- ⚠ THE PRICING DATE IS SHUTDOWN TIME, not call time, and it cannot be anything
  -- else: the journal never records when the individual calls happened. For a session
  -- that straddles a midnight price change this prices the whole session at the later
  -- rate. That is a known, bounded inaccuracy of the source, stated rather than
  -- papered over; splitting the total across the boundary by elapsed time would be
  -- modelling dressed as measurement.
  journal_priced AS (
    SELECT
      c.copilot_session_id,
      c.model_id,
      c.input_tokens,
      c.output_tokens,
      c.cached_input_tokens,
      c.cache_write_tokens,
      c.reasoning_tokens,
      c.request_count,
      c.premium_requests,
      c.nano_aiu,
      CASE
        WHEN c.input_tokens IS NULL AND c.output_tokens IS NULL THEN NULL
        ELSE COALESCE(c.input_tokens, 0) + COALESCE(c.output_tokens, 0)
      END AS total_tokens,
      p.effective_from AS pricing_effective_from,
      p.is_placeholder AS pricing_is_placeholder,
      (   p.input_per_1k_usd  IS NOT NULL
      AND p.output_per_1k_usd IS NOT NULL
      AND (COALESCE(c.cached_input_tokens, 0) = 0 OR p.cached_input_per_1k_usd IS NOT NULL)
      ) AS is_priceable,
      CASE
        WHEN p.input_per_1k_usd IS NULL OR p.output_per_1k_usd IS NULL THEN NULL
        WHEN COALESCE(c.cached_input_tokens, 0) > 0
             AND p.cached_input_per_1k_usd IS NULL THEN NULL
        -- CONTRACT §4 cost formula — THREE terms. cache_write_tokens is NOT one of
        -- them and must never be added here: it is neither an input nor an output
        -- term, and folding it in would inflate every cost figure across the cutover
        -- by an amount nobody could reconstruct afterwards.
        ELSE
            ( SAFE_CAST(GREATEST(COALESCE(c.input_tokens, 0)
                                 - COALESCE(c.cached_input_tokens, 0), 0) AS NUMERIC)
              / 1000 * p.input_per_1k_usd )
          + ( SAFE_CAST(COALESCE(c.output_tokens, 0) AS NUMERIC)
              / 1000 * p.output_per_1k_usd )
          + ( SAFE_CAST(COALESCE(c.cached_input_tokens, 0) AS NUMERIC)
              / 1000 * COALESCE(p.cached_input_per_1k_usd, 0) )
      END AS call_cost_usd
    FROM journal_call AS c
    LEFT JOIN `${PROJECT_ID}.core.dim_model_pricing` AS p
      ON  p.model_id = c.model_id
      AND DATE(c.shutdown_at) BETWEEN p.effective_from
                                  AND COALESCE(p.effective_to, DATE '9999-12-31')
  ),

  -- ===================================================================================
  -- STEP 8b — roll the journal side up to SESSION grain
  -- ===================================================================================
  -- Session grain is where this source is honest. CONTRACT §3 lists what stays valid:
  -- "tokens and premium requests per session, per model, per person, per repository,
  -- per week" — every §6 Cost figure is built from these and is unaffected by the
  -- change. What is invalid is a per-run share of a multi-run session, which is what
  -- STEP 12 refuses to compute.
  journal_session AS (
    SELECT
      copilot_session_id,
      -- ⚠ COUNT of (session, model) tuples, NOT of API calls. It keeps the name
      -- model_call_count because that is what the column has always counted —
      -- model.call events — but the thing an event represents changed underneath it.
      -- request_count below is the real call count.
      COUNT(*)                                                   AS model_call_count,
      SUM(request_count)                                         AS request_count,
      -- NULL-preserving throughout: a session whose usage was never reported must show
      -- NULL, not 0. SUM() over all-NULL already returns NULL, which is what we want.
      SUM(input_tokens)                                          AS input_tokens,
      SUM(output_tokens)                                         AS output_tokens,
      SUM(cached_input_tokens)                                   AS cached_input_tokens,
      SUM(cache_write_tokens)                                    AS cache_write_tokens,
      SUM(reasoning_tokens)                                      AS reasoning_tokens,
      SUM(total_tokens)                                          AS total_tokens,
      -- CONTRACT §4.1 — measured, dimensionless, and carried BESIDE cost_usd. Never
      -- summed into it and never into a total_cost_usd: "one is measured and
      -- dimensionless, the other is modelled and in dollars, and a single number
      -- carrying both would be defensible as neither." DQ-BILL enforces it.
      SUM(premium_requests)                                      AS premium_requests,
      SUM(nano_aiu)                                              AS nano_aiu,
      -- Same all-or-nothing rule as the span path: if ANY model in the session could
      -- not be priced, the session's cost is unknown. A partial sum looks complete and
      -- is systematically low.
      IF(LOGICAL_AND(is_priceable), SUM(call_cost_usd), NULL)    AS cost_usd,
      LOGICAL_OR(COALESCE(pricing_is_placeholder, FALSE))        AS cost_is_placeholder,
      MIN(pricing_effective_from)                                AS pricing_effective_from
    FROM journal_priced
    GROUP BY copilot_session_id
  ),

  -- The model a session is reported under: the one that consumed the most tokens.
  -- Deterministic (ties broken on model_id) because this transform is idempotent and
  -- §9.5 requires a restatement to be visible — a re-run that silently changed the
  -- reported model would be an invisible one.
  journal_session_model AS (
    SELECT copilot_session_id, model_id
    FROM journal_priced
    WHERE model_id IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY copilot_session_id
      ORDER BY COALESCE(total_tokens, -1) DESC, model_id ASC
    ) = 1
  ),

  -- ===================================================================================
  -- STEP 8c — ⭐ attribute session usage to a run, or refuse to
  -- ===================================================================================
  -- The refusal is the point. CONTRACT §3: "A session that hosted more than one run
  -- must report cost_usd = NULL for its constituent runs rather than a share of the
  -- total. §2.4 already forbids synthesising a join key; apportioning a measured total
  -- across runs by time or by call count is the same offence wearing arithmetic."
  --
  -- The whole usage block is withheld, not just cost — charging one run a session's
  -- tokens is the same error whether or not a price is attached to them. The totals
  -- are not lost: they are published at session grain in marts.v_session_usage, which
  -- is the grain at which they are true.
  journal_run AS (
    SELECT
      rs.run_id,
      rs.copilot_session_id,
      sc.session_run_count,
      (sc.session_run_count = 1)                          AS cost_attributable,
      jm.model_id,
      js.model_call_count,
      js.request_count,
      js.input_tokens,
      js.output_tokens,
      js.cached_input_tokens,
      js.cache_write_tokens,
      js.reasoning_tokens,
      js.total_tokens,
      js.premium_requests,
      js.nano_aiu,
      js.cost_usd,
      js.cost_is_placeholder,
      js.pricing_effective_from
    -- Explicit ON rather than USING: three of these CTEs carry the same column name
    -- and a merged USING column cannot then be qualified per-source.
    FROM run_session AS rs
    JOIN journal_session       AS js ON js.copilot_session_id = rs.copilot_session_id
    JOIN session_counts        AS sc ON sc.copilot_session_id = rs.copilot_session_id
    JOIN journal_session_model AS jm ON jm.copilot_session_id = rs.copilot_session_id
  ),

  -- ===================================================================================
  -- STEP 9 — MODELLED FALLBACK (design §8.1, §8.2)
  -- ===================================================================================
  -- For surfaces that do not export OTel (unmanaged laptops, third-party IDE plugins,
  -- and the window before enterprise-managed settings roll out), emit.py may push
  -- `model.call` events into the correlation stream carrying ESTIMATED token counts.
  -- These are accepted, but they are tagged cost_basis = 'modelled' and must be
  -- rendered visually distinct downstream (§8.1) — a dashboard that silently blends
  -- measured and modelled cost is the fastest way to lose trust in the programme.
  --
  -- Accuracy expectation for this path is ±40% (§8.2): adequate for relative
  -- comparison and outlier spotting, NOT for chargeback.
  --
  -- ⚠ `run_id IS NOT NULL` SPLITS THIS FROM STEP 7b. Since 1.1.0 two different shapes
  -- of `model.call` land on this stream: an emitter estimate stamped with the run that
  -- made it (here), and a journal session total with no run at all (STEP 7b). Reading
  -- both here produced one row keyed on a NULL run_id that joined to nothing, so the
  -- usage side of the warehouse was silently empty.
  modelled_call AS (
    SELECT
      e.run_id,
      e.event_time,
      JSON_VALUE(e.attributes, '$.model_id')                              AS model_id,
      SAFE_CAST(JSON_VALUE(e.attributes, '$.input_tokens')  AS INT64)     AS input_tokens,
      SAFE_CAST(JSON_VALUE(e.attributes, '$.output_tokens') AS INT64)     AS output_tokens,
      SAFE_CAST(JSON_VALUE(e.attributes, '$.cached_input_tokens') AS INT64) AS cached_input_tokens,
      SAFE_CAST(JSON_VALUE(e.attributes, '$.reasoning_tokens') AS INT64)  AS reasoning_tokens,
      SAFE_CAST(JSON_VALUE(e.attributes, '$.retry_count') AS INT64)       AS retry_count
    FROM dedup_event AS e
    WHERE e.event_type = 'model.call'
      AND e.run_id IS NOT NULL      -- per-call estimates only; see above
  ),

  -- The model an estimated run is reported under: the one that answered the most
  -- calls. Was APPROX_TOP_COUNT inline; made exact and deterministic because this
  -- transform is idempotent and a re-run that silently changed the reported model
  -- would be an invisible restatement (design §9.5). At these volumes the exact
  -- version costs nothing.
  modelled_model AS (
    SELECT run_id, model_id
    FROM (
      SELECT run_id, model_id, COUNT(*) AS call_count
      FROM modelled_call
      WHERE model_id IS NOT NULL
      GROUP BY run_id, model_id
    )
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY run_id ORDER BY call_count DESC, model_id ASC
    ) = 1
  ),

  modelled_rollup AS (
    SELECT
      m.run_id,
      COUNT(*)                                          AS model_call_count,
      SUM(m.retry_count)                                AS retry_count,
      SUM(m.input_tokens)                               AS input_tokens,
      SUM(m.output_tokens)                              AS output_tokens,
      SUM(m.cached_input_tokens)                        AS cached_input_tokens,
      SUM(m.reasoning_tokens)                           AS reasoning_tokens,
      -- Same derivation rule as the OTel side: total = input + output only.
      CASE
        WHEN SUM(m.input_tokens) IS NULL AND SUM(m.output_tokens) IS NULL THEN NULL
        ELSE COALESCE(SUM(m.input_tokens), 0) + COALESCE(SUM(m.output_tokens), 0)
      END                                               AS total_tokens,
      ANY_VALUE(mm.model_id)                            AS model_id,
      IF(LOGICAL_AND(p.input_per_1k_usd IS NOT NULL AND p.output_per_1k_usd IS NOT NULL),
         SUM(
             ( SAFE_CAST(GREATEST(COALESCE(m.input_tokens, 0)
                                  - COALESCE(m.cached_input_tokens, 0), 0) AS NUMERIC)
               / 1000 * p.input_per_1k_usd )
           + ( SAFE_CAST(COALESCE(m.output_tokens, 0) AS NUMERIC)
               / 1000 * p.output_per_1k_usd )
           + ( SAFE_CAST(COALESCE(m.cached_input_tokens, 0) AS NUMERIC)
               / 1000 * COALESCE(p.cached_input_per_1k_usd, 0) )
         ),
         NULL)                                          AS cost_usd,
      LOGICAL_OR(COALESCE(p.is_placeholder, FALSE))     AS cost_is_placeholder,
      MIN(p.effective_from)                             AS pricing_effective_from
    FROM modelled_call AS m
    LEFT JOIN modelled_model AS mm ON mm.run_id = m.run_id
    -- Same effective-dated pricing join as the measured path (CONTRACT §4).
    LEFT JOIN `${PROJECT_ID}.core.dim_model_pricing` AS p
      ON  p.model_id = m.model_id
      AND DATE(m.event_time) BETWEEN p.effective_from
                                 AND COALESCE(p.effective_to, DATE '9999-12-31')
    GROUP BY m.run_id
  ),

  -- ===================================================================================
  -- STEP 10 — correlation-stream activity rollups
  -- ===================================================================================
  -- Human turns are split by turn_kind and NEVER pre-totalled into one "intervention"
  -- number here. §8.11 EXCLUDES 'approval' and 'clarification' by design: the agents
  -- deliberately ask for approval (developer.implementer.agent.md
  -- autonomous_policy.ask_user_when covers architectural choices, breaking changes and
  -- security trade-offs), and counting a designed gate as an intervention would punish
  -- correct behaviour. Collapsing them here would make that exclusion impossible.
  human_turn_rollup AS (
    SELECT
      run_id,
      COUNT(*)                                                            AS human_turns_total,
      COUNTIF(JSON_VALUE(attributes, '$.turn_kind') = 'correction')       AS human_turns_correction,
      COUNTIF(JSON_VALUE(attributes, '$.turn_kind') = 'rejection')        AS human_turns_rejection,
      COUNTIF(JSON_VALUE(attributes, '$.turn_kind') = 'approval')         AS human_turns_approval,
      COUNTIF(JSON_VALUE(attributes, '$.turn_kind') = 'clarification')    AS human_turns_clarification,
      SUM(SAFE_CAST(JSON_VALUE(attributes, '$.chars') AS INT64))          AS human_turn_chars
    FROM dedup_event
    WHERE event_type = 'human.turn'
    GROUP BY run_id
  ),

  -- Gate outcomes. attempt_index > 0 marks an auto-fix retry: the agent re-running a
  -- gate it just failed. That counts toward manual_intervention_rate per the §8.11
  -- formula, but is explicitly NOT rework (§8.9) — it happens before any human looked.
  gate_rollup AS (
    SELECT
      run_id,
      ARRAY_AGG(
        STRUCT(
          JSON_VALUE(attributes, '$.gate_name')                        AS gate_name,
          JSON_VALUE(attributes, '$.status')                           AS status,
          SAFE_CAST(JSON_VALUE(attributes, '$.quality_score') AS FLOAT64) AS quality_score,
          SAFE_CAST(JSON_VALUE(attributes, '$.coverage_pct')  AS FLOAT64) AS coverage_pct,
          SAFE_CAST(JSON_VALUE(attributes, '$.attempt_index') AS INT64)   AS attempt_index
        )
        ORDER BY event_time
      )                                                                AS gate_results,
      COUNTIF(SAFE_CAST(JSON_VALUE(attributes, '$.attempt_index') AS INT64) > 0)
                                                                       AS gate_auto_fix_attempts,
      MAX(SAFE_CAST(JSON_VALUE(attributes, '$.coverage_pct') AS FLOAT64))
                                                                       AS max_coverage_pct
    FROM dedup_event
    WHERE event_type = 'gate.evaluated'
    GROUP BY run_id
  ),

  -- Pass/fail is judged on each gate's FINAL attempt only. A build gate that failed
  -- twice and then passed is a PASSING gate with two auto-fix cycles, not two failures.
  --
  -- ⭐ `gate_unknown_count` IS NEW IN 1.1.0 AND IT CLOSES A SILENT LEAK.
  --
  -- Under the span source `status` was structurally NULL — spans carried no status
  -- field — so BOTH counts here were 0 for every run since inception, and the gate
  -- pass rate was an undefined ratio that rendered as zero. The journal supplies a
  -- real verdict from the `<exited with exit code N>` trailer Copilot's bash tool
  -- appends, so ~88% of gate evaluations now carry pass or fail. The other ~12% do
  -- not: the command was still running, or its output was truncated past the trailer.
  --
  -- A gate whose FINAL attempt has a NULL status used to count as neither pass nor
  -- fail and so vanished from numerator AND denominator with no trace that it had
  -- been dropped. Counting it explicitly means the exclusion is visible, and the
  -- known-share can be published next to the rate the way explicit_link_pct already
  -- is. A missing verdict is not a pass; it is also not a fail.
  --
  -- pass/fail are NULL — not 0 — when the run ran gates and none carried a verdict,
  -- so that "no known verdicts" and "no gates at all" stop being the same row.
  gate_final AS (
    SELECT
      run_id,
      -- The sentinel that lets `assembled` tell "no gates ran" (this CTE has no row
      -- for the run) from "gates ran, none carried a verdict" (a row with NULL
      -- pass/fail). Testing g.gate_results IS NULL would be subtler and less safe: an
      -- outer-joined ARRAY is a NULL the write path turns into an empty array.
      COUNT(*)                                              AS gate_count,
      IF(COUNTIF(status IN ('pass', 'fail')) = 0, NULL, COUNTIF(status = 'pass'))
                                                            AS gate_pass_count,
      IF(COUNTIF(status IN ('pass', 'fail')) = 0, NULL, COUNTIF(status = 'fail'))
                                                            AS gate_fail_count,
      COUNTIF(status IS NULL OR status NOT IN ('pass', 'fail', 'skipped'))
                                                            AS gate_unknown_count
    FROM (
      SELECT
        run_id,
        JSON_VALUE(attributes, '$.gate_name') AS gate_name,
        JSON_VALUE(attributes, '$.status')    AS status
      FROM dedup_event
      WHERE event_type = 'gate.evaluated'
      QUALIFY ROW_NUMBER() OVER (
        PARTITION BY run_id, JSON_VALUE(attributes, '$.gate_name')
        ORDER BY COALESCE(SAFE_CAST(JSON_VALUE(attributes, '$.attempt_index') AS INT64), 0) DESC,
                 event_time DESC
      ) = 1
    )
    GROUP BY run_id
  ),

  phase_rollup AS (
    SELECT
      run_id,
      COUNTIF(JSON_VALUE(attributes, '$.status') = 'failed') AS phase_failed_count,
      COUNTIF(JSON_VALUE(attributes, '$.status') = 'ok')     AS phase_ok_count
    FROM dedup_event
    WHERE event_type = 'run.phase.completed'
    GROUP BY run_id
  ),

  -- Tool calls observed on the correlation stream. Was the fallback for a
  -- non-exporting surface; since 1.1.0 this is THE source — tool.call comes from the
  -- session journal.
  --
  -- ⚠⚠ FALSE MEASUREMENT, FIXED 2026-08-26. This counted errors as
  --        COUNTIF(JSON_VALUE(attributes, '$.status') = 'failed')
  -- and the CONTRACT §3 row 6 enum has never contained 'failed' — it is `ok` | `error`
  -- ('failed' is the run.phase.completed enum, one row further down this file, which
  -- is presumably where it came from). The comparison therefore never matched
  -- anything and tool_error_count has been exactly 0 since the file was written.
  -- Nobody noticed, because under the span source `status` was NULL anyway, so a
  -- structural zero and a measured zero were indistinguishable and the weekly report
  -- published "zero tool failures" as though it were a finding.
  --
  -- Fixing it converts that structural zero into a real number: measured 2026-08-26,
  -- 62 errors in 2,062 journal tool calls. This is a LOUD behaviour change — anyone
  -- who has quoted zero tool failures was quoting a schema artefact.
  --
  -- `tool_status_unknown_count` exists so the same silence cannot return under a new
  -- name: unknown and ok must not share a row. And when a run made tool calls and NOT
  -- ONE of them reported a status, the error count is NULL rather than 0 — a zero is
  -- only a zero if something was watching.
  tool_rollup_event AS (
    SELECT
      run_id,
      COUNT(*)                                                   AS tool_call_count,
      IF(COUNTIF(JSON_VALUE(attributes, '$.status') IS NOT NULL) = 0,
         NULL,
         COUNTIF(JSON_VALUE(attributes, '$.status') = 'error'))  AS tool_error_count,
      COUNTIF(JSON_VALUE(attributes, '$.status') IS NULL)        AS tool_status_unknown_count
    FROM dedup_event
    WHERE event_type = 'tool.call'
    GROUP BY run_id
  ),

  -- ===================================================================================
  -- STEP 11 — identity resolution (design §9.4 DQ-1)
  -- ===================================================================================
  -- person_id from the event is trusted when present. When it is NULL — an emitter
  -- that only had a git identity — resolve through the effective-dated dim_person
  -- alias map. Resolution is against the row valid AT RUN TIME, so a person who
  -- changed team last month does not have their old runs silently re-teamed.
  -- Unverified aliases are excluded: they are steward candidates, not evidence.
  person_resolved AS (
    SELECT
      d.person_id,
      a.email_hash,
      d.team_id,
      d.role,
      d.effective_from,
      d.effective_to
    FROM `${PROJECT_ID}.core.dim_person` AS d,
         UNNEST(d.git_author_aliases) AS a
    WHERE COALESCE(a.is_verified, FALSE)
  ),

  -- ===================================================================================
  -- STEP 12 — assemble
  -- ===================================================================================
  assembled AS (
    SELECT
      s.run_id,
      s.trace_id,
      rs.trace_id_namespace,
      s.parent_run_id,
      (s.parent_run_id IS NULL)                                       AS is_root_run,
      s.workflow_id,

      rs.copilot_session_id,
      sc.session_run_count,
      o.otel_trace_id,
      COALESCE(o.otel_span_count, 0)                                  AS otel_span_count,

      s.started_at,
      t.ended_at,
      -- Prefer the client-reported duration; fall back to the timestamp difference.
      -- DQ-10 handles the case where the difference is negative (clock skew).
      COALESCE(
        t.duration_ms,
        TIMESTAMP_DIFF(t.ended_at, s.started_at, MILLISECOND)
      )                                                               AS duration_ms,

      -- Identity: the event's own person_id wins; the alias map is the fallback.
      COALESCE(s.person_id, pr.person_id)                             AS person_id,
      s.person_email_hash,
      COALESCE(s.team_id, pr.team_id)                                 AS team_id,
      COALESCE(s.role, pr.role)                                       AS role,

      -- AR-3: attribute to the FEATURE ticket, keep the delivery ticket separate.
      COALESCE(b.bound_jira_issue_key, s.jira_issue_key)              AS jira_issue_key,
      b.delivery_ticket_key,
      COALESCE(
        s.jira_project_key,
        REGEXP_EXTRACT(COALESCE(b.bound_jira_issue_key, s.jira_issue_key), r'^([A-Z][A-Z0-9]+)-\d+$')
      )                                                               AS jira_project_key,
      s.repo_full_name,
      s.branch_name,
      s.product_profile,
      s.environment,

      s.agent_name,
      s.agent_version,
      av.agent_kind,
      s.skill_name,
      s.skill_version,
      s.surface,
      COALESCE(s.model_declared_id, av.model_declared_id)             AS model_declared_id,

      -- model_id: measured first, modelled fallback, declared value last. The declared
      -- value is the weakest source — it is what the agent ASKED for, not what
      -- answered — so it is used only when nothing observed the runtime. The journal
      -- names the model even when its usage cannot be attributed to this run, so it
      -- is read here without the attributability gate: which model answered is a fact
      -- about the session and is true of every run in it.
      COALESCE(ju.model_id, o.model_id, mr.model_id, s.model_declared_id) AS model_id,

      s.invocation_mode,
      s.input_source,

      -- ---- usage provenance and grain (1.1.0) ----
      -- The three branches are mutually exclusive by construction: the span branch is
      -- gated on started_at < cutover_date and the journal branch on event_time >=
      -- cutover_date, so a COALESCE order here is a formality rather than a
      -- precedence rule. DQ-GRAIN checks that from the other side, on the data.
      CASE
        WHEN ju.copilot_session_id IS NOT NULL THEN 'per_session_model'
        WHEN o.total_tokens  IS NOT NULL       THEN 'per_call'
        WHEN mr.total_tokens IS NOT NULL       THEN 'per_call'
        ELSE 'none'
      END                                                             AS usage_grain,
      CASE
        WHEN ju.copilot_session_id IS NOT NULL THEN 'copilot_journal'
        WHEN o.total_tokens  IS NOT NULL       THEN 'otel_span'
        WHEN mr.total_tokens IS NOT NULL       THEN 'emitter_estimate'
        ELSE 'none'
      END                                                             AS usage_source,

      -- ⭐ CONTRACT §3: session-grain usage may be attributed to a run ONLY when the
      -- session hosted exactly one run. Per-call sources were always attributable —
      -- the timestamp placed each call on the run that made it.
      CASE
        WHEN ju.copilot_session_id IS NOT NULL   THEN ju.cost_attributable
        WHEN o.total_tokens IS NOT NULL
          OR mr.total_tokens IS NOT NULL         THEN TRUE
        ELSE NULL   -- no usage observed: attributability is unknown, not FALSE
      END                                                             AS cost_attributable,

      -- ---- token block ----
      -- ⚠ THE WHOLE BLOCK IS WITHHELD, not just the cost, when a multi-run session
      -- made attribution impossible. Charging one run a whole session's tokens is the
      -- same error whether or not a price is attached to them. The totals are not
      -- lost — marts.v_session_usage publishes them at the grain where they are true.
      COALESCE(IF(ju.cost_attributable, ju.input_tokens,        NULL), o.input_tokens,        mr.input_tokens)        AS input_tokens,
      COALESCE(IF(ju.cost_attributable, ju.output_tokens,       NULL), o.output_tokens,       mr.output_tokens)       AS output_tokens,
      COALESCE(IF(ju.cost_attributable, ju.cached_input_tokens, NULL), o.cached_input_tokens, mr.cached_input_tokens) AS cached_input_tokens,
      COALESCE(IF(ju.cost_attributable, ju.reasoning_tokens,    NULL), o.reasoning_tokens,    mr.reasoning_tokens)    AS reasoning_tokens,
      -- cache_write_tokens has no pre-cutover equivalent and is NOT part of the §4
      -- cost formula; it is carried for visibility and must never be added into
      -- total_tokens.
      IF(ju.cost_attributable, ju.cache_write_tokens, NULL)           AS cache_write_tokens,
      COALESCE(IF(ju.cost_attributable, ju.total_tokens,        NULL), o.total_tokens,        mr.total_tokens)        AS total_tokens,
      CASE
        WHEN ju.copilot_session_id IS NOT NULL AND ju.cost_attributable THEN 'measured_journal'
        -- Usage EXISTS for this session and is NOT attributable to this run. A
        -- distinct value, because "unknowable at this grain" and "never measured" are
        -- different facts and only one of them is fixable by turning telemetry on.
        WHEN ju.copilot_session_id IS NOT NULL                         THEN 'session_not_attributable'
        WHEN o.total_tokens  IS NOT NULL                               THEN 'measured_otel'
        WHEN mr.total_tokens IS NOT NULL                               THEN 'modelled_estimate'
        ELSE 'none'
      END                                                             AS token_source,

      -- ---- billing units (CONTRACT §4.1) — measured, dimensionless, NEVER blended ----
      -- Carried BESIDE cost_usd. Nothing in this warehouse adds them to a dollar
      -- figure, and DQ-BILL scans view definitions to keep it that way.
      IF(ju.cost_attributable, ju.premium_requests, NULL)              AS premium_requests,
      IF(ju.cost_attributable, ju.request_count,    NULL)              AS request_count,
      IF(ju.cost_attributable, ju.nano_aiu,         NULL)              AS nano_aiu,

      -- ---- cost block (CONTRACT §4) ----
      -- NULL propagates deliberately. cr.cost_usd is already NULL when any call in the
      -- run was unpriced; falling through to the modelled figure in that case would
      -- mix bases within one number, so the fallback applies only when there was no
      -- measured token data AT ALL.
      CASE
        WHEN ju.copilot_session_id IS NOT NULL THEN IF(ju.cost_attributable, ju.cost_usd, NULL)
        WHEN o.total_tokens  IS NOT NULL       THEN cr.cost_usd
        WHEN mr.total_tokens IS NOT NULL       THEN mr.cost_usd
        ELSE NULL
      END                                                             AS cost_usd,
      CASE
        WHEN ju.copilot_session_id IS NOT NULL AND ju.cost_attributable THEN 'measured'
        WHEN ju.copilot_session_id IS NOT NULL                          THEN NULL
        WHEN o.total_tokens  IS NOT NULL                                THEN 'measured'
        WHEN mr.total_tokens IS NOT NULL                                THEN 'modelled'
        ELSE NULL      -- no tokens observed => no basis to claim. Not 'measured'.
      END                                                             AS cost_basis,
      CASE
        WHEN ju.copilot_session_id IS NOT NULL THEN COALESCE(ju.cost_is_placeholder, FALSE)
        WHEN o.total_tokens  IS NOT NULL       THEN COALESCE(cr.cost_is_placeholder, FALSE)
        WHEN mr.total_tokens IS NOT NULL       THEN COALESCE(mr.cost_is_placeholder, FALSE)
        ELSE FALSE
      END                                                             AS cost_is_placeholder,
      CASE
        WHEN ju.copilot_session_id IS NOT NULL THEN ju.pricing_effective_from
        WHEN o.total_tokens  IS NOT NULL       THEN cr.pricing_effective_from
        WHEN mr.total_tokens IS NOT NULL       THEN mr.pricing_effective_from
        ELSE NULL
      END                                                             AS pricing_effective_from,

      -- ---- activity ----
      COALESCE(o.tool_call_count,  te.tool_call_count,  0)            AS tool_call_count,
      -- No trailing 0: te.tool_error_count is already NULL when the run made tool
      -- calls and none reported a status, and that NULL must survive to the mart.
      COALESCE(o.tool_error_count, te.tool_error_count)               AS tool_error_count,
      COALESCE(te.tool_status_unknown_count, 0)                       AS tool_status_unknown_count,
      COALESCE(ju.model_call_count, o.model_call_count, mr.model_call_count, 0) AS model_call_count,
      -- ⚠ NO COALESCE TO ZERO. retry_count is retired: neither the journal nor the
      -- frozen span view reports it, so only an emitter estimate can supply one and
      -- everything else is NULL. Defaulting to 0 here is what made retry_rate_pct
      -- read exactly 0.0% and get published as a measurement.
      mr.retry_count                                                  AS retry_count,
      COALESCE(t.phases_completed, ph.phase_ok_count,    0)           AS phases_completed,
      COALESCE(ph.phase_failed_count, 0)                              AS phase_failed_count,

      COALESCE(h.human_turns_total,         0)                        AS human_turns_total,
      COALESCE(h.human_turns_correction,    0)                        AS human_turns_correction,
      COALESCE(h.human_turns_rejection,     0)                        AS human_turns_rejection,
      COALESCE(h.human_turns_approval,      0)                        AS human_turns_approval,
      COALESCE(h.human_turns_clarification, 0)                        AS human_turns_clarification,
      COALESCE(h.human_turn_chars,          0)                        AS human_turn_chars,

      g.gate_results,
      -- Three-way, not two: 0 when no gate ran at all (no gate_final row — we were
      -- watching and nothing happened), NULL when gates ran but none carried a
      -- verdict, the count otherwise. COALESCE(..., 0) here used to collapse the
      -- first two into the same row, so "no known verdicts" and "no gates" were
      -- indistinguishable on every dashboard.
      IF(gf.gate_count IS NULL, 0, gf.gate_pass_count)                AS gate_pass_count,
      IF(gf.gate_count IS NULL, 0, gf.gate_fail_count)                AS gate_fail_count,
      COALESCE(gf.gate_unknown_count, 0)                              AS gate_unknown_count,
      COALESCE(g.gate_auto_fix_attempts, 0)                           AS gate_auto_fix_attempts,
      g.max_coverage_pct,

      -- ---- outcome ----
      CASE t.terminal_event_type
        WHEN 'run.completed' THEN 'completed'
        WHEN 'run.failed'    THEN 'failed'
        WHEN 'run.timeout'   THEN 'timeout'
        WHEN 'run.abandoned' THEN 'abandoned'
        ELSE 'in_flight'
      END                                                             AS terminal_status,
      t.failure_class,
      t.dependency_failed,
      t.timeout_policy,

      s.link_method,
      s.link_confidence,
      s.schema_version,
      CURRENT_TIMESTAMP()                                             AS transformed_at
    FROM run_started AS s
    LEFT JOIN run_bound            AS b  USING (run_id)
    LEFT JOIN run_session          AS rs USING (run_id)
    LEFT JOIN session_counts       AS sc ON sc.copilot_session_id = rs.copilot_session_id
    LEFT JOIN run_terminal         AS t  USING (run_id)
    LEFT JOIN otel_rollup          AS o  USING (run_id)
    LEFT JOIN cost_rollup          AS cr USING (run_id)
    LEFT JOIN journal_run          AS ju USING (run_id)
    LEFT JOIN modelled_rollup      AS mr USING (run_id)
    LEFT JOIN human_turn_rollup    AS h  USING (run_id)
    LEFT JOIN gate_rollup          AS g  USING (run_id)
    LEFT JOIN gate_final           AS gf USING (run_id)
    LEFT JOIN phase_rollup         AS ph USING (run_id)
    LEFT JOIN tool_rollup_event    AS te USING (run_id)
    LEFT JOIN `${PROJECT_ID}.core.dim_agent_version` AS av
      ON  av.agent_name    = s.agent_name
      AND av.agent_version = s.agent_version
    -- Identity fallback. Effective-dated: resolve against the dim_person row that was
    -- valid on the day the run happened, not the row that is current today.
    LEFT JOIN person_resolved AS pr
      ON  pr.email_hash = s.person_email_hash
      AND DATE(s.started_at) BETWEEN pr.effective_from
                                 AND COALESCE(pr.effective_to, DATE '9999-12-31')
  )

  SELECT
    a.*,
    -- Model drift: the agent declared one model, the runtime answered with another.
    -- Not a data error — a finding. Every design §9.1 model-comparison arm is
    -- meaningless if the declared model is not the one doing the work.
    (a.model_declared_id IS NOT NULL
     AND a.model_id IS NOT NULL
     AND a.model_declared_id <> a.model_id) AS model_drift
  FROM assembled AS a

) AS src
ON tgt.run_id = src.run_id

WHEN MATCHED THEN UPDATE SET
  trace_id                  = src.trace_id,
  trace_id_namespace        = src.trace_id_namespace,
  parent_run_id             = src.parent_run_id,
  is_root_run               = src.is_root_run,
  workflow_id               = src.workflow_id,
  copilot_session_id        = src.copilot_session_id,
  session_run_count         = src.session_run_count,
  cost_attributable         = src.cost_attributable,
  otel_trace_id             = src.otel_trace_id,
  otel_span_count           = src.otel_span_count,
  started_at                = src.started_at,
  ended_at                  = src.ended_at,
  duration_ms               = src.duration_ms,
  person_id                 = src.person_id,
  person_email_hash         = src.person_email_hash,
  team_id                   = src.team_id,
  role                      = src.role,
  jira_issue_key            = src.jira_issue_key,
  delivery_ticket_key       = src.delivery_ticket_key,
  jira_project_key          = src.jira_project_key,
  repo_full_name            = src.repo_full_name,
  branch_name               = src.branch_name,
  product_profile           = src.product_profile,
  environment               = src.environment,
  agent_name                = src.agent_name,
  agent_version             = src.agent_version,
  agent_kind                = src.agent_kind,
  skill_name                = src.skill_name,
  skill_version             = src.skill_version,
  surface                   = src.surface,
  model_declared_id         = src.model_declared_id,
  model_id                  = src.model_id,
  model_drift               = src.model_drift,
  invocation_mode           = src.invocation_mode,
  input_source              = src.input_source,
  input_tokens              = src.input_tokens,
  output_tokens             = src.output_tokens,
  cached_input_tokens       = src.cached_input_tokens,
  reasoning_tokens          = src.reasoning_tokens,
  cache_write_tokens        = src.cache_write_tokens,
  total_tokens              = src.total_tokens,
  token_source              = src.token_source,
  usage_grain               = src.usage_grain,
  usage_source              = src.usage_source,
  premium_requests          = src.premium_requests,
  request_count             = src.request_count,
  nano_aiu                  = src.nano_aiu,
  cost_usd                  = src.cost_usd,
  cost_basis                = src.cost_basis,
  cost_is_placeholder       = src.cost_is_placeholder,
  pricing_effective_from    = src.pricing_effective_from,
  tool_call_count           = src.tool_call_count,
  tool_error_count          = src.tool_error_count,
  tool_status_unknown_count = src.tool_status_unknown_count,
  model_call_count          = src.model_call_count,
  retry_count               = src.retry_count,
  phases_completed          = src.phases_completed,
  phase_failed_count        = src.phase_failed_count,
  human_turns_total         = src.human_turns_total,
  human_turns_correction    = src.human_turns_correction,
  human_turns_rejection     = src.human_turns_rejection,
  human_turns_approval      = src.human_turns_approval,
  human_turns_clarification = src.human_turns_clarification,
  human_turn_chars          = src.human_turn_chars,
  gate_results              = src.gate_results,
  gate_pass_count           = src.gate_pass_count,
  gate_fail_count           = src.gate_fail_count,
  gate_unknown_count        = src.gate_unknown_count,
  gate_auto_fix_attempts    = src.gate_auto_fix_attempts,
  max_coverage_pct          = src.max_coverage_pct,
  terminal_status           = src.terminal_status,
  failure_class             = src.failure_class,
  dependency_failed         = src.dependency_failed,
  timeout_policy            = src.timeout_policy,
  link_method               = src.link_method,
  link_confidence           = src.link_confidence,
  schema_version            = src.schema_version,
  transformed_at            = src.transformed_at

WHEN NOT MATCHED THEN INSERT (
  run_id, trace_id, trace_id_namespace, parent_run_id, is_root_run, workflow_id,
  copilot_session_id, session_run_count, cost_attributable,
  otel_trace_id, otel_span_count,
  started_at, ended_at, duration_ms,
  person_id, person_email_hash, team_id, role,
  jira_issue_key, delivery_ticket_key, jira_project_key, repo_full_name,
  branch_name, product_profile, environment,
  agent_name, agent_version, agent_kind, skill_name, skill_version, surface,
  model_declared_id, model_id, model_drift, invocation_mode, input_source,
  input_tokens, output_tokens, cached_input_tokens, reasoning_tokens,
  cache_write_tokens, total_tokens,
  token_source, usage_grain, usage_source,
  premium_requests, request_count, nano_aiu,
  cost_usd, cost_basis, cost_is_placeholder, pricing_effective_from,
  tool_call_count, tool_error_count, tool_status_unknown_count,
  model_call_count, retry_count,
  phases_completed, phase_failed_count,
  human_turns_total, human_turns_correction, human_turns_rejection,
  human_turns_approval, human_turns_clarification, human_turn_chars,
  gate_results, gate_pass_count, gate_fail_count, gate_unknown_count,
  gate_auto_fix_attempts,
  max_coverage_pct,
  terminal_status, failure_class, dependency_failed, timeout_policy,
  link_method, link_confidence, schema_version, transformed_at
)
VALUES (
  src.run_id, src.trace_id, src.trace_id_namespace, src.parent_run_id, src.is_root_run, src.workflow_id,
  src.copilot_session_id, src.session_run_count, src.cost_attributable,
  src.otel_trace_id, src.otel_span_count,
  src.started_at, src.ended_at, src.duration_ms,
  src.person_id, src.person_email_hash, src.team_id, src.role,
  src.jira_issue_key, src.delivery_ticket_key, src.jira_project_key, src.repo_full_name,
  src.branch_name, src.product_profile, src.environment,
  src.agent_name, src.agent_version, src.agent_kind, src.skill_name, src.skill_version, src.surface,
  src.model_declared_id, src.model_id, src.model_drift, src.invocation_mode, src.input_source,
  src.input_tokens, src.output_tokens, src.cached_input_tokens, src.reasoning_tokens,
  src.cache_write_tokens, src.total_tokens,
  src.token_source, src.usage_grain, src.usage_source,
  src.premium_requests, src.request_count, src.nano_aiu,
  src.cost_usd, src.cost_basis, src.cost_is_placeholder, src.pricing_effective_from,
  src.tool_call_count, src.tool_error_count, src.tool_status_unknown_count,
  src.model_call_count, src.retry_count,
  src.phases_completed, src.phase_failed_count,
  src.human_turns_total, src.human_turns_correction, src.human_turns_rejection,
  src.human_turns_approval, src.human_turns_clarification, src.human_turn_chars,
  src.gate_results, src.gate_pass_count, src.gate_fail_count, src.gate_unknown_count,
  src.gate_auto_fix_attempts,
  src.max_coverage_pct,
  src.terminal_status, src.failure_class, src.dependency_failed, src.timeout_policy,
  src.link_method, src.link_confidence, src.schema_version, src.transformed_at
);
