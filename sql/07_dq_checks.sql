-- =====================================================================================
-- 07_dq_checks.sql — dq.dq_findings + DQ-1 … DQ-16 (plus fourteen invariant guards)
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
-- Ten further guards were added with contract 1.1.0 (2026-08-26), at the end of this
-- file: DQ-SESSION, DQ-GATE, DQ-COV, DQ-GRAIN, DQ-ATTR, DQ-TOOL, DQ-BILL, DQ-MODEL,
-- DQ-PATH, DQ-LINK. They exist because the switch from OTel spans to the Copilot
-- session journal created several ways for a number to be wrong while looking right —
-- an unmeasured surface reading as an unused one, a session total charged to one run,
-- a structural zero read as a measurement. Cadence: DQ-SESSION, DQ-ATTR, DQ-MODEL and
-- DQ-PATH hourly with the transform; the rest nightly.
--
-- ⚠ DQ-COV emits an `info` finding EVERY DAY whether or not anything is wrong. That is
-- deliberate and it is the only check here that does so: a check which only speaks up
-- on failure cannot tell you that a whole surface has been unmeasured all along.
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
  check_id     STRING    NOT NULL OPTIONS (description = 'Bounded enum: DQ-1 … DQ-16 from design §9.4, plus the invariant guards defined in this file — DQ-6b, DQ-6c, DQ-17, DQ-RET, and the contract-1.1.0 set DQ-SESSION, DQ-GATE, DQ-COV, DQ-GRAIN, DQ-ATTR, DQ-TOOL, DQ-BILL, DQ-MODEL, DQ-PATH and DQ-LINK. CLUSTER KEY (CONTRACT §7).'),
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
-- DQ-11 — DUPLICATE EVENT (on ingest / hourly)   ⚠ RETUNED 2026-08-26
-- =====================================================================================
-- "Repeated event_id." Action: idempotent drop.
-- The transforms already dedup (CONTRACT §1.3). This check exists to measure HOW MUCH
-- duplication the pipeline is absorbing — a sudden rise means the collector's ack path
-- is failing and delivery volume, hence cost, is being wasted.
--
-- ⚠⚠ WHY THIS IS NO LONGER ONE FINDING PER DUPLICATED EVENT.
--
-- Contract 1.1.0 made duplicates the EXPECTED STEADY STATE rather than a fault.
-- cli/copilot_read.py derives every `event_id` from the journal record's own id, so
-- re-reading a journal is byte-identical by design — that determinism is what makes an
-- hourly unattended read safe over a session that stays open for days. An open session
-- therefore re-emits every event it has ever written, every hour, until it closes.
-- Measured on the reference machine: a second read of the same tree re-delivered 2,614
-- of 2,935 events.
--
-- Left as it was, this check wrote thousands of `low` findings a day, all of them
-- correct and none of them actionable, and the predictable result is that people stop
-- reading dq_findings at all — which is a worse outcome than the check not existing.
--
-- Retuned to report the RATE, once, as a single dataset-level row, and to escalate
-- only when the rate exceeds what re-reading open journals can explain. A per-event
-- finding is kept for one case only: an event_id duplicated with DIFFERENT payloads,
-- which is a genuine collision and means the natural key is not unique.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
-- (a) the aggregate rate — one row, always emitted, so the trend is visible
WITH dup AS (
  SELECT
    e.event_id,
    COUNT(*)                              AS deliveries,
    COUNT(DISTINCT TO_JSON_STRING(e.attributes)) AS distinct_payloads
  FROM `${PROJECT_ID}.raw.ai_run_event` AS e
  WHERE DATE(e.event_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL 2 DAY)
  GROUP BY e.event_id
)
SELECT
  'DQ-11',
  -- 70% is not a measurement, it is a threshold chosen above the observed re-read rate
  -- (2,614/2,935 = 89% on a tree of mostly-open sessions is normal; a steady state far
  -- above that would mean something other than re-reading).
  IF(SAFE_DIVIDE(SUM(deliveries) - COUNT(*), NULLIF(SUM(deliveries), 0)) > 0.95,
     'medium', 'info'),
  'dataset', 'raw.ai_run_event',
  FORMAT('Duplicate delivery rate over 2 days: %d deliveries for %d distinct event_id (%.1f%% redundant). EXPECTED, not a fault: journal event_ids are deterministic and an open Copilot session re-emits every event on each hourly read. Investigate only on a sustained step change, or if DQ-11(b) fires.',
         SUM(deliveries), COUNT(*),
         SAFE_DIVIDE(SUM(deliveries) - COUNT(*), NULLIF(SUM(deliveries), 0)) * 100),
  CURRENT_TIMESTAMP()
FROM dup

UNION ALL

-- (b) the real fault: one event_id, two different payloads. Not a re-delivery — a
-- natural-key collision, which means two distinct facts are overwriting each other.
SELECT
  'DQ-11', 'critical', 'event', d.event_id,
  FORMAT('event_id delivered %d times with %d DIFFERENT attribute payloads. This is a natural-key collision, not a re-delivery: the dedup keeps one arbitrary row and silently discards a distinct fact. Fix the id derivation at the producer.',
         d.deliveries, d.distinct_payloads),
  CURRENT_TIMESTAMP()
FROM dup AS d
WHERE d.distinct_payloads > 1;


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
  AND TIMESTAMP_DIFF(e.ingested_at, e.event_time, DAY) > 7
  -- ⚠ model.call IS EXCLUDED, 2026-08-26. Since 1.1.0 that event is written at
  -- `session.shutdown` and covers the whole session, so its event_time is the moment
  -- the session ended — which for a session an engineer left open over a weekend is
  -- legitimately days after the run it belongs to. That is a GRAIN, not a late
  -- arrival, and reporting it as one buries the genuine late arrivals in noise. The
  -- honest check for it is DQ-SESSION(b), which asks whether the shutdown landed
  -- outside the transform's rebuild window — the thing that actually costs data.
  AND e.event_type <> 'model.call';


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
-- copilot_session_id — are payload/identifier columns and are never grouped on.
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
  -- ⚠ THE TWO otel_span PROBES THAT USED TO SIT HERE WERE VACUOUS FROM 2026-08-26.
  -- raw.otel_span is frozen (01_raw.sql), so a 30-day window over it returns no rows
  -- and COUNT(DISTINCT ...) returns 0 — a cardinality guard that can never fire is
  -- worse than no guard, because the dashboard it protects still says "checked".
  -- The tool-name dimension did not go away; it moved to tool.call on the correlation
  -- stream, and it is MORE dangerous there because journal tool names include MCP
  -- server tools, which are named by whoever wrote the server. Probed at the source.
  UNION ALL SELECT 'ai_run_event.tool.call.tool_name', COUNT(DISTINCT JSON_VALUE(attributes, '$.tool_name')) FROM `${PROJECT_ID}.raw.ai_run_event` WHERE event_type = 'tool.call'      AND event_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'ai_run_event.tool.call.tool_kind', COUNT(DISTINCT JSON_VALUE(attributes, '$.tool_kind')) FROM `${PROJECT_ID}.raw.ai_run_event` WHERE event_type = 'tool.call'      AND event_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'ai_run_event.gate.evaluated.gate_name', COUNT(DISTINCT JSON_VALUE(attributes, '$.gate_name')) FROM `${PROJECT_ID}.raw.ai_run_event` WHERE event_type = 'gate.evaluated' AND event_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'ai_run_event.model.call.model_id',  COUNT(DISTINCT JSON_VALUE(attributes, '$.model_id'))  FROM `${PROJECT_ID}.raw.ai_run_event` WHERE event_type = 'model.call'      AND event_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'fct_ai_run.usage_grain',      COUNT(DISTINCT usage_grain)           FROM `${PROJECT_ID}.core.fct_ai_run` WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'fct_ai_run.usage_source',     COUNT(DISTINCT usage_source)          FROM `${PROJECT_ID}.core.fct_ai_run` WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  UNION ALL SELECT 'fct_ai_run.trace_id_namespace', COUNT(DISTINCT trace_id_namespace)  FROM `${PROJECT_ID}.core.fct_ai_run` WHERE started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
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
-- DQ-17 — USAGE BIND FAILURE (daily)  [invariant guard, not in the §9.4 catalogue]
-- =====================================================================================
-- Added because the run.bound bridge in 04_transform_run.sql is the single point of
-- failure for the entire cost side of this system, and design §9.4 has no check for
-- it. Distinct failure modes which look identical on a dashboard (cost = NULL) and
-- have completely different fixes:
--
--   (a) NO BIND      — the run has no session id at all: no run.bound, and its
--                      trace_id is an emitter trace rather than a session. Fix: the
--                      emitter's session-id capture on this surface.
--   (b) BOUND, EMPTY — a session id exists but the session recorded no usage. Since
--                      1.1.0 that means `session.shutdown` never arrived — the CLI
--                      crashed, was killed, or the session is still open — so the
--                      tokens are UNKNOWABLE, not zero. DQ-SESSION owns that case in
--                      detail; this row is the run-level view of it.
--   (c) SHARED       — one session id claimed by many runs. Before the cutover this
--                      was a warning that the time-window disambiguation was doing
--                      heavy lifting. It is no longer a warning at all: at
--                      session-model grain a shared session makes attribution
--                      IMPOSSIBLE rather than merely delicate, the transform sets
--                      cost_attributable = FALSE, and the reported severity drops to
--                      info because the correct behaviour is already happening.
--
-- ⚠ REWRITTEN 2026-08-26. The old (b) named `github.copilot.chat.otel.enabled` /
-- `COPILOT_OTEL_ENABLED` as the remediation. That setting is now actively REMOVED by
-- cli/vscode_setup.py, so the advice would have sent someone to switch an exporter
-- back on — re-enabling a known content leak (microsoft/vscode#326254) to fix a
-- problem it cannot fix. A stale remediation is worse than none.
--
-- Runs on a non-exporting surface (surface='headless') legitimately have no usage and
-- are excluded from (a).
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
-- (a) no bind at all
SELECT
  'DQ-17', 'high', 'run', r.run_id,
  FORMAT('No session bind: copilot_session_id is NULL (agent=%s, surface=%s). This run can NEVER be priced — nothing joins it to the usage stream. Fix: emit.py must emit run.bound carrying copilot_session_id (CONTRACT §3 row 2). For a run read from a journal this should be impossible: cli/copilot_read.py writes the session id into trace_id.',
         COALESCE(r.agent_name, 'unknown'), COALESCE(r.surface, 'unknown')),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_run` AS r
WHERE r.started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 DAY)
  AND r.copilot_session_id IS NULL
  AND COALESCE(r.surface, 'unknown') IN ('vscode-copilot-chat', 'copilot-cli')

UNION ALL

-- (b) bound but the session recorded no usage
SELECT
  'DQ-17', 'high', 'run', r.run_id,
  FORMAT('Bound to session %s but the session recorded NO usage (agent=%s, surface=%s). The bind resolved; nothing measured. Post-cutover this means session.shutdown never arrived, so the tokens are unknowable rather than zero — see DQ-SESSION. Do NOT re-enable the OTel exporter to "fix" it; that path is closed and leaked content.',
         r.copilot_session_id, COALESCE(r.agent_name, 'unknown'), COALESCE(r.surface, 'unknown')),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_run` AS r
WHERE r.started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 DAY)
  AND r.copilot_session_id IS NOT NULL
  AND COALESCE(r.token_source, 'none') = 'none'

UNION ALL

-- (c) heavily shared session id — informational since 1.1.0, see the header
SELECT
  'DQ-17', 'info', 'dimension', FORMAT('session:%s', r.copilot_session_id),
  FORMAT('Session id shared by %d runs in one day. Expected on the CLI surface: /resume opens a new run in the same session and every sub-agent shares it. All %d runs correctly report cost_usd = NULL (CONTRACT §3); their usage total is in marts.v_session_usage at session grain.',
         COUNT(*), COUNT(*)),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_run` AS r
WHERE r.started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 DAY)
  AND r.copilot_session_id IS NOT NULL
GROUP BY r.copilot_session_id, DATE(r.started_at)
HAVING COUNT(*) > 20;


-- =====================================================================================
-- CONTRACT 1.1.0 GUARDS — added 2026-08-26 with the source switch
-- =====================================================================================
-- Ten checks, all of them written because the switch from OTel spans to the Copilot
-- session journal created a way for a number to be wrong while looking right. None of
-- them is in the design §9.4 catalogue, because the catalogue predates the source.
-- =====================================================================================


-- =====================================================================================
-- DQ-SESSION — SESSION WITHOUT USAGE (hourly)
-- =====================================================================================
-- A Copilot session records its usage totals in `session.shutdown`. A session that
-- crashed, was killed, or is still open never writes one, so it carries NO
-- modelMetrics at all — and its tokens are UNKNOWABLE, not zero. Measured on the
-- reference machine 2026-08-26: 2 of 22 sessions.
--
-- This is the check that stops "usage fell this week" being read off a collection gap.
-- The denominator matters and is easy to get wrong: that same tree held 28 session
-- DIRECTORIES, six of them containing no events.jsonl at all. An empty directory
-- recorded nothing; it is not a session whose usage went missing, and counting it here
-- would report a gap that does not exist. This check therefore counts only sessions
-- that produced at least one run.
--
-- (b) is the other half of the same fact: usage that arrived, but too late for the
-- transform to attach it. 04_transform_run.sql rebuilds a 21-day trailing window, and
-- 21 is a policy choice rather than a measurement — this is what makes it falsifiable.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
WITH session_span AS (
  SELECT
    e.trace_id                                                   AS session_id,
    MIN(e.event_time)                                            AS first_event_at,
    MAX(e.event_time)                                            AS last_event_at,
    COUNTIF(e.event_type = 'run.started')                        AS run_count,
    COUNTIF(e.event_type = 'model.call')                         AS usage_event_count,
    MIN(IF(e.event_type = 'model.call', e.event_time, NULL))     AS usage_at
  FROM `${PROJECT_ID}.raw.ai_run_event` AS e
  WHERE DATE(e.event_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL 14 DAY)
    AND e.trace_id IS NOT NULL
    -- Session ids only. The emitter's trc_ namespace is a different thing and has no
    -- shutdown event to be missing.
    AND NOT STARTS_WITH(e.trace_id, 'trc_')
  GROUP BY e.trace_id
)
-- (a) ran, but never reported usage
SELECT
  'DQ-SESSION', 'medium', 'dimension', FORMAT('session:%s', s.session_id),
  FORMAT('Session hosted %d run(s) over %d hour(s) but emitted NO model.call: session.shutdown never arrived (crashed, killed, or still open). Its tokens and premium requests are UNKNOWABLE, not zero — every run in it reports token_source = none. Do not read a fall in measured usage as a fall in usage until this count is stable.',
         s.run_count,
         TIMESTAMP_DIFF(s.last_event_at, s.first_event_at, HOUR)),
  CURRENT_TIMESTAMP()
FROM session_span AS s
WHERE s.run_count > 0
  AND s.usage_event_count = 0
  -- Still-open sessions are the normal case within the last day; only report one that
  -- has gone quiet, or the check fires on every session anybody is currently using.
  AND s.last_event_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)

UNION ALL

-- (b) usage arrived, but outside the transform's rebuild window
SELECT
  'DQ-SESSION', 'high', 'dimension', FORMAT('session:%s', s.session_id),
  FORMAT('Session usage arrived %d days after the session first appeared, which exceeds the 21-day rebuild window in 04_transform_run.sql. The run rows had already left the window, so these tokens were merged onto nothing. Widen window_days, or accept the loss knowingly — this is the check that makes that a decision rather than a discovery.',
         TIMESTAMP_DIFF(s.usage_at, s.first_event_at, DAY)),
  CURRENT_TIMESTAMP()
FROM session_span AS s
WHERE s.usage_at IS NOT NULL
  AND TIMESTAMP_DIFF(s.usage_at, s.first_event_at, DAY) > 21;


-- =====================================================================================
-- DQ-GATE — GATE VERDICT ABSENT (daily)
-- =====================================================================================
-- CONTRACT §3 row 9: a gate verdict comes from the `<exited with exit code N>` trailer
-- Copilot's bash tool appends, and it is absent on roughly 12% of calls — the command
-- was still running, or its output was truncated past the trailer.
--
-- Absent means NULL. A gate with no verdict is excluded from the pass-rate denominator
-- (04_transform_run.sql `gate_final`), which is correct and which also means it leaves
-- no trace in the rate itself. This check is the trace. A pass rate quoted without its
-- known-share is not a pass rate, and `gate_verdict_known_pct` in the mart exists so
-- the share travels with the number.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
SELECT
  'DQ-GATE',
  -- Above a third unknown the rate is being computed over a minority of the evidence.
  IF(SAFE_DIVIDE(COUNTIF(JSON_VALUE(e.attributes, '$.status') IS NULL), COUNT(*)) > 0.33,
     'high', 'info'),
  'dimension', COALESCE(JSON_VALUE(e.attributes, '$.gate_name'), 'unknown'),
  FORMAT('%d of %d %s-gate evaluations in the last 7 days carried NO verdict (%.1f%%). Those are excluded from gate_pass_rate_pct — a missing verdict is neither a pass nor a fail. Publish gate_verdict_known_pct beside the rate or the rate is unqualified. Expected steady state is ~12%%.',
         COUNTIF(JSON_VALUE(e.attributes, '$.status') IS NULL),
         COUNT(*),
         COALESCE(JSON_VALUE(e.attributes, '$.gate_name'), 'unknown'),
         SAFE_DIVIDE(COUNTIF(JSON_VALUE(e.attributes, '$.status') IS NULL), COUNT(*)) * 100),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.raw.ai_run_event` AS e
WHERE e.event_type = 'gate.evaluated'
  AND DATE(e.event_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
GROUP BY JSON_VALUE(e.attributes, '$.gate_name')
HAVING COUNTIF(JSON_VALUE(e.attributes, '$.status') IS NULL) > 0;


-- =====================================================================================
-- DQ-COV — SURFACE COVERAGE (daily)  ⭐ the one that stops a coverage cut reading as a usage cut
-- =====================================================================================
-- The Copilot session journal covers the `copilot-cli` surface and NOTHING ELSE. VS
-- Code's Copilot Chat panel and inline completions write nothing to it. That is a
-- deliberate scope decision (TODO.md, 2026-08-26), not an oversight — and it is
-- exactly the kind of decision that becomes invisible three months later, at which
-- point a drop in measured tokens gets reported as a drop in AI usage.
--
-- So this check emits an `info` row EVERY DAY, unconditionally, naming the unmeasured
-- surfaces. A check that only fires when something is wrong cannot tell you that
-- something has been missing all along.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
-- (a) the standing statement of scope — always emitted
SELECT
  'DQ-COV', 'info', 'dataset', 'surface_coverage',
  FORMAT('Measured surfaces: copilot-cli only. UNMEASURED: vscode-copilot-chat, inline-completions — they write no session journal and no span stream (the exporter is removed). Last 7 days: %d run(s) on measured surfaces, %d on unmeasured ones. A fall in measured tokens is a fall in COVERAGE unless the unmeasured count also fell. Never present a token total as organisation-wide AI usage.',
         COUNTIF(COALESCE(r.surface, 'unknown') IN ('copilot-cli', 'headless')),
         COUNTIF(COALESCE(r.surface, 'unknown') NOT IN ('copilot-cli', 'headless'))),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_run` AS r
WHERE r.started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)

UNION ALL

-- (b) an unmeasured surface that is actually being used, at volume
SELECT
  'DQ-COV', 'medium', 'dimension', COALESCE(r.surface, 'unknown'),
  FORMAT('%d run(s) in the last 7 days on surface=%s, which emits NO usage data at all. Their tokens are unmeasured, not zero: token_source = none on every one. Any per-surface cost comparison that includes this surface is comparing a measurement against a blank.',
         COUNT(*), COALESCE(r.surface, 'unknown')),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_run` AS r
WHERE r.started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
  AND COALESCE(r.surface, 'unknown') NOT IN ('copilot-cli', 'headless')
GROUP BY r.surface
HAVING COUNT(*) > 0;


-- =====================================================================================
-- DQ-GRAIN — GRAIN MIX (daily)  ⭐ the trend-honesty guard
-- =====================================================================================
-- Three invariants, all about the same thing: a chart must never add a per-call
-- measurement to a per-session measurement without saying so.
--
--   (a) a fact row whose usage_source contradicts the cutover date — the hard guard in
--       04_transform_run.sql should make this impossible, which is exactly why it is
--       worth checking: an impossible thing that happens is a broken assumption.
--   (b) a mart row aggregating both grains outside the cutover day itself.
--   (c) marts.dim_grain_cutover disagreeing with the data. A dashboard annotating the
--       wrong day is worse than one annotating nothing.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
-- (a) a run processed by the wrong branch
SELECT
  'DQ-GRAIN', 'critical', 'run', r.run_id,
  FORMAT('Run started %s carries usage_source=%s / usage_grain=%s, which contradicts the cutover boundary in marts.dim_grain_cutover (%s). 04_transform_run.sql gates the two branches on started_at, so this should be structurally impossible — the guard has been removed, the cutover date has moved without a rebuild, or a row predates the migration and was never backfilled.',
         FORMAT_TIMESTAMP('%Y-%m-%d', r.started_at),
         COALESCE(r.usage_source, 'NULL'), COALESCE(r.usage_grain, 'NULL'),
         FORMAT_DATE('%Y-%m-%d', c.cutover_date)),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_run` AS r
CROSS JOIN `${PROJECT_ID}.marts.dim_grain_cutover` AS c
WHERE r.started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  AND (   (DATE(r.started_at) >= c.cutover_date AND r.usage_source = 'otel_span')
       OR (DATE(r.started_at) <  c.cutover_date AND r.usage_source = 'copilot_journal'))

UNION ALL

-- (b) a mart row that mixes grains away from the boundary
SELECT
  'DQ-GRAIN', 'high', 'dataset',
  FORMAT('agg_daily_person_agent:%s', FORMAT_DATE('%Y-%m-%d', a.day)),
  FORMAT('%d mart row(s) on %s aggregate BOTH usage grains (per_call and per_session_model) into one row. Every token total and every rate on those rows adds two different measurements together. Only the cutover day itself (%s) may legitimately do this.',
         COUNT(*), FORMAT_DATE('%Y-%m-%d', a.day),
         FORMAT_DATE('%Y-%m-%d', ANY_VALUE(c.cutover_date))),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.marts.agg_daily_person_agent` AS a
CROSS JOIN `${PROJECT_ID}.marts.dim_grain_cutover` AS c
WHERE a.day >= DATE_SUB(CURRENT_DATE(), INTERVAL 45 DAY)
  AND COALESCE(a.usage_grain_mixed, FALSE)
  AND a.day <> c.cutover_date
GROUP BY a.day

UNION ALL

-- (c) the published boundary does not match the data
SELECT
  'DQ-GRAIN', 'high', 'dimension', 'marts.dim_grain_cutover',
  FORMAT('dim_grain_cutover.cutover_date is %s but the earliest run measured at per_session_model grain started %s. Every dashboard annotation drawn from this table is on the wrong day. Reconcile it with the cutover_date DECLARE in 04_transform_run.sql.',
         FORMAT_DATE('%Y-%m-%d', ANY_VALUE(c.cutover_date)),
         FORMAT_DATE('%Y-%m-%d', MIN(DATE(r.started_at)))),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_run` AS r
CROSS JOIN `${PROJECT_ID}.marts.dim_grain_cutover` AS c
WHERE r.usage_grain = 'per_session_model'
HAVING MIN(DATE(r.started_at)) IS DISTINCT FROM ANY_VALUE(c.cutover_date);


-- =====================================================================================
-- DQ-ATTR — MULTI-RUN SESSION PRICED (hourly)
-- =====================================================================================
-- A direct CONTRACT §3 breach: "A session that hosted more than one run must report
-- cost_usd = NULL for its constituent runs rather than a share of the total."
--
-- 04_transform_run.sql enforces it structurally, so this check should never fire. That
-- is the reason to run it. The failure it guards against is the most seductive one in
-- the whole system — somebody, reasonably, wanting the cost column filled in, and
-- splitting a session total by run count or by elapsed time. Both look like arithmetic
-- and are both §2.4's forbidden synthesised join key.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
SELECT
  'DQ-ATTR', 'critical', 'run', r.run_id,
  FORMAT('Run carries a non-NULL cost_usd while sharing session %s with %d other run(s). CONTRACT §3 forbids this: a per-session usage total cannot be apportioned across the runs in the session by any rule. Somebody has added an apportionment. Remove it; the session total belongs in marts.v_session_usage, at the grain where it is true.',
         r.copilot_session_id, r.session_run_count - 1),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_run` AS r
WHERE r.started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
  AND r.usage_grain = 'per_session_model'
  AND COALESCE(r.session_run_count, 1) > 1
  AND r.cost_usd IS NOT NULL;


-- =====================================================================================
-- DQ-TOOL — TOOL STATUS COVERAGE (daily)
-- =====================================================================================
-- CONTRACT §3 row 6: `status` is `ok` | `error`, and it was structurally NULL under the
-- span source. 04_transform_run.sql compounded that by counting errors as
-- `status = 'failed'` — a value the enum has never contained — so tool_error_count was
-- 0 by construction from the day it was written and "zero tool failures" was published
-- as a measurement for the life of the pipeline.
--
-- Both are fixed. This check exists so the same silence cannot come back: it reports
-- how much of the tool traffic actually carries a verdict, and fires loudly if the
-- share collapses (a producer regression) or if a value outside the enum appears (a
-- producer drifting away from the contract, which is how the last one started).
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
-- (a) coverage
SELECT
  'DQ-TOOL',
  IF(SAFE_DIVIDE(COUNTIF(JSON_VALUE(e.attributes, '$.status') IS NULL), COUNT(*)) > 0.5,
     'high', 'info'),
  'dataset', 'tool.call.status',
  FORMAT('%d of %d tool.call events in the last 7 days carry a status (%.1f%% coverage); %d report error. A zero error count is only meaningful at high coverage — at low coverage it means nothing was watching.',
         COUNTIF(JSON_VALUE(e.attributes, '$.status') IS NOT NULL), COUNT(*),
         SAFE_DIVIDE(COUNTIF(JSON_VALUE(e.attributes, '$.status') IS NOT NULL), COUNT(*)) * 100,
         COUNTIF(JSON_VALUE(e.attributes, '$.status') = 'error')),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.raw.ai_run_event` AS e
WHERE e.event_type = 'tool.call'
  AND DATE(e.event_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
HAVING COUNT(*) > 0

UNION ALL

-- (b) a value outside the CONTRACT §3 row 6 enum
SELECT
  'DQ-TOOL', 'high', 'dimension',
  FORMAT('tool.call.status:%s', JSON_VALUE(e.attributes, '$.status')),
  FORMAT('tool.call status value %s is outside the CONTRACT §3 row 6 enum (ok | error), seen %d times in 7 days. Every COUNTIF in the transform tests the enum exactly, so an out-of-enum value is counted as NEITHER ok nor error and disappears — which is precisely how tool_error_count came to be permanently zero.',
         JSON_VALUE(e.attributes, '$.status'), COUNT(*)),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.raw.ai_run_event` AS e
WHERE e.event_type = 'tool.call'
  AND DATE(e.event_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
  AND JSON_VALUE(e.attributes, '$.status') IS NOT NULL
  AND JSON_VALUE(e.attributes, '$.status') NOT IN ('ok', 'error')
GROUP BY JSON_VALUE(e.attributes, '$.status');


-- =====================================================================================
-- DQ-BILL — BILLING SEPARATION (daily)
-- =====================================================================================
-- CONTRACT §4.1: `premium_requests` is the measured count of the unit Copilot actually
-- bills; `cost_usd` is a modelled economic weight in dollars. "Report them alongside
-- cost_usd, never blended into it: one is measured and dimensionless, the other is
-- modelled and in dollars, and a single number carrying both would be defensible as
-- neither."
--
-- The rule is easy to state and easy to break by accident, because both columns sit
-- next to each other on every cost row and both look like "how much did this cost".
-- This check reads the actual view definitions out of INFORMATION_SCHEMA and looks for
-- one appearing in an arithmetic expression with the other.
--
-- It is a HEURISTIC — a regex over SQL text, not a parse — and it is deliberately
-- biased towards false positives. A spurious finding costs somebody two minutes; a
-- missed one puts a number in front of finance that is neither a count nor a currency.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
SELECT
  'DQ-BILL', 'critical', 'dataset',
  FORMAT('marts.%s', v.table_name),
  FORMAT('View marts.%s appears to combine premium_requests with a dollar column in one expression. CONTRACT §4.1 forbids it: premium_requests is a measured, dimensionless billing count and cost_usd is a modelled dollar figure. Carry them side by side. (Heuristic text match — verify, then either fix the view or narrow the pattern.)',
         v.table_name),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.marts.INFORMATION_SCHEMA.VIEWS` AS v
WHERE REGEXP_CONTAINS(v.view_definition, r'premium_requests')
  AND REGEXP_CONTAINS(v.view_definition, r'_usd')
  AND REGEXP_CONTAINS(
        v.view_definition,
        r'(?i)(premium_requests\s*[\+\-\*/]|[\+\-\*/]\s*premium_requests|[a-z_]*_usd\s*[\+\-\*/]\s*[a-z_.]*premium_requests|premium_requests[a-z_.]*\s*[\+\-\*/]\s*[a-z_.]*_usd)');


-- =====================================================================================
-- DQ-MODEL — MODEL ID UNKNOWN TO THE PRICE BOOK (hourly)
-- =====================================================================================
-- The pricing join in 04_transform_run.sql is EXACT on model_id, so a one-character
-- drift between what the source calls a model and what core.dim_model_pricing calls it
-- prices EVERYTHING to NULL. DQ-6 catches this at run grain, after the damage; this
-- catches it at the source, where the fix is a single row in the price book.
--
-- It matters more than it did. Under the span source the id came from
-- gen_ai.response.model; since 1.1.0 it is the MAP KEY of
-- session.shutdown.modelMetrics — a different field from a different producer, which
-- can rename or restyle without any notice reaching this warehouse.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
SELECT
  'DQ-MODEL', 'high', 'model',
  JSON_VALUE(e.attributes, '$.model_id'),
  FORMAT('model_id %s appeared on %d model.call event(s) in the last 7 days and has NO row in core.dim_model_pricing valid on those dates. Every one of those sessions prices to cost_usd = NULL (CONTRACT §4, never 0). Add the id with sql/09_set_model_price.sql — and check it against the journal key, not against the agent frontmatter spelling.',
         JSON_VALUE(e.attributes, '$.model_id'), COUNT(*)),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.raw.ai_run_event` AS e
WHERE e.event_type = 'model.call'
  AND DATE(e.event_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
  AND JSON_VALUE(e.attributes, '$.model_id') IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM `${PROJECT_ID}.core.dim_model_pricing` AS p
    WHERE p.model_id = JSON_VALUE(e.attributes, '$.model_id')
      AND DATE(e.event_time) BETWEEN p.effective_from
                                 AND COALESCE(p.effective_to, DATE '9999-12-31')
  )
GROUP BY JSON_VALUE(e.attributes, '$.model_id');


-- =====================================================================================
-- DQ-PATH — ABSOLUTE FILE PATH (hourly)
-- =====================================================================================
-- CONTRACT §1.1 and design §11.3: no raw identifiers. Journal file paths are absolute
-- and begin `/Users/<name>/`, which is somebody's name. cli/copilot_read.py makes them
-- repo-relative and DROPS a path that sits under no known root rather than truncating
-- it — a half-path is not worth a guess about which prefix was safe to remove.
--
-- The client guards it. This checks it. The two are not the same thing: the guard runs
-- on a laptop that may be running last month's build, and "the client handles it" is
-- how a leak reaches a warehouse table with 396-day retention.
--
-- ⚠ THIS FINDING MUST NOT QUOTE THE PATH. Doing so would copy the identifier into
-- dq_findings, which is the leak this check exists to detect. Only the shape is
-- reported.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
SELECT
  'DQ-PATH', 'critical', 'run', e.run_id,
  FORMAT('%d output.generated event(s) on this run carry an ABSOLUTE file_path (shape: %s). CONTRACT §1.1 / design §11.3 — an absolute path begins with somebody home directory and is a raw identifier. The client makes paths repo-relative; this one did not, so a client is out of date or a new producer skipped the rule. The path itself is deliberately NOT quoted here: repeating it would copy the identifier into dq_findings.',
         COUNT(*),
         ANY_VALUE(CASE
           WHEN STARTS_WITH(JSON_VALUE(e.attributes, '$.file_path'), '/Users/')  THEN 'posix-home-macos'
           WHEN STARTS_WITH(JSON_VALUE(e.attributes, '$.file_path'), '/home/')   THEN 'posix-home-linux'
           WHEN STARTS_WITH(JSON_VALUE(e.attributes, '$.file_path'), '/')        THEN 'posix-absolute'
           ELSE 'windows-drive-absolute'
         END)),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.raw.ai_run_event` AS e
WHERE e.event_type = 'output.generated'
  AND DATE(e.event_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL 2 DAY)
  AND JSON_VALUE(e.attributes, '$.file_path') IS NOT NULL
  AND (   STARTS_WITH(JSON_VALUE(e.attributes, '$.file_path'), '/')
       OR REGEXP_CONTAINS(JSON_VALUE(e.attributes, '$.file_path'), r'^[A-Za-z]:[\\/]'))
GROUP BY e.run_id;


-- =====================================================================================
-- DQ-LINK — MEASURED USAGE EXCLUDED BY THE EXPLICIT-LINK GATE (daily)
-- =====================================================================================
-- CONTRACT §2.4: only `link_method = 'explicit'` rows may feed cost-per-output
-- metrics, and marts.v_cost_per_accepted_output / marts.v_config_comparison apply that
-- filter. The rule is correct and this check does NOT propose widening it: a heuristic
-- link cannot reliably attribute an output to a run, and cli/link_runs.py says so in
-- its own docstring.
--
-- What the rule does NOT do is explain itself. Journal-derived runs carry
-- `heuristic`, so on a CLI-only estate those two views can be entirely EMPTY while
-- tokens are flowing — and an empty view reads as "no cost" rather than "cost
-- excluded by design". This check turns a mysterious blank into a stated exclusion,
-- and names the one thing that changes it: emit.py emitting run.bound with
-- copilot_session_id, which is what earns `explicit`.
-- =====================================================================================
INSERT INTO `${PROJECT_ID}.dq.dq_findings` (check_id, severity, entity_type, entity_id, detail, detected_at)
SELECT
  'DQ-LINK',
  IF(SAFE_DIVIDE(SUM(IF(r.link_method = 'explicit', r.total_tokens, 0)),
                 NULLIF(SUM(r.total_tokens), 0)) < 0.05, 'high', 'info'),
  'dataset', 'v_cost_per_accepted_output',
  FORMAT('Of %d measured tokens in the last 7 days, %.1f%% sit on runs with link_method = explicit and are therefore the ONLY tokens visible to v_cost_per_accepted_output and v_config_comparison (CONTRACT §2.4). The rest are excluded BY DESIGN, not lost: they remain in marts.v_session_usage and in every session/person/repo/week aggregate. If those two views look empty, this is why. The unlock is emit.py emitting run.bound with copilot_session_id — that, and only that, earns explicit.',
         SUM(r.total_tokens),
         SAFE_DIVIDE(SUM(IF(r.link_method = 'explicit', r.total_tokens, 0)),
                     NULLIF(SUM(r.total_tokens), 0)) * 100),
  CURRENT_TIMESTAMP()
FROM `${PROJECT_ID}.core.fct_ai_run` AS r
WHERE r.started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
  AND r.total_tokens IS NOT NULL
HAVING SUM(r.total_tokens) > 0;


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
