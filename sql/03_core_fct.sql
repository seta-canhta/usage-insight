-- =====================================================================================
-- 03_core_fct.sql — CORE fact tables
-- =====================================================================================
-- Contract:  schema/CONTRACT.md §2.4 (link), §4 (cost), §5
--            (acceptance state machine), §6 (attribution), §7 (names/partition/cluster)
-- Design:    docs/spikes/ai-effectiveness-observability.md §6.4, §8.x, §9.2, §9.3, §11.2
--
-- Retention: 13 months = 396 days (design §11.2) on every fact table, enforced by
-- partition_expiration_days. 13 rather than 12 gives one month of overlap so a
-- year-on-year comparison has both endpoints available.
--
-- Every fact row carries link_method and link_confidence (design §5.3 "Design rule").
-- Dashboards default to link_method = 'explicit'; widening that filter must be a
-- deliberate, visible act.
--
-- Run 01_raw.sql and 02_dims.sql first. Substitute ${PROJECT_ID}.
-- =====================================================================================


-- =====================================================================================
-- core.fct_ai_run — one row per agent invocation
-- =====================================================================================
-- The atom of AI measurement. Built by 04_transform_run.sql from the correlation
-- stream JOINED to the OTel stream via the run.bound bridge.
--
-- Grain: run_id. A supervisor and each of its sub-agents are SEPARATE rows sharing one
-- trace_id (AR-4: the supervisor's totals are computed by rolling up trace_id, never
-- by re-counting sub-agent outputs onto the supervisor row).
-- =====================================================================================
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.core.fct_ai_run`
(
  -- ---- identity ----
  run_id                 STRING    NOT NULL OPTIONS (description = 'PRIMARY KEY. run_<uuid4hex>. One agent invocation.'),
  trace_id               STRING    NOT NULL OPTIONS (description = 'One user-initiated workflow. Supervisor + sub-agents share it. AR-4 rolls up on this column.'),
  parent_run_id          STRING             OPTIONS (description = 'Sub-agent -> supervisor edge. NULL for a root run. is_root_run below is the convenience flag.'),
  is_root_run            BOOL               OPTIONS (description = 'parent_run_id IS NULL. Denormalised because almost every aggregate needs to avoid double-counting nested runs.'),
  workflow_id            STRING             OPTIONS (description = 'Legacy human-readable bridge, wf-{JIRA}-{YYYYMMDD}.'),

  -- ---- ⭐ the OTel bridge ----
  otel_conversation_id   STRING             OPTIONS (description = '⭐ gen_ai.conversation.id, taken from the run.bound event attributes. THE key that binds the correlation stream to the OTel stream. NULL means this run has no token data at all — see token_source.'),
  otel_trace_id          STRING             OPTIONS (description = 'The OTel-side trace id observed for this conversation. Diagnostic only: NOT the same namespace as trace_id above. Never join the two.'),
  otel_span_count        INT64              OPTIONS (description = 'Number of OTel spans bound to this run. 0 with a non-NULL conversation id means the bind resolved but the exporter sent nothing — a distinct failure mode from an unbound run, and DQ-17 separates them.'),

  -- ---- time ----
  started_at             TIMESTAMP NOT NULL OPTIONS (description = 'event_time of run.started. PARTITION KEY (CONTRACT §7).'),
  ended_at               TIMESTAMP          OPTIONS (description = 'event_time of the terminal event. NULL while in flight or if the run was abandoned before a terminal event.'),
  duration_ms            INT64              OPTIONS (description = 'Wall-clock run duration. Taken from the terminal event attributes where present, otherwise derived from ended_at - started_at.'),

  -- ---- actor (CONTRACT §2.1) ----
  person_id              STRING             OPTIONS (description = 'Atlassian accountId, resolved through core.dim_person. NULL means identity resolution failed — DQ-1. Such rows are suppressed from person-level dashboards.'),
  person_email_hash      STRING             OPTIONS (description = 'Salted SHA-256 of the git author email. Never the raw address.'),
  team_id                STRING             OPTIONS (description = 'Team at the time of the run (resolved against dim_person effective dates, not the current team).'),
  role                   STRING             OPTIONS (description = 'dev | qa | devops | po | lead.'),

  -- ---- context (CONTRACT §2.2) ----
  jira_issue_key         STRING             OPTIONS (description = 'FEATURE ticket after AR-3 resolution. Never the delivery ticket.'),
  delivery_ticket_key    STRING             OPTIONS (description = 'AR-3: the QualDev qd_jira_key, retained SEPARATELY so one run does not mark two tickets in the metrics.'),
  jira_project_key       STRING             OPTIONS (description = 'Bounded dimension.'),
  repo_full_name         STRING             OPTIONS (description = '{workspace}/{repo_slug}.'),
  branch_name            STRING             OPTIONS (description = 'Payload only — high cardinality, never a metric label (CONTRACT §1.5).'),
  product_profile        STRING             OPTIONS (description = 'watchtower | automotive | ...'),
  environment            STRING             OPTIONS (description = 'dev | sit | pre | prd | local.'),

  -- ---- agent / model dims (CONTRACT §2.3) ----
  agent_name             STRING             OPTIONS (description = 'CLUSTER KEY (CONTRACT §7). Design §9.1 comparison arm.'),
  agent_version          STRING             OPTIONS (description = 'Short git SHA of the .agent.md. Design §9.1 "before vs after a prompt change" arm.'),
  agent_kind             STRING             OPTIONS (description = 'supervisor | phase | standalone, from dim_agent_version.'),
  skill_name             STRING             OPTIONS (description = 'Design §9.1 "skill on vs off" arm.'),
  skill_version          STRING             OPTIONS (description = 'Short git SHA of the SKILL.md.'),
  surface                STRING             OPTIONS (description = 'vscode-copilot-chat | copilot-cli | headless | unknown. Determines whether OTel spans can exist at all — a headless surface legitimately has no token data.'),
  model_declared_id      STRING             OPTIONS (description = 'Model declared in the agent frontmatter at run start.'),
  model_id               STRING             OPTIONS (description = 'CLUSTER KEY (CONTRACT §7). Model the runtime actually used (gen_ai.response.model, falling back to gen_ai.request.model). THIS is what cost is priced on. Design §9.1 model-comparison arm.'),
  model_drift            BOOL               OPTIONS (description = 'model_id <> model_declared_id. Not a data error — a finding. A cost comparison across agents is meaningless if the declared model is not the one answering.'),

  invocation_mode        STRING             OPTIONS (description = 'From run.started: jira_driven | direct_plan | unknown.'),
  input_source           STRING             OPTIONS (description = 'From run.started. Bounded.'),

  -- ---- tokens (from the OTel stream; design §4.4a) ----
  input_tokens           INT64              OPTIONS (description = 'SUM(gen_ai.usage.input_tokens) over the chat spans bound to this run. Includes the cached portion. NULL = usage genuinely unknown, never 0.'),
  output_tokens          INT64              OPTIONS (description = 'SUM(gen_ai.usage.output_tokens).'),
  cached_input_tokens    INT64              OPTIONS (description = 'SUM of cached-input tokens. A SUBSET of input_tokens, billed at the cheaper cached rate (CONTRACT §4).'),
  reasoning_tokens       INT64              OPTIONS (description = 'SUM of reasoning tokens. A SUBSET of output_tokens. Reported for visibility; NOT added into total_tokens.'),
  total_tokens           INT64              OPTIONS (description = 'DERIVED as input + output. Copilot does NOT emit gen_ai.usage.total_tokens (design §4.4a), so this is computed, never read. Cached and reasoning are subsets and are not added.'),
  token_source           STRING             OPTIONS (description = 'measured_otel | modelled_estimate | none. Explains WHY tokens are NULL, which matters more than the NULL itself.'),

  -- ---- cost (CONTRACT §4) ----
  cost_usd               NUMERIC            OPTIONS (description = 'Token cost per CONTRACT §4, priced through the effective-dated dim_model_pricing join. NULL for an unpriced model — NEVER 0 (CONTRACT §4). Excludes seat and infra, which are allocated per person-month under AR-8; see 06_marts.sql.'),
  cost_basis             STRING             OPTIONS (description = 'measured | modelled | seat_allocated (CONTRACT §4). A dashboard that silently blends measured and modelled is the fastest way to lose trust — render modelled distinctly (design §8.1).'),
  cost_is_placeholder    BOOL               OPTIONS (description = 'TRUE when the price row used carried is_placeholder = TRUE. Any figure built on these rows is unusable for chargeback. DQ-6c reports it.'),
  pricing_effective_from DATE               OPTIONS (description = 'effective_from of the price row actually used. Makes a historical cost figure auditable and reproducible.'),

  -- ---- activity counters ----
  tool_call_count        INT64              OPTIONS (description = 'COUNT of execute_tool spans bound to this run.'),
  tool_error_count       INT64              OPTIONS (description = 'execute_tool spans with status_code = ERROR. Feeds the §7.8 per-integration error rate.'),
  model_call_count       INT64              OPTIONS (description = 'COUNT of chat spans bound to this run.'),
  retry_count            INT64              OPTIONS (description = 'SUM of retries across model and tool calls. Feeds the §7.8 retry rate.'),
  phases_completed       INT64              OPTIONS (description = 'From the terminal event attributes.'),
  phase_failed_count     INT64              OPTIONS (description = 'run.phase.completed events with status = failed.'),

  -- ---- human turns, split by kind (CONTRACT §3 event 7; design §8.11) ----
  -- Split into four columns rather than one total because §8.11 EXCLUDES approval and
  -- clarification from the intervention definition BY DESIGN. The agents deliberately
  -- ask for approval (developer.implementer.agent.md autonomous_policy.ask_user_when
  -- lists architectural choices, breaking changes, security trade-offs). Counting a
  -- designed approval gate as an "intervention" would punish correct behaviour.
  -- Keeping one total would make that exclusion impossible downstream.
  human_turns_total          INT64          OPTIONS (description = 'All human.turn events. Diagnostic total — do NOT use for manual_intervention_rate.'),
  human_turns_correction     INT64          OPTIONS (description = 'turn_kind = correction. COUNTS as an intervention (§8.11).'),
  human_turns_rejection      INT64          OPTIONS (description = 'turn_kind = rejection. COUNTS as an intervention (§8.11).'),
  human_turns_approval       INT64          OPTIONS (description = 'turn_kind = approval. EXCLUDED from interventions by design (§8.11) — this is a designed gate, not a failure.'),
  human_turns_clarification  INT64          OPTIONS (description = 'turn_kind = clarification. EXCLUDED from interventions by design (§8.11).'),
  human_turn_chars           INT64          OPTIONS (description = 'SUM of `chars` across human turns. A size proxy — the turn CONTENT is never stored (CONTRACT §1.1).'),

  -- ---- gate results (CONTRACT §3 event 9) ----
  gate_results           ARRAY<STRUCT<
    gate_name            STRING,
    status               STRING,
    quality_score        FLOAT64,
    coverage_pct         FLOAT64,
    attempt_index        INT64
  >>                                        OPTIONS (description = 'One entry per gate.evaluated event: build | test | lint | secrets | coverage, status pass | fail | skipped. attempt_index > 0 marks an auto-fix retry.'),
  gate_pass_count        INT64              OPTIONS (description = 'Gates whose FINAL attempt passed.'),
  gate_fail_count        INT64              OPTIONS (description = 'Gates whose FINAL attempt failed.'),
  gate_auto_fix_attempts INT64              OPTIONS (description = 'Gate evaluations with attempt_index > 0 — the agent fixing itself. COUNTS toward manual_intervention_rate per the §8.11 formula, but is NOT rework (§8.9 attribution boundary): it happens before any human looked at the work.'),
  max_coverage_pct       FLOAT64            OPTIONS (description = 'Highest coverage_pct reported by any gate in this run. DQ-14 watches for >30pp jumps between consecutive runs on one repo.'),

  -- ---- outcome ----
  terminal_status        STRING             OPTIONS (description = 'Bounded: completed | failed | timeout | abandoned | in_flight. Derived from which terminal event arrived; in_flight means none has yet and the 24h DQ-2 window has not expired.'),
  failure_class          STRING             OPTIONS (description = 'From run.failed. Bounded enum only — never an error message body.'),
  dependency_failed      STRING             OPTIONS (description = 'From run.failed: vpn | network | jira | bitbucket | mcp | aio | ci | none. Directly answers the brief question about interruptions caused by VPN or external services (§7.8).'),
  timeout_policy         STRING             OPTIONS (description = 'From run.timeout: strict | graceful | best_effort (skills/agent-watchdog).'),

  -- ---- link provenance (CONTRACT §2.4, design §5.3) ----
  link_method            STRING             OPTIONS (description = 'explicit | heuristic | marker_only. CONTRACT §2.4: only explicit rows may feed cost-per-output metrics.'),
  link_confidence        FLOAT64            OPTIONS (description = '0.0-1.0. explicit => 1.0.'),

  -- ---- lineage ----
  schema_version         STRING             OPTIONS (description = 'Contract version the source events were emitted against.'),
  transformed_at         TIMESTAMP          OPTIONS (description = 'When this row was last (re)built. Restatements must be visible, never silent (design §9.5).')
)
PARTITION BY DATE(started_at)
CLUSTER BY agent_name, model_id
OPTIONS (
  partition_expiration_days = 396,
  description = 'One row per AI run. Correlation stream JOINED to the OTel stream via the run.bound conversation id. Partition DATE(started_at), cluster agent_name, model_id (CONTRACT §7). 13-month retention (design §11.2). cost_usd is NULL for unpriced models, never 0 (CONTRACT §4).',
  labels = [('layer', 'core'), ('domain', 'ai-telemetry')]
);


-- =====================================================================================
-- core.fct_ai_output — one row per generated artifact, with its acceptance state
-- =====================================================================================
-- The central table for the spike question. Grain: output_id.
--
-- AR-1 (one output, one run) is enforced HERE: output_id is unique. If a later run
-- modifies the artifact that is a NEW output with parent_output_id set, never a
-- re-attribution. Conflicts are quarantined by DQ-16 and blocked from aggregates.
-- =====================================================================================
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.core.fct_ai_output`
(
  -- ---- identity ----
  output_id                  STRING    NOT NULL OPTIONS (description = 'PRIMARY KEY. AR-1: unique. Two runs claiming one output_id is an attribution conflict — DQ-16 quarantines BOTH until resolved.'),
  parent_output_id           STRING             OPTIONS (description = 'AR-1: set when a later run modified an earlier artifact. That is a NEW output, not a re-attribution of the old one.'),
  run_id                     STRING    NOT NULL OPTIONS (description = 'The run that emitted output.generated. The ONLY run this output is attributed to (AR-1).'),
  trace_id                   STRING             OPTIONS (description = 'Workflow the emitting run belonged to. AR-4 rolls up here; the supervisor never re-counts this row.'),

  -- ---- what was produced (CONTRACT §3 event 8) ----
  generated_at               TIMESTAMP NOT NULL OPTIONS (description = 'event_time of output.generated. PARTITION KEY (CONTRACT §7). Also the clock for the DQ-3 / §5 "never committed within 7 days" rule.'),
  artifact_type              STRING             OPTIONS (description = 'CLUSTER KEY (CONTRACT §7). Bounded: code | test | spec | mock | doc | csv | config.'),
  file_path                  STRING             OPTIONS (description = 'Path only. The artifact CONTENT is never stored (CONTRACT §1.1).'),
  lines_added                INT64              OPTIONS (description = 'Lines the run added. The lines_generated denominator of post_review_change_ratio.'),
  lines_removed              INT64              OPTIONS (description = 'Lines the run removed.'),
  output_content_hash        STRING             OPTIONS (description = 'SHA-256 of the artifact. Enables dedup and "did this survive review" WITHOUT storing content (design §11.3).'),
  reuse_source               STRING             OPTIONS (description = 'AR-5: non-NULL means the artifact was suppressed/merged by the dedup checker. Such rows are REUSED, not generated — is_reused below excludes them from output volume.'),
  is_reused                  BOOL               OPTIONS (description = 'AR-5: reuse_source IS NOT NULL. Contributes to reuse metrics; contributes ZERO to output volume and ZERO to acceptance denominators.'),

  -- ---- attribution context (denormalised from the run for query economy) ----
  person_id                  STRING             OPTIONS (description = 'Attributed person, from the emitting run.'),
  team_id                    STRING             OPTIONS (description = 'Team at generation time.'),
  agent_name                 STRING             OPTIONS (description = 'Emitting agent. §8.7 dimension.'),
  agent_version              STRING             OPTIONS (description = 'Emitting agent version. Design §9.1 arm.'),
  skill_name                 STRING             OPTIONS (description = 'Skill loaded on the emitting run. Design §9.1 arm.'),
  model_id                   STRING             OPTIONS (description = 'Model that produced it. Design §9.1 arm.'),
  jira_issue_key             STRING             OPTIONS (description = 'FEATURE ticket after AR-3 resolution.'),
  delivery_ticket_key        STRING             OPTIONS (description = 'AR-3: the QualDev delivery ticket, kept separate so both tickets are not counted.'),
  jira_project_key           STRING             OPTIONS (description = 'Bounded dimension.'),
  repo_full_name             STRING             OPTIONS (description = 'Repository.'),

  -- ---- SCM journey ----
  first_commit_sha           STRING             OPTIONS (description = 'First commit carrying this output_id in its scm.commit event. NULL after 7 days => never committed => rejected (CONTRACT §5, DQ-3).'),
  first_commit_at            TIMESTAMP          OPTIONS (description = 'When it first reached a commit.'),
  commit_count               INT64              OPTIONS (description = 'Distinct commits touching this output.'),
  has_ai_marker              BOOL               OPTIONS (description = 'The commit subject carried a CONTRACT.md §3.1 commit marker ([AUTH_BY_COPILOT] or [GEN_BY_COPILOT]). FALSE with a known run_id is DQ-5 (the convention is degrading).'),
  pr_id                      INT64              OPTIONS (description = 'Bitbucket PR number containing the first commit. NULL => still uncommitted or pushed direct to branch (DQ-9).'),
  pr_created_at              TIMESTAMP          OPTIONS (description = 'PR creation. THE CLOCK for the CONTRACT §5 seven-day maturity window.'),
  first_review_at            TIMESTAMP          OPTIONS (description = 'Earliest review action by a person OTHER than the PR author (§8.14). Self-comments are not review. THE BOUNDARY between auto-fix and rework (§8.9).'),
  merged_at                  TIMESTAMP          OPTIONS (description = 'PR merge time.'),
  declined_at                TIMESTAMP          OPTIONS (description = 'PR decline time.'),
  reverted_at                TIMESTAMP          OPTIONS (description = 'Revert-commit time, if a revert of the merge commit was detected within 30 days (CONTRACT §5, AR-9).'),
  days_to_revert             INT64              OPTIONS (description = 'merged_at -> reverted_at in days. Only reverts <= 30 days count (CONTRACT §5).'),

  -- ---- rework accounting (design §8.9) ----
  -- The distinction below is the whole point of §8.9 and is easy to get wrong:
  --   lines_changed_after_first_review  -> REWORK   (a human asked for it)
  --   auto_fix_cycles / lines_changed_pre_review -> NOT rework (the agent fixing
  --      itself before anyone looked). developer.implementer.agent.md permits up to
  --      3 auto-fix cycles [V]. Conflating them would penalise the agent for
  --      successfully fixing itself.
  lines_generated                  INT64        OPTIONS (description = 'Denominator of post_review_change_ratio. AI lines this output contributed to the PR.'),
  lines_changed_pre_review         INT64        OPTIONS (description = 'Lines changed by commits authored BEFORE first_review_at. The agent auto-fix loop. NOT rework (§8.9 attribution boundary).'),
  lines_changed_after_first_review INT64        OPTIONS (description = 'Lines changed by commits authored AFTER first_review_at. THE ONLY numerator of rework.'),
  auto_fix_cycles                  INT64        OPTIONS (description = 'Distinct pre-review self-correction commits. Reported SEPARATELY (§8.9); must never inflate rework.'),
  post_review_change_ratio         FLOAT64      OPTIONS (description = 'lines_changed_after_first_review / lines_generated (CONTRACT §5). <= 0.25 is the accepted threshold. THE 0.25 THRESHOLD IS A POLICY CHOICE, NOT A FACT — §8.7 requires it to be stated on every dashboard, with sensitivity published at 0.10 / 0.25 / 0.50.'),
  ai_line_share                    FLOAT64      OPTIONS (description = 'AR-7: ai_attributed_lines / total_changed_lines of the containing PR. A mixed PR contributes PRO RATA to AI metrics, never wholly. A PR with one AI commit and nine human commits is not an "AI PR".'),

  -- ---- acceptance state machine (CONTRACT §5, design §9.2) ----
  acceptance_state           STRING             OPTIONS (description = 'CLUSTER KEY (CONTRACT §7). Bounded: generated | in_flight | accepted | reworked | rejected | reverted. Recomputed nightly by 05_transform_output.sql. MERGED IS NOT ACCEPTED — a heavily rewritten output is merged and reworked (design §9.2).'),
  acceptance_state_reason    STRING             OPTIONS (description = 'Bounded machine-readable reason, e.g. merged_low_change | merged_high_change | pr_declined | never_committed_7d | reverted_within_30d | inside_maturity_window | pr_open. Makes the state auditable without re-deriving it.'),
  is_terminal_state          BOOL               OPTIONS (description = 'TRUE for accepted | reworked | rejected | reverted (design §9.2 Terminal? column). Non-terminal rows will be revisited by the nightly job.'),
  is_mature                  BOOL               OPTIONS (description = 'PR is at least 7 days old (CONTRACT §5). Immature outputs stay in_flight and are EXCLUDED from acceptance-rate denominators.'),
  maturity_at                TIMESTAMP          OPTIONS (description = 'pr_created_at + 7 days. When this output becomes eligible for a terminal merged-state classification.'),
  revert_window_ends_at      TIMESTAMP          OPTIONS (description = 'merged_at + 30 days. After this, an accepted output can no longer flip to reverted (CONTRACT §5).'),
  state_changed_at           TIMESTAMP          OPTIONS (description = 'When acceptance_state last changed. AR-9: a revert withdraws credit in the period the revert OCCURS, not retroactively — this column is what makes that period assignment possible.'),

  -- ---- link provenance ----
  link_method                STRING             OPTIONS (description = 'explicit | heuristic | marker_only. §8.7 computes acceptance over EXPLICIT ONLY: a heuristic link cannot reliably attribute an output to a run, so including it inflates the denominator with outputs whose fate is unknown.'),
  link_confidence            FLOAT64            OPTIONS (description = '0.0-1.0.'),

  -- ---- lineage ----
  is_quarantined             BOOL               OPTIONS (description = 'DQ-16: TRUE when two runs claim this output_id (AR-1 breach). Quarantined rows are BLOCKED from every aggregate until a steward resolves them.'),
  transformed_at             TIMESTAMP          OPTIONS (description = 'Last rebuild timestamp.')
)
PARTITION BY DATE(generated_at)
CLUSTER BY acceptance_state, artifact_type
OPTIONS (
  partition_expiration_days = 396,
  description = 'One row per AI-generated artifact with its acceptance state (CONTRACT §5, design §9.2) — the central table for the spike question. Partition DATE(generated_at), cluster acceptance_state, artifact_type (CONTRACT §7). 13-month retention. Enforces AR-1, AR-3, AR-4, AR-5, AR-7, AR-9.',
  labels = [('layer', 'core'), ('domain', 'ai-telemetry')]
);


-- =====================================================================================
-- core.fct_pull_request — Bitbucket PR facts
-- =====================================================================================
-- Built from the poller events scm.pr.created / .reviewed / .merged / .declined and
-- scm.revert. Grain: (repo_full_name, pr_id) — pr_id alone is only unique per repo.
-- =====================================================================================
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.core.fct_pull_request`
(
  pr_id                    INT64     NOT NULL OPTIONS (description = 'Bitbucket PR number. Unique only WITHIN a repository — always key on (repo_full_name, pr_id).'),
  repo_full_name           STRING    NOT NULL OPTIONS (description = 'CLUSTER KEY (CONTRACT §7). {workspace}/{repo_slug}.'),
  created_on               TIMESTAMP NOT NULL OPTIONS (description = 'PARTITION KEY (CONTRACT §7).'),

  author_person_id         STRING             OPTIONS (description = 'PR author, resolved to Atlassian accountId.'),
  author_email_hash        STRING             OPTIONS (description = 'Salted hash. Used to enforce the §8.14 rule that a self-comment is not a review.'),
  jira_issue_key           STRING             OPTIONS (description = 'Parsed from branch/title/commits with ([A-Z][A-Z0-9]+-\\d+).'),
  jira_project_key         STRING             OPTIONS (description = 'Bounded dimension.'),
  branch_name              STRING             OPTIONS (description = 'Source branch.'),
  target_branch            STRING             OPTIONS (description = 'Destination branch.'),

  -- ---- lifecycle timestamps (§8.14) ----
  first_review_at          TIMESTAMP          OPTIONS (description = 'Earliest of {first comment, first approval, first changes-requested} BY A PERSON OTHER THAN THE AUTHOR (§8.14). Self-comments are not review.'),
  first_approval_at        TIMESTAMP          OPTIONS (description = 'First approval by a non-author.'),
  merged_at                TIMESTAMP          OPTIONS (description = 'Merge time.'),
  declined_at              TIMESTAMP          OPTIONS (description = 'Decline time.'),
  closed_at                TIMESTAMP          OPTIONS (description = 'merged_at or declined_at, whichever applies.'),
  pr_state                 STRING             OPTIONS (description = 'Bounded: OPEN | MERGED | DECLINED | SUPERSEDED.'),
  decline_reason_class     STRING             OPTIONS (description = 'Bounded enum. Never free text.'),

  -- ---- lead times (§8.14) ----
  review_lead_time_hours   FLOAT64            OPTIONS (description = 'first_review_at - created_on (§8.14). NULL while unreviewed.'),
  merge_lead_time_hours    FLOAT64            OPTIONS (description = 'merged_at - created_on (§8.14).'),
  review_duration_hours    FLOAT64            OPTIONS (description = 'merged_at - first_review_at (§8.14).'),

  -- ---- size (§8.14 normalisation) ----
  lines_added              INT64              OPTIONS (description = 'From the Bitbucket diffstat.'),
  lines_removed            INT64              OPTIONS (description = 'From the Bitbucket diffstat.'),
  total_changed_lines      INT64              OPTIONS (description = 'lines_added + lines_removed. §8.14 REQUIRES lead time to be reported per 100 changed lines alongside the raw figure — AI PRs are typically larger, and an unnormalised comparison is meaningless.'),
  files_changed            INT64              OPTIONS (description = 'File count.'),
  commit_count             INT64              OPTIONS (description = 'Commits in the PR.'),
  comment_count            INT64              OPTIONS (description = 'Review comments. Counts only, never comment text.'),
  reviewer_count           INT64              OPTIONS (description = 'Distinct non-author reviewers.'),

  -- ---- AI attribution (AR-7) ----
  ai_commit_count          INT64              OPTIONS (description = 'Commits carrying an AI-Run-Id trailer or a CONTRACT.md §3.1 commit marker ([AUTH_BY_COPILOT] or [GEN_BY_COPILOT]).'),
  ai_attributed_lines      INT64              OPTIONS (description = 'Lines attributable to AI commits.'),
  ai_line_share            FLOAT64            OPTIONS (description = 'AR-7: ai_attributed_lines / total_changed_lines. FRACTIONAL, never binary. A PR with one AI commit and nine human commits is not an "AI PR" and must contribute pro rata.'),
  contains_ai_output       BOOL               OPTIONS (description = 'At least one fct_ai_output row maps to this PR.'),

  -- ---- rework split (§8.9) ----
  lines_changed_pre_review    INT64           OPTIONS (description = 'Changed by commits BEFORE first_review_at — the agent auto-fix loop. NOT rework.'),
  lines_changed_after_review  INT64           OPTIONS (description = 'Changed by commits AFTER first_review_at — the ONLY rework numerator (§8.9).'),
  auto_fix_cycles             INT64           OPTIONS (description = 'Pre-review self-correction commits. Reported separately (§8.9).'),
  post_review_change_ratio    FLOAT64         OPTIONS (description = 'lines_changed_after_review / ai_attributed_lines. PR-level; propagated to each contained output.'),

  -- ---- revert (AR-9) ----
  merge_commit_sha         STRING             OPTIONS (description = 'Merge commit SHA — the anchor revert detection searches against.'),
  is_reverted              BOOL               OPTIONS (description = 'A revert of merge_commit_sha was detected within 30 days (CONTRACT §5).'),
  reverted_at              TIMESTAMP          OPTIONS (description = 'Revert commit time.'),

  -- ---- exclusions (§8.14) ----
  is_draft                 BOOL               OPTIONS (description = 'Draft PRs are EXCLUDED from lead-time metrics until marked ready (§8.14).'),
  is_bot_only              BOOL               OPTIONS (description = 'Bot-authored PRs with no human commits are EXCLUDED from lead-time metrics (§8.14).'),
  spans_shutdown           BOOL               OPTIONS (description = 'PR open across a company shutdown. EXCLUDED from lead-time metrics (§8.14) — otherwise the holiday shows up as a review-quality regression.'),

  link_method              STRING             OPTIONS (description = 'explicit | heuristic | marker_only.'),
  link_confidence          FLOAT64            OPTIONS (description = '0.0-1.0.'),
  transformed_at           TIMESTAMP          OPTIONS (description = 'Last rebuild timestamp.')
)
PARTITION BY DATE(created_on)
CLUSTER BY repo_full_name
OPTIONS (
  partition_expiration_days = 396,
  description = 'One row per Bitbucket pull request with review/merge timings, size, AI attribution share (AR-7) and revert flag. Partition DATE(created_on), cluster repo_full_name (CONTRACT §7). 13-month retention.',
  labels = [('layer', 'core'), ('domain', 'ai-telemetry')]
);


-- =====================================================================================
-- core.fct_ci_run — CI pipeline facts
-- =====================================================================================
-- [A] in the design: CI configuration is pending OQ-3. The table is defined now so the
-- transform and the metrics do not have to change shape when CI is confirmed; until
-- then it is simply empty and every metric reading it degrades to NULL, not to 0.
-- =====================================================================================
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.core.fct_ci_run`
(
  pipeline_id       STRING    NOT NULL OPTIONS (description = 'CI pipeline uuid.'),
  repo_full_name    STRING    NOT NULL OPTIONS (description = 'CLUSTER KEY (CONTRACT §7).'),
  started_at        TIMESTAMP NOT NULL OPTIONS (description = 'PARTITION KEY (CONTRACT §7).'),
  ended_at          TIMESTAMP          OPTIONS (description = 'Pipeline end.'),
  duration_ms       INT64              OPTIONS (description = 'Pipeline duration.'),

  commit_sha        STRING             OPTIONS (description = 'Commit under test. The join key to fct_pull_request and, through scm.commit, to fct_ai_output.'),
  pr_id             INT64              OPTIONS (description = 'PR the pipeline ran for, where resolvable.'),
  branch_name       STRING             OPTIONS (description = 'Branch under test.'),
  jira_issue_key    STRING             OPTIONS (description = 'Resolved from the commit or branch.'),
  jira_project_key  STRING             OPTIONS (description = 'Bounded dimension.'),
  environment       STRING             OPTIONS (description = 'dev | sit | pre | prd.'),
  trigger_type      STRING             OPTIONS (description = 'Bounded: push | pr | manual | schedule.'),

  status            STRING             OPTIONS (description = 'Bounded: passed | failed | error | stopped.'),
  tests_total       INT64              OPTIONS (description = 'Total tests executed.'),
  tests_passed      INT64              OPTIONS (description = 'Passing tests.'),
  tests_failed      INT64              OPTIONS (description = 'Failing tests.'),
  coverage_pct      FLOAT64            OPTIONS (description = 'Coverage percentage. DQ-14 flags >30pp jumps between consecutive runs on one repo — usually a changed measurement scope, not real improvement.'),

  is_ai_related     BOOL               OPTIONS (description = 'The commit under test carries AI attribution. Enables the §8.13 "defects caught before human review" proxy.'),
  link_method       STRING             OPTIONS (description = 'explicit | heuristic | marker_only.'),
  link_confidence   FLOAT64            OPTIONS (description = '0.0-1.0.'),
  transformed_at    TIMESTAMP          OPTIONS (description = 'Last rebuild timestamp.')
)
PARTITION BY DATE(started_at)
CLUSTER BY repo_full_name
OPTIONS (
  partition_expiration_days = 396,
  description = 'One row per CI pipeline run. Partition DATE(started_at), cluster repo_full_name (CONTRACT §7). 13-month retention. [A] pending CI confirmation (OQ-3) — empty until then, and dependent metrics degrade to NULL, never to 0.',
  labels = [('layer', 'core'), ('domain', 'ai-telemetry')]
);


-- =====================================================================================
-- core.fct_jira_issue — work-item facts
-- =====================================================================================
-- Grain: jira_issue_key. Built from the jira.transition poller events plus the issue
-- snapshot. Holds NO issue text, AC content, or descriptions (design §11.3) — keys,
-- bounded enums, counts, and timestamps only.
-- =====================================================================================
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.core.fct_jira_issue`
(
  jira_issue_key        STRING    NOT NULL OPTIONS (description = 'e.g. PRJ-6383. The spine of the whole model.'),
  jira_issue_id         STRING             OPTIONS (description = 'Immutable numeric Jira id. Survives project-key renames — keep it alongside the key.'),
  jira_project_key      STRING    NOT NULL OPTIONS (description = 'CLUSTER KEY (CONTRACT §7). Bounded: PRJ, APR, AERLABS, AMS, ATSP, CVC2, PGEIN, AUT observed.'),
  created_at            TIMESTAMP NOT NULL OPTIONS (description = 'PARTITION KEY (CONTRACT §7).'),

  issue_type            STRING             OPTIONS (description = 'Bounded: Story | Bug | Task | Sub-task | Epic. Design §9.1 stratifies config comparisons by task class using this.'),
  parent_epic_key       STRING             OPTIONS (description = 'For epic rollups.'),
  delivery_ticket_key   STRING             OPTIONS (description = 'AR-3: the QualDev qd_jira_key when this issue is the FEATURE ticket. Kept separate so one run does not mark two tickets.'),
  is_delivery_ticket    BOOL               OPTIONS (description = 'TRUE when THIS row is itself a delivery ticket. AR-3 requires such rows be excluded from feature-level counts.'),

  status                STRING             OPTIONS (description = 'Current status name.'),
  status_category       STRING             OPTIONS (description = 'Bounded: To Do | In Progress | Done.'),
  resolution            STRING             OPTIONS (description = 'Bounded resolution name.'),
  assignee_person_id    STRING             OPTIONS (description = 'Atlassian accountId.'),
  reporter_person_id    STRING             OPTIONS (description = 'Atlassian accountId.'),
  team_id               STRING             OPTIONS (description = 'Team at creation time.'),
  story_points          FLOAT64            OPTIONS (description = 'For §8.6 execution rate. [A] estimate population unknown — if sparse, the metric must fall back to issue COUNTS and be labelled execution_rate_by_count. Never silently mix the two bases.'),
  priority              STRING             OPTIONS (description = 'Bounded priority name.'),
  labels                ARRAY<STRING>      OPTIONS (description = 'Issue labels. Bounded in practice; DQ-15 guards any use as a metric dimension.'),

  first_in_progress_at  TIMESTAMP          OPTIONS (description = 'First transition into an In Progress status. NULL on a Done issue is DQ-8 (missing status transitions) — such issues are EXCLUDED from cycle time, not treated as zero.'),
  done_at               TIMESTAMP          OPTIONS (description = 'First transition into a Done-category status.'),
  cycle_time_hours      FLOAT64            OPTIONS (description = 'done_at - first_in_progress_at. NULL when DQ-8 fires.'),
  lead_time_hours       FLOAT64            OPTIONS (description = 'done_at - created_at.'),
  transition_count      INT64              OPTIONS (description = 'Total status transitions. A high count with no progress is a churn signal.'),
  reopen_count          INT64              OPTIONS (description = 'Transitions from Done back to a non-Done status.'),

  has_ai_run            BOOL               OPTIONS (description = 'At least one fct_ai_run row carries this jira_issue_key.'),
  ai_run_count          INT64              OPTIONS (description = 'Runs against this issue.'),
  dq_incomplete_workflow BOOL              OPTIONS (description = 'DQ-8: Done with no In-Progress transition. Excluded from cycle time.'),
  transformed_at        TIMESTAMP          OPTIONS (description = 'Last rebuild timestamp.')
)
PARTITION BY DATE(created_at)
CLUSTER BY jira_project_key
OPTIONS (
  partition_expiration_days = 396,
  description = 'One row per Jira issue with lifecycle timings. Partition DATE(created_at), cluster jira_project_key (CONTRACT §7). 13-month retention. Holds NO issue text or AC content (design §11.3) — keys, bounded enums, counts and timestamps only.',
  labels = [('layer', 'core'), ('domain', 'ai-telemetry')]
);
