-- =====================================================================================
-- 05_transform_output.sql — raw + Bitbucket facts -> core.fct_ai_output
-- =====================================================================================
-- Contract:  schema/CONTRACT.md §5 (acceptance state machine),
--            §6 (attribution rules AR-1, AR-3, AR-4, AR-5, AR-7, AR-9)
-- Design:    docs/spikes/ai-effectiveness-observability.md §8.7, §8.9, §9.2, §9.3, §9.4
--
-- Cadence: NIGHTLY (CONTRACT §5: "Computed nightly on fct_ai_output"). Not hourly —
-- the state machine reads maturity and revert windows that only move once a day.
--
-- This file rebuilds the acceptance state of EVERY output still inside its 30-day
-- revert window, not just newly generated ones. An output accepted on day 8 can flip
-- to reverted on day 29, and AR-9 requires that flip to happen in the period the
-- revert occurs.
--
-- ┌───────────────────────────────────────────────────────────────────────────────────┐
-- │ THE TWO MISTAKES THIS FILE EXISTS TO PREVENT                                      │
-- │                                                                                   │
-- │ 1. MERGED ≠ ACCEPTED (design §9.2). A heavily rewritten output is merged and       │
-- │    REWORKED. Any metric that treats merge as acceptance overstates AI quality —   │
-- │    design §9.2 names this as the most likely way for the programme to produce a   │
-- │    flattering, wrong answer.                                                      │
-- │                                                                                   │
-- │ 2. AUTO-FIX ≠ REWORK (design §8.9). developer.implementer.agent.md permits up to  │
-- │    3 auto-fix cycles [V]. Those commits happen BEFORE any human looked at the     │
-- │    work. Counting them as rework would penalise the agent for successfully        │
-- │    fixing itself. Only commits authored AFTER first_review_at are rework; the     │
-- │    pre-review cycles are reported separately as auto_fix_cycles.                  │
-- └───────────────────────────────────────────────────────────────────────────────────┘
--
-- Substitute ${PROJECT_ID}. Run after 04_transform_run.sql.
-- =====================================================================================

-- CONTRACT §5 policy constants. Named, not inlined, so a sensitivity analysis
-- (§8.7 requires publishing at 0.10 / 0.25 / 0.50) is a one-line change.
DECLARE accept_change_ratio_threshold FLOAT64 DEFAULT 0.25;  -- CONTRACT §5
DECLARE maturity_window_days          INT64   DEFAULT 7;     -- CONTRACT §5
DECLARE revert_window_days            INT64   DEFAULT 30;    -- CONTRACT §5
DECLARE commit_deadline_days          INT64   DEFAULT 7;     -- CONTRACT §5 / DQ-3

-- How far back to re-evaluate. Must exceed the revert window, otherwise an output
-- reverted on day 29 would never be revisited and would stay wrongly 'accepted'.
DECLARE rebuild_days INT64 DEFAULT 45;


MERGE `${PROJECT_ID}.core.fct_ai_output` AS tgt
USING (

  -- ===================================================================================
  -- STEP 1 — deduplicate the correlation stream (CONTRACT §1.3)
  -- ===================================================================================
  WITH dedup_event AS (
    SELECT *
    FROM `${PROJECT_ID}.raw.ai_run_event`
    WHERE DATE(event_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL rebuild_days DAY)
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY event_id
      ORDER BY COALESCE(ingested_at, event_time) ASC
    ) = 1
  ),

  -- ===================================================================================
  -- STEP 2 — the generated artifacts
  -- ===================================================================================
  -- AR-5 is applied at the source: reuse_source non-NULL means the dedup checker
  -- suppressed or merged this artifact rather than the agent creating one. Such rows
  -- are REUSED, not generated. They stay in the table (they feed reuse metrics) but
  -- is_reused excludes them from output volume, acceptance denominators, and time
  -- saved — all three, per AR-5.
  output_raw AS (
    SELECT
      JSON_VALUE(e.attributes, '$.output_id')                              AS output_id,
      JSON_VALUE(e.attributes, '$.parent_output_id')                       AS parent_output_id,
      e.run_id,
      e.trace_id,
      e.event_time                                                         AS generated_at,
      JSON_VALUE(e.attributes, '$.artifact_type')                          AS artifact_type,
      JSON_VALUE(e.attributes, '$.file_path')                              AS file_path,
      SAFE_CAST(JSON_VALUE(e.attributes, '$.lines_added')   AS INT64)      AS lines_added,
      SAFE_CAST(JSON_VALUE(e.attributes, '$.lines_removed') AS INT64)      AS lines_removed,
      JSON_VALUE(e.attributes, '$.output_content_hash')                    AS output_content_hash,
      JSON_VALUE(e.attributes, '$.reuse_source')                           AS reuse_source,
      e.link_method,
      e.link_confidence
    FROM dedup_event AS e
    WHERE e.event_type = 'output.generated'
      AND JSON_VALUE(e.attributes, '$.output_id') IS NOT NULL
  ),

  -- ===================================================================================
  -- STEP 3 — AR-1 ENFORCEMENT: one output, one run
  -- ===================================================================================
  -- "An output is attributed to exactly one run_id (the run that emitted
  -- output.generated). If a later run modifies it, that is a NEW output with
  -- parent_output_id set, not a re-attribution."
  --
  -- Two runs emitting output.generated for the same output_id is an AR-1 breach.
  -- DQ-16 says: QUARANTINE BOTH, block from aggregates until resolved. We keep the
  -- EARLIEST claim as the row (so the table still has one row per output_id, which is
  -- itself part of AR-1) and stamp is_quarantined so every downstream aggregate
  -- excludes it. Silently picking a winner would let a double-claim quietly inflate
  -- one agent's numbers.
  output_claims AS (
    SELECT
      output_id,
      COUNT(DISTINCT run_id) AS claiming_run_count
    FROM output_raw
    GROUP BY output_id
  ),

  output_deduped AS (
    SELECT
      o.*,
      (c.claiming_run_count > 1) AS is_quarantined
    FROM output_raw AS o
    JOIN output_claims AS c USING (output_id)
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY o.output_id
      ORDER BY o.generated_at ASC, o.run_id ASC
    ) = 1
  ),

  -- ===================================================================================
  -- STEP 4 — commits, and which outputs they carry
  -- ===================================================================================
  -- scm.commit (git hook) carries an ARRAY of output_ids. Unnesting it gives the
  -- output -> commit edge. This is the L1 explicit link of design §5.3: the AI-Run-Id
  -- trailer written by prepare-commit-msg is what makes it deterministic.
  commit_event AS (
    SELECT
      e.run_id,
      e.event_time                                                     AS committed_at,
      JSON_VALUE(e.attributes, '$.commit_sha')                         AS commit_sha,
      SAFE_CAST(JSON_VALUE(e.attributes, '$.lines_added')   AS INT64)  AS lines_added,
      SAFE_CAST(JSON_VALUE(e.attributes, '$.lines_removed') AS INT64)  AS lines_removed,
      SAFE_CAST(JSON_VALUE(e.attributes, '$.has_ai_marker') AS BOOL)   AS has_ai_marker,
      JSON_VALUE_ARRAY(e.attributes, '$.output_ids')                   AS output_ids,
      e.repo_full_name
    FROM dedup_event AS e
    WHERE e.event_type = 'scm.commit'
  ),

  output_commit AS (
    SELECT
      oid          AS output_id,
      c.commit_sha,
      c.committed_at,
      c.lines_added,
      c.lines_removed,
      c.has_ai_marker,
      c.repo_full_name
    FROM commit_event AS c, UNNEST(c.output_ids) AS oid
  ),

  -- Commit -> PR edge. scm.pr.created carries the commit SHAs the PR contains.
  -- A commit can appear in more than one PR (cherry-pick, re-target); the EARLIEST PR
  -- wins, because that is the PR in which the output was actually first reviewed.
  pr_commit AS (
    SELECT
      sha AS commit_sha,
      SAFE_CAST(JSON_VALUE(e.attributes, '$.pr_id') AS INT64) AS pr_id,
      e.repo_full_name,
      e.event_time AS pr_created_event_time
    FROM dedup_event AS e, UNNEST(JSON_VALUE_ARRAY(e.attributes, '$.commit_shas')) AS sha
    WHERE e.event_type = 'scm.pr.created'
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY sha, e.repo_full_name ORDER BY e.event_time ASC
    ) = 1
  ),

  -- ===================================================================================
  -- STEP 5 — each output's SCM journey
  -- ===================================================================================
  -- FIRST commit only. An output's fate is decided in the PR where it first landed;
  -- later commits touching the same file belong to later outputs (AR-1).
  output_scm AS (
    SELECT
      oc.output_id,
      MIN(oc.committed_at)                              AS first_commit_at,
      COUNT(DISTINCT oc.commit_sha)                     AS commit_count,
      LOGICAL_OR(COALESCE(oc.has_ai_marker, FALSE))     AS has_ai_marker,
      ARRAY_AGG(oc.commit_sha ORDER BY oc.committed_at ASC LIMIT 1)[SAFE_OFFSET(0)]
                                                        AS first_commit_sha,
      ANY_VALUE(oc.repo_full_name)                      AS repo_full_name
    FROM output_commit AS oc
    GROUP BY oc.output_id
  ),

  output_pr AS (
    SELECT
      oc.output_id,
      pc.pr_id,
      pc.repo_full_name
    FROM output_commit AS oc
    JOIN pr_commit AS pc
      ON  pc.commit_sha     = oc.commit_sha
      AND pc.repo_full_name = oc.repo_full_name
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY oc.output_id ORDER BY oc.committed_at ASC
    ) = 1
  ),

  -- ===================================================================================
  -- STEP 6 — ⭐ POST-REVIEW CHANGE ACCOUNTING (design §8.9)
  -- ===================================================================================
  -- The attribution boundary is first_review_at, taken from core.fct_pull_request.
  -- §8.14 defines it as the earliest review action BY A PERSON OTHER THAN THE AUTHOR —
  -- self-comments are not review, and using them would let an agent that comments on
  -- its own PR reclassify its auto-fixes as post-review rework.
  --
  --   commits BEFORE first_review_at  -> auto_fix_cycles, lines_changed_pre_review
  --                                      (the agent's own loop — NOT rework)
  --   commits AFTER  first_review_at  -> lines_changed_after_first_review
  --                                      (the ONLY numerator of rework)
  --
  -- When first_review_at is NULL the PR was merged without review, so no commit can
  -- be post-review: the rework numerator is 0 by construction, not NULL. Everything
  -- lands in the pre-review bucket.
  --
  -- GRAIN NOTE — read before "fixing" this.
  -- CONTRACT §5 states the ratio per output; design §8.9 states it summed over a PR
  -- (Σ lines_changed / Σ lines_generated). These are reconciled by computing the ratio
  -- ONCE PER PR and propagating it to every output in that PR. That is the honest
  -- reading: post-review commits change FILES, and without git-blame line-level
  -- attribution (§8.9 [A], not available from the Bitbucket API alone) there is no
  -- defensible way to split a post-review commit across the individual outputs that
  -- preceded it. Splitting it pro-rata by lines_generated would invent precision that
  -- the source data does not contain.
  pr_facts AS (
    SELECT
      pr.pr_id,
      pr.repo_full_name,
      pr.created_on          AS pr_created_at,
      pr.first_review_at,
      pr.merged_at,
      pr.declined_at,
      pr.merge_commit_sha,
      pr.is_reverted,
      pr.reverted_at,
      pr.total_changed_lines,
      pr.ai_attributed_lines,
      pr.ai_line_share,
      pr.is_draft,
      pr.is_bot_only
    FROM `${PROJECT_ID}.core.fct_pull_request` AS pr
    WHERE pr.created_on >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(),
                                         INTERVAL (rebuild_days + 30) DAY)
  ),

  -- Per-PR split of commit activity around the review boundary.
  pr_change_split AS (
    SELECT
      p.pr_id,
      p.repo_full_name,
      -- Pre-review: the agent auto-fix loop. Reported, never counted as rework.
      SUM(IF(p.first_review_at IS NULL OR oc.committed_at < p.first_review_at,
             COALESCE(oc.lines_added, 0) + COALESCE(oc.lines_removed, 0), 0))
                                                                  AS lines_changed_pre_review,
      COUNT(DISTINCT IF(p.first_review_at IS NULL OR oc.committed_at < p.first_review_at,
                        oc.commit_sha, NULL))                     AS auto_fix_cycles,
      -- Post-review: the ONLY rework numerator (§8.9).
      SUM(IF(p.first_review_at IS NOT NULL AND oc.committed_at >= p.first_review_at,
             COALESCE(oc.lines_added, 0) + COALESCE(oc.lines_removed, 0), 0))
                                                                  AS lines_changed_after_review
    FROM pr_facts AS p
    JOIN pr_commit AS pc
      ON pc.pr_id = p.pr_id AND pc.repo_full_name = p.repo_full_name
    JOIN output_commit AS oc
      ON oc.commit_sha = pc.commit_sha AND oc.repo_full_name = pc.repo_full_name
    GROUP BY p.pr_id, p.repo_full_name
  ),

  -- Denominator: Σ lines_generated over the AI outputs in that PR (§8.9).
  -- AR-5: reused artifacts contribute ZERO lines_generated — they were not generated.
  pr_generated_lines AS (
    SELECT
      op.pr_id,
      op.repo_full_name,
      SUM(IF(od.reuse_source IS NULL,
             COALESCE(od.lines_added, 0) + COALESCE(od.lines_removed, 0),
             0)) AS lines_generated_in_pr
    FROM output_pr AS op
    JOIN output_deduped AS od USING (output_id)
    GROUP BY op.pr_id, op.repo_full_name
  ),

  pr_rework AS (
    SELECT
      f.pr_id,
      f.repo_full_name,
      f.pr_created_at,
      f.first_review_at,
      f.merged_at,
      f.declined_at,
      f.merge_commit_sha,
      f.is_reverted,
      f.reverted_at,
      f.ai_line_share,                                   -- AR-7, fractional not binary
      COALESCE(cs.lines_changed_pre_review, 0)           AS lines_changed_pre_review,
      COALESCE(cs.lines_changed_after_review, 0)         AS lines_changed_after_review,
      COALESCE(cs.auto_fix_cycles, 0)                    AS auto_fix_cycles,
      gl.lines_generated_in_pr,
      -- post_review_change_ratio (CONTRACT §5, design §8.9).
      -- NULL — not 0 — when the denominator is zero or unknown: a ratio with no
      -- denominator is undefined, and reporting it as 0 would classify a
      -- zero-line output as flawlessly accepted.
      SAFE_DIVIDE(
        COALESCE(cs.lines_changed_after_review, 0),
        NULLIF(gl.lines_generated_in_pr, 0)
      )                                                  AS post_review_change_ratio
    FROM pr_facts AS f
    LEFT JOIN pr_change_split    AS cs USING (pr_id, repo_full_name)
    LEFT JOIN pr_generated_lines AS gl USING (pr_id, repo_full_name)
  ),

  -- ===================================================================================
  -- STEP 7 — run context (AR-3, AR-4)
  -- ===================================================================================
  -- Dimensions come from the EMITTING run only. AR-4: the supervisor never inherits a
  -- sub-agent's outputs, so this join is to fct_ai_run.run_id and never walks up
  -- parent_run_id. Supervisor totals are obtained by rolling up trace_id in the
  -- metrics layer instead.
  -- AR-3: jira_issue_key on the run is already the FEATURE ticket, with the QualDev
  -- delivery ticket carried separately, so it propagates correctly by simple copy.
  run_ctx AS (
    SELECT
      r.run_id,
      r.person_id,
      r.team_id,
      r.agent_name,
      r.agent_version,
      r.skill_name,
      r.model_id,
      r.jira_issue_key,
      r.delivery_ticket_key,
      r.jira_project_key,
      r.repo_full_name
    FROM `${PROJECT_ID}.core.fct_ai_run` AS r
    WHERE r.started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL (rebuild_days + 2) DAY)
  ),

  -- ===================================================================================
  -- STEP 8 — assemble, then run the state machine
  -- ===================================================================================
  assembled AS (
    SELECT
      o.output_id,
      o.parent_output_id,
      o.run_id,
      COALESCE(o.trace_id, NULL)                              AS trace_id,
      o.generated_at,
      o.artifact_type,
      o.file_path,
      o.lines_added,
      o.lines_removed,
      o.output_content_hash,
      o.reuse_source,
      (o.reuse_source IS NOT NULL)                            AS is_reused,   -- AR-5

      rc.person_id,
      rc.team_id,
      rc.agent_name,
      rc.agent_version,
      rc.skill_name,
      rc.model_id,
      rc.jira_issue_key,
      rc.delivery_ticket_key,                                                -- AR-3
      rc.jira_project_key,
      COALESCE(rc.repo_full_name, sc.repo_full_name)          AS repo_full_name,

      sc.first_commit_sha,
      sc.first_commit_at,
      COALESCE(sc.commit_count, 0)                            AS commit_count,
      COALESCE(sc.has_ai_marker, FALSE)                       AS has_ai_marker,

      op.pr_id,
      pw.pr_created_at,
      pw.first_review_at,
      pw.merged_at,
      pw.declined_at,
      -- AR-9: only a revert INSIDE the 30-day window withdraws credit (CONTRACT §5).
      -- A revert on day 31 is ordinary maintenance, not a rejection of the output.
      IF(pw.is_reverted
         AND pw.reverted_at IS NOT NULL
         AND pw.merged_at   IS NOT NULL
         AND TIMESTAMP_DIFF(pw.reverted_at, pw.merged_at, DAY) <= revert_window_days,
         pw.reverted_at, NULL)                                AS reverted_at,
      IF(pw.merged_at IS NOT NULL AND pw.reverted_at IS NOT NULL,
         TIMESTAMP_DIFF(pw.reverted_at, pw.merged_at, DAY), NULL) AS days_to_revert,

      -- lines_generated: AR-5 zeroes reused artifacts.
      IF(o.reuse_source IS NULL,
         COALESCE(o.lines_added, 0) + COALESCE(o.lines_removed, 0),
         0)                                                   AS lines_generated,
      pw.lines_changed_pre_review,
      pw.lines_changed_after_review                           AS lines_changed_after_first_review,
      pw.auto_fix_cycles,
      pw.post_review_change_ratio,
      pw.ai_line_share,                                                      -- AR-7

      -- CONTRACT §5 maturity: the clock runs from PR CREATION, not from generation.
      TIMESTAMP_ADD(pw.pr_created_at, INTERVAL maturity_window_days DAY)     AS maturity_at,
      TIMESTAMP_ADD(pw.merged_at,     INTERVAL revert_window_days   DAY)     AS revert_window_ends_at,
      (pw.pr_created_at IS NOT NULL
       AND TIMESTAMP_ADD(pw.pr_created_at, INTERVAL maturity_window_days DAY)
             <= CURRENT_TIMESTAMP())                          AS is_mature,

      o.link_method,
      o.link_confidence,
      o.is_quarantined                                                        -- AR-1 / DQ-16
    FROM output_deduped AS o
    LEFT JOIN run_ctx    AS rc USING (run_id)
    LEFT JOIN output_scm AS sc USING (output_id)
    LEFT JOIN output_pr  AS op USING (output_id)
    LEFT JOIN pr_rework  AS pw
      ON  pw.pr_id          = op.pr_id
      AND pw.repo_full_name = op.repo_full_name
  ),

  -- ===================================================================================
  -- STEP 9 — ⭐ THE ACCEPTANCE STATE MACHINE (CONTRACT §5)
  -- ===================================================================================
  --   generated ──> in_flight ──┬──> accepted   (merged AND ratio <= 0.25
  --                             │                AND NOT reverted within 30d)
  --                             ├──> reworked   (merged AND ratio > 0.25)
  --                             ├──> rejected   (PR declined OR never committed in 7d)
  --                             └──> reverted   (was merged, revert within 30d)
  --
  -- BRANCH ORDER IS THE SPECIFICATION. Each rule below is written in precedence
  -- order and the reasons are recorded, so a state is auditable without re-deriving.
  state_machine AS (
    SELECT
      a.*,
      CASE
        -- (0) AR-1 breach. DQ-16 quarantines BOTH claimants. The row is parked in
        --     'generated' and is_quarantined blocks it from every aggregate until a
        --     steward resolves it. Classifying a contested output would let a
        --     double-claim silently inflate one agent's acceptance numbers.
        WHEN a.is_quarantined THEN 'generated'

        -- (1) AR-5. A deduplicated artifact was REUSED, not generated. It contributes
        --     to reuse metrics and ZERO to output volume, so it never enters the
        --     acceptance machine at all.
        WHEN a.is_reused THEN 'generated'

        -- (2) AR-9 — HIGHEST PRECEDENCE TERMINAL STATE. A revert withdraws credit,
        --     overriding an earlier 'accepted'. It is evaluated before maturity
        --     because a revert is definitive information: waiting out a maturity
        --     window on work that has already been undone would publish a merged
        --     output as in_flight while the revert sits in the repo.
        WHEN a.reverted_at IS NOT NULL THEN 'reverted'

        -- (3) PR declined => rejected (CONTRACT §5). Terminal immediately: a declined
        --     PR cannot mature into anything. The maturity window exists to stop us
        --     judging work that is still MOVING; declined work has stopped.
        WHEN a.declined_at IS NOT NULL THEN 'rejected'

        -- (4) Never committed within 7 days => rejected (CONTRACT §5, DQ-3).
        --     Clock runs from generated_at — there is no PR to date it from.
        WHEN a.first_commit_at IS NULL
             AND a.generated_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(),
                                                INTERVAL commit_deadline_days DAY)
          THEN 'rejected'

        -- (5) Not yet committed, still inside the 7-day grace.
        WHEN a.first_commit_at IS NULL THEN 'in_flight'

        -- (6) Committed but no PR yet. NOT rejected — DQ-9 flags this as a probable
        --     direct-to-branch push bypassing the flow. Calling it rejected would
        --     punish an output for a process gap it had no part in.
        WHEN a.pr_id IS NULL THEN 'in_flight'

        -- (7) PR still open.
        WHEN a.merged_at IS NULL THEN 'in_flight'

        -- (8) MATURITY WINDOW (CONTRACT §5). Merged, but the PR is younger than 7
        --     days: hold at in_flight and EXCLUDE from acceptance-rate denominators.
        --     A revert can still arrive, and classifying too early produces an
        --     acceptance rate that quietly revises itself downward every week.
        WHEN NOT a.is_mature THEN 'in_flight'

        -- (9) Merged, mature, not reverted. Ratio decides accepted vs reworked.
        --     ⚠ The 0.25 threshold is a POLICY CHOICE, not a fact (§8.7). It must be
        --     stated on every dashboard showing this metric, with sensitivity
        --     published at 0.10 / 0.25 / 0.50.
        WHEN a.post_review_change_ratio IS NOT NULL
             AND a.post_review_change_ratio > accept_change_ratio_threshold
          THEN 'reworked'
        WHEN a.post_review_change_ratio IS NOT NULL
             AND a.post_review_change_ratio <= accept_change_ratio_threshold
          THEN 'accepted'

        -- (10) Merged and mature, but the ratio is undefined — the output generated
        --      no measurable lines (a spec or config artifact), so there is nothing
        --      for a reviewer to have rewritten. Accepted, with a DISTINCT reason so
        --      the case stays visible and auditable rather than hiding inside the
        --      ordinary accepted bucket.
        ELSE 'accepted'
      END AS acceptance_state,

      CASE
        WHEN a.is_quarantined THEN 'attribution_conflict_dq16'
        WHEN a.is_reused THEN 'reused_artifact_ar5'
        WHEN a.reverted_at IS NOT NULL THEN 'reverted_within_30d'
        WHEN a.declined_at IS NOT NULL THEN 'pr_declined'
        WHEN a.first_commit_at IS NULL
             AND a.generated_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(),
                                                INTERVAL commit_deadline_days DAY)
          THEN 'never_committed_7d'
        WHEN a.first_commit_at IS NULL THEN 'awaiting_commit'
        WHEN a.pr_id IS NULL THEN 'committed_no_pr'
        WHEN a.merged_at IS NULL THEN 'pr_open'
        WHEN NOT a.is_mature THEN 'inside_maturity_window'
        WHEN a.post_review_change_ratio IS NULL THEN 'merged_ratio_undefined'
        WHEN a.post_review_change_ratio > accept_change_ratio_threshold
          THEN 'merged_high_change'
        ELSE 'merged_low_change'
      END AS acceptance_state_reason,

      -- AR-9: "State -> reverted in the period the revert OCCURS, not retroactively."
      -- state_changed_at is the timestamp of the event that produced the state, so a
      -- revert lands in the revert's period and published history stays stable.
      CASE
        WHEN a.reverted_at     IS NOT NULL THEN a.reverted_at
        WHEN a.declined_at     IS NOT NULL THEN a.declined_at
        WHEN a.merged_at       IS NOT NULL AND a.is_mature THEN GREATEST(a.merged_at, a.maturity_at)
        WHEN a.merged_at       IS NOT NULL THEN a.merged_at
        WHEN a.first_commit_at IS NOT NULL THEN a.first_commit_at
        ELSE a.generated_at
      END AS state_changed_at
    FROM assembled AS a
  )

  SELECT
    sm.*,
    sm.acceptance_state IN ('accepted', 'reworked', 'rejected', 'reverted')
      AS is_terminal_state,
    CURRENT_TIMESTAMP() AS transformed_at
  FROM state_machine AS sm

) AS src
ON tgt.output_id = src.output_id

WHEN MATCHED THEN UPDATE SET
  parent_output_id                 = src.parent_output_id,
  run_id                           = src.run_id,
  trace_id                         = src.trace_id,
  generated_at                     = src.generated_at,
  artifact_type                    = src.artifact_type,
  file_path                        = src.file_path,
  lines_added                      = src.lines_added,
  lines_removed                    = src.lines_removed,
  output_content_hash              = src.output_content_hash,
  reuse_source                     = src.reuse_source,
  is_reused                        = src.is_reused,
  person_id                        = src.person_id,
  team_id                          = src.team_id,
  agent_name                       = src.agent_name,
  agent_version                    = src.agent_version,
  skill_name                       = src.skill_name,
  model_id                         = src.model_id,
  jira_issue_key                   = src.jira_issue_key,
  delivery_ticket_key              = src.delivery_ticket_key,
  jira_project_key                 = src.jira_project_key,
  repo_full_name                   = src.repo_full_name,
  first_commit_sha                 = src.first_commit_sha,
  first_commit_at                  = src.first_commit_at,
  commit_count                     = src.commit_count,
  has_ai_marker                    = src.has_ai_marker,
  pr_id                            = src.pr_id,
  pr_created_at                    = src.pr_created_at,
  first_review_at                  = src.first_review_at,
  merged_at                        = src.merged_at,
  declined_at                      = src.declined_at,
  reverted_at                      = src.reverted_at,
  days_to_revert                   = src.days_to_revert,
  lines_generated                  = src.lines_generated,
  lines_changed_pre_review         = src.lines_changed_pre_review,
  lines_changed_after_first_review = src.lines_changed_after_first_review,
  auto_fix_cycles                  = src.auto_fix_cycles,
  post_review_change_ratio         = src.post_review_change_ratio,
  ai_line_share                    = src.ai_line_share,
  acceptance_state                 = src.acceptance_state,
  acceptance_state_reason          = src.acceptance_state_reason,
  is_terminal_state                = src.is_terminal_state,
  is_mature                        = src.is_mature,
  maturity_at                      = src.maturity_at,
  revert_window_ends_at            = src.revert_window_ends_at,
  -- Only advance state_changed_at when the state genuinely changed. Rewriting it on
  -- every nightly run would destroy AR-9's period assignment.
  state_changed_at                 = IF(tgt.acceptance_state IS DISTINCT FROM src.acceptance_state,
                                        src.state_changed_at,
                                        tgt.state_changed_at),
  link_method                      = src.link_method,
  link_confidence                  = src.link_confidence,
  is_quarantined                   = src.is_quarantined,
  transformed_at                   = src.transformed_at

WHEN NOT MATCHED THEN INSERT (
  output_id, parent_output_id, run_id, trace_id,
  generated_at, artifact_type, file_path, lines_added, lines_removed,
  output_content_hash, reuse_source, is_reused,
  person_id, team_id, agent_name, agent_version, skill_name, model_id,
  jira_issue_key, delivery_ticket_key, jira_project_key, repo_full_name,
  first_commit_sha, first_commit_at, commit_count, has_ai_marker,
  pr_id, pr_created_at, first_review_at, merged_at, declined_at,
  reverted_at, days_to_revert,
  lines_generated, lines_changed_pre_review, lines_changed_after_first_review,
  auto_fix_cycles, post_review_change_ratio, ai_line_share,
  acceptance_state, acceptance_state_reason, is_terminal_state,
  is_mature, maturity_at, revert_window_ends_at, state_changed_at,
  link_method, link_confidence, is_quarantined, transformed_at
)
VALUES (
  src.output_id, src.parent_output_id, src.run_id, src.trace_id,
  src.generated_at, src.artifact_type, src.file_path, src.lines_added, src.lines_removed,
  src.output_content_hash, src.reuse_source, src.is_reused,
  src.person_id, src.team_id, src.agent_name, src.agent_version, src.skill_name, src.model_id,
  src.jira_issue_key, src.delivery_ticket_key, src.jira_project_key, src.repo_full_name,
  src.first_commit_sha, src.first_commit_at, src.commit_count, src.has_ai_marker,
  src.pr_id, src.pr_created_at, src.first_review_at, src.merged_at, src.declined_at,
  src.reverted_at, src.days_to_revert,
  src.lines_generated, src.lines_changed_pre_review, src.lines_changed_after_first_review,
  src.auto_fix_cycles, src.post_review_change_ratio, src.ai_line_share,
  src.acceptance_state, src.acceptance_state_reason, src.is_terminal_state,
  src.is_mature, src.maturity_at, src.revert_window_ends_at, src.state_changed_at,
  src.link_method, src.link_confidence, src.is_quarantined, src.transformed_at
);
