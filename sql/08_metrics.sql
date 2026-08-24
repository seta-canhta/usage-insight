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
    SUM(r.total_tokens)                             AS total_tokens
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
--   retry rate          = runs_with_retry / total_runs × 100
--   dependency failures = count GROUP BY dependency_failed
--   VPN/network interruptions = count(run.failed WHERE dependency_failed IN {vpn, network})
--   per-integration error rate = failed / total GROUP BY tool_name
--
-- The VPN/network line answers the brief's explicit question about "interruptions
-- caused by VPN or external services" directly, which is why the breakdown is split
-- out into named columns rather than left as a generic GROUP BY.
-- =====================================================================================
CREATE OR REPLACE VIEW `${PROJECT_ID}.marts.v_run_reliability`
OPTIONS (description = '§7.8 run reliability: success rate, timeout rate, retry rate, and the dependency-failure breakdown including the VPN/network interruption count the brief asks for.')
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
  IF(COUNT(*) < 5, NULL,
     SAFE_DIVIDE(COUNTIF(retry_count >= 1), NULLIF(COUNT(*), 0)) * 100)
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
  SUM(tool_error_count)                        AS tool_error_count,
  SAFE_DIVIDE(SUM(tool_error_count), NULLIF(SUM(tool_call_count), 0)) * 100
                                               AS tool_error_rate_pct,
  (COUNT(*) < 5)                               AS k_anonymity_applied
FROM `${PROJECT_ID}.core.fct_ai_run`
GROUP BY week_start, month_start, agent_name, agent_version, model_id,
         surface, team_id, jira_project_key;


-- Per-integration error rate (§7.8, "MCP/Jira/Bitbucket error rate"). Reads the OTel
-- stream directly because tool identity lives on execute_tool spans, not on the run.
-- ⚠ gen_ai_tool_name is potentially unbounded across MCP servers — DQ-15 guards it.
-- This view is a diagnostic, not a dashboard dimension.
CREATE OR REPLACE VIEW `${PROJECT_ID}.marts.v_tool_error_rate`
OPTIONS (description = '§7.8 per-integration error rate from execute_tool spans. Diagnostic view — gen_ai_tool_name is unbounded and guarded by DQ-15; do not use it as a dashboard dimension.')
AS
SELECT
  DATE(start_time)        AS day,
  gen_ai_tool_name        AS tool_name,
  gen_ai_tool_type        AS tool_kind,
  COUNT(*)                AS call_count,
  COUNTIF(status_code = 'ERROR') AS error_count,
  SAFE_DIVIDE(COUNTIF(status_code = 'ERROR'), NULLIF(COUNT(*), 0)) * 100
                          AS error_rate_pct,
  APPROX_QUANTILES(duration_ms, 100)[OFFSET(50)] AS duration_median_ms,
  APPROX_QUANTILES(duration_ms, 100)[OFFSET(85)] AS duration_p85_ms
FROM `${PROJECT_ID}.raw.otel_span`
WHERE gen_ai_operation_name = 'execute_tool'
GROUP BY day, tool_name, tool_kind;


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
    COUNT(DISTINCT trace_id)                        AS workflow_count,   -- AR-4
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
