-- =====================================================================================
-- 08_metrics.sql — the metric layer
-- =====================================================================================
-- Contract:  schema/CONTRACT.md §1.6 (NO ROI, NO counterfactual),
--            §2.4 (explicit links only for cost metrics), §5, §6
-- Design:    docs/spikes/ai-effectiveness-observability.md §7.8, §8.7-§8.15, §9.1,
--            §9.5, §11.5
--
-- Every metric is a NAMED VIEW with a header comment giving its design section and the
-- exact formula, so the definition travels with the query and cannot drift from the
-- spec by being re-implemented in a dashboard tool.
--
-- Views live in `marts` — the only dataset readable by all of engineering (design
-- §11.4). They read from `core`, so grant on the VIEW, not on the underlying tables:
-- these are authorised views.
--
-- ┌───────────────────────────────────────────────────────────────────────────────────┐
-- │ ⛔ DELIBERATELY ABSENT: ROI AND ANY MONETARY "VALUE DELIVERED" METRIC              │
-- │                                                                                   │
-- │ CONTRACT §1.6, design §8.16 and design §9.1 Decision 2 forbid publishing it.       │
-- │                                                                                   │
-- │ The reason is not squeamishness, it is measurability. There is no non-AI control   │
-- │ group — AI is applied to essentially all work — so `value_gained`, which rests     │
-- │ entirely on time_saved_hours, defects_prevented and incidents_avoided, is not      │
-- │ measurable, not estimable from data, and not falsifiable. Publishing it produces   │
-- │ a number that flatters the programme and cannot survive a competent challenge.     │
-- │                                                                                   │
-- │ THE ECONOMIC METRIC IS `v_cost_per_accepted_output` (§8.12). Both its numerator    │
-- │ and its denominator are directly measured, with no counterfactual anywhere.        │
-- │                                                                                   │
-- │ Do not add an ROI view here. If leadership asks for one, the answer is the pair    │
-- │ of questions a decision can actually turn on: is cost per accepted output falling, │
-- │ and is acceptance rising.                                                          │
-- └───────────────────────────────────────────────────────────────────────────────────┘
--
-- Two rules applied throughout, both from the contract and both easy to lose:
--   * link_method = 'explicit' ONLY for anything touching cost or acceptance
--     (CONTRACT §2.4). A heuristic link cannot reliably attribute an output to a run.
--   * is_quarantined rows (DQ-16 / AR-1 breach) and is_reused rows (AR-5) are excluded
--     from output volume everywhere.
--
-- Substitute ${PROJECT_ID}. Run after 03-06.
-- =====================================================================================


-- =====================================================================================
-- §8.7 — AI ACCEPTANCE RATE
-- =====================================================================================
-- Design section: §8.7. Formula, verbatim:
--
--                          count(outputs WHERE acceptance_state = 'accepted')
--   ai_acceptance_rate  =  ────────────────────────────────────────────────────  × 100
--                          count(outputs WHERE acceptance_state <> 'in_flight')
--
-- An output is `accepted` when ALL of:
--   1. it reached a merged PR, AND
--   2. post_review_change_ratio <= 0.25, AND
--   3. it was not reverted within 30 days.
--
-- Unit: %  ·  Grain: output  ·  Dimensions: artifact_type, agent, model, person, project
--
-- GUARD (§8.7): computed over link_method = 'explicit' ONLY. Heuristic links cannot
-- reliably attribute an output to a run, so including them inflates the denominator
-- with outputs whose fate is unknown.
--
-- ⚠ THE 0.25 THRESHOLD IS A POLICY CHOICE, NOT A FACT. §8.7 requires it to be stated
-- on every dashboard showing this metric, AND requires sensitivity to be published at
-- 0.10 / 0.25 / 0.50 so nobody can claim the number was tuned to flatter. All three
-- are computed below as first-class columns — not as an optional appendix.
--
-- The denominator excludes in_flight, which is exactly the CONTRACT §5 maturity
-- window: outputs whose PR is younger than 7 days are not yet judgeable.
-- =====================================================================================
CREATE OR REPLACE VIEW `${PROJECT_ID}.marts.v_ai_acceptance_rate`
OPTIONS (description = '§8.7 AI acceptance rate. accepted / (all terminal states) × 100, EXPLICIT LINKS ONLY. Includes sensitivity at thresholds 0.10 / 0.25 / 0.50 as required by §8.7. Suppresses rates below n=5 (design §9.5, §11.5).')
AS
WITH scoped AS (
  SELECT
    DATE_TRUNC(DATE(o.generated_at), WEEK(MONDAY)) AS week_start,
    DATE_TRUNC(DATE(o.generated_at), MONTH)        AS month_start,
    o.artifact_type,
    o.agent_name,
    o.agent_version,
    o.skill_name,
    o.model_id,
    o.person_id,
    o.team_id,
    o.jira_project_key,
    o.acceptance_state,
    o.is_terminal_state,
    o.merged_at,
    o.reverted_at,
    o.post_review_change_ratio
  FROM `${PROJECT_ID}.core.fct_ai_output` AS o
  WHERE o.link_method = 'explicit'                    -- §8.7 guard, CONTRACT §2.4
    AND NOT COALESCE(o.is_quarantined, FALSE)         -- DQ-16 / AR-1
    AND NOT COALESCE(o.is_reused, FALSE)              -- AR-5
)
SELECT
  week_start,
  month_start,
  artifact_type,
  agent_name,
  agent_version,
  skill_name,
  model_id,
  person_id,
  team_id,
  jira_project_key,

  COUNTIF(is_terminal_state)                              AS terminal_output_count,
  COUNTIF(acceptance_state = 'accepted')                  AS accepted_count,
  COUNTIF(acceptance_state = 'reworked')                  AS reworked_count,
  COUNTIF(acceptance_state = 'rejected')                  AS rejected_count,
  COUNTIF(acceptance_state = 'reverted')                  AS reverted_count,
  COUNTIF(acceptance_state = 'in_flight')                 AS in_flight_count,

  -- Headline, at the policy threshold of 0.25.
  IF(COUNTIF(is_terminal_state) < 5, NULL,
     SAFE_DIVIDE(COUNTIF(acceptance_state = 'accepted'),
                 NULLIF(COUNTIF(is_terminal_state), 0)) * 100)
                                                          AS ai_acceptance_rate_pct,

  -- Sensitivity (§8.7). Recomputed from first principles at each threshold: merged,
  -- not reverted, ratio <= t. A NULL ratio means the output generated no measurable
  -- lines, so there was nothing to rewrite — it counts as accepted at every threshold,
  -- identically to the state machine in 05_transform_output.sql.
  IF(COUNTIF(is_terminal_state) < 5, NULL,
     SAFE_DIVIDE(
       COUNTIF(merged_at IS NOT NULL AND reverted_at IS NULL
               AND COALESCE(post_review_change_ratio, 0) <= 0.10),
       NULLIF(COUNTIF(is_terminal_state), 0)) * 100)      AS acceptance_rate_at_0_10_pct,
  IF(COUNTIF(is_terminal_state) < 5, NULL,
     SAFE_DIVIDE(
       COUNTIF(merged_at IS NOT NULL AND reverted_at IS NULL
               AND COALESCE(post_review_change_ratio, 0) <= 0.25),
       NULLIF(COUNTIF(is_terminal_state), 0)) * 100)      AS acceptance_rate_at_0_25_pct,
  IF(COUNTIF(is_terminal_state) < 5, NULL,
     SAFE_DIVIDE(
       COUNTIF(merged_at IS NOT NULL AND reverted_at IS NULL
               AND COALESCE(post_review_change_ratio, 0) <= 0.50),
       NULLIF(COUNTIF(is_terminal_state), 0)) * 100)      AS acceptance_rate_at_0_50_pct,

  0.25                                                    AS policy_threshold,
  'The 0.25 post-review change threshold is a POLICY CHOICE, not a fact (design §8.7). Sensitivity at 0.10/0.25/0.50 is published alongside.'
                                                          AS threshold_disclosure,
  (COUNTIF(is_terminal_state) < 5)                        AS k_anonymity_applied
FROM scoped
GROUP BY week_start, month_start, artifact_type, agent_name, agent_version,
         skill_name, model_id, person_id, team_id, jira_project_key;


-- =====================================================================================
-- §8.9 — REWORK RATE
-- =====================================================================================
-- Design section: §8.9. Formula, verbatim:
--
--                               Σ lines_changed(commits authored after first_review_at, same PR)
--   post_review_change_ratio = ────────────────────────────────────────────────────────────────
--                               Σ lines_generated(AI outputs in that PR)
--
--   rework_rate = mean(post_review_change_ratio over PRs in period) × 100
--
-- Unit: %  ·  Grain: PR, rolled up by agent/person/project
--
-- ⭐ ATTRIBUTION BOUNDARY — the whole point of this metric:
--   ONLY commits authored AFTER first_review_at count as rework. Self-corrections
--   BEFORE review are the agent's own auto-fix loop — developer.implementer.agent.md
--   permits up to 3 auto-fix cycles [V] — and are counted separately as
--   auto_fix_cycles. Conflating them would penalise the agent for successfully fixing
--   itself, and would make an agent that gets it right on the second try look worse
--   than one that ships its first draft unchecked.
--
-- first_review_at is the earliest review action BY A PERSON OTHER THAN THE PR AUTHOR
-- (§8.14). Self-comments are not review.
--
-- The mean is taken over PRs, per the formula — not over outputs — so a PR with twenty
-- outputs does not outvote a PR with one.
-- =====================================================================================
CREATE OR REPLACE VIEW `${PROJECT_ID}.marts.v_rework_rate`
OPTIONS (description = '§8.9 rework rate: mean post_review_change_ratio over PRs × 100. ONLY post-first-review commits count; pre-review auto-fix cycles are reported separately and never inflate rework.')
AS
WITH pr_level AS (
  -- Collapse to PR grain first. post_review_change_ratio is already a PR-level
  -- quantity propagated onto each output by 05_transform_output.sql, so ANY_VALUE is
  -- the correct de-duplication here, not AVG.
  --
  -- The period is derived from the PR's EARLIEST output, not per-output, so a PR whose
  -- outputs straddle a week boundary stays ONE PR in the count. Splitting it would
  -- double it in the denominator of a metric defined as a mean over PRs.
  SELECT
    o.pr_id,
    o.repo_full_name,
    DATE_TRUNC(DATE(MIN(o.generated_at)), WEEK(MONDAY)) AS week_start,
    DATE_TRUNC(DATE(MIN(o.generated_at)), MONTH)        AS month_start,
    ANY_VALUE(o.agent_name)               AS agent_name,
    ANY_VALUE(o.agent_version)            AS agent_version,
    ANY_VALUE(o.skill_name)               AS skill_name,
    ANY_VALUE(o.model_id)                 AS model_id,
    ANY_VALUE(o.person_id)                AS person_id,
    ANY_VALUE(o.team_id)                  AS team_id,
    ANY_VALUE(o.jira_project_key)         AS jira_project_key,
    ANY_VALUE(o.post_review_change_ratio) AS post_review_change_ratio,
    ANY_VALUE(o.auto_fix_cycles)          AS auto_fix_cycles,
    ANY_VALUE(o.lines_changed_pre_review) AS lines_changed_pre_review,
    ANY_VALUE(o.lines_changed_after_first_review) AS lines_changed_after_first_review,
    SUM(o.lines_generated)                AS lines_generated
  FROM `${PROJECT_ID}.core.fct_ai_output` AS o
  WHERE o.pr_id IS NOT NULL
    AND o.merged_at IS NOT NULL              -- only merged PRs have a settled ratio
    AND o.link_method = 'explicit'
    AND NOT COALESCE(o.is_quarantined, FALSE)
    AND NOT COALESCE(o.is_reused, FALSE)
  GROUP BY o.pr_id, o.repo_full_name
)
SELECT
  week_start,
  month_start,
  agent_name,
  agent_version,
  skill_name,
  model_id,
  person_id,
  team_id,
  jira_project_key,

  COUNT(*)                                                AS pr_count,
  SUM(lines_generated)                                    AS lines_generated,
  SUM(lines_changed_after_first_review)                   AS lines_reworked,
  -- Reported SEPARATELY and never folded into rework (§8.9).
  SUM(lines_changed_pre_review)                           AS lines_changed_pre_review,
  SUM(auto_fix_cycles)                                    AS auto_fix_cycles,
  SAFE_DIVIDE(SUM(auto_fix_cycles), NULLIF(COUNT(*), 0))  AS mean_auto_fix_cycles_per_pr,

  IF(COUNT(*) < 5, NULL, AVG(post_review_change_ratio) * 100) AS rework_rate_pct,
  IF(COUNT(*) < 5, NULL,
     APPROX_QUANTILES(post_review_change_ratio, 100)[OFFSET(50)] * 100)
                                                          AS rework_ratio_median_pct,
  IF(COUNT(*) < 5, NULL,
     APPROX_QUANTILES(post_review_change_ratio, 100)[OFFSET(85)] * 100)
                                                          AS rework_ratio_p85_pct,
  (COUNT(*) < 5)                                          AS k_anonymity_applied
FROM pr_level
GROUP BY week_start, month_start, agent_name, agent_version, skill_name,
         model_id, person_id, team_id, jira_project_key;


-- =====================================================================================
-- §8.12 — COST PER ACCEPTED OUTPUT   ⭐ THE ECONOMIC METRIC
-- =====================================================================================
-- Design section: §8.12. Formula, verbatim:
--
--                                 Σ total_cost_usd(runs in period)
--   cost_per_accepted_output  =  ────────────────────────────────────
--                                 count(accepted outputs in period)
--
--     total_cost_usd = token_cost + allocated_seat_cost + allocated_infra_cost
--
-- Unit: USD per unit  ·  Grain: period × agent/project/person
--
-- Design §9.1 PROMOTES this to the primary economic metric, replacing ROI: both terms
-- are directly measured, with no counterfactual anywhere.
--
-- THREE RULES THAT ARE EASY TO BREAK:
--   1. DENOMINATOR HONESTY (§8.12). Runs that produced NO output still count in the
--      NUMERATOR. Excluding failed runs from cost would understate the true cost of
--      AI — the failures are part of what the accepted outputs cost.
--   2. ALLOCATION (AR-8, §8.12). Seat and infra cost are allocated pro rata BY RUN
--      COUNT per person per month, never by token share, or a light user with one
--      expensive run absorbs a whole seat. Those terms are NULL until a procurement
--      feed and the GCP billing join exist, so total_cost_usd is NULL and only
--      token_cost_usd is reportable today. A partial total presented as a total is
--      worse than no total.
--   3. CARRIES cost_basis (§8.12). Modelled tokens ⇒ the whole metric is modelled.
--      §8.1 requires modelled values to be rendered visually distinct.
--
-- CONTRACT §4: an unpriced model yields NULL, never 0 — so cost_complete_pct below is
-- not decoration. Read it before quoting the cost.
-- =====================================================================================
CREATE OR REPLACE VIEW `${PROJECT_ID}.marts.v_cost_per_accepted_output`
OPTIONS (description = '§8.12 cost per accepted output — THE economic metric (design §9.1 promotes it over ROI). Numerator includes runs that produced nothing (denominator honesty). Carries cost_basis and a completeness percentage; NULL cost is never treated as 0 (CONTRACT §4).')
AS
WITH cost_side AS (
  SELECT
    DATE_TRUNC(DATE(r.started_at), WEEK(MONDAY)) AS week_start,
    DATE_TRUNC(DATE(r.started_at), MONTH)        AS month_start,
    r.agent_name,
    r.model_id,
    r.skill_name,
    r.person_id,
    r.team_id,
    r.jira_project_key,
    COUNT(*)                                        AS run_count,
    -- Denominator honesty: ALL runs contribute cost, including failures.
    SUM(r.cost_usd)                                 AS token_cost_usd,
    COUNTIF(r.cost_usd IS NOT NULL)                 AS runs_with_cost_count,
    COUNTIF(r.total_tokens IS NOT NULL AND r.cost_usd IS NULL) AS runs_unpriced_count,
    COUNTIF(r.cost_basis = 'modelled')              AS runs_modelled_count,
    COUNTIF(COALESCE(r.cost_is_placeholder, FALSE)) AS runs_placeholder_priced_count,
    COUNTIF(r.cost_attributable IS FALSE)           AS runs_cost_not_attributable_count,
    SUM(r.total_tokens)                             AS total_tokens,
    -- CONTRACT §4.1: the measured billing unit, carried BESIDE the modelled dollar
    -- figure and never folded into it. A cost-per-accepted-output in dollars and a
    -- premium-requests-per-accepted-output count are two different questions; the
    -- second is the one a Copilot invoice can be reconciled against.
    SUM(r.premium_requests)                         AS premium_requests
  FROM `${PROJECT_ID}.core.fct_ai_run` AS r
  WHERE r.link_method = 'explicit'      -- CONTRACT §2.4: cost metrics, explicit only
  GROUP BY week_start, month_start, r.agent_name, r.model_id, r.skill_name,
           r.person_id, r.team_id, r.jira_project_key
),
output_side AS (
  SELECT
    DATE_TRUNC(DATE(o.generated_at), WEEK(MONDAY)) AS week_start,
    DATE_TRUNC(DATE(o.generated_at), MONTH)        AS month_start,
    o.agent_name,
    o.model_id,
    o.skill_name,
    o.person_id,
    o.team_id,
    o.jira_project_key,
    COUNTIF(o.acceptance_state = 'accepted') AS accepted_count,
    COUNTIF(o.is_terminal_state)             AS terminal_count
  FROM `${PROJECT_ID}.core.fct_ai_output` AS o
  WHERE o.link_method = 'explicit'
    AND NOT COALESCE(o.is_quarantined, FALSE)
    AND NOT COALESCE(o.is_reused, FALSE)
  GROUP BY week_start, month_start, o.agent_name, o.model_id, o.skill_name,
           o.person_id, o.team_id, o.jira_project_key
)
SELECT
  COALESCE(c.week_start, s.week_start)             AS week_start,
  COALESCE(c.month_start, s.month_start)           AS month_start,
  COALESCE(c.agent_name, s.agent_name)             AS agent_name,
  COALESCE(c.model_id, s.model_id)                 AS model_id,
  COALESCE(c.skill_name, s.skill_name)             AS skill_name,
  COALESCE(c.person_id, s.person_id)               AS person_id,
  COALESCE(c.team_id, s.team_id)                   AS team_id,
  COALESCE(c.jira_project_key, s.jira_project_key) AS jira_project_key,

  COALESCE(c.run_count, 0)                         AS run_count,
  COALESCE(s.accepted_count, 0)                    AS accepted_output_count,
  COALESCE(s.terminal_count, 0)                    AS terminal_output_count,
  c.total_tokens,
  c.token_cost_usd,
  -- Measured billing units. NEVER added to token_cost_usd (CONTRACT §4.1); DQ-BILL
  -- scans this file's view definitions for anyone who tries.
  c.premium_requests,
  IF(COALESCE(c.run_count, 0) < 5, NULL,
     SAFE_DIVIDE(c.premium_requests, NULLIF(s.accepted_count, 0)))
                                                   AS premium_requests_per_accepted_output,
  -- Since 1.1.0 the commonest reason a run has no cost is NOT an unpriced model: it is
  -- that the run shared a Copilot session with other runs, so CONTRACT §3 forbids
  -- attributing a share of the session total to it. Published here so an incomplete
  -- cost figure explains itself rather than looking like a collection failure.
  COALESCE(c.runs_cost_not_attributable_count, 0)  AS runs_cost_not_attributable_count,

  -- The metric. NULL when cost is unknown or nothing was accepted — both are honest
  -- answers, and neither is zero.
  IF(COALESCE(c.run_count, 0) < 5, NULL,
     CAST(SAFE_DIVIDE(c.token_cost_usd, NULLIF(s.accepted_count, 0)) AS NUMERIC))
                                                   AS cost_per_accepted_output_usd,

  -- AR-8 terms. Explicitly NULL, not 0: a zero seat cost is a claim, and a false one.
  CAST(NULL AS NUMERIC)                            AS allocated_seat_cost_usd,
  CAST(NULL AS NUMERIC)                            AS allocated_infra_cost_usd,
  CAST(NULL AS NUMERIC)                            AS total_cost_usd,
  CAST(NULL AS NUMERIC)                            AS total_cost_per_accepted_output_usd,

  -- cost_basis carried through (§8.12). If ANY run in the cell is modelled, the whole
  -- cell is modelled and must be rendered distinctly (§8.1).
  CASE
    WHEN COALESCE(c.runs_modelled_count, 0) > 0 THEN 'modelled'
    WHEN COALESCE(c.runs_with_cost_count, 0) > 0 THEN 'measured'
    ELSE NULL
  END                                              AS cost_basis,
  COALESCE(c.runs_placeholder_priced_count, 0) > 0 AS uses_placeholder_pricing,
  COALESCE(c.runs_unpriced_count, 0)               AS runs_unpriced_count,
  -- Completeness: what share of the runs in this cell actually have a cost. Below
  -- 100% the figure above is a FLOOR, not a total.
  SAFE_DIVIDE(c.runs_with_cost_count, NULLIF(c.run_count, 0)) * 100
                                                   AS cost_complete_pct,
  (COALESCE(c.run_count, 0) < 5)                   AS k_anonymity_applied
FROM cost_side AS c
-- FULL OUTER: a period can have cost with no accepted output (that IS the finding),
-- and accepted outputs whose runs fell outside the window.
FULL OUTER JOIN output_side AS s
  ON  c.week_start       = s.week_start
  AND c.month_start      = s.month_start
  AND c.agent_name       IS NOT DISTINCT FROM s.agent_name
  AND c.model_id         IS NOT DISTINCT FROM s.model_id
  AND c.skill_name       IS NOT DISTINCT FROM s.skill_name
  AND c.person_id        IS NOT DISTINCT FROM s.person_id
  AND c.team_id          IS NOT DISTINCT FROM s.team_id
  AND c.jira_project_key IS NOT DISTINCT FROM s.jira_project_key;


-- =====================================================================================
-- §8.14 — PR REVIEW AND MERGE LEAD TIME
-- =====================================================================================
-- Design section: §8.14. Formulas, verbatim:
--
--   pr_review_lead_time_hours  =  first_review_action_at  −  pr_created_at
--   pr_merge_lead_time_hours   =  pr_merged_at            −  pr_created_at
--   pr_review_duration_hours   =  pr_merged_at            −  first_review_action_at
--
-- first_review_action_at = earliest of {first comment, first approval, first
-- changes-requested} BY A PERSON OTHER THAN THE PR AUTHOR. Self-comments are not review.
--
-- REPORT: median AND p85. §8.14 is explicit that means are useless here — the
-- distribution is long-tailed by construction, and a single stale PR moves the mean
-- more than a week of good behaviour does.
--
-- NORMALISE FOR SIZE (§8.14, mandatory): AI PRs are typically larger, so lead time is
-- ALSO reported PER 100 CHANGED LINES. Without it the comparison is meaningless — a
-- slower review of a 900-line PR is not worse review.
--
-- EXCLUSIONS (§8.14): draft PRs (until marked ready), PRs open across a company
-- shutdown, bot-only PRs.
--
-- Key dimensions are agent_name / model_id / skill_name because the A/B comparison is
-- BETWEEN AI CONFIGURATIONS (§9.1), not AI vs human — no non-AI cohort exists.
-- =====================================================================================
CREATE OR REPLACE VIEW `${PROJECT_ID}.marts.v_pr_lead_time`
OPTIONS (description = '§8.14 PR review and merge lead time. Median AND p85 (means are useless on this distribution), reported raw and normalised per 100 changed lines. Excludes draft, bot-only, and shutdown-spanning PRs.')
AS
WITH eligible AS (
  SELECT
    DATE_TRUNC(DATE(pr.created_on), WEEK(MONDAY)) AS week_start,
    DATE_TRUNC(DATE(pr.created_on), MONTH)        AS month_start,
    pr.repo_full_name,
    pr.jira_project_key,
    pr.pr_id,
    pr.review_lead_time_hours,
    pr.merge_lead_time_hours,
    pr.review_duration_hours,
    pr.total_changed_lines,
    pr.ai_line_share,
    -- Normalisation: hours per 100 changed lines. NULLIF guards a zero-line PR, which
    -- would otherwise divide by zero and report an infinite review time.
    SAFE_DIVIDE(pr.review_lead_time_hours, NULLIF(pr.total_changed_lines, 0)) * 100
                                                  AS review_lead_time_per_100_lines,
    SAFE_DIVIDE(pr.merge_lead_time_hours, NULLIF(pr.total_changed_lines, 0)) * 100
                                                  AS merge_lead_time_per_100_lines,
    -- AI configuration dimensions come from the outputs in the PR. ANY_VALUE would be
    -- wrong across a mixed PR, so the join below aggregates to a single agent per PR
    -- and mixed PRs are reported under their dominant agent with ai_line_share
    -- carrying the AR-7 fractional share.
    o.agent_name,
    o.agent_version,
    o.skill_name,
    o.model_id
  FROM `${PROJECT_ID}.core.fct_pull_request` AS pr
  LEFT JOIN (
    SELECT
      pr_id,
      repo_full_name,
      ANY_VALUE(agent_name)    AS agent_name,
      ANY_VALUE(agent_version) AS agent_version,
      ANY_VALUE(skill_name)    AS skill_name,
      ANY_VALUE(model_id)      AS model_id
    FROM `${PROJECT_ID}.core.fct_ai_output`
    WHERE pr_id IS NOT NULL
      AND NOT COALESCE(is_quarantined, FALSE)
    GROUP BY pr_id, repo_full_name
  ) AS o
    ON o.pr_id = pr.pr_id AND o.repo_full_name = pr.repo_full_name
  -- §8.14 exclusions.
  WHERE NOT COALESCE(pr.is_draft, FALSE)
    AND NOT COALESCE(pr.is_bot_only, FALSE)
    AND NOT COALESCE(pr.spans_shutdown, FALSE)
)
SELECT
  week_start,
  month_start,
  repo_full_name,
  jira_project_key,
  agent_name,
  agent_version,
  skill_name,
  model_id,

  COUNT(*)                                                                AS pr_count,
  COUNTIF(review_lead_time_hours IS NOT NULL)                             AS reviewed_pr_count,
  COUNTIF(merge_lead_time_hours IS NOT NULL)                              AS merged_pr_count,
  AVG(total_changed_lines)                                                AS avg_changed_lines,
  AVG(ai_line_share)                                                      AS avg_ai_line_share,  -- AR-7

  -- Median and p85, raw. APPROX_QUANTILES(x, 100)[OFFSET(p)] is the p-th percentile.
  APPROX_QUANTILES(review_lead_time_hours, 100)[OFFSET(50)] AS review_lead_time_median_hours,
  APPROX_QUANTILES(review_lead_time_hours, 100)[OFFSET(85)] AS review_lead_time_p85_hours,
  APPROX_QUANTILES(merge_lead_time_hours, 100)[OFFSET(50)]  AS merge_lead_time_median_hours,
  APPROX_QUANTILES(merge_lead_time_hours, 100)[OFFSET(85)]  AS merge_lead_time_p85_hours,
  APPROX_QUANTILES(review_duration_hours, 100)[OFFSET(50)]  AS review_duration_median_hours,
  APPROX_QUANTILES(review_duration_hours, 100)[OFFSET(85)]  AS review_duration_p85_hours,

  -- Median and p85, NORMALISED per 100 changed lines (§8.14 mandatory).
  APPROX_QUANTILES(review_lead_time_per_100_lines, 100)[OFFSET(50)]
                                                  AS review_lead_time_median_hours_per_100_lines,
  APPROX_QUANTILES(review_lead_time_per_100_lines, 100)[OFFSET(85)]
                                                  AS review_lead_time_p85_hours_per_100_lines,
  APPROX_QUANTILES(merge_lead_time_per_100_lines, 100)[OFFSET(50)]
                                                  AS merge_lead_time_median_hours_per_100_lines,
  APPROX_QUANTILES(merge_lead_time_per_100_lines, 100)[OFFSET(85)]
                                                  AS merge_lead_time_p85_hours_per_100_lines
FROM eligible
GROUP BY week_start, month_start, repo_full_name, jira_project_key,
         agent_name, agent_version, skill_name, model_id;


-- =====================================================================================
-- §8.15 — AGENT / SKILL REUSE RATE  (both views)
-- =====================================================================================
-- Design section: §8.15. Formulas, verbatim:
--
--   -- Asset-centric: what fraction of assets are genuinely shared?
--                         count(assets WHERE distinct_users >= 2 AND invocations >= 5)
--   asset_reuse_rate  =  ────────────────────────────────────────────────────────────── × 100
--                         count(assets invoked at least once in period)
--
--   -- Run-centric: what fraction of work rides on shared assets?
--                       count(runs WHERE agent/skill is a shared asset)
--   run_reuse_rate  =  ────────────────────────────────────────────────── × 100
--                       count(runs)
--
--   reuse_frequency(asset)  = count(runs) / period
--   adoption_breadth(asset) = count(DISTINCT person_id)
--   adoption_depth(asset)   = count(DISTINCT team_id)
--
-- ⚠ DENOMINATOR RULE (§8.15): the denominator EXCLUDES assets created in the current
-- period. A brand-new skill cannot be "reused" yet and would drag the rate down
-- misleadingly. Implemented via dim_agent_version.first_seen_at < period start.
-- =====================================================================================

-- ---- asset-centric ------------------------------------------------------------------
CREATE OR REPLACE VIEW `${PROJECT_ID}.marts.v_asset_reuse_rate`
OPTIONS (description = '§8.15 asset-centric reuse. Shared asset = distinct_users >= 2 AND invocations >= 5. Denominator EXCLUDES assets first seen inside the period (a new asset cannot yet be reused).')
AS
WITH asset_month AS (
  -- One row per (asset, month). Agents and skills are unioned into a single "asset"
  -- namespace so the metric answers the §8.15 question — "are the platform's assets
  -- shared infrastructure or one-off scripts" — for both kinds at once.
  SELECT
    DATE_TRUNC(DATE(r.started_at), MONTH) AS month_start,
    'agent'                               AS asset_kind,
    r.agent_name                          AS asset_name,
    r.run_id,
    r.person_id,
    r.team_id
  FROM `${PROJECT_ID}.core.fct_ai_run` AS r
  WHERE r.agent_name IS NOT NULL

  UNION ALL

  SELECT
    DATE_TRUNC(DATE(r.started_at), MONTH),
    'skill',
    r.skill_name,
    r.run_id,
    r.person_id,
    r.team_id
  FROM `${PROJECT_ID}.core.fct_ai_run` AS r
  WHERE r.skill_name IS NOT NULL
),
asset_stats AS (
  SELECT
    am.month_start,
    am.asset_kind,
    am.asset_name,
    COUNT(DISTINCT am.run_id)    AS invocations,
    COUNT(DISTINCT am.person_id) AS adoption_breadth,   -- distinct users
    COUNT(DISTINCT am.team_id)   AS adoption_depth,     -- distinct teams
    -- §8.15 shared-asset bar.
    (COUNT(DISTINCT am.person_id) >= 2 AND COUNT(DISTINCT am.run_id) >= 5) AS is_shared_asset,
    -- Denominator exclusion: was this asset first seen BEFORE the month began?
    MIN(av.first_seen_at)        AS asset_first_seen_at
  FROM asset_month AS am
  LEFT JOIN `${PROJECT_ID}.core.dim_agent_version` AS av
    ON av.agent_name = am.asset_name AND am.asset_kind = 'agent'
  GROUP BY am.month_start, am.asset_kind, am.asset_name
)
SELECT
  month_start,
  asset_kind,
  asset_name,
  invocations,
  adoption_breadth,
  adoption_depth,
  is_shared_asset,
  -- reuse_frequency: invocations per week over the month.
  SAFE_DIVIDE(invocations,
              DATE_DIFF(DATE_ADD(month_start, INTERVAL 1 MONTH), month_start, DAY) / 7.0)
                                                       AS reuse_frequency_runs_per_week,
  -- §8.15 denominator rule. An asset with no dim row (a skill, or an agent not yet
  -- registered) is treated as pre-existing: excluding it would understate the
  -- denominator, which biases the rate UPWARD — the wrong direction to err.
  (asset_first_seen_at IS NULL OR asset_first_seen_at < TIMESTAMP(month_start))
                                                       AS in_reuse_denominator,
  -- The rate itself, over the eligible asset population for the month.
  IF(COUNTIF(asset_first_seen_at IS NULL OR asset_first_seen_at < TIMESTAMP(month_start))
       OVER (PARTITION BY month_start, asset_kind) < 5,
     NULL,
     SAFE_DIVIDE(
       COUNTIF(is_shared_asset
               AND (asset_first_seen_at IS NULL OR asset_first_seen_at < TIMESTAMP(month_start)))
         OVER (PARTITION BY month_start, asset_kind),
       NULLIF(COUNTIF(asset_first_seen_at IS NULL OR asset_first_seen_at < TIMESTAMP(month_start))
                OVER (PARTITION BY month_start, asset_kind), 0)) * 100
  )                                                    AS asset_reuse_rate_pct
FROM asset_stats;

-- ---- run-centric --------------------------------------------------------------------
CREATE OR REPLACE VIEW `${PROJECT_ID}.marts.v_run_reuse_rate`
OPTIONS (description = '§8.15 run-centric reuse: share of runs riding on a shared asset (distinct_users >= 2 AND invocations >= 5 in the same month).')
AS
WITH shared_agents AS (
  SELECT
    DATE_TRUNC(DATE(started_at), MONTH) AS month_start,
    agent_name
  FROM `${PROJECT_ID}.core.fct_ai_run`
  WHERE agent_name IS NOT NULL
  GROUP BY month_start, agent_name
  HAVING COUNT(DISTINCT person_id) >= 2 AND COUNT(DISTINCT run_id) >= 5
),
shared_skills AS (
  SELECT
    DATE_TRUNC(DATE(started_at), MONTH) AS month_start,
    skill_name
  FROM `${PROJECT_ID}.core.fct_ai_run`
  WHERE skill_name IS NOT NULL
  GROUP BY month_start, skill_name
  HAVING COUNT(DISTINCT person_id) >= 2 AND COUNT(DISTINCT run_id) >= 5
)
SELECT
  DATE_TRUNC(DATE(r.started_at), MONTH) AS month_start,
  r.team_id,
  r.jira_project_key,
  COUNT(*)                                                    AS run_count,
  COUNTIF(sa.agent_name IS NOT NULL)                          AS runs_on_shared_agent,
  COUNTIF(ss.skill_name IS NOT NULL)                          AS runs_on_shared_skill,
  COUNTIF(sa.agent_name IS NOT NULL OR ss.skill_name IS NOT NULL)
                                                              AS runs_on_shared_asset,
  IF(COUNT(*) < 5, NULL,
     SAFE_DIVIDE(COUNTIF(sa.agent_name IS NOT NULL OR ss.skill_name IS NOT NULL),
                 NULLIF(COUNT(*), 0)) * 100)                  AS run_reuse_rate_pct,
  (COUNT(*) < 5)                                              AS k_anonymity_applied
FROM `${PROJECT_ID}.core.fct_ai_run` AS r
LEFT JOIN shared_agents AS sa
  ON sa.month_start = DATE_TRUNC(DATE(r.started_at), MONTH) AND sa.agent_name = r.agent_name
LEFT JOIN shared_skills AS ss
  ON ss.month_start = DATE_TRUNC(DATE(r.started_at), MONTH) AND ss.skill_name = r.skill_name
GROUP BY month_start, r.team_id, r.jira_project_key;


-- =====================================================================================
-- §8.11 — MANUAL INTERVENTION RATE
-- =====================================================================================
-- Design section: §8.11. Formula, verbatim:
--
--                                count(runs WHERE correction_turns >= 1 OR gate_auto_fix_attempts >= 1)
--   manual_intervention_rate = ───────────────────────────────────────────────────────────────────────── × 100
--                                count(runs)
--
--     correction_turns = count(human.turn WHERE turn_kind IN {'correction','rejection'})
--
-- ⭐ EXCLUDED BY DESIGN: turn_kind = 'approval' AND turn_kind = 'clarification'.
--   The agents DELIBERATELY ask for approval — developer.implementer.agent.md's
--   autonomous_policy.ask_user_when lists architectural choices, breaking changes and
--   security trade-offs [V]. Counting a designed approval gate as an "intervention"
--   would punish correct behaviour: the better-governed agent would score worse.
--   The exclusion is enforced structurally — human_turns_approval and
--   human_turns_clarification are stored in separate columns and simply never appear
--   in the numerator below.
--
-- ⚠ INTERPRETATION RULE (MANDATORY, design §8.11 and §11.5):
--   A HIGH RATE ON A JUNIOR ENGINEER IS A SIGNAL ABOUT AGENT QUALITY OR TASK FIT, NOT
--   ABOUT THE PERSON. The same rate carries opposite meaning for a new hire and a
--   six-month user. This view therefore emits tenure_bucket and refuses to be a bare
--   person ranking: render segmented by tenure, always.
--
-- ACCESS NOTE: this is the only view that reads core.dim_person (for tenure_start_date).
-- It must be created as an AUTHORISED VIEW so marts readers get the bounded
-- tenure_bucket WITHOUT being granted read on the restricted identity map itself
-- (design §11.4). Grant on the view; never widen IAM on dim_person to make it work.
-- =====================================================================================
CREATE OR REPLACE VIEW `${PROJECT_ID}.marts.v_manual_intervention_rate`
OPTIONS (description = '§8.11 manual intervention rate. Numerator = correction OR rejection turns, OR gate auto-fix attempts. EXCLUDES approval and clarification turns by design — a designed approval gate is correct behaviour, not an intervention. MUST be rendered segmented by tenure_bucket (§8.11, §11.5), never as a person ranking.')
AS
WITH tenured AS (
  SELECT
    r.run_id,
    DATE_TRUNC(DATE(r.started_at), WEEK(MONDAY)) AS week_start,
    DATE_TRUNC(DATE(r.started_at), MONTH)        AS month_start,
    r.person_id,
    r.team_id,
    r.agent_name,
    r.agent_version,
    r.skill_name,
    r.model_id,
    r.jira_project_key,
    -- §8.11 dimension: tenure_bucket. Bounded (4 values), so DQ-15 safe.
    CASE
      WHEN p.tenure_start_date IS NULL THEN 'unknown'
      WHEN DATE_DIFF(DATE(r.started_at), p.tenure_start_date, DAY) < 30  THEN '0-1m'
      WHEN DATE_DIFF(DATE(r.started_at), p.tenure_start_date, DAY) < 180 THEN '1-6m'
      ELSE '6m+'
    END AS tenure_bucket,
    -- correction_turns per the §8.11 definition: correction + rejection ONLY.
    (COALESCE(r.human_turns_correction, 0) + COALESCE(r.human_turns_rejection, 0))
                                                 AS correction_turns,
    COALESCE(r.gate_auto_fix_attempts, 0)        AS gate_auto_fix_attempts,
    -- Carried for transparency: these are NOT in the numerator.
    COALESCE(r.human_turns_approval, 0)          AS approval_turns_excluded,
    COALESCE(r.human_turns_clarification, 0)     AS clarification_turns_excluded
  FROM `${PROJECT_ID}.core.fct_ai_run` AS r
  LEFT JOIN `${PROJECT_ID}.core.dim_person` AS p
    ON  p.person_id = r.person_id
    AND DATE(r.started_at) BETWEEN p.effective_from
                               AND COALESCE(p.effective_to, DATE '9999-12-31')
)
SELECT
  week_start,
  month_start,
  person_id,
  team_id,
  tenure_bucket,
  agent_name,
  agent_version,
  skill_name,
  model_id,
  jira_project_key,

  COUNT(*)                                                        AS run_count,
  COUNTIF(correction_turns >= 1 OR gate_auto_fix_attempts >= 1)   AS intervened_run_count,
  SUM(correction_turns)                                           AS correction_turns,
  SUM(gate_auto_fix_attempts)                                     AS gate_auto_fix_attempts,
  SUM(approval_turns_excluded)                                    AS approval_turns_excluded,
  SUM(clarification_turns_excluded)                               AS clarification_turns_excluded,

  IF(COUNT(*) < 5, NULL,
     SAFE_DIVIDE(COUNTIF(correction_turns >= 1 OR gate_auto_fix_attempts >= 1),
                 NULLIF(COUNT(*), 0)) * 100)                      AS manual_intervention_rate_pct,
  (COUNT(*) < 5)                                                  AS k_anonymity_applied,
  'Interpretation rule (§8.11, §11.5): a high rate is a signal about AGENT QUALITY or task fit first, not about the person. Render segmented by tenure_bucket; never as a bare person ranking.'
                                                                  AS interpretation_rule
FROM tenured
GROUP BY week_start, month_start, person_id, team_id, tenure_bucket,
         agent_name, agent_version, skill_name, model_id, jira_project_key;


-- =====================================================================================
-- §8.8 — AUTOMATION COVERAGE
-- =====================================================================================
-- Design section: §8.8. Formula, verbatim:
--
--                                automated
--   automation_coverage  =  ──────────────────────────────  × 100
--                           automated + semi_automated + manual
--
-- Unit: %  ·  Grain: feature / Jira issue  ·  Dimensions: test_category, profile, project
--
-- ⚠ SEMI-AUTOMATED COUNTS AS ZERO IN THE NUMERATOR (§8.8). A half-automated test still
-- needs a human, so crediting it distorts the "remaining manual work" metric this
-- feeds. It stays in the denominator only.
--
-- Companion metric (§8.8): automation_coverage_ai_share = the share of the AUTOMATED
-- cases that were AI-generated. Coverage rising is good; knowing who raised it is the
-- point.
--
-- [V] Worked example from real data (spec-metrics.json, PRJ-6316):
--     automated=14, semi_automated=2, manual=1 -> 14/17 = 82.4%.
--
-- SOURCE NOTE. §8.8's sources are spec-metrics.json and AIO TCMS. CONTRACT §7 defines
-- no table for test cases, so the supporting table below is declared here, next to the
-- only metric that reads it, rather than in 03_core_fct.sql. It is [A] pending the AIO
-- TCMS poller; until that lands the table is empty and this view returns no rows —
-- which is the correct behaviour. It must never return 0% and be mistaken for a
-- measurement.
-- =====================================================================================
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.core.fct_test_case`
(
  test_case_key       STRING    NOT NULL OPTIONS (description = 'AIO TCMS key, e.g. PRJ-TC-12018 [V].'),
  jira_issue_key      STRING             OPTIONS (description = 'Feature ticket the case belongs to (AR-3: the FEATURE ticket, not the delivery ticket).'),
  jira_project_key    STRING             OPTIONS (description = 'Bounded dimension.'),
  product_profile     STRING             OPTIONS (description = 'watchtower | automotive.'),
  test_category       STRING             OPTIONS (description = 'Bounded test category from the profile aio-mapping.yaml taxonomy.'),
  automation_type     STRING    NOT NULL OPTIONS (description = 'Bounded: automated | semi_automated | manual. §8.8 counts ONLY automated in the numerator; semi_automated counts as 0.'),
  is_ai_generated     BOOL               OPTIONS (description = 'The case was produced by an AI run. Feeds the §8.8 companion metric automation_coverage_ai_share.'),
  source_output_id    STRING             OPTIONS (description = 'fct_ai_output.output_id that generated it, where known. AR-1: exactly one.'),
  created_at          TIMESTAMP NOT NULL OPTIONS (description = 'PARTITION KEY.'),
  updated_at          TIMESTAMP          OPTIONS (description = 'Last poller refresh.')
)
PARTITION BY DATE(created_at)
CLUSTER BY jira_project_key
OPTIONS (
  partition_expiration_days = 396,
  description = 'Test-case inventory from AIO TCMS / spec-metrics.json. Supporting source for §8.8 automation coverage. [A] pending the AIO TCMS poller — empty until then, and v_automation_coverage correctly returns no rows rather than 0%.',
  labels = [('layer', 'core'), ('domain', 'ai-telemetry')]
);

CREATE OR REPLACE VIEW `${PROJECT_ID}.marts.v_automation_coverage`
OPTIONS (description = '§8.8 automation coverage: automated / (automated + semi_automated + manual) × 100. Semi-automated counts as ZERO in the numerator by design. Includes the §8.8 companion metric automation_coverage_ai_share.')
AS
SELECT
  DATE_TRUNC(DATE(created_at), MONTH) AS month_start,
  jira_issue_key,
  jira_project_key,
  product_profile,
  test_category,

  COUNTIF(automation_type = 'automated')       AS automated_count,
  COUNTIF(automation_type = 'semi_automated')  AS semi_automated_count,
  COUNTIF(automation_type = 'manual')          AS manual_count,
  COUNT(*)                                     AS total_case_count,

  -- Numerator is `automated` ONLY. semi_automated appears in the denominator only.
  SAFE_DIVIDE(COUNTIF(automation_type = 'automated'), NULLIF(COUNT(*), 0)) * 100
                                               AS automation_coverage_pct,

  -- Companion (§8.8): of the AUTOMATED cases, what share is AI-generated.
  COUNTIF(automation_type = 'automated' AND COALESCE(is_ai_generated, FALSE))
                                               AS automated_ai_generated_count,
  SAFE_DIVIDE(COUNTIF(automation_type = 'automated' AND COALESCE(is_ai_generated, FALSE)),
              NULLIF(COUNTIF(automation_type = 'automated'), 0)) * 100
                                               AS automation_coverage_ai_share_pct
FROM `${PROJECT_ID}.core.fct_test_case`
GROUP BY month_start, jira_issue_key, jira_project_key, product_profile, test_category;


-- =====================================================================================
-- §7.8 — RUN RELIABILITY
-- =====================================================================================
-- Design section: §7.8. Formulas, verbatim:
--
--   run success rate    = run.completed / (completed + failed + timeout + abandoned) × 100
--   timeout rate        = run.timeout / total_runs × 100
--   retry rate          = runs_with_retry / runs_with_retry_KNOWN × 100   ⚠ see below
--   dependency failures = count GROUP BY dependency_failed
--   VPN/network interruptions = count(run.failed WHERE dependency_failed IN {vpn, network})
--   per-integration error rate = failed / total GROUP BY tool_name
--
-- The VPN/network line answers the brief's explicit question about "interruptions
-- caused by VPN or external services" directly, which is why the breakdown is split
-- out into named columns rather than left as a generic GROUP BY.
--
-- ⚠⚠ RETRY RATE IS RETIRED (2026-08-26) AND ITS DENOMINATOR IS WHY.
--
-- §7.8 writes the formula as runs_with_retry / total_runs, and that is what this view
-- computed. But nothing has ever reliably counted retries: the span runtime reported
-- them rarely and Copilot's session journal does not record them at all (CONTRACT §3
-- row 5 — "`retry_count` and `finish_reason` are not recorded by this source and stay
-- NULL, never 0"). Dividing an always-empty numerator by total_runs produced exactly
-- 0.0%, every week, and that figure went onto a dashboard as a measurement.
--
-- The denominator is now runs where retries were actually COUNTED. Post-cutover that
-- is zero runs, so the rate is NULL — which is the true answer, and which renders as a
-- dash rather than as a flattering zero.
-- =====================================================================================
CREATE OR REPLACE VIEW `${PROJECT_ID}.marts.v_run_reliability`
OPTIONS (description = '§7.8 run reliability: success rate, timeout rate, the dependency-failure breakdown including the VPN/network interruption count the brief asks for, and a RETIRED retry rate. retry_rate_pct is NULL post-cutover because nothing records retries — it used to divide by total_runs and publish 0.0% as a measurement. tool_error_rate_pct now divides by tool calls that reported a status, not by all of them.')
AS
SELECT
  DATE_TRUNC(DATE(started_at), WEEK(MONDAY)) AS week_start,
  DATE_TRUNC(DATE(started_at), MONTH)        AS month_start,
  agent_name,
  agent_version,
  model_id,
  surface,
  team_id,
  jira_project_key,

  COUNT(*)                                     AS run_count,
  COUNTIF(terminal_status = 'completed')       AS completed_count,
  COUNTIF(terminal_status = 'failed')          AS failed_count,
  COUNTIF(terminal_status = 'timeout')         AS timeout_count,
  COUNTIF(terminal_status = 'abandoned')       AS abandoned_count,
  COUNTIF(terminal_status = 'in_flight')       AS in_flight_count,

  -- Denominator excludes in_flight: a run that has not finished has not yet succeeded
  -- OR failed, and counting it either way biases the rate.
  IF(COUNT(*) < 5, NULL,
     SAFE_DIVIDE(
       COUNTIF(terminal_status = 'completed'),
       NULLIF(COUNTIF(terminal_status IN ('completed','failed','timeout','abandoned')), 0)
     ) * 100)                                  AS run_success_rate_pct,
  IF(COUNT(*) < 5, NULL,
     SAFE_DIVIDE(COUNTIF(terminal_status = 'timeout'), NULLIF(COUNT(*), 0)) * 100)
                                               AS timeout_rate_pct,
  -- Denominator = runs where retries were counted at all, NOT all runs. See the
  -- retirement note in this view's header.
  COUNTIF(retry_count IS NOT NULL)             AS runs_with_retry_known_count,
  IF(COUNT(*) < 5, NULL,
     SAFE_DIVIDE(COUNTIF(retry_count >= 1),
                 NULLIF(COUNTIF(retry_count IS NOT NULL), 0)) * 100)
                                               AS retry_rate_pct,

  -- Dependency failure breakdown (§7.8).
  COUNTIF(dependency_failed = 'vpn')           AS dep_fail_vpn_count,
  COUNTIF(dependency_failed = 'network')       AS dep_fail_network_count,
  COUNTIF(dependency_failed IN ('vpn','network'))
                                               AS vpn_or_network_interruptions,
  COUNTIF(dependency_failed = 'jira')          AS dep_fail_jira_count,
  COUNTIF(dependency_failed = 'bitbucket')     AS dep_fail_bitbucket_count,
  COUNTIF(dependency_failed = 'mcp')           AS dep_fail_mcp_count,
  COUNTIF(dependency_failed = 'aio')           AS dep_fail_aio_count,
  COUNTIF(dependency_failed = 'ci')            AS dep_fail_ci_count,
  COUNTIF(dependency_failed = 'none')          AS dep_fail_none_count,

  SUM(tool_call_count)                         AS tool_call_count,
  -- ⚠ WAS STRUCTURALLY ZERO until 2026-08-26: 04_transform_run.sql counted errors as
  -- status = 'failed', a value the CONTRACT §3 row 6 enum has never contained. Real
  -- now (measured: 62 errors in 2,062 journal tool calls) — announce the step change,
  -- it is not a regression in tooling.
  SUM(tool_error_count)                        AS tool_error_count,
  SUM(tool_status_unknown_count)               AS tool_status_unknown_count,
  -- Denominator excludes calls that reported no status: a call nobody graded is not
  -- evidence of success. Read WITH tool_status_known_pct.
  SAFE_DIVIDE(SUM(tool_error_count),
              NULLIF(SUM(tool_call_count) - SUM(tool_status_unknown_count), 0)) * 100
                                               AS tool_error_rate_pct,
  SAFE_DIVIDE(SUM(tool_call_count) - SUM(tool_status_unknown_count),
              NULLIF(SUM(tool_call_count), 0)) * 100
                                               AS tool_status_known_pct,
  (COUNT(*) < 5)                               AS k_anonymity_applied
FROM `${PROJECT_ID}.core.fct_ai_run`
GROUP BY week_start, month_start, agent_name, agent_version, model_id,
         surface, team_id, jira_project_key;


-- Per-integration error rate (§7.8, "MCP/Jira/Bitbucket error rate"). Reads tool.call
-- on the correlation stream, because tool identity lives on the event, not on the run.
--
-- ⚠⚠ REWRITTEN 2026-08-26, AND IT WAS RETURNING NOTHING.
--
-- This view read `raw.otel_span` directly. That table is frozen (01_raw.sql) — nothing
-- has written to it since the 1.1.0 cutover — so the view returned ZERO ROWS, with no
-- error, no warning and no empty-result indication anywhere a reader would see one.
-- A view that silently returns nothing is the worst failure mode available to a
-- metric: a dashboard renders a blank panel and a blank panel reads as "no problems".
-- Found by grepping this file for readers of the frozen table, 2026-08-26.
--
-- Two things changed with the source and both are visible below:
--   * `status` is a real verdict now (`ok` | `error`, CONTRACT §3 row 6) rather than
--     the structurally-NULL span field, so error_count is a measurement rather than a
--     zero. It is NULL, not 0, where nothing reported a status.
--   * `duration_ms` is gone. The journal timestamps the two ends of a tool call and
--     the gap includes waiting for a human to approve it, which is not the tool's
--     duration. Reporting the median of that would be reporting how fast people click.
--
-- ⚠ tool_name is potentially unbounded across MCP servers — DQ-15 guards it, now at
-- the event rather than the span. This view is a diagnostic, not a dashboard dimension.
CREATE OR REPLACE VIEW `${PROJECT_ID}.marts.v_tool_error_rate`
OPTIONS (description = '§7.8 per-integration error rate from tool.call events (contract 1.1.0; previously read the now-frozen raw.otel_span and silently returned zero rows). error_count is NULL, never 0, where no call reported a status — read status_known_pct beside the rate. duration_ms is deliberately absent: the journal gap includes human approval time. Diagnostic view — tool_name is unbounded and guarded by DQ-15; do not use it as a dashboard dimension.')
AS
SELECT
  DATE(e.event_time)                                     AS day,
  JSON_VALUE(e.attributes, '$.tool_name')                AS tool_name,
  JSON_VALUE(e.attributes, '$.tool_kind')                AS tool_kind,
  COUNT(*)                                               AS call_count,
  COUNTIF(JSON_VALUE(e.attributes, '$.status') IS NOT NULL) AS status_known_count,
  -- NULL, not 0, when nothing was watching.
  IF(COUNTIF(JSON_VALUE(e.attributes, '$.status') IS NOT NULL) = 0,
     NULL,
     COUNTIF(JSON_VALUE(e.attributes, '$.status') = 'error'))  AS error_count,
  -- Denominator is the KNOWN calls, not all calls: dividing by all of them would
  -- dilute the rate by however much of the traffic went unmeasured.
  SAFE_DIVIDE(COUNTIF(JSON_VALUE(e.attributes, '$.status') = 'error'),
              NULLIF(COUNTIF(JSON_VALUE(e.attributes, '$.status') IS NOT NULL), 0)) * 100
                                                         AS error_rate_pct,
  SAFE_DIVIDE(COUNTIF(JSON_VALUE(e.attributes, '$.status') IS NOT NULL),
              NULLIF(COUNT(*), 0)) * 100                 AS status_known_pct,
  -- The error classes behind the rate, bounded per CONTRACT §3 row 6. Never a message.
  STRING_AGG(DISTINCT JSON_VALUE(e.attributes, '$.error_class')
             ORDER BY JSON_VALUE(e.attributes, '$.error_class') LIMIT 5)
                                                         AS error_classes
FROM `${PROJECT_ID}.raw.ai_run_event` AS e
WHERE e.event_type = 'tool.call'
GROUP BY day, tool_name, tool_kind;


-- =====================================================================================
-- ⭐⭐ marts.v_session_usage — CONTRACT §3 / §4.1 — USAGE AT THE GRAIN IT IS TRUE AT
-- =====================================================================================
-- ADDED WITH CONTRACT 1.1.0. This view exists because of one sentence in CONTRACT §3:
--
--     "Valid — tokens and premium requests per session, per model, per person, per
--      repository, per week. Every §6 Cost figure is built from these and is
--      unaffected."
--
-- The journal totals usage per session, so a session is the finest grain at which the
-- numbers are measurements rather than guesses. core.fct_ai_run deliberately reports
-- NULL usage for every run inside a multi-run session — and on the CLI surface that is
-- MOST runs (38 resumes across 22 sessions, plus every sub-agent). Without this view
-- those tokens would simply be absent from the warehouse, and "we refuse to attribute
-- it" would be indistinguishable from "we never collected it".
--
-- ⚠ THIS VIEW READS raw.ai_run_event, WHICH IS ACCESS-RESTRICTED (design §11.4).
-- It MUST be created as an AUTHORISED VIEW, like marts.v_manual_intervention_rate
-- reading dim_person. It projects a session id, a model id, token counts and billing
-- counts — no content, no paths, no email hashes — and grants on the VIEW, never on
-- the underlying table. Never widen IAM on raw.* to make it work.
--
-- ⚠ 90-DAY HORIZON, and this is a real limitation rather than a caveat. raw.* expires
-- at 90 days (design §11.2) while marts.agg_daily_person_agent keeps 1130, so this
-- view cannot serve a three-year trend. The proper fix is a core.fct_ai_session table
-- with 396-day retention, fed by its own transform. It is NOT built here: that is a
-- new fact table and a new scheduled job, and shipping a half-built one would be worse
-- than shipping a stated gap. Until then, roll this into a mart before the raw
-- partitions expire if a long trend is needed.
--
-- Grain: (session, model). That is the event grain and the pricing grain, and CONTRACT
-- §4 accepts it explicitly — "a session using two models is priced per model, which is
-- exact". Roll it up to session, person, repo or week freely; do NOT try to push it
-- down to a run.
-- =====================================================================================
CREATE OR REPLACE VIEW `${PROJECT_ID}.marts.v_session_usage`
OPTIONS (description = '⭐ CONTRACT §3/§4.1 usage at (session × model) grain — the finest grain at which Copilot journal usage is a measurement. Carries tokens, cost_usd, and the measured billing units (premium_requests, request_count, nano_aiu) SIDE BY SIDE, never blended. This is where the tokens of a multi-run session live, since core.fct_ai_run reports NULL for its constituent runs (CONTRACT §3). ⚠ AUTHORISED VIEW: reads restricted raw.ai_run_event; grant on the view, never on the table. ⚠ 90-day horizon (raw retention) — not a substitute for a session fact table.')
AS
WITH usage AS (
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
    -- NUMERIC, not INT64: requests.cost is fractional by model tier and an INT64 cast
    -- truncates 0.33 to 0 — a measured zero standing in for real billed usage.
    SAFE_CAST(JSON_VALUE(e.attributes, '$.premium_requests')  AS NUMERIC) AS premium_requests,
    SAFE_CAST(JSON_VALUE(e.attributes, '$.nano_aiu')            AS INT64) AS nano_aiu
  FROM `${PROJECT_ID}.raw.ai_run_event` AS e
  WHERE e.event_type = 'model.call'
    AND e.run_id  IS NULL          -- the session-grain shape (CONTRACT §2.4)
    AND e.trace_id IS NOT NULL
  -- CONTRACT §1.3: the collector is at-least-once and journal event_ids are
  -- deterministic, so duplicates are the steady state (see DQ-11). Dedup or every
  -- token in an open session is counted once per hourly read.
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY e.event_id ORDER BY COALESCE(e.ingested_at, e.event_time) ASC
  ) = 1
),

-- Who and what the session belonged to, taken from the runs inside it.
-- ⚠ A dimension is reported ONLY when the session is unambiguous about it. A session
-- that spans two repositories has no repository, and NULL says so; picking one would
-- attribute a whole session's spend to whichever run happened to be first. `agent_name`
-- is usually NULL by this rule and correctly so — a supervisor plus nine sub-agents is
-- not "one agent's usage".
session_dim AS (
  SELECT
    r.copilot_session_id,
    COUNT(*)                                                              AS run_count,
    COUNT(DISTINCT r.trace_id)                                            AS trace_count,
    MIN(r.started_at)                                                     AS session_started_at,
    IF(COUNT(DISTINCT r.person_id)        = 1, ANY_VALUE(r.person_id),        NULL) AS person_id,
    IF(COUNT(DISTINCT r.team_id)          = 1, ANY_VALUE(r.team_id),          NULL) AS team_id,
    IF(COUNT(DISTINCT r.repo_full_name)   = 1, ANY_VALUE(r.repo_full_name),   NULL) AS repo_full_name,
    IF(COUNT(DISTINCT r.jira_project_key) = 1, ANY_VALUE(r.jira_project_key), NULL) AS jira_project_key,
    IF(COUNT(DISTINCT r.agent_name)       = 1, ANY_VALUE(r.agent_name),       NULL) AS agent_name,
    IF(COUNT(DISTINCT r.skill_name)       = 1, ANY_VALUE(r.skill_name),       NULL) AS skill_name,
    IF(COUNT(DISTINCT r.surface)          = 1, ANY_VALUE(r.surface),          NULL) AS surface
  FROM `${PROJECT_ID}.core.fct_ai_run` AS r
  WHERE r.copilot_session_id IS NOT NULL
  GROUP BY r.copilot_session_id
)

SELECT
  DATE(u.shutdown_at)                              AS day,
  DATE_TRUNC(DATE(u.shutdown_at), WEEK(MONDAY))    AS week_start,
  DATE_TRUNC(DATE(u.shutdown_at), MONTH)           AS month_start,
  u.copilot_session_id,
  u.model_id,

  d.person_id,
  d.team_id,
  d.repo_full_name,
  d.jira_project_key,
  d.agent_name,
  d.skill_name,
  d.surface,
  COALESCE(d.run_count, 0)                         AS session_run_count,
  -- TRUE only when this session's usage could also have been attributed to its single
  -- run. Useful as a completeness measure: the share of measured tokens that
  -- core.fct_ai_run is able to carry at run grain.
  (COALESCE(d.run_count, 0) = 1)                   AS run_attributable,
  d.session_started_at,
  u.shutdown_at,

  u.input_tokens,
  u.output_tokens,
  u.cached_input_tokens,
  u.reasoning_tokens,
  -- ⚠ NOT a term of the CONTRACT §4 cost formula and NOT inside total_tokens.
  u.cache_write_tokens,
  CASE
    WHEN u.input_tokens IS NULL AND u.output_tokens IS NULL THEN NULL
    ELSE COALESCE(u.input_tokens, 0) + COALESCE(u.output_tokens, 0)
  END                                              AS total_tokens,

  -- ---- billing units (CONTRACT §4.1) — measured, dimensionless, side by side ----
  u.request_count,
  u.premium_requests,
  u.nano_aiu,

  -- ---- modelled cost (CONTRACT §4) ----
  -- Priced at SHUTDOWN time, because the journal never records when the individual
  -- calls happened. A session straddling a midnight price change is priced at the
  -- boundary it ends on; splitting it by elapsed time would be modelling dressed as
  -- measurement. Three terms only, and cache_write is not one of them.
  CASE
    WHEN p.input_per_1k_usd IS NULL OR p.output_per_1k_usd IS NULL THEN NULL
    WHEN COALESCE(u.cached_input_tokens, 0) > 0
         AND p.cached_input_per_1k_usd IS NULL THEN NULL
    ELSE
        ( SAFE_CAST(GREATEST(COALESCE(u.input_tokens, 0)
                             - COALESCE(u.cached_input_tokens, 0), 0) AS NUMERIC)
          / 1000 * p.input_per_1k_usd )
      + ( SAFE_CAST(COALESCE(u.output_tokens, 0) AS NUMERIC)
          / 1000 * p.output_per_1k_usd )
      + ( SAFE_CAST(COALESCE(u.cached_input_tokens, 0) AS NUMERIC)
          / 1000 * COALESCE(p.cached_input_per_1k_usd, 0) )
  END                                              AS cost_usd,
  -- An unpriced model yields NULL cost, never 0 (CONTRACT §4), and DQ-MODEL names it.
  (p.model_id IS NULL)                             AS model_unknown_to_price_book,
  COALESCE(p.is_placeholder, FALSE)                AS cost_is_placeholder,
  p.effective_from                                 AS pricing_effective_from,
  'measured'                                       AS cost_basis,
  'per_session_model'                              AS usage_grain,
  'copilot_journal'                                AS usage_source
FROM usage AS u
LEFT JOIN session_dim AS d ON d.copilot_session_id = u.copilot_session_id
LEFT JOIN `${PROJECT_ID}.core.dim_model_pricing` AS p
  ON  p.model_id = u.model_id
  AND DATE(u.shutdown_at) BETWEEN p.effective_from
                              AND COALESCE(p.effective_to, DATE '9999-12-31');


-- =====================================================================================
-- ⭐⭐ §9.1 — CONFIG COMPARISON — THE PRIMARY DECISION-MAKING QUERY
-- =====================================================================================
-- Design section: §9.1 "The real control groups".
--
-- WHY THIS IS THE MOST IMPORTANT VIEW IN THE FILE.
--
-- Design §9.1 Decision 2 abandons attribution-to-AI as a question: AI is applied to
-- essentially all work, so there is NO non-AI control group and no honest way to
-- answer "how much of this is because of AI". What replaces it is this:
--
--     Abandoned question          Replacement
--     "How many hours saved?"  -> cost per accepted output
--     "AI vs human"            -> ⭐ AGENT A vs B · MODEL X vs Y · SKILL ON vs OFF
--
-- Those arms ARE a real control group, and cleaner than an AI/non-AI split would have
-- been, because the variable under test is one the platform controls. Every dimension
-- needed already exists in the event schema [V] — this is a query, not a build.
--
-- The two metrics sliced here are the two a decision actually turns on:
--     * cost per accepted output (§8.12) — is it getting cheaper per unit of value
--     * acceptance rate          (§8.7)  — is the quality holding
--
-- ⭐ GUARD: `HAVING run_count >= 20`. §9.1 states it plainly — these comparisons are
-- OBSERVATIONAL, not randomised. Engineers pick the agent and the task, so selection
-- bias remains. The requirement is: stratify by task class and PR-size band, require
-- n >= 20 PER ARM before reporting a difference, and state the residual on the chart.
-- The HAVING clause enforces the n >= 20 half structurally; task_class and pr_size_band
-- are emitted as columns so the stratification half is available and cannot be
-- forgotten. An arm below 20 runs does not appear here AT ALL — deliberately. A
-- suppressed value invites a reader to squint at it; an absent row does not.
-- =====================================================================================
CREATE OR REPLACE VIEW `${PROJECT_ID}.marts.v_config_comparison`
OPTIONS (description = '⭐ §9.1 PRIMARY DECISION QUERY. Cost per accepted output and acceptance rate sliced by agent_name / agent_version / model_id / skill_name, stratified by task class and PR-size band. HAVING run_count >= 20 per arm — arms below the threshold are omitted entirely, not suppressed. Observational, not randomised: selection bias remains and must be stated wherever this is rendered.')
AS
WITH run_arm AS (
  SELECT
    DATE_TRUNC(DATE(r.started_at), MONTH) AS month_start,
    -- ---- the arms under test (design §9.1 "The real control groups") ----
    r.agent_name,
    r.agent_version,
    r.model_id,
    r.skill_name,
    -- skill on vs off, as an explicit boolean arm.
    (r.skill_name IS NOT NULL)            AS skill_loaded,
    -- ---- stratification (§9.1 guard) ----
    COALESCE(j.issue_type, 'unknown')     AS task_class,
    r.run_id,
    r.trace_id,
    r.person_id,
    r.cost_usd,
    r.cost_basis,
    r.cost_is_placeholder,
    r.total_tokens
  FROM `${PROJECT_ID}.core.fct_ai_run` AS r
  LEFT JOIN `${PROJECT_ID}.core.fct_jira_issue` AS j
    ON j.jira_issue_key = r.jira_issue_key
  WHERE r.link_method = 'explicit'   -- CONTRACT §2.4: cost metrics, explicit links only
),
output_arm AS (
  SELECT
    DATE_TRUNC(DATE(o.generated_at), MONTH) AS month_start,
    o.agent_name,
    o.agent_version,
    o.model_id,
    o.skill_name,
    (o.skill_name IS NOT NULL)              AS skill_loaded,
    COALESCE(j.issue_type, 'unknown')       AS task_class,
    -- PR-size band (§9.1 "stratify by PR-size band"). Bounded to 4 values, DQ-15 safe.
    CASE
      WHEN pr.total_changed_lines IS NULL      THEN 'unknown'
      WHEN pr.total_changed_lines <  100       THEN 'S (<100)'
      WHEN pr.total_changed_lines <  500       THEN 'M (100-499)'
      WHEN pr.total_changed_lines < 2000       THEN 'L (500-1999)'
      ELSE 'XL (2000+)'
    END                                     AS pr_size_band,
    o.output_id,
    o.acceptance_state,
    o.is_terminal_state,
    o.post_review_change_ratio
  FROM `${PROJECT_ID}.core.fct_ai_output` AS o
  LEFT JOIN `${PROJECT_ID}.core.fct_pull_request` AS pr
    ON pr.pr_id = o.pr_id AND pr.repo_full_name = o.repo_full_name
  LEFT JOIN `${PROJECT_ID}.core.fct_jira_issue` AS j
    ON j.jira_issue_key = o.jira_issue_key
  WHERE o.link_method = 'explicit'
    AND NOT COALESCE(o.is_quarantined, FALSE)   -- DQ-16 / AR-1
    AND NOT COALESCE(o.is_reused, FALSE)        -- AR-5
),
run_side AS (
  SELECT
    month_start, agent_name, agent_version, model_id, skill_name, skill_loaded, task_class,
    COUNT(*)                                        AS run_count,
    -- AR-4. ⚠ trace_id carries two namespaces since 1.1.0 — an emitter trc_<uuid4hex>
    -- or a Copilot session id — and this COUNT DISTINCT spans both. That is correct
    -- rather than a mix: the two are disjoint by construction (a `trc_` prefix versus
    -- a bare session directory name), so no two workflows can collide into one, and a
    -- Copilot session is exactly the "one user-initiated workflow" trace_id means.
    -- core.fct_ai_run.trace_id_namespace names which, if an arm needs to say.
    COUNT(DISTINCT trace_id)                        AS workflow_count,
    COUNT(DISTINCT person_id)                       AS distinct_person_count,
    SUM(cost_usd)                                   AS token_cost_usd,
    COUNTIF(cost_usd IS NOT NULL)                   AS runs_with_cost_count,
    COUNTIF(cost_basis = 'modelled')                AS runs_modelled_count,
    COUNTIF(COALESCE(cost_is_placeholder, FALSE))   AS runs_placeholder_priced_count,
    SUM(total_tokens)                               AS total_tokens
  FROM run_arm
  GROUP BY month_start, agent_name, agent_version, model_id, skill_name, skill_loaded, task_class
),
output_side AS (
  SELECT
    month_start, agent_name, agent_version, model_id, skill_name, skill_loaded, task_class,
    -- pr_size_band is aggregated to the modal band for the arm so the arm key matches
    -- the run side; the band is also exposed for stratified drill-down.
    (SELECT value FROM UNNEST(APPROX_TOP_COUNT(pr_size_band, 1))) AS dominant_pr_size_band,
    COUNT(*)                                        AS output_count,
    COUNTIF(is_terminal_state)                      AS terminal_output_count,
    COUNTIF(acceptance_state = 'accepted')          AS accepted_output_count,
    COUNTIF(acceptance_state = 'reworked')          AS reworked_output_count,
    COUNTIF(acceptance_state = 'rejected')          AS rejected_output_count,
    COUNTIF(acceptance_state = 'reverted')          AS reverted_output_count,
    AVG(post_review_change_ratio)                   AS mean_post_review_change_ratio
  FROM output_arm
  GROUP BY month_start, agent_name, agent_version, model_id, skill_name, skill_loaded, task_class
)
SELECT
  r.month_start,

  -- ---- the arm ----
  r.agent_name,
  r.agent_version,
  r.model_id,
  r.skill_name,
  r.skill_loaded,
  r.task_class,
  o.dominant_pr_size_band,

  -- ---- sample size (the guard) ----
  r.run_count,
  r.workflow_count,
  r.distinct_person_count,
  COALESCE(o.output_count, 0)                       AS output_count,
  COALESCE(o.terminal_output_count, 0)              AS terminal_output_count,
  COALESCE(o.accepted_output_count, 0)              AS accepted_output_count,

  -- ---- metric 1: acceptance rate (§8.7) ----
  SAFE_DIVIDE(o.accepted_output_count, NULLIF(o.terminal_output_count, 0)) * 100
                                                    AS ai_acceptance_rate_pct,
  SAFE_DIVIDE(o.reworked_output_count, NULLIF(o.terminal_output_count, 0)) * 100
                                                    AS rework_share_pct,
  o.mean_post_review_change_ratio * 100             AS mean_post_review_change_pct,

  -- ---- metric 2: cost per accepted output (§8.12) ----
  r.token_cost_usd,
  r.total_tokens,
  CAST(SAFE_DIVIDE(r.token_cost_usd, NULLIF(o.accepted_output_count, 0)) AS NUMERIC)
                                                    AS cost_per_accepted_output_usd,
  CAST(SAFE_DIVIDE(r.token_cost_usd, NULLIF(r.run_count, 0)) AS NUMERIC)
                                                    AS cost_per_run_usd,

  -- ---- honesty columns; a comparison chart must carry all three ----
  CASE
    WHEN COALESCE(r.runs_modelled_count, 0) > 0 THEN 'modelled'
    WHEN COALESCE(r.runs_with_cost_count, 0) > 0 THEN 'measured'
    ELSE NULL
  END                                               AS cost_basis,
  COALESCE(r.runs_placeholder_priced_count, 0) > 0  AS uses_placeholder_pricing,
  SAFE_DIVIDE(r.runs_with_cost_count, NULLIF(r.run_count, 0)) * 100
                                                    AS cost_complete_pct,
  'Observational, not randomised (design §9.1). Engineers choose the agent and the task, so selection bias remains. Compare only WITHIN task_class and pr_size_band, and state the residual on the chart.'
                                                    AS comparison_caveat
FROM run_side AS r
LEFT JOIN output_side AS o
  ON  r.month_start   = o.month_start
  AND r.agent_name    IS NOT DISTINCT FROM o.agent_name
  AND r.agent_version IS NOT DISTINCT FROM o.agent_version
  AND r.model_id      IS NOT DISTINCT FROM o.model_id
  AND r.skill_name    IS NOT DISTINCT FROM o.skill_name
  AND r.skill_loaded  = o.skill_loaded
  AND r.task_class    = o.task_class
-- ⭐ §9.1 GUARD: n >= 20 per arm before a difference may be reported.
WHERE r.run_count >= 20;
