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
-- │  THE CORRELATION PROBLEM (design §4.4a, §6.5)                                     │
-- │                                                                                   │
-- │  Two streams. Neither is sufficient. Neither knows about the other.               │
-- │                                                                                   │
-- │    raw.ai_run_event   knows  run_id, jira_issue_key, person_id, agent_name,       │
-- │    (emit.py + hooks)         outputs, gates, PR ids, commit SHAs                  │
-- │                       lacks  TOKENS. The agent never sees them.                   │
-- │                                                                                   │
-- │    raw.otel_span      knows  tokens, model, latency, tool calls,                  │
-- │    (Copilot OTLP)            gen_ai.conversation.id                               │
-- │                       lacks  jira key, person, PR, commit — everything that       │
-- │                              makes a token count MEAN anything                    │
-- │                                                                                   │
-- │  THE BRIDGE is exactly one event type: `run.bound` (CONTRACT §3, event #2).       │
-- │  emit.py captures the active Copilot conversation id at run start and emits       │
-- │  one row carrying {run_id, otel_conversation_id, jira_issue_key}. That single     │
-- │  row is the only thing stitching the two halves of this system together. If it    │
-- │  is missing, the run has no cost — and it must show as NULL cost, never as a      │
-- │  free run.                                                                        │
-- │                                                                                   │
-- │  ⚠ WHY A NAIVE JOIN ON conversation_id IS WRONG                                   │
-- │                                                                                   │
-- │  A Copilot conversation is a CHAT SESSION, not a run. One session routinely       │
-- │  hosts several runs:                                                              │
-- │     - the engineer invokes an agent, it finishes, they invoke another;            │
-- │     - a supervisor invokes nine sub-agents, ALL inside one conversation.          │
-- │                                                                                   │
-- │  So `ON run.otel_conversation_id = span.conversation_id` is many-to-many. It      │
-- │  would attribute EVERY token in the session to EVERY run in it — inflating cost   │
-- │  by the number of runs in the conversation, and doing so silently.                │
-- │                                                                                   │
-- │  THE FIX, implemented in the `span_binding` CTE below:                            │
-- │  bind on conversation id AND time, then force the result to be one-span-to-one-   │
-- │  run with QUALIFY ROW_NUMBER() = 1 partitioned by span_id. A span is assigned to  │
-- │  the MOST RECENTLY STARTED run whose active window contains it. Consequences,     │
-- │  both intended:                                                                   │
-- │     - sequential runs in one session each get their own tokens;                   │
-- │     - for a supervisor + sub-agent, the tokens land on the SUB-AGENT (the run     │
-- │       that actually made the call). The supervisor's totals are obtained by       │
-- │       rolling up trace_id — AR-4 — never by re-counting onto the supervisor row.  │
-- │                                                                                   │
-- │  The partition-by-span_id guarantee is the load-bearing part: it makes            │
-- │  double-counting structurally impossible rather than merely unlikely.             │
-- └───────────────────────────────────────────────────────────────────────────────────┘
--
-- Substitute ${PROJECT_ID}. Run after 01_raw.sql, 02_dims.sql, 03_core_fct.sql.
-- =====================================================================================

-- Reprocessing window. 8 days by default: DQ-13 accepts events up to 7 days late, so
-- anything shorter would build a run row from a partial event set and never revisit it.
DECLARE window_days INT64 DEFAULT 8;

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
  -- FIRST bind wins. A conversation id is captured once at run start; a second
  -- run.bound for the same run means the emitter re-bound after a client restart, and
  -- re-pointing an already-priced run at a different conversation would silently
  -- restate its cost.
  run_bound AS (
    SELECT
      run_id,
      JSON_VALUE(attributes, '$.otel_conversation_id') AS otel_conversation_id,
      JSON_VALUE(attributes, '$.jira_issue_key')       AS bound_jira_issue_key,
      JSON_VALUE(attributes, '$.qd_jira_key')          AS delivery_ticket_key,
      event_time                                       AS bound_at
    FROM dedup_event
    WHERE event_type = 'run.bound'
      AND JSON_VALUE(attributes, '$.otel_conversation_id') IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY event_time ASC) = 1
  ),

  -- ===================================================================================
  -- STEP 5 — each bound run's ACTIVE WINDOW
  -- ===================================================================================
  -- The interval during which a span in this conversation may be attributed to this
  -- run. Open runs are capped at max_open_run_hours so an abandoned run cannot keep
  -- absorbing tokens from every later run in the same chat session.
  run_window AS (
    SELECT
      s.run_id,
      s.trace_id,
      b.otel_conversation_id,
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
    JOIN run_bound   AS b USING (run_id)      -- INNER: an unbound run has no token data
    LEFT JOIN run_terminal AS t USING (run_id)
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
      sp.retry_count,
      sp.gen_ai_agent_name,
      sp.start_time
    FROM `${PROJECT_ID}.raw.v_otel_span_tokens` AS sp
    JOIN run_window AS w
      ON  sp.conversation_id = w.otel_conversation_id
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
  -- STEP 7 — price each model call individually (CONTRACT §4)
  -- ===================================================================================
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
  -- STEP 8 — roll the OTel side up to run grain
  -- ===================================================================================
  otel_rollup AS (
    SELECT
      run_id,
      ANY_VALUE(otel_trace_id)                                                   AS otel_trace_id,
      COUNT(*)                                                                   AS otel_span_count,
      COUNTIF(gen_ai_operation_name = 'chat')                                    AS model_call_count,
      COUNTIF(gen_ai_operation_name = 'execute_tool')                            AS tool_call_count,
      COUNTIF(gen_ai_operation_name = 'execute_tool' AND status_code = 'ERROR')  AS tool_error_count,
      SUM(COALESCE(retry_count, 0))                                              AS retry_count,

      -- NULL-preserving sums: a run whose usage was never reported must show NULL,
      -- not 0. SUM() over all-NULL input already returns NULL, which is what we want;
      -- COALESCE here would silently manufacture a free run.
      SUM(IF(gen_ai_operation_name = 'chat', input_tokens,        NULL)) AS input_tokens,
      SUM(IF(gen_ai_operation_name = 'chat', output_tokens,       NULL)) AS output_tokens,
      SUM(IF(gen_ai_operation_name = 'chat', cached_input_tokens, NULL)) AS cached_input_tokens,
      SUM(IF(gen_ai_operation_name = 'chat', reasoning_tokens,    NULL)) AS reasoning_tokens,
      SUM(IF(gen_ai_operation_name = 'chat', total_tokens,        NULL)) AS total_tokens,

      -- The model this run is reported under: the one that answered the most calls.
      -- APPROX_TOP_COUNT over the chat spans only.
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
      (SELECT value FROM UNNEST(APPROX_TOP_COUNT(m.model_id, 1)))        AS model_id,
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
  gate_final AS (
    SELECT
      run_id,
      COUNTIF(status = 'pass') AS gate_pass_count,
      COUNTIF(status = 'fail') AS gate_fail_count
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

  -- Tool calls observed on the correlation stream. Used only when the OTel stream is
  -- absent, so a non-exporting surface still reports tool activity.
  tool_rollup_fallback AS (
    SELECT
      run_id,
      COUNT(*)                                                   AS tool_call_count,
      COUNTIF(JSON_VALUE(attributes, '$.status') = 'failed')     AS tool_error_count
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
      s.parent_run_id,
      (s.parent_run_id IS NULL)                                       AS is_root_run,
      s.workflow_id,

      b.otel_conversation_id,
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
      -- answered — so it is used only when nothing observed the runtime.
      COALESCE(o.model_id, mr.model_id, s.model_declared_id)          AS model_id,

      s.invocation_mode,
      s.input_source,

      -- ---- token block: measured OTel wins; modelled estimate is the fallback ----
      COALESCE(o.input_tokens,        mr.input_tokens)                AS input_tokens,
      COALESCE(o.output_tokens,       mr.output_tokens)               AS output_tokens,
      COALESCE(o.cached_input_tokens, mr.cached_input_tokens)         AS cached_input_tokens,
      COALESCE(o.reasoning_tokens,    mr.reasoning_tokens)            AS reasoning_tokens,
      COALESCE(o.total_tokens,        mr.total_tokens)                AS total_tokens,
      CASE
        WHEN o.total_tokens  IS NOT NULL THEN 'measured_otel'
        WHEN mr.total_tokens IS NOT NULL THEN 'modelled_estimate'
        ELSE 'none'
      END                                                             AS token_source,

      -- ---- cost block (CONTRACT §4) ----
      -- NULL propagates deliberately. cr.cost_usd is already NULL when any call in the
      -- run was unpriced; falling through to the modelled figure in that case would
      -- mix bases within one number, so the fallback applies only when there was no
      -- measured token data AT ALL.
      CASE
        WHEN o.total_tokens IS NOT NULL THEN cr.cost_usd
        WHEN mr.total_tokens IS NOT NULL THEN mr.cost_usd
        ELSE NULL
      END                                                             AS cost_usd,
      CASE
        WHEN o.total_tokens  IS NOT NULL THEN 'measured'
        WHEN mr.total_tokens IS NOT NULL THEN 'modelled'
        ELSE NULL      -- no tokens observed => no basis to claim. Not 'measured'.
      END                                                             AS cost_basis,
      CASE
        WHEN o.total_tokens  IS NOT NULL THEN COALESCE(cr.cost_is_placeholder, FALSE)
        WHEN mr.total_tokens IS NOT NULL THEN COALESCE(mr.cost_is_placeholder, FALSE)
        ELSE FALSE
      END                                                             AS cost_is_placeholder,
      CASE
        WHEN o.total_tokens  IS NOT NULL THEN cr.pricing_effective_from
        WHEN mr.total_tokens IS NOT NULL THEN mr.pricing_effective_from
        ELSE NULL
      END                                                             AS pricing_effective_from,

      -- ---- activity ----
      COALESCE(o.tool_call_count,  tf.tool_call_count,  0)            AS tool_call_count,
      COALESCE(o.tool_error_count, tf.tool_error_count, 0)            AS tool_error_count,
      COALESCE(o.model_call_count, mr.model_call_count,  0)           AS model_call_count,
      COALESCE(o.retry_count,      mr.retry_count,       0)           AS retry_count,
      COALESCE(t.phases_completed, ph.phase_ok_count,    0)           AS phases_completed,
      COALESCE(ph.phase_failed_count, 0)                              AS phase_failed_count,

      COALESCE(h.human_turns_total,         0)                        AS human_turns_total,
      COALESCE(h.human_turns_correction,    0)                        AS human_turns_correction,
      COALESCE(h.human_turns_rejection,     0)                        AS human_turns_rejection,
      COALESCE(h.human_turns_approval,      0)                        AS human_turns_approval,
      COALESCE(h.human_turns_clarification, 0)                        AS human_turns_clarification,
      COALESCE(h.human_turn_chars,          0)                        AS human_turn_chars,

      g.gate_results,
      COALESCE(gf.gate_pass_count, 0)                                 AS gate_pass_count,
      COALESCE(gf.gate_fail_count, 0)                                 AS gate_fail_count,
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
    LEFT JOIN run_terminal         AS t  USING (run_id)
    LEFT JOIN otel_rollup          AS o  USING (run_id)
    LEFT JOIN cost_rollup          AS cr USING (run_id)
    LEFT JOIN modelled_rollup      AS mr USING (run_id)
    LEFT JOIN human_turn_rollup    AS h  USING (run_id)
    LEFT JOIN gate_rollup          AS g  USING (run_id)
    LEFT JOIN gate_final           AS gf USING (run_id)
    LEFT JOIN phase_rollup         AS ph USING (run_id)
    LEFT JOIN tool_rollup_fallback AS tf USING (run_id)
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
  parent_run_id             = src.parent_run_id,
  is_root_run               = src.is_root_run,
  workflow_id               = src.workflow_id,
  otel_conversation_id      = src.otel_conversation_id,
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
  total_tokens              = src.total_tokens,
  token_source              = src.token_source,
  cost_usd                  = src.cost_usd,
  cost_basis                = src.cost_basis,
  cost_is_placeholder       = src.cost_is_placeholder,
  pricing_effective_from    = src.pricing_effective_from,
  tool_call_count           = src.tool_call_count,
  tool_error_count          = src.tool_error_count,
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
  run_id, trace_id, parent_run_id, is_root_run, workflow_id,
  otel_conversation_id, otel_trace_id, otel_span_count,
  started_at, ended_at, duration_ms,
  person_id, person_email_hash, team_id, role,
  jira_issue_key, delivery_ticket_key, jira_project_key, repo_full_name,
  branch_name, product_profile, environment,
  agent_name, agent_version, agent_kind, skill_name, skill_version, surface,
  model_declared_id, model_id, model_drift, invocation_mode, input_source,
  input_tokens, output_tokens, cached_input_tokens, reasoning_tokens, total_tokens,
  token_source, cost_usd, cost_basis, cost_is_placeholder, pricing_effective_from,
  tool_call_count, tool_error_count, model_call_count, retry_count,
  phases_completed, phase_failed_count,
  human_turns_total, human_turns_correction, human_turns_rejection,
  human_turns_approval, human_turns_clarification, human_turn_chars,
  gate_results, gate_pass_count, gate_fail_count, gate_auto_fix_attempts,
  max_coverage_pct,
  terminal_status, failure_class, dependency_failed, timeout_policy,
  link_method, link_confidence, schema_version, transformed_at
)
VALUES (
  src.run_id, src.trace_id, src.parent_run_id, src.is_root_run, src.workflow_id,
  src.otel_conversation_id, src.otel_trace_id, src.otel_span_count,
  src.started_at, src.ended_at, src.duration_ms,
  src.person_id, src.person_email_hash, src.team_id, src.role,
  src.jira_issue_key, src.delivery_ticket_key, src.jira_project_key, src.repo_full_name,
  src.branch_name, src.product_profile, src.environment,
  src.agent_name, src.agent_version, src.agent_kind, src.skill_name, src.skill_version, src.surface,
  src.model_declared_id, src.model_id, src.model_drift, src.invocation_mode, src.input_source,
  src.input_tokens, src.output_tokens, src.cached_input_tokens, src.reasoning_tokens, src.total_tokens,
  src.token_source, src.cost_usd, src.cost_basis, src.cost_is_placeholder, src.pricing_effective_from,
  src.tool_call_count, src.tool_error_count, src.model_call_count, src.retry_count,
  src.phases_completed, src.phase_failed_count,
  src.human_turns_total, src.human_turns_correction, src.human_turns_rejection,
  src.human_turns_approval, src.human_turns_clarification, src.human_turn_chars,
  src.gate_results, src.gate_pass_count, src.gate_fail_count, src.gate_auto_fix_attempts,
  src.max_coverage_pct,
  src.terminal_status, src.failure_class, src.dependency_failed, src.timeout_policy,
  src.link_method, src.link_confidence, src.schema_version, src.transformed_at
);
