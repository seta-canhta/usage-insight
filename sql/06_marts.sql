-- =====================================================================================
-- 06_marts.sql — marts.agg_daily_person_agent
-- =====================================================================================
-- Contract:  schema/CONTRACT.md §6 (AR-4, AR-5, AR-8), §7 (names)
-- Design:    docs/spikes/ai-effectiveness-observability.md §6.4, §9.5, §10.1, §11.2,
--            §11.4, §11.5
--
-- Grain: day × person × agent × project (design §6.4).
-- Retention: 37 months = 1130 days (design §11.2). Aggregated, low-sensitivity, cheap,
-- and it supports three-year executive trends.
--
-- ┌───────────────────────────────────────────────────────────────────────────────────┐
-- │ ⭐ K-ANONYMITY GUARD — design §11.5 point 3, §9.5 "suppress small denominators"    │
-- │                                                                                   │
-- │ Any group with fewer than K_MIN (5) runs EXPOSES ITS COUNTS but has every ratio    │
-- │ and percentage column NULLed out.                                                 │
-- │                                                                                   │
-- │ This is deliberately two controls in one:                                         │
-- │   • Privacy — a rate computed over 2 runs is effectively a statement about one     │
-- │     identifiable person on one identifiable afternoon.                             │
-- │   • Statistics — "100% acceptance" over 1 run is noise rendered as a headline.     │
-- │     §9.5 requires rolling means over point values for exactly this reason.         │
-- │                                                                                   │
-- │ Counts are kept, NOT suppressed, so the row still shows there WAS activity. A      │
-- │ dashboard that hides its own gaps is worse than no dashboard (§9.4).               │
-- │                                                                                   │
-- │ ⚠ Downstream consumers MUST NOT reconstruct a suppressed ratio by dividing the     │
-- │ exposed counts. If a view needs rates at a coarser grain, it must re-aggregate     │
-- │ the counts to that grain and re-apply the k >= 5 test there — never divide these   │
-- │ ones. The counts are published for volume reporting and audit, not as a numerator  │
-- │ and denominator pair.                                                             │
-- └───────────────────────────────────────────────────────────────────────────────────┘
--
-- Access: this dataset is the ONLY layer readable by all of engineering (design §11.4),
-- which is precisely why the k-guard lives here rather than in a dashboard filter.
--
-- Substitute ${PROJECT_ID}. Run after 04 and 05.
-- =====================================================================================

-- Script variables. BigQuery requires DECLARE to precede every other statement in a
-- script, so they sit here rather than next to the rebuild block that uses them.
DECLARE rebuild_days INT64 DEFAULT 45;  -- must exceed 30d revert + 7d maturity window
DECLARE k_min_runs   INT64 DEFAULT 5;   -- design §11.5 point 3, §9.5


CREATE SCHEMA IF NOT EXISTS `${PROJECT_ID}.marts`
OPTIONS (
  location    = 'EU',
  description = 'Pre-aggregated dashboard feed. k-anonymity >= 5 enforced on every rate column (design §11.5). 37-month retention (design §11.2). Access: all engineering (design §11.4).'
);


CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.marts.agg_daily_person_agent`
(
  -- ---- grain ----
  day                        DATE   NOT NULL OPTIONS (description = 'PARTITION KEY (CONTRACT §7). DATE(started_at) of the run, UTC.'),
  person_id                  STRING          OPTIONS (description = 'Atlassian accountId. NULL = identity unresolved (DQ-1); such rows are surfaced as an explicit "unresolved" bucket, never dropped.'),
  team_id                    STRING          OPTIONS (description = 'Team as at the run date.'),
  agent_name                 STRING          OPTIONS (description = 'Bounded dimension. Design §9.1 comparison arm.'),
  jira_project_key           STRING          OPTIONS (description = 'Project. Bounded dimension.'),

  -- ---- volume ----
  run_count                  INT64           OPTIONS (description = 'Runs started this day. THE K-ANONYMITY DENOMINATOR: run_count < 5 NULLs every ratio column on this row.'),
  root_run_count             INT64           OPTIONS (description = 'AR-4: runs with no parent. Sub-agent runs are counted in run_count but a workflow-level count must use this.'),
  distinct_trace_count       INT64           OPTIONS (description = 'AR-4: distinct workflows. The correct denominator for anything phrased "per piece of work".'),
  distinct_model_count       INT64           OPTIONS (description = 'Models used. > 1 means this row mixes cost bases across models — do not read cost_usd as a single-model figure.'),

  -- ---- run outcomes (§7.8) ----
  run_completed_count        INT64           OPTIONS (description = 'terminal_status = completed.'),
  run_failed_count           INT64           OPTIONS (description = 'terminal_status = failed.'),
  run_timeout_count          INT64           OPTIONS (description = 'terminal_status = timeout.'),
  run_abandoned_count        INT64           OPTIONS (description = 'terminal_status = abandoned (DQ-2).'),
  run_in_flight_count        INT64           OPTIONS (description = 'No terminal event yet.'),
  dep_fail_vpn_count         INT64           OPTIONS (description = 'dependency_failed = vpn. Directly answers the brief question about VPN interruptions.'),
  dep_fail_network_count     INT64           OPTIONS (description = 'dependency_failed = network.'),
  dep_fail_external_count    INT64           OPTIONS (description = 'dependency_failed IN (jira, bitbucket, mcp, aio, ci) — external service failures.'),

  -- ---- tokens and cost ----
  input_tokens               INT64           OPTIONS (description = 'SUM. NULL-preserving: NULL means no run this day reported usage, not zero usage.'),
  output_tokens              INT64           OPTIONS (description = 'SUM.'),
  cached_input_tokens        INT64           OPTIONS (description = 'SUM. Subset of input_tokens.'),
  reasoning_tokens           INT64           OPTIONS (description = 'SUM. Subset of output_tokens.'),
  total_tokens               INT64           OPTIONS (description = 'SUM of the derived per-run totals (input + output). Copilot emits no total_tokens (design §4.4a).'),

  token_cost_usd             NUMERIC         OPTIONS (description = 'SUM of fct_ai_run.cost_usd over runs that HAVE a cost. NULL when no run had one. See runs_with_cost_count before reading this as complete.'),
  runs_with_cost_count       INT64           OPTIONS (description = 'Runs whose cost_usd is non-NULL. token_cost_usd is only complete when this equals run_count — otherwise the figure is a floor, not a total.'),
  runs_unpriced_count        INT64           OPTIONS (description = 'Runs with tokens but NULL cost — the model was not in dim_model_pricing (CONTRACT §4, DQ-6).'),
  cost_basis_measured_count  INT64           OPTIONS (description = 'Runs with cost_basis = measured.'),
  cost_basis_modelled_count  INT64           OPTIONS (description = 'Runs with cost_basis = modelled (±40% accuracy, §8.2). §8.1 requires modelled figures to be rendered visually distinct — this column is how a dashboard knows to do it.'),
  cost_is_placeholder_count  INT64           OPTIONS (description = 'Runs priced from a dim_model_pricing row flagged is_placeholder. ANY non-zero value here means the cost figures on this row are NOT usable for chargeback.'),

  allocated_seat_cost_usd    NUMERIC         OPTIONS (description = 'AR-8: seat cost allocated once per person-month, PRO RATA BY RUN COUNT (§8.12 allocation rule) — never by token share, or a light user with one expensive run absorbs a whole seat. NULL until a procurement seat-cost source exists (design §8.1 [A]); it is not in this repository.'),
  allocated_infra_cost_usd   NUMERIC         OPTIONS (description = 'AR-8: infra cost allocated the same way, from the GCP billing export (design §8.3). NULL until wired.'),
  total_cost_usd             NUMERIC         OPTIONS (description = 'token + seat + infra (§8.12). NULL while the seat/infra terms are unavailable — a partial total presented as a total is worse than no total.'),

  -- ---- activity ----
  tool_call_count            INT64           OPTIONS (description = 'SUM.'),
  tool_error_count           INT64           OPTIONS (description = 'SUM.'),
  model_call_count           INT64           OPTIONS (description = 'SUM.'),
  retry_count                INT64           OPTIONS (description = 'SUM. §7.8 retry rate.'),
  runs_with_retry_count      INT64           OPTIONS (description = 'Runs with retry_count >= 1. §7.8 retry rate numerator.'),
  total_duration_ms          INT64           OPTIONS (description = 'SUM of run durations.'),

  -- ---- human intervention (§8.11) ----
  -- Counted, never pre-ratioed, and split so the §8.11 exclusion survives to the mart.
  human_turns_correction     INT64           OPTIONS (description = 'SUM. COUNTS as intervention (§8.11).'),
  human_turns_rejection      INT64           OPTIONS (description = 'SUM. COUNTS as intervention (§8.11).'),
  human_turns_approval       INT64           OPTIONS (description = 'SUM. EXCLUDED from intervention BY DESIGN (§8.11) — the agents deliberately ask for approval; counting a designed gate would punish correct behaviour.'),
  human_turns_clarification  INT64           OPTIONS (description = 'SUM. EXCLUDED from intervention BY DESIGN (§8.11).'),
  intervened_run_count       INT64           OPTIONS (description = 'Runs where correction_turns >= 1 OR gate_auto_fix_attempts >= 1 (§8.11 numerator). correction_turns = correction + rejection only.'),

  -- ---- gates ----
  gate_pass_count            INT64           OPTIONS (description = 'SUM of gates passing on their final attempt.'),
  gate_fail_count            INT64           OPTIONS (description = 'SUM of gates failing on their final attempt.'),
  gate_auto_fix_attempts     INT64           OPTIONS (description = 'SUM of gate re-runs (attempt_index > 0). The agent fixing itself. NOT rework (§8.9).'),
  avg_max_coverage_pct       FLOAT64         OPTIONS (description = 'Mean of each run max coverage_pct. K-GUARDED.'),

  -- ---- outputs and acceptance (CONTRACT §5) ----
  -- AR-5: reused artifacts are EXCLUDED from every count below except output_reused_count.
  output_generated_count     INT64           OPTIONS (description = 'Outputs generated (AR-5 excludes reused).'),
  output_reused_count        INT64           OPTIONS (description = 'AR-5: artifacts suppressed/merged by the dedup checker. Feed reuse metrics; contribute ZERO to output volume.'),
  output_accepted_count      INT64           OPTIONS (description = 'acceptance_state = accepted.'),
  output_reworked_count      INT64           OPTIONS (description = 'acceptance_state = reworked. MERGED IS NOT ACCEPTED (design §9.2).'),
  output_rejected_count      INT64           OPTIONS (description = 'acceptance_state = rejected.'),
  output_reverted_count      INT64           OPTIONS (description = 'acceptance_state = reverted (AR-9).'),
  output_in_flight_count     INT64           OPTIONS (description = 'acceptance_state = in_flight. EXCLUDED from acceptance denominators (CONTRACT §5 maturity window).'),
  output_terminal_count      INT64           OPTIONS (description = 'accepted + reworked + rejected + reverted. THE acceptance-rate denominator (§8.7).'),
  lines_generated            INT64           OPTIONS (description = 'SUM of lines_generated over non-reused outputs.'),

  -- ---- rates (⭐ ALL NULLED WHEN run_count < 5) ----
  ai_acceptance_rate_pct     FLOAT64         OPTIONS (description = '§8.7: accepted / terminal × 100, EXPLICIT LINKS ONLY. NULL when run_count < 5 (k-anonymity, design §11.5).'),
  rework_rate_pct            FLOAT64         OPTIONS (description = '§8.9: mean post_review_change_ratio × 100. NULL when run_count < 5.'),
  manual_intervention_rate_pct FLOAT64       OPTIONS (description = '§8.11: intervened_run_count / run_count × 100, EXCLUDING approval and clarification turns. ⚠ §8.11 mandates rendering this segmented by tenure and never as a bare person ranking — a high rate on a junior engineer is a signal about AGENT QUALITY, not the person. NULL when run_count < 5.'),
  run_success_rate_pct       FLOAT64         OPTIONS (description = '§7.8: completed / (completed+failed+timeout+abandoned) × 100. NULL when run_count < 5.'),
  timeout_rate_pct           FLOAT64         OPTIONS (description = '§7.8: timeout / run_count × 100. NULL when run_count < 5.'),
  retry_rate_pct             FLOAT64         OPTIONS (description = '§7.8: runs_with_retry / run_count × 100. NULL when run_count < 5.'),
  gate_pass_rate_pct         FLOAT64         OPTIONS (description = 'gate_pass / (pass+fail) × 100. NULL when run_count < 5.'),
  cost_per_accepted_output_usd NUMERIC       OPTIONS (description = '§8.12: token_cost_usd / accepted outputs. NULL when run_count < 5, when cost is unknown, or when there are no accepted outputs. Denominator honesty (§8.12): runs producing NO output still count in the numerator — excluding failed runs from cost would understate the true cost of AI.'),

  -- ---- completeness (design §9.4: "publish a completeness score on every dashboard") ----
  explicit_link_run_count    INT64           OPTIONS (description = 'Runs with link_method = explicit.'),
  explicit_link_pct          FLOAT64         OPTIONS (description = '% of runs explicitly linked. THE completeness score (§9.4). NOT k-guarded — a reader must always be able to see how much of the picture is missing, and this number is about data plumbing, not about a person.'),
  runs_with_tokens_pct       FLOAT64         OPTIONS (description = '% of runs with non-NULL total_tokens — i.e. how much of the day OTel actually saw. Not k-guarded, same reasoning.'),

  -- ---- guard metadata ----
  k_anonymity_applied        BOOL   NOT NULL OPTIONS (description = 'TRUE when run_count < 5 and the rate columns above were suppressed to NULL. Renderers MUST show "n = <run_count>" instead of a rate on these rows (design §9.5).'),
  k_min                      INT64           OPTIONS (description = 'The k threshold in force when this row was built (5). Recorded so a historical row stays interpretable if the policy ever changes.'),
  built_at                   TIMESTAMP       OPTIONS (description = 'Build timestamp. Restatements must be visible, never silent (design §9.5).')
)
PARTITION BY day
OPTIONS (
  partition_expiration_days = 1130,
  description = 'Daily person × agent × project aggregate (design §6.4). Partition on day (CONTRACT §7). 37-month retention (design §11.2). ⭐ K-ANONYMITY: rows with run_count < 5 expose counts but have EVERY ratio/percentage column NULLed (design §11.5, §9.5). Do not reconstruct suppressed rates by dividing the exposed counts.',
  labels = [('layer', 'mart'), ('domain', 'ai-telemetry'), ('privacy', 'k-anon-5')]
);


-- =====================================================================================
-- REBUILD
-- =====================================================================================
-- DELETE + INSERT over a trailing window rather than MERGE, because the grain columns
-- are nullable (person_id can be NULL when identity resolution fails) and NULL never
-- equals NULL in a MERGE ON clause — such rows would be inserted again on every run.
--
-- The window must exceed the 30-day revert window plus the 7-day maturity window, or
-- an output that flips to 'reverted' on day 29 would never be reflected here.
-- (rebuild_days and k_min_runs are declared at the top of this file.)
-- =====================================================================================
DELETE FROM `${PROJECT_ID}.marts.agg_daily_person_agent`
WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL rebuild_days DAY);

-- Column list is explicit rather than positional: this INSERT has ~70 columns and a
-- positional mapping would silently shift every value if a column were ever added to
-- the DDL above.
INSERT INTO `${PROJECT_ID}.marts.agg_daily_person_agent`
(
  day, person_id, team_id, agent_name, jira_project_key,
  run_count, root_run_count, distinct_trace_count, distinct_model_count,
  run_completed_count, run_failed_count, run_timeout_count, run_abandoned_count,
  run_in_flight_count, dep_fail_vpn_count, dep_fail_network_count, dep_fail_external_count,
  input_tokens, output_tokens, cached_input_tokens, reasoning_tokens, total_tokens,
  token_cost_usd, runs_with_cost_count, runs_unpriced_count,
  cost_basis_measured_count, cost_basis_modelled_count, cost_is_placeholder_count,
  allocated_seat_cost_usd, allocated_infra_cost_usd, total_cost_usd,
  tool_call_count, tool_error_count, model_call_count, retry_count,
  runs_with_retry_count, total_duration_ms,
  human_turns_correction, human_turns_rejection, human_turns_approval,
  human_turns_clarification, intervened_run_count,
  gate_pass_count, gate_fail_count, gate_auto_fix_attempts, avg_max_coverage_pct,
  output_generated_count, output_reused_count, output_accepted_count,
  output_reworked_count, output_rejected_count, output_reverted_count,
  output_in_flight_count, output_terminal_count, lines_generated,
  ai_acceptance_rate_pct, rework_rate_pct, manual_intervention_rate_pct,
  run_success_rate_pct, timeout_rate_pct, retry_rate_pct, gate_pass_rate_pct,
  cost_per_accepted_output_usd,
  explicit_link_run_count, explicit_link_pct, runs_with_tokens_pct,
  k_anonymity_applied, k_min, built_at
)
WITH
-- -------------------------------------------------------------------------------------
-- Run-side aggregate.
-- -------------------------------------------------------------------------------------
run_agg AS (
  SELECT
    DATE(r.started_at)                                          AS day,
    r.person_id,
    r.team_id,
    r.agent_name,
    r.jira_project_key,

    COUNT(*)                                                    AS run_count,
    COUNTIF(r.is_root_run)                                      AS root_run_count,
    COUNT(DISTINCT r.trace_id)                                  AS distinct_trace_count,
    COUNT(DISTINCT r.model_id)                                  AS distinct_model_count,

    COUNTIF(r.terminal_status = 'completed')                    AS run_completed_count,
    COUNTIF(r.terminal_status = 'failed')                       AS run_failed_count,
    COUNTIF(r.terminal_status = 'timeout')                      AS run_timeout_count,
    COUNTIF(r.terminal_status = 'abandoned')                    AS run_abandoned_count,
    COUNTIF(r.terminal_status = 'in_flight')                    AS run_in_flight_count,
    COUNTIF(r.dependency_failed = 'vpn')                        AS dep_fail_vpn_count,
    COUNTIF(r.dependency_failed = 'network')                    AS dep_fail_network_count,
    COUNTIF(r.dependency_failed IN ('jira','bitbucket','mcp','aio','ci'))
                                                                AS dep_fail_external_count,

    -- NULL-preserving. SUM() over all-NULL returns NULL, which is exactly right: it
    -- means "no run reported usage", not "usage was zero".
    SUM(r.input_tokens)                                         AS input_tokens,
    SUM(r.output_tokens)                                        AS output_tokens,
    SUM(r.cached_input_tokens)                                  AS cached_input_tokens,
    SUM(r.reasoning_tokens)                                     AS reasoning_tokens,
    SUM(r.total_tokens)                                         AS total_tokens,

    SUM(r.cost_usd)                                             AS token_cost_usd,
    COUNTIF(r.cost_usd IS NOT NULL)                             AS runs_with_cost_count,
    COUNTIF(r.total_tokens IS NOT NULL AND r.cost_usd IS NULL)  AS runs_unpriced_count,
    COUNTIF(r.cost_basis = 'measured')                          AS cost_basis_measured_count,
    COUNTIF(r.cost_basis = 'modelled')                          AS cost_basis_modelled_count,
    COUNTIF(COALESCE(r.cost_is_placeholder, FALSE))             AS cost_is_placeholder_count,

    SUM(r.tool_call_count)                                      AS tool_call_count,
    SUM(r.tool_error_count)                                     AS tool_error_count,
    SUM(r.model_call_count)                                     AS model_call_count,
    SUM(r.retry_count)                                          AS retry_count,
    COUNTIF(r.retry_count >= 1)                                 AS runs_with_retry_count,
    SUM(r.duration_ms)                                          AS total_duration_ms,

    SUM(r.human_turns_correction)                               AS human_turns_correction,
    SUM(r.human_turns_rejection)                                AS human_turns_rejection,
    SUM(r.human_turns_approval)                                 AS human_turns_approval,
    SUM(r.human_turns_clarification)                            AS human_turns_clarification,
    -- §8.11 numerator. correction_turns = correction + rejection ONLY. Approval and
    -- clarification are deliberately absent — see the column comments above.
    COUNTIF((COALESCE(r.human_turns_correction, 0)
             + COALESCE(r.human_turns_rejection, 0)) >= 1
            OR COALESCE(r.gate_auto_fix_attempts, 0) >= 1)      AS intervened_run_count,

    SUM(r.gate_pass_count)                                      AS gate_pass_count,
    SUM(r.gate_fail_count)                                      AS gate_fail_count,
    SUM(r.gate_auto_fix_attempts)                               AS gate_auto_fix_attempts,
    AVG(r.max_coverage_pct)                                     AS avg_max_coverage_pct,

    COUNTIF(r.link_method = 'explicit')                         AS explicit_link_run_count,
    COUNTIF(r.total_tokens IS NOT NULL)                         AS runs_with_tokens_count
  FROM `${PROJECT_ID}.core.fct_ai_run` AS r
  WHERE DATE(r.started_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL rebuild_days DAY)
  GROUP BY day, r.person_id, r.team_id, r.agent_name, r.jira_project_key
),

-- -------------------------------------------------------------------------------------
-- Output-side aggregate.
--
-- Keyed on the OUTPUT's generation day and its own person/agent/project — not on the
-- run's day — so an output generated at 23:58 lands in the day it was produced.
--
-- Exclusions applied here, once, so no downstream consumer has to remember them:
--   * is_quarantined  — DQ-16 / AR-1 breach, blocked from aggregates until resolved.
--   * §8.7 acceptance is computed over link_method = 'explicit' ONLY. A heuristic link
--     cannot reliably attribute an output to a run, so including it inflates the
--     denominator with outputs whose fate is unknown.
--   * AR-5 reused artifacts are counted separately and contribute zero volume.
-- -------------------------------------------------------------------------------------
output_agg AS (
  SELECT
    DATE(o.generated_at)                                        AS day,
    o.person_id,
    o.team_id,
    o.agent_name,
    o.jira_project_key,

    COUNTIF(NOT o.is_reused)                                    AS output_generated_count,
    COUNTIF(o.is_reused)                                        AS output_reused_count,
    COUNTIF(NOT o.is_reused AND o.acceptance_state = 'accepted')   AS output_accepted_count,
    COUNTIF(NOT o.is_reused AND o.acceptance_state = 'reworked')   AS output_reworked_count,
    COUNTIF(NOT o.is_reused AND o.acceptance_state = 'rejected')   AS output_rejected_count,
    COUNTIF(NOT o.is_reused AND o.acceptance_state = 'reverted')   AS output_reverted_count,
    COUNTIF(NOT o.is_reused AND o.acceptance_state = 'in_flight')  AS output_in_flight_count,
    COUNTIF(NOT o.is_reused AND o.is_terminal_state)               AS output_terminal_count,
    SUM(IF(NOT o.is_reused, o.lines_generated, 0))              AS lines_generated,

    -- §8.7 explicit-only acceptance. Kept as its own pair so the k-guard applies to a
    -- ratio built from the SAME filter the metric definition uses.
    COUNTIF(NOT o.is_reused AND o.link_method = 'explicit'
            AND o.acceptance_state = 'accepted')                AS explicit_accepted_count,
    COUNTIF(NOT o.is_reused AND o.link_method = 'explicit'
            AND o.is_terminal_state)                            AS explicit_terminal_count,

    -- §8.9 rework: mean of the per-output post-review change ratio, over MERGED
    -- outputs only. An unmerged output has no meaningful ratio and averaging in its
    -- NULL would silently shrink the denominator.
    AVG(IF(NOT o.is_reused AND o.merged_at IS NOT NULL,
           o.post_review_change_ratio, NULL))                   AS mean_post_review_change_ratio
  FROM `${PROJECT_ID}.core.fct_ai_output` AS o
  WHERE DATE(o.generated_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL rebuild_days DAY)
    AND NOT COALESCE(o.is_quarantined, FALSE)   -- DQ-16 / AR-1
  GROUP BY day, o.person_id, o.team_id, o.agent_name, o.jira_project_key
),

-- FULL OUTER JOIN: a day can have runs with no outputs (a failed run still costs
-- money — §8.12 denominator honesty) and, more rarely, outputs whose run row has
-- aged out of the rebuild window. Dropping either side would bias cost downward.
combined AS (
  SELECT
    COALESCE(r.day, o.day)                           AS day,
    COALESCE(r.person_id, o.person_id)               AS person_id,
    COALESCE(r.team_id, o.team_id)                   AS team_id,
    COALESCE(r.agent_name, o.agent_name)             AS agent_name,
    COALESCE(r.jira_project_key, o.jira_project_key) AS jira_project_key,
    r.* EXCEPT (day, person_id, team_id, agent_name, jira_project_key),
    o.* EXCEPT (day, person_id, team_id, agent_name, jira_project_key)
  FROM run_agg AS r
  FULL OUTER JOIN output_agg AS o
    ON  r.day              = o.day
    AND r.person_id        IS NOT DISTINCT FROM o.person_id
    AND r.team_id          IS NOT DISTINCT FROM o.team_id
    AND r.agent_name       IS NOT DISTINCT FROM o.agent_name
    AND r.jira_project_key IS NOT DISTINCT FROM o.jira_project_key
),

-- -------------------------------------------------------------------------------------
-- ⭐ K-ANONYMITY GUARD
-- -------------------------------------------------------------------------------------
-- One boolean, computed once, applied to every rate column. Keeping the test in a
-- single CTE column rather than repeating `IF(run_count < 5, ...)` in nine places is
-- deliberate: the guard must be impossible to forget on a newly added rate.
guarded AS (
  SELECT
    c.*,
    (COALESCE(c.run_count, 0) < k_min_runs) AS k_suppressed
  FROM combined AS c
)

SELECT
  g.day,
  g.person_id,
  g.team_id,
  g.agent_name,
  g.jira_project_key,

  COALESCE(g.run_count, 0)              AS run_count,
  COALESCE(g.root_run_count, 0)         AS root_run_count,
  COALESCE(g.distinct_trace_count, 0)   AS distinct_trace_count,
  COALESCE(g.distinct_model_count, 0)   AS distinct_model_count,

  COALESCE(g.run_completed_count, 0)    AS run_completed_count,
  COALESCE(g.run_failed_count, 0)       AS run_failed_count,
  COALESCE(g.run_timeout_count, 0)      AS run_timeout_count,
  COALESCE(g.run_abandoned_count, 0)    AS run_abandoned_count,
  COALESCE(g.run_in_flight_count, 0)    AS run_in_flight_count,
  COALESCE(g.dep_fail_vpn_count, 0)     AS dep_fail_vpn_count,
  COALESCE(g.dep_fail_network_count, 0) AS dep_fail_network_count,
  COALESCE(g.dep_fail_external_count, 0) AS dep_fail_external_count,

  -- Token sums stay NULL-preserving on purpose (see run_agg).
  g.input_tokens,
  g.output_tokens,
  g.cached_input_tokens,
  g.reasoning_tokens,
  g.total_tokens,

  g.token_cost_usd,
  COALESCE(g.runs_with_cost_count, 0)      AS runs_with_cost_count,
  COALESCE(g.runs_unpriced_count, 0)       AS runs_unpriced_count,
  COALESCE(g.cost_basis_measured_count, 0) AS cost_basis_measured_count,
  COALESCE(g.cost_basis_modelled_count, 0) AS cost_basis_modelled_count,
  COALESCE(g.cost_is_placeholder_count, 0) AS cost_is_placeholder_count,

  -- AR-8 seat/infra allocation. NULL until a procurement seat-cost feed and the GCP
  -- billing join exist (design §8.1 [A], §8.3). Left explicitly NULL rather than 0:
  -- a zero seat cost is a claim, and it is a false one.
  CAST(NULL AS NUMERIC)                    AS allocated_seat_cost_usd,
  CAST(NULL AS NUMERIC)                    AS allocated_infra_cost_usd,
  CAST(NULL AS NUMERIC)                    AS total_cost_usd,

  COALESCE(g.tool_call_count, 0)     AS tool_call_count,
  COALESCE(g.tool_error_count, 0)    AS tool_error_count,
  COALESCE(g.model_call_count, 0)    AS model_call_count,
  COALESCE(g.retry_count, 0)         AS retry_count,
  COALESCE(g.runs_with_retry_count, 0) AS runs_with_retry_count,
  COALESCE(g.total_duration_ms, 0)   AS total_duration_ms,

  COALESCE(g.human_turns_correction, 0)    AS human_turns_correction,
  COALESCE(g.human_turns_rejection, 0)     AS human_turns_rejection,
  COALESCE(g.human_turns_approval, 0)      AS human_turns_approval,
  COALESCE(g.human_turns_clarification, 0) AS human_turns_clarification,
  COALESCE(g.intervened_run_count, 0)      AS intervened_run_count,

  COALESCE(g.gate_pass_count, 0)        AS gate_pass_count,
  COALESCE(g.gate_fail_count, 0)        AS gate_fail_count,
  COALESCE(g.gate_auto_fix_attempts, 0) AS gate_auto_fix_attempts,
  -- avg_max_coverage_pct is a mean, i.e. a rate-like statistic about individuals.
  -- K-GUARDED.
  IF(g.k_suppressed, NULL, g.avg_max_coverage_pct) AS avg_max_coverage_pct,

  COALESCE(g.output_generated_count, 0) AS output_generated_count,
  COALESCE(g.output_reused_count, 0)    AS output_reused_count,
  COALESCE(g.output_accepted_count, 0)  AS output_accepted_count,
  COALESCE(g.output_reworked_count, 0)  AS output_reworked_count,
  COALESCE(g.output_rejected_count, 0)  AS output_rejected_count,
  COALESCE(g.output_reverted_count, 0)  AS output_reverted_count,
  COALESCE(g.output_in_flight_count, 0) AS output_in_flight_count,
  COALESCE(g.output_terminal_count, 0)  AS output_terminal_count,
  COALESCE(g.lines_generated, 0)        AS lines_generated,

  -- =====================================================================================
  -- RATE COLUMNS — every one of these is NULL when run_count < 5.
  -- =====================================================================================
  IF(g.k_suppressed, NULL,
     SAFE_DIVIDE(g.explicit_accepted_count, NULLIF(g.explicit_terminal_count, 0)) * 100
  )                                            AS ai_acceptance_rate_pct,

  IF(g.k_suppressed, NULL,
     g.mean_post_review_change_ratio * 100
  )                                            AS rework_rate_pct,

  IF(g.k_suppressed, NULL,
     SAFE_DIVIDE(g.intervened_run_count, NULLIF(g.run_count, 0)) * 100
  )                                            AS manual_intervention_rate_pct,

  IF(g.k_suppressed, NULL,
     SAFE_DIVIDE(
       g.run_completed_count,
       NULLIF(g.run_completed_count + g.run_failed_count
              + g.run_timeout_count + g.run_abandoned_count, 0)) * 100
  )                                            AS run_success_rate_pct,

  IF(g.k_suppressed, NULL,
     SAFE_DIVIDE(g.run_timeout_count, NULLIF(g.run_count, 0)) * 100
  )                                            AS timeout_rate_pct,

  IF(g.k_suppressed, NULL,
     SAFE_DIVIDE(g.runs_with_retry_count, NULLIF(g.run_count, 0)) * 100
  )                                            AS retry_rate_pct,

  IF(g.k_suppressed, NULL,
     SAFE_DIVIDE(g.gate_pass_count, NULLIF(g.gate_pass_count + g.gate_fail_count, 0)) * 100
  )                                            AS gate_pass_rate_pct,

  -- §8.12. NUMERIC division stays NUMERIC. NULL whenever cost is unknown — an
  -- unpriced model must never surface as a cheap one (CONTRACT §4).
  IF(g.k_suppressed, NULL,
     CAST(SAFE_DIVIDE(g.token_cost_usd, NULLIF(g.output_accepted_count, 0)) AS NUMERIC)
  )                                            AS cost_per_accepted_output_usd,

  -- Completeness (design §9.4). NOT k-guarded: it describes the data pipeline, not a
  -- person, and a reader must always be able to see how much of the picture is missing.
  COALESCE(g.explicit_link_run_count, 0)       AS explicit_link_run_count,
  SAFE_DIVIDE(g.explicit_link_run_count, NULLIF(g.run_count, 0)) * 100
                                               AS explicit_link_pct,
  SAFE_DIVIDE(g.runs_with_tokens_count, NULLIF(g.run_count, 0)) * 100
                                               AS runs_with_tokens_pct,

  g.k_suppressed                               AS k_anonymity_applied,
  k_min_runs                                   AS k_min,
  CURRENT_TIMESTAMP()                          AS built_at
FROM guarded AS g;
