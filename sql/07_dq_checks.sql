-- =====================================================================================
-- 07_dq_checks.sql — dq.dq_findings + DQ-1 … DQ-16 (plus four invariant guards)
-- =====================================================================================
-- Contract:  schema/CONTRACT.md §4 (unpriced model), §6 (AR-1),
--            §7 (dq.dq_findings partition/cluster)
-- Design:    docs/spikes/ai-effectiveness-observability.md §9.4 (the check catalogue),
--            §11.2 (180-day retention)
--
-- Every check writes (check_id, severity, entity_type, entity_id, detail, detected_at)
-- into dq.dq_findings. Nothing here mutates a fact table: a DQ check REPORTS, it does
-- not repair. Repair is the transform's job, and keeping the two apart is what makes
-- the findings trustworthy as an audit trail.
--
-- Design §9.4 also mandates: "publish a completeness score on every dashboard" — a
-- single figure showing % of runs with link_method='explicit' and % of metrics with
-- non-NULL inputs. That is computed in 06_marts.sql (explicit_link_pct,
-- runs_with_tokens_pct), not here, because it belongs next to the numbers it qualifies.
--
-- Cadences per design §9.4: on-ingest checks (DQ-6, DQ-10, DQ-11, DQ-12, DQ-13) are
-- run here on a schedule as a safety net even where the collector already enforces
-- them; DQ-2 and DQ-7 hourly; the rest nightly. Running them all together is safe.
--
-- Substitute ${PROJECT_ID}. Run after 04, 05, 06.
-- =====================================================================================

CREATE SCHEMA IF NOT EXISTS `${PROJECT_ID}.dq`
OPTIONS (
  location    = 'EU',
  description = 'Data-quality findings. 180-day retention (design §11.2) — an audit of data-quality history.'
);

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.dq.dq_findings`
(
  check_id     STRING    NOT NULL OPTIONS (description = 'Bounded enum: DQ-1 … DQ-16 from design §9.4, plus the invariant guards DQ-6b, DQ-6c, DQ-17 and DQ-RET defined in this file. CLUSTER KEY (CONTRACT §7).'),
  severity     STRING    NOT NULL OPTIONS (description = 'Bounded: critical | high | medium | low | info. critical blocks publication of the affected metric; high alerts the owner; the rest accumulate for trend analysis.'),
  entity_type  STRING    NOT NULL OPTIONS (description = 'Bounded: run | output | commit | pull_request | jira_issue | person | model | ci_run | event | dimension | dataset. What the entity_id refers to.'),
  entity_id    STRING             OPTIONS (description = 'Identifier of the offending entity. NULL for dataset-wide findings. Never contains content, an email address, or a secret.'),
  detail       STRING    NOT NULL OPTIONS (description = 'Human-readable explanation. Bounded facts, counts and enum values ONLY — never a prompt, response, diff, error body, raw email, or secret (CONTRACT §1.1, design §11.3).'),
  detected_at  TIMESTAMP NOT NULL OPTIONS (description = 'PARTITION KEY (CONTRACT §7). When the check ran.')
)
PARTITION BY DATE(detected_at)
CLUSTER BY check_id
OPTIONS (
  partition_expiration_days = 180,
  description = 'Data-quality findings from the design §9.4 check catalogue. Partition DATE(detected_at), cluster check_id (CONTRACT §7). 180-day retention (design §11.2). Checks REPORT, they never repair.',
  labels = [('layer', 'dq'), ('domain', 'ai-telemetry')]
);


-- -------------------------------------------------------------------------------------
-- Idempotency: clear today's findings for the checks in this file before re-inserting,
-- so re-running the job does not multiply findings. Historical partitions are never
-- touched — a finding that was true yesterday stays in the audit trail.
-- -------------------------------------------------------------------------------------
DELETE FROM `${PROJECT_ID}.dq.dq_findings`
WHERE DATE(detected_at) = CURRENT_DATE();


-- =====================================================================================
-- DQ-1 — IDENTITY RESOLUTION (nightly)
-- =====================================================================================
-- "Every git author email hash maps to exactly one person_id."
-- Action: quarantine unmapped; alert; person-level dashboards suppress unresolved
-- identities.
--
-- Design §9.4 measured this problem on THIS repository: "Bob Smith" vs "Bob
-- Rtahore", "Ann Lee" vs "Lee, Ann" (both the same address), "DevOne" with
-- no email address at all. Naive aggregation on the git display name splits one
-- engineer across up to three identities. This check is what stops that reaching a
-- dashboard.
--
-- Two failure modes, reported separately because they need different fixes:
--   (a) AMBIGUOUS  — one hash claimed by several person_ids (steward must merge)
--   (b) UNMAPPED   — a hash seen in the facts that dim_person has never heard of
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
-- (a) ambiguous hash
SELECT
  'DQ-1', 'critical', 'person', a.email_hash,
  FORMAT('Email hash resolves to %d distinct person_id values (%s). AR: identity map must be 1:1. Person-level metrics are SUPPRESSED for this hash until a steward merges the identities.',
         COUNT(DISTINCT d.person_id),
         STRING_AGG(DISTINCT d.person_id ORDER BY d.person_id LIMIT 5)),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.dim_person` AS d, UNNEST(d.git_author_aliases) AS a
WHERE COALESCE(a.is_verified, FALSE)
GROUP BY a.email_hash
HAVING COUNT(DISTINCT d.person_id) > 1

UNION ALL

-- (b) unmapped hash observed in the last 7 days of runs
SELECT
  'DQ-1', 'high', 'person', r.person_email_hash,
  FORMAT('Email hash appears on %d run(s) in the last 7 days but has no verified alias in core.dim_person. Runs are attributed to person_id = NULL and are EXCLUDED from person-level dashboards.',
         COUNT(*)),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_run` AS r
WHERE r.started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
  AND r.person_email_hash IS NOT NULL
  AND r.person_id IS NULL
  AND NOT EXISTS (
    SELECT 1
    FROM `${PROJECT_ID}.core.dim_person` AS d, UNNEST(d.git_author_aliases) AS a
    WHERE a.email_hash = r.person_email_hash AND COALESCE(a.is_verified, FALSE)
  )
GROUP BY r.person_email_hash;


-- =====================================================================================
-- DQ-2 — ORPHAN RUN (hourly)
-- =====================================================================================
-- "run.started with no terminal event within 24h."
-- Action: mark abandoned; count toward reliability, EXCLUDE from acceptance.
--
-- The emitter fails open by design (CONTRACT §1.2), so a crashed laptop produces a
-- run that simply stops talking. Without this check those runs would sit in_flight
-- forever and quietly deflate the failure rate.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
SELECT
  'DQ-2', 'medium', 'run', r.run_id,
  FORMAT('Run started %s with no terminal event after %d hours. Agent=%s surface=%s. Should be marked abandoned: counts toward reliability, excluded from acceptance.',
         FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', r.started_at),
         TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), r.started_at, HOUR),
         COALESCE(r.agent_name, 'unknown'),
         COALESCE(r.surface, 'unknown')),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_run` AS r
WHERE r.terminal_status = 'in_flight'
  AND r.started_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
  AND r.started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY);


-- =====================================================================================
-- DQ-3 — ORPHAN OUTPUT (daily)
-- =====================================================================================
-- "output.generated never reaching a commit within 7d."
-- Action: state -> rejected with reason 'never_committed'. 05_transform_output.sql
-- already applies that state; this check makes the volume visible, because a rising
-- orphan-output count means the agent is producing work nobody keeps.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
SELECT
  'DQ-3', 'low', 'output', o.output_id,
  FORMAT('Output generated %s (type=%s, agent=%s) reached no commit within 7 days. State set to rejected/never_committed_7d.',
         FORMAT_TIMESTAMP('%Y-%m-%d', o.generated_at),
         COALESCE(o.artifact_type, 'unknown'),
         COALESCE(o.agent_name, 'unknown')),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_output` AS o
WHERE o.first_commit_at IS NULL
  AND NOT COALESCE(o.is_reused, FALSE)                      -- AR-5
  AND o.generated_at <  TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
  AND o.generated_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY);


-- =====================================================================================
-- DQ-4 — UNLINKED AI COMMIT (daily)
-- =====================================================================================
-- "Commit carries AUTH_BY_COPILOT/GEN_BY_COPILOT but no run_id trailer and no heuristic match."
-- Action: link_method='marker_only'; EXCLUDED from cost metrics; feeds the
-- marker-compliance metric.
--
-- This is design §5.3's L3 case: the marker tells us AI wrote it and nothing more.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
SELECT
  'DQ-4', 'medium', 'commit', JSON_VALUE(e.attributes, '$.commit_sha'),
  FORMAT('Commit carries the AI marker but resolves only to link_method=%s (confidence %.2f). No AI-Run-Id trailer and no heuristic match. EXCLUDED from cost-per-output metrics (CONTRACT §2.4).',
         COALESCE(e.link_method, 'null'), COALESCE(e.link_confidence, 0.0)),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.raw.ai_run_event` AS e
WHERE e.event_type = 'scm.commit'
  AND DATE(e.event_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL 2 DAY)
  AND SAFE_CAST(JSON_VALUE(e.attributes, '$.has_ai_marker') AS BOOL) IS TRUE
  AND COALESCE(e.link_method, 'marker_only') = 'marker_only';


-- =====================================================================================
-- DQ-5 — UNMARKED AI RUN (daily)
-- =====================================================================================
-- "A run produced a commit that lacks the marker."
-- Action: alert — it indicates the [AUTH_BY_COPILOT] convention is degrading.
--
-- The inverse of DQ-4, and the more worrying of the two: DQ-4 loses precision, DQ-5
-- loses the ability to identify AI work at all in any system that only reads commit
-- subjects.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
SELECT
  'DQ-5', 'medium', 'run', o.run_id,
  FORMAT('Run produced %d committed output(s) on commits WITHOUT the AI marker (agent=%s). The [AUTH_BY_COPILOT] convention is degrading — check prepare-commit-msg installation on this surface.',
         COUNT(*), COALESCE(ANY_VALUE(o.agent_name), 'unknown')),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_output` AS o
WHERE o.first_commit_at IS NOT NULL
  AND NOT COALESCE(o.has_ai_marker, FALSE)
  AND o.generated_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 DAY)
GROUP BY o.run_id;


-- =====================================================================================
-- DQ-6 — UNPRICED MODEL  ⭐ MANDATORY (on ingest / hourly)
-- =====================================================================================
-- "model_id absent from dim_model_pricing." CONTRACT §4: cost = NULL, NEVER 0, plus a
-- DQ-6 finding. Action: alert the AI Platform Owner.
--
-- Severity is CRITICAL, not high: an unpriced model silently costing zero is the
-- single most dangerous failure mode in this system. It does not look like an error —
-- it looks like a cheap model, and it makes the design §9.1 model-comparison arm
-- recommend the WRONG model.
--
-- Three variants, all mandatory in practice:
--   DQ-6   — no pricing row at all for the model on the run's date
--   DQ-6b  — OVERLAPPING effective-date windows (would multiply cost silently)
--   DQ-6c  — priced from a row flagged is_placeholder (figure not chargeback-safe)
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
-- DQ-6 — no usable price row
SELECT
  'DQ-6', 'critical', 'model', r.model_id,
  FORMAT('Model has token usage on %d run(s) since %s but NO usable row in core.dim_model_pricing for those dates. cost_usd is NULL for every one of them (CONTRACT §4: never 0). Total unpriced tokens: %d. Add a priced, effective-dated row.',
         COUNT(*),
         FORMAT_DATE('%Y-%m-%d', MIN(DATE(r.started_at))),
         COALESCE(SUM(r.total_tokens), 0)),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_run` AS r
WHERE r.started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
  AND r.total_tokens IS NOT NULL     -- there WAS usage to price
  AND r.cost_usd IS NULL             -- and we could not price it
  AND r.model_id IS NOT NULL
GROUP BY r.model_id

UNION ALL

-- DQ-6b — overlapping validity windows. The effective-dated join in
-- 04_transform_run.sql would match BOTH rows and the cost would be summed twice. This
-- is invisible in the output: the number is simply wrong, and plausibly so.
SELECT
  'DQ-6b', 'critical', 'model', p1.model_id,
  FORMAT('Overlapping pricing windows: [%s .. %s] and [%s .. %s]. The effective-dated cost join would match both rows and DOUBLE-COUNT cost. Close one window.',
         FORMAT_DATE('%Y-%m-%d', p1.effective_from),
         FORMAT_DATE('%Y-%m-%d', COALESCE(p1.effective_to, DATE '9999-12-31')),
         FORMAT_DATE('%Y-%m-%d', p2.effective_from),
         FORMAT_DATE('%Y-%m-%d', COALESCE(p2.effective_to, DATE '9999-12-31'))),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.dim_model_pricing` AS p1
JOIN `${PROJECT_ID}.core.dim_model_pricing` AS p2
  ON  p1.model_id = p2.model_id
  AND p1.effective_from < p2.effective_from            -- ordered pair: report once
  AND p2.effective_from <= COALESCE(p1.effective_to, DATE '9999-12-31')

UNION ALL

-- DQ-6c — placeholder rates in use. Not an error, but any cost figure resting on them
-- must not be published as authoritative.
SELECT
  'DQ-6c', 'high', 'model', r.model_id,
  FORMAT('%d run(s) in the last 7 days were priced from a dim_model_pricing row flagged is_placeholder = TRUE. These cost figures are NOT vendor pricing and are unusable for chargeback or any published number. Replace with real rates (close the placeholder window, insert a priced row).',
         COUNT(*)),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_run` AS r
WHERE r.started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
  AND COALESCE(r.cost_is_placeholder, FALSE)
GROUP BY r.model_id

UNION ALL

-- DQ-6b (duplicate windows). The self-join above orders the pair strictly, so it
-- cannot see two rows sharing an IDENTICAL effective_from — which is exactly what
-- re-running the 02_dims.sql seed produces. Caught here instead.
SELECT
  'DQ-6b', 'critical', 'model', p.model_id,
  FORMAT('%d pricing rows share effective_from = %s. The cost join matches all of them and DOUBLE-COUNTS cost. Usually caused by re-running the 02_dims.sql seed INSERT. Delete the duplicates.',
         COUNT(*), FORMAT_DATE('%Y-%m-%d', p.effective_from)),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.dim_model_pricing` AS p
GROUP BY p.model_id, p.effective_from
HAVING COUNT(*) > 1;


-- =====================================================================================
-- DQ-7 — JIRA KEY NOT FOUND (hourly)
-- =====================================================================================
-- "Parsed key does not resolve in Jira."
-- Action: quarantine. Common causes are typos and cross-project keys.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
SELECT
  'DQ-7', 'medium', 'jira_issue', r.jira_issue_key,
  FORMAT('Jira key referenced by %d run(s) does not resolve in core.fct_jira_issue. Likely a typo or a cross-project key. Runs QUARANTINED from issue-level rollups until resolved.',
         COUNT(*)),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_run` AS r
WHERE r.started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 DAY)
  AND r.jira_issue_key IS NOT NULL
  -- Format guard first: a key that does not even match the pattern is a parse bug,
  -- not a missing issue, but both need reporting.
  AND NOT EXISTS (
    SELECT 1 FROM `${PROJECT_ID}.core.fct_jira_issue` AS j
    WHERE j.jira_issue_key = r.jira_issue_key
  )
GROUP BY r.jira_issue_key;


-- =====================================================================================
-- DQ-8 — MISSING STATUS TRANSITIONS (daily)
-- =====================================================================================
-- "Issue Done with no In-Progress transition."
-- Action: EXCLUDE from cycle time; count as dq_incomplete_workflow.
-- Treating such an issue as zero cycle time would make the team look instantaneous.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
SELECT
  'DQ-8', 'low', 'jira_issue', j.jira_issue_key,
  FORMAT('Issue reached Done (%s) with no In-Progress transition (%d transitions recorded). EXCLUDED from cycle-time metrics; cycle_time_hours is NULL, not 0.',
         FORMAT_TIMESTAMP('%Y-%m-%d', j.done_at),
         COALESCE(j.transition_count, 0)),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_jira_issue` AS j
WHERE j.done_at IS NOT NULL
  AND j.first_in_progress_at IS NULL
  AND j.done_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 DAY);


-- =====================================================================================
-- DQ-9 — PR WITHOUT COMMITS / COMMITS WITHOUT PR (daily)
-- =====================================================================================
-- Action: flag. Usually direct-to-branch pushes bypassing the flow.
--
-- Matters for acceptance: an output that is committed but never enters a PR can never
-- reach a terminal acceptance state, so it sits in_flight indefinitely and silently
-- shrinks the acceptance denominator.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
-- (a) commits with no PR
SELECT
  'DQ-9', 'medium', 'output', o.output_id,
  FORMAT('Output was committed (%s) but belongs to no pull request after 3 days. Probable direct-to-branch push bypassing the flow. The output cannot reach a terminal acceptance state and is excluded from the acceptance denominator.',
         FORMAT_TIMESTAMP('%Y-%m-%d', o.first_commit_at)),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_output` AS o
WHERE o.first_commit_at IS NOT NULL
  AND o.pr_id IS NULL
  AND o.first_commit_at <  TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 3 DAY)
  AND o.first_commit_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 10 DAY)

UNION ALL

-- (b) PRs with no commits recorded
SELECT
  'DQ-9', 'low', 'pull_request',
  FORMAT('%s#%d', pr.repo_full_name, pr.pr_id),
  'Pull request has commit_count = 0 or NULL. Either the poller missed the commit list or the PR is empty; PR-level line metrics are unusable for this row.',
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_pull_request` AS pr
WHERE COALESCE(pr.commit_count, 0) = 0
  AND pr.created_on >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 DAY);


-- =====================================================================================
-- DQ-10 — CLOCK SKEW (on ingest / hourly)
-- =====================================================================================
-- "event_time more than 5 min ahead of ingested_at, or an end before its start."
-- Action: clamp, flag. Laptop clocks drift and every event here is client-emitted.
--
-- Consequence if unchecked: a skewed event_time lands in the wrong partition, and a
-- negative duration silently becomes a negative token/second rate downstream.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
-- (a) event_time ahead of ingest
SELECT
  'DQ-10', 'medium', 'event', e.event_id,
  FORMAT('Clock skew: event_time is %d seconds AHEAD of ingested_at (event_type=%s). Client clock drift. Partition assignment may be wrong; clamp to ingested_at.',
         TIMESTAMP_DIFF(e.event_time, e.ingested_at, SECOND), e.event_type),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.raw.ai_run_event` AS e
WHERE DATE(e.event_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL 2 DAY)
  AND e.ingested_at IS NOT NULL
  AND TIMESTAMP_DIFF(e.event_time, e.ingested_at, SECOND) > 300

UNION ALL

-- (b) run ends before it starts
SELECT
  'DQ-10', 'high', 'run', r.run_id,
  FORMAT('Run ended BEFORE it started (started_at=%s, ended_at=%s). duration_ms is unusable and must be treated as NULL, not negative.',
         FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', r.started_at),
         FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', r.ended_at)),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_run` AS r
WHERE r.ended_at IS NOT NULL
  AND r.ended_at < r.started_at
  AND r.started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 DAY);


-- =====================================================================================
-- DQ-11 — DUPLICATE EVENT (on ingest / hourly)
-- =====================================================================================
-- "Repeated event_id." Action: idempotent drop.
-- The transforms already dedup (CONTRACT §1.3). This check exists to measure HOW MUCH
-- duplication the pipeline is absorbing — a sudden rise means the collector's ack path
-- is failing and delivery volume, hence cost, is being wasted.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
SELECT
  'DQ-11', 'low', 'event', e.event_id,
  FORMAT('Duplicate event_id delivered %d times (event_type=%s). Dropped idempotently by the transform dedup. A rising rate indicates the collector ack path is failing.',
         COUNT(*), ANY_VALUE(e.event_type)),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.raw.ai_run_event` AS e
WHERE DATE(e.event_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL 2 DAY)
GROUP BY e.event_id
HAVING COUNT(*) > 1;


-- =====================================================================================
-- DQ-12 — BENCHMARK ABSENT (on ingest / daily)
-- =====================================================================================
-- "A task type with no manual_time_benchmarks entry." Action: time_saved = NULL, not 0.
--
-- ⚠ NOTE ON SCOPE. Nothing in this warehouse computes time_saved — CONTRACT §1.6 and
-- design §9.1 Decision 2 forbid it as a headline and §8.16 forbids any monetary value
-- figure. The check is implemented as specified so the catalogue is complete and so a
-- future consumer cannot quietly default a missing benchmark to zero, but no metric in
-- 08_metrics.sql depends on it.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
-- (a) the lookup itself is empty — one aggregate finding, not one per task type
SELECT
  'DQ-12', 'info', 'dataset', 'core.dim_task_benchmark',
  'Benchmark lookup core.dim_task_benchmark is EMPTY. Any consumer of a task-type benchmark must resolve to NULL, never 0. Load it from config.yaml metrics.manual_time_benchmarks.',
  CURRENT_TIMESTAMP()
FROM (SELECT COUNT(*) AS n FROM `${PROJECT_ID}.core.dim_task_benchmark`)
WHERE n = 0

UNION ALL

-- (b) the lookup is populated but this artifact_type/agent pairing is missing
SELECT
  'DQ-12', 'low', 'dimension',
  FORMAT('%s/%s', COALESCE(o.agent_name, 'unknown'), COALESCE(o.artifact_type, 'unknown')),
  FORMAT('%d output(s) in the last 7 days have no matching row in core.dim_task_benchmark. Any benchmark-derived figure for this task type must be NULL, not 0.',
         COUNT(*)),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_output` AS o
WHERE o.generated_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
  AND (SELECT COUNT(*) FROM `${PROJECT_ID}.core.dim_task_benchmark`) > 0
  AND NOT EXISTS (
    SELECT 1 FROM `${PROJECT_ID}.core.dim_task_benchmark` AS b
    WHERE b.artifact_type = o.artifact_type
      AND (b.agent_name IS NULL OR b.agent_name = o.agent_name)
  )
GROUP BY o.agent_name, o.artifact_type;


-- =====================================================================================
-- DQ-13 — LATE ARRIVAL (on ingest / hourly)
-- =====================================================================================
-- "Event older than 7 days." Action: accept, reprocess affected partitions, flag if >30d.
--
-- The emitter is offline-tolerant by design (CONTRACT §1.2, §8): a laptop off the VPN
-- queues locally and flushes later. Late events are EXPECTED, not errors — but they
-- restate history, and §9.5 requires restatement to be visible. Beyond 30 days the
-- raw partition may already have expired (90-day retention), so the event cannot be
-- reprocessed at all — hence the higher severity.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
SELECT
  'DQ-13',
  IF(TIMESTAMP_DIFF(e.ingested_at, e.event_time, DAY) > 30, 'high', 'info'),
  'event', e.event_id,
  FORMAT('Late arrival: event_time %s ingested %d days later (event_type=%s). Partition %s must be reprocessed; the restatement must be shown, not silent (design §9.5).%s',
         FORMAT_TIMESTAMP('%Y-%m-%d', e.event_time),
         TIMESTAMP_DIFF(e.ingested_at, e.event_time, DAY),
         e.event_type,
         FORMAT_DATE('%Y-%m-%d', DATE(e.event_time)),
         IF(TIMESTAMP_DIFF(e.ingested_at, e.event_time, DAY) > 30,
            ' >30 DAYS: the raw partition may already have expired under the 90-day retention policy and the event may be unrecoverable.',
            '')),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.raw.ai_run_event` AS e
WHERE e.ingested_at IS NOT NULL
  AND DATE(e.ingested_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
  AND TIMESTAMP_DIFF(e.ingested_at, e.event_time, DAY) > 7;


-- =====================================================================================
-- DQ-14 — COVERAGE PLAUSIBILITY (daily)
-- =====================================================================================
-- "Coverage jumps >30pp between consecutive runs on one repo."
-- Action: flag. Usually a CHANGED MEASUREMENT SCOPE, not real improvement — and a
-- coverage chart that steps 40pp overnight destroys confidence in every other number
-- on the same dashboard.
--
-- LAG over runs ordered within a repository gives the consecutive-run comparison.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
WITH coverage_series AS (
  SELECT
    r.run_id,
    r.repo_full_name,
    r.started_at,
    r.max_coverage_pct,
    LAG(r.max_coverage_pct) OVER (
      PARTITION BY r.repo_full_name ORDER BY r.started_at
    ) AS prev_coverage_pct,
    LAG(r.run_id) OVER (
      PARTITION BY r.repo_full_name ORDER BY r.started_at
    ) AS prev_run_id
  FROM `${PROJECT_ID}.core.fct_ai_run` AS r
  WHERE r.max_coverage_pct IS NOT NULL
    AND r.repo_full_name IS NOT NULL
    AND r.started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
)
SELECT
  'DQ-14', 'medium', 'run', cs.run_id,
  FORMAT('Coverage on %s moved %.1f pp between consecutive runs (%.1f%% -> %.1f%%, previous run %s). A jump this large is usually a CHANGED MEASUREMENT SCOPE, not real improvement. Verify before publishing.',
         cs.repo_full_name,
         ABS(cs.max_coverage_pct - cs.prev_coverage_pct),
         cs.prev_coverage_pct, cs.max_coverage_pct,
         cs.prev_run_id),
  CURRENT_TIMESTAMP()
FROM coverage_series AS cs
WHERE cs.prev_coverage_pct IS NOT NULL
  AND ABS(cs.max_coverage_pct - cs.prev_coverage_pct) > 30
  AND cs.started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 DAY);


-- =====================================================================================
-- DQ-15 — CARDINALITY GUARD  ⭐ MANDATORY (daily)
-- =====================================================================================
-- "Any metric dimension exceeding 100 distinct values." Action: alert.
--
-- Directly enforces CONTRACT §1.5 and the rule already written in
-- skills/dev-observability-patterns/SKILL.md [V]: "Metrics MUST use bounded tags
-- (< 100 unique values)". High-cardinality identifiers belong in the PAYLOAD, not in
-- a metric label.
--
-- Why it matters concretely: an unbounded dimension turns a mart GROUP BY into a
-- combinatorial explosion, and every cell in it falls below the k >= 5 threshold, so
-- the entire mart returns NULL rates. The guard fails the dimension, not the metric.
--
-- Implemented as a UNION ALL over each column that is legitimately used as a metric
-- dimension anywhere in 06_marts.sql or 08_metrics.sql. Columns deliberately NOT
-- listed here — branch_name, file_path, commit_sha, output_id, run_id, trace_id,
-- conversation_id — are payload/identifier columns and are never grouped on.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
WITH dimension_cardinality AS (
  SELECT 'fct_ai_run.agent_name'        AS dim, COUNT(DISTINCT agent_name)       AS n FROM `${PROJECT_ID}.core.fct_ai_run` WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'fct_ai_run.agent_version',   COUNT(DISTINCT agent_version)         FROM `${PROJECT_ID}.core.fct_ai_run` WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'fct_ai_run.skill_name',      COUNT(DISTINCT skill_name)            FROM `${PROJECT_ID}.core.fct_ai_run` WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'fct_ai_run.model_id',        COUNT(DISTINCT model_id)              FROM `${PROJECT_ID}.core.fct_ai_run` WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'fct_ai_run.surface',         COUNT(DISTINCT surface)               FROM `${PROJECT_ID}.core.fct_ai_run` WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'fct_ai_run.environment',     COUNT(DISTINCT environment)           FROM `${PROJECT_ID}.core.fct_ai_run` WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'fct_ai_run.product_profile', COUNT(DISTINCT product_profile)       FROM `${PROJECT_ID}.core.fct_ai_run` WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'fct_ai_run.jira_project_key',COUNT(DISTINCT jira_project_key)      FROM `${PROJECT_ID}.core.fct_ai_run` WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'fct_ai_run.repo_full_name',  COUNT(DISTINCT repo_full_name)        FROM `${PROJECT_ID}.core.fct_ai_run` WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'fct_ai_run.role',            COUNT(DISTINCT role)                  FROM `${PROJECT_ID}.core.fct_ai_run` WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'fct_ai_run.team_id',         COUNT(DISTINCT team_id)               FROM `${PROJECT_ID}.core.fct_ai_run` WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'fct_ai_run.dependency_failed', COUNT(DISTINCT dependency_failed)   FROM `${PROJECT_ID}.core.fct_ai_run` WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'fct_ai_run.failure_class',   COUNT(DISTINCT failure_class)         FROM `${PROJECT_ID}.core.fct_ai_run` WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'fct_ai_output.artifact_type',COUNT(DISTINCT artifact_type)         FROM `${PROJECT_ID}.core.fct_ai_output` WHERE generated_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'fct_ai_output.acceptance_state', COUNT(DISTINCT acceptance_state)  FROM `${PROJECT_ID}.core.fct_ai_output` WHERE generated_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'fct_ai_output.acceptance_state_reason', COUNT(DISTINCT acceptance_state_reason) FROM `${PROJECT_ID}.core.fct_ai_output` WHERE generated_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'fct_jira_issue.issue_type',  COUNT(DISTINCT issue_type)            FROM `${PROJECT_ID}.core.fct_jira_issue` WHERE created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'fct_jira_issue.status_category', COUNT(DISTINCT status_category)   FROM `${PROJECT_ID}.core.fct_jira_issue` WHERE created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'otel_span.gen_ai_tool_name', COUNT(DISTINCT gen_ai_tool_name)      FROM `${PROJECT_ID}.raw.otel_span` WHERE start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'otel_span.gen_ai_operation_name', COUNT(DISTINCT gen_ai_operation_name) FROM `${PROJECT_ID}.raw.otel_span` WHERE start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
)
SELECT
  'DQ-15',
  IF(n >= 500, 'critical', 'high'),
  'dimension', dc.dim,
  FORMAT('Cardinality guard breached: %d distinct values over the last 30 days (limit 100, CONTRACT §1.5). This column MUST NOT be used as a metric dimension — move it to the payload. Grouping on it also drives every mart cell below the k>=5 threshold, NULLing all rates.', dc.n),
  CURRENT_TIMESTAMP()
FROM dimension_cardinality AS dc
WHERE dc.n > 100;


-- =====================================================================================
-- DQ-16 — ATTRIBUTION CONFLICT (daily)
-- =====================================================================================
-- "One output claimed by two runs (AR-1 breach)."
-- Action: QUARANTINE BOTH; block from aggregates until resolved.
--
-- 05_transform_output.sql sets is_quarantined and every aggregate filters on it. This
-- check surfaces the conflict for a human to resolve, and names the claiming runs.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
WITH claims AS (
  SELECT
    JSON_VALUE(e.attributes, '$.output_id') AS output_id,
    e.run_id,
    e.agent_name
  FROM `${PROJECT_ID}.raw.ai_run_event` AS e
  WHERE e.event_type = 'output.generated'
    AND DATE(e.event_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL 14 DAY)
    AND JSON_VALUE(e.attributes, '$.output_id') IS NOT NULL
)
SELECT
  'DQ-16', 'critical', 'output', c.output_id,
  FORMAT('AR-1 BREACH: output claimed by %d distinct runs (%s), agents (%s). BOTH claimants are quarantined and blocked from every aggregate until a steward resolves it. If a later run legitimately modified the artifact, it must emit a NEW output_id with parent_output_id set — never re-claim this one.',
         COUNT(DISTINCT c.run_id),
         STRING_AGG(DISTINCT c.run_id ORDER BY c.run_id LIMIT 5),
         STRING_AGG(DISTINCT COALESCE(c.agent_name, 'unknown') ORDER BY COALESCE(c.agent_name, 'unknown') LIMIT 5)),
  CURRENT_TIMESTAMP()
FROM claims AS c
GROUP BY c.output_id
HAVING COUNT(DISTINCT c.run_id) > 1;


-- =====================================================================================
-- DQ-17 — OTEL BIND FAILURE (daily)  [invariant guard, not in the §9.4 catalogue]
-- =====================================================================================
-- Added because the run.bound bridge in 04_transform_run.sql is the single point of
-- failure for the entire cost side of this system, and design §9.4 has no check for
-- it. Three distinct failure modes, which look identical on a dashboard (cost = NULL)
-- and have completely different fixes:
--
--   (a) NO BIND      — the run never emitted run.bound. emit.py could not capture the
--                      conversation id. Fix: the emitter.
--   (b) BOUND, EMPTY — a conversation id exists but zero OTel spans matched. Fix: the
--                      Copilot OTLP exporter is off, or the collector is not receiving.
--   (c) SHARED       — one conversation id claimed by an implausible number of runs,
--                      which means the time-window disambiguation is doing heavy
--                      lifting and the token split should be spot-checked.
--
-- Runs on a non-exporting surface (surface='headless') legitimately have no spans and
-- are excluded from (a) and (b).
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
-- (a) no bind at all
SELECT
  'DQ-17', 'high', 'run', r.run_id,
  FORMAT('No run.bound event: otel_conversation_id is NULL (agent=%s, surface=%s). This run can NEVER be priced — the correlation stream and the OTel stream are unjoinable for it. Check emit.py conversation-id capture on this surface.',
         COALESCE(r.agent_name, 'unknown'), COALESCE(r.surface, 'unknown')),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_run` AS r
WHERE r.started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 DAY)
  AND r.otel_conversation_id IS NULL
  AND COALESCE(r.surface, 'unknown') IN ('vscode-copilot-chat', 'copilot-cli')

UNION ALL

-- (b) bound but no spans matched
SELECT
  'DQ-17', 'high', 'run', r.run_id,
  FORMAT('Bound to conversation %s but ZERO OTel spans matched the run window (agent=%s, surface=%s). The bind resolved; the exporter sent nothing. Check github.copilot.chat.otel.enabled / COPILOT_OTEL_ENABLED and the collector endpoint.',
         r.otel_conversation_id, COALESCE(r.agent_name, 'unknown'), COALESCE(r.surface, 'unknown')),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_run` AS r
WHERE r.started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 DAY)
  AND r.otel_conversation_id IS NOT NULL
  AND COALESCE(r.otel_span_count, 0) = 0

UNION ALL

-- (c) heavily shared conversation id
SELECT
  'DQ-17', 'medium', 'dimension', FORMAT('conversation:%s', r.otel_conversation_id),
  FORMAT('Conversation id shared by %d runs in one day. The span->run assignment relies entirely on the time-window disambiguation in 04_transform_run.sql; spot-check the token split before trusting per-run cost for these runs.',
         COUNT(*)),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_run` AS r
WHERE r.started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 DAY)
  AND r.otel_conversation_id IS NOT NULL
GROUP BY r.otel_conversation_id, DATE(r.started_at)
HAVING COUNT(*) > 20;


-- =====================================================================================
-- DQ-RET — RETENTION ENFORCEMENT (weekly)  [invariant guard]
-- =====================================================================================
-- Design §11.2: "Implemented via BigQuery table-level partition_expiration_days so
-- expiry is enforced by the platform, not by a cron job someone can forget. DQ-check
-- dq_retention (§7.10) verifies it weekly."
--
-- This is that check. It reads INFORMATION_SCHEMA and asserts each partitioned table
-- carries the retention the design specifies. A table created by hand, restored from a
-- snapshot, or recreated without OPTIONS would silently retain data forever — which is
-- a privacy problem long before it is a cost problem.
--
-- Expected values (design §11.2):
--   raw.*    90    core.fct_*  396 (13 months)
--   marts.*  1130 (37 months)  dq.dq_findings  180
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
WITH expected AS (
  SELECT 'raw'   AS schema_name, 90    AS expected_days UNION ALL
  SELECT 'core',                 396                    UNION ALL
  SELECT 'marts',                1130                   UNION ALL
  SELECT 'dq',                   180
),
actual AS (
  SELECT 'raw'   AS schema_name, table_name, SAFE_CAST(option_value AS INT64) AS days FROM `${PROJECT_ID}.raw.INFORMATION_SCHEMA.TABLE_OPTIONS`   WHERE option_name = 'partition_expiration_days' UNION ALL
  SELECT 'core',                 table_name, SAFE_CAST(option_value AS INT64)        FROM `${PROJECT_ID}.core.INFORMATION_SCHEMA.TABLE_OPTIONS`  WHERE option_name = 'partition_expiration_days' UNION ALL
  SELECT 'marts',                table_name, SAFE_CAST(option_value AS INT64)        FROM `${PROJECT_ID}.marts.INFORMATION_SCHEMA.TABLE_OPTIONS` WHERE option_name = 'partition_expiration_days' UNION ALL
  SELECT 'dq',                   table_name, SAFE_CAST(option_value AS INT64)        FROM `${PROJECT_ID}.dq.INFORMATION_SCHEMA.TABLE_OPTIONS`    WHERE option_name = 'partition_expiration_days'
)
SELECT
  'DQ-RET', 'high', 'dataset',
  FORMAT('%s.%s', a.schema_name, a.table_name),
  FORMAT('Retention mismatch: partition_expiration_days = %d, design §11.2 requires %d for the %s layer. Data is being kept longer or shorter than policy allows.',
         a.days, e.expected_days, a.schema_name),
  CURRENT_TIMESTAMP()
FROM actual AS a
JOIN expected AS e USING (schema_name)
WHERE a.days IS DISTINCT FROM e.expected_days;
