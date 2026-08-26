-- =====================================================================================
-- 02_dims.sql — CORE dimensions
-- =====================================================================================
-- Contract:  schema/CONTRACT.md §2.1 (actor), §4 (cost), §7 (names)
-- Design:    docs/spikes/ai-effectiveness-observability.md §5.2, §6.4, §8.4, §9.4, §11.4
--
-- Three dimensions:
--   core.dim_person         — identity resolution. ACCESS-RESTRICTED.
--   core.dim_model_pricing  — effective-dated price book. Makes historical cost
--                             reproducible when vendors change prices.
--   core.dim_agent_version  — agent/skill version registry with declared model.
--
-- Dimensions are NOT partitioned (CONTRACT §7 lists no partition for them) and carry
-- no partition_expiration_days: they are small, slowly-changing, and expiring them
-- would break the reproducibility of historical facts that reference them.
--
-- Substitute ${PROJECT_ID} before running.
-- =====================================================================================

CREATE SCHEMA IF NOT EXISTS `${PROJECT_ID}.core`
OPTIONS (
  location    = 'EU',
  description = 'AI telemetry conformed layer: facts and dimensions. 13-month fact retention (design §11.2). Access: engineering leads, QA leads, DevOps (design §11.4).'
);


-- =====================================================================================
-- core.dim_person
-- =====================================================================================
-- ⚠⚠ ACCESS-RESTRICTED TABLE — design §11.4. ⚠⚠
--
--   Who may read it:      NAMED DATA STEWARDS ONLY.
--   Mechanism:            separate IAM binding on this table (not dataset-inherited),
--                         GCP Data Access audit logging enabled, quarterly access
--                         review. It is the only place in the warehouse where a
--                         pseudonymous hash can be turned back into a human being.
--   Everyone else:        reads person facts through authorised views that expose
--                         person_id / team_id only and never git_author_aliases or
--                         person_email_hash -> alias mapping.
--   Retention:            life of employment + 30 days (design §11.2). Deletion on
--                         request. This table is therefore the ONE table in the
--                         warehouse subject to manual deletion — everything else is
--                         append-only with platform-enforced expiry.
--
-- Why it has to exist at all: design §9.4 measured the identity-collision problem on
-- this very repository — "Bob Smith" vs "Bob Smtih", "Ann Lee" vs
-- "Lee, Ann" (same address), "DevOne" with no email address at all. Naive
-- aggregation on the git display name splits one engineer across up to three rows and
-- in one case produces a row that cannot be joined to Jira at all. person_id must be
-- the Atlassian accountId, resolved through this maintained map — never a git name.
--
-- git_author_aliases is REPEATED because one person legitimately has many git
-- identities (work laptop, CI, a typo'd .gitconfig). Each alias holds the SALTED HASH
-- of the email plus the display name; the raw email address is NEVER stored anywhere
-- in the warehouse (CONTRACT §1.1, design §11.3).
-- =====================================================================================
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.core.dim_person`
(
  person_id           STRING NOT NULL OPTIONS (description = 'Atlassian accountId. CANONICAL PERSON KEY across the entire warehouse.'),
  person_email_hash   STRING NOT NULL OPTIONS (description = 'PRIMARY email hash: sha256(salt + lower(primary_git_email)), hex. Join key from git-derived facts. Never the raw address.'),

  git_author_aliases  ARRAY<STRUCT<
    email_hash        STRING,
    display_name      STRING,
    first_seen        DATE,
    last_seen         DATE,
    resolution_source STRING,
    is_verified       BOOL
  >>                          OPTIONS (description = 'All git identities that resolve to this person. email_hash is salted SHA-256 — the raw address is NEVER stored (design §11.3). display_name is retained ONLY here, in this restricted table, so a steward can audit a resolution. resolution_source: directory | manual | heuristic_name_match. is_verified=FALSE rows are candidates awaiting steward confirmation and MUST NOT be used by DQ-1 as evidence of resolution.'),

  team_id             STRING          OPTIONS (description = 'From the HR/org directory. NULL until OQ-6 resolves — team rollups are blocked on this, and a NULL here must surface as an explicit "unassigned" bucket, never be dropped silently.'),
  role                STRING          OPTIONS (description = 'Bounded enum: dev | qa | devops | po | lead.'),
  tenure_start_date   DATE            OPTIONS (description = 'Start of AI-tooling tenure, NOT employment start. Drives the tenure_bucket dimension that §8.11 makes MANDATORY for rendering manual_intervention_rate — the same rate means opposite things for a new hire and a six-month user (design §11.5).'),
  jira_account_active BOOL            OPTIONS (description = 'FALSE once the Atlassian account is deactivated. Retention clock (employment + 30d) starts here.'),

  effective_from      DATE   NOT NULL OPTIONS (description = 'Slowly-changing-dimension validity start. Team and role change; historical facts must keep resolving to the team the person was in AT THE TIME.'),
  effective_to        DATE            OPTIONS (description = 'SCD validity end. NULL = current row.'),
  updated_at          TIMESTAMP       OPTIONS (description = 'Last steward edit. Audit trail.')
)
OPTIONS (
  description = '⚠ ACCESS-RESTRICTED (design §11.4): named data stewards only, separate IAM, access-logged, quarterly review. Identity map person_id (Atlassian accountId) <-> person_email_hash <-> git aliases <-> team_id. Retention: life of employment + 30 days; deletion on request. Contains NO raw email addresses. Effective-dated so historical team/role attribution stays correct.',
  labels = [('layer', 'dim'), ('domain', 'ai-telemetry'), ('pii', 'restricted'), ('access', 'stewards-only')]
);


-- =====================================================================================
-- core.dim_model_pricing
-- =====================================================================================
-- Design §8.4 / CONTRACT §4. EFFECTIVE-DATED by construction.
--
-- Why effective-dating is not optional: a price change must not silently restate last
-- quarter's cost. The cost join in 04_transform_run.sql is
--
--     ON  p.model_id = r.model_id
--     AND DATE(event_time) BETWEEN p.effective_from
--                              AND COALESCE(p.effective_to, DATE '9999-12-31')
--
-- so a run priced in June keeps June's rate forever. Closing a price row means setting
-- effective_to on the old row AND inserting a new row starting the next day — never
-- UPDATE-ing the rate in place.
--
-- Invariant (checked by DQ-6b in 07_dq_checks.sql): for any (model_id, date) there is
-- AT MOST ONE row. Overlapping validity windows silently multiply cost.
--
-- Replaces the four-entry hardcoded price map in the legacy
-- claude_agents/skills/qa-metrics-tracker.yaml, whose models are no longer in use.
-- =====================================================================================
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.core.dim_model_pricing`
(
  model_id                 STRING  NOT NULL OPTIONS (description = 'EXACT runtime model id. Since 1.1.0 that means the KEY of session.shutdown.modelMetrics in the Copilot session journal, e.g. "claude-sonnet-4.6"; pre-cutover rows were keyed on the span field gen_ai.response.model. Must match byte-for-byte — a mismatch produces a DQ-6 unpriced-model finding and cost_usd = NULL, never 0.'),
  model_family             STRING           OPTIONS (description = 'Bounded rollup dimension, e.g. "gpt-5", "claude-sonnet". Used for reporting when individual ids churn.'),
  vendor                   STRING           OPTIONS (description = 'Bounded: openai | anthropic | github | other.'),

  effective_from           DATE    NOT NULL OPTIONS (description = 'Inclusive start of this rate window.'),
  effective_to             DATE             OPTIONS (description = 'INCLUSIVE end of this rate window. NULL = currently in force. Set this when superseding a rate; never edit a rate in place.'),

  input_per_1k_usd         NUMERIC          OPTIONS (description = 'USD per 1000 input tokens. NULL = price genuinely unknown -> cost_usd resolves to NULL (CONTRACT §4). Do NOT enter 0 to mean unknown.'),
  output_per_1k_usd        NUMERIC          OPTIONS (description = 'USD per 1000 output tokens. NULL = unknown, never 0.'),
  cached_input_per_1k_usd  NUMERIC          OPTIONS (description = 'USD per 1000 cached-input tokens. Cached input is a SUBSET of input_tokens billed at this cheaper rate — CONTRACT §4 prices it as its own term. NULL = unknown, never 0.'),

  is_placeholder           BOOL    NOT NULL OPTIONS (description = '⚠ TRUE means the rates are NOT real vendor pricing. Any cost figure derived from a placeholder row is unusable for chargeback or any published number. 07_dq_checks.sql DQ-6c raises a finding for every run priced from a placeholder row.'),
  source_url               STRING           OPTIONS (description = 'Provenance for audit — the vendor pricing page the numbers came from. Mandatory for is_placeholder = FALSE rows.'),
  notes                    STRING           OPTIONS (description = 'Free-text provenance note for the steward who entered the row.'),
  updated_at               TIMESTAMP        OPTIONS (description = 'When this row was written.')
)
OPTIONS (
  description = 'Effective-dated model price book (design §8.4, CONTRACT §4). Joined on model_id AND event_time BETWEEN effective_from AND COALESCE(effective_to, 9999-12-31) so historical costs stay reproducible across price changes. An unpriced model_id yields cost_usd = NULL (NEVER 0) plus a DQ-6 finding. Rows with is_placeholder = TRUE carry non-authoritative rates.',
  labels = [('layer', 'dim'), ('domain', 'ai-telemetry')]
);


-- -------------------------------------------------------------------------------------
-- SEED — the two models actually declared in this repository's agent frontmatter
-- -------------------------------------------------------------------------------------
-- ⚠⚠ THESE RATES ARE PLACEHOLDERS. THEY ARE NOT VENDOR PRICING. ⚠⚠
--
-- Design §8.4 says to populate this table "from the models actually declared in agent
-- frontmatter (GPT-5.3-Codex, Claude Sonnet 4.6) plus whatever the runtime reports".
-- Those two ids are seeded below so that the transform has a joinable row and the
-- pipeline is testable end to end.
--
-- The RATES, however, are deliberately left as NULL. Reasons, in order of importance:
--
--   1. Inventing a plausible-looking number is worse than having none. A NULL rate
--      propagates to cost_usd = NULL and fires DQ-6, which is loud and correct.
--      A fabricated 0.003 propagates to a dashboard, gets screenshotted into a
--      steering deck, and is indistinguishable from a real figure.
--   2. CONTRACT §4 already mandates NULL-not-zero for unpriced models. Seeding NULL
--      rates exercises that path from day one rather than discovering it in
--      production.
--   3. No vendor pricing for these ids is confirmed anywhere in this repository, and
--      this spike is not the place to assert one.
--
-- ACTION REQUIRED BEFORE ANY COST FIGURE IS PUBLISHED:
--   a) obtain the real per-1k rates from the vendor pricing pages;
--   b) close these placeholder rows by setting effective_to to the day before the
--      real rate starts;
--   c) INSERT real rows with is_placeholder = FALSE and a populated source_url.
--   Do NOT UPDATE the rate columns of these rows in place — that would silently
--   restate every historical cost (design §8.4).
--
-- effective_from is set to the first AI-marked commit date observed in this
-- repository (2026-03-25, design §9.1b [V]) so that the window covers all telemetry
-- the platform could ever have collected.
--
-- ⚠ PROVENANCE CORRECTED 2026-08-26 (contract 1.1.0). The note under these rows used
-- to read "Verified against real Copilot Chat spans, 2026-08-19", where "the wire"
-- meant the OTel attribute gen_ai.response.model. That stream is gone. The key the
-- pricing join now matches is the MAP KEY of session.shutdown.modelMetrics in
-- Copilot CLI's own journal, which is a different field from a different producer —
-- so the old sentence was a provenance claim about a source no longer read. Left
-- alone it would have looked like verification of something nobody had checked.
--
-- The join at 04_transform_run.sql is EXACT on model_id, so a one-character drift
-- between these seeds and the journal keys prices EVERYTHING to NULL. Re-check the
-- keys, do not assume them:
--
--   python3 cli/copilot_read.py --root ~/.copilot 2>/dev/null \
--     | python3 -c 'import json,sys;print(sorted({json.loads(l)["attributes"]["model_id"] \
--         for l in sys.stdin if json.loads(l)["event_type"]=="model.call"}))'
--
-- DQ-MODEL in 07_dq_checks.sql runs the warehouse-side equivalent nightly, so a model
-- id that appears on the wire and not here becomes a finding rather than a silent NULL.
-- -------------------------------------------------------------------------------------
INSERT INTO `${PROJECT_ID}.core.dim_model_pricing`
  (model_id, model_family, vendor,
   effective_from, effective_to,
   input_per_1k_usd, output_per_1k_usd, cached_input_per_1k_usd,
   is_placeholder, source_url, notes, updated_at)
VALUES
  -- ⚠ MODEL IDS BELOW ARE THE ONES OBSERVED ON THE WIRE, not the ones written in
  -- agent frontmatter, which spells the same model differently:
  --     frontmatter "Claude Sonnet 4.6"  ->  journal key "claude-sonnet-4.6"
  -- Seeding the frontmatter spelling would miss every row and price everything to
  -- NULL, because the join is exact.
  --
  -- [1.1.0 — CURRENT] Observed as a session.shutdown.modelMetrics key in Copilot
  -- CLI's own journal, measured 2026-08-26 across 22 sessions on the reference
  -- machine. This is the only id the live source has produced.
  ('claude-sonnet-4.6', 'claude-sonnet', 'anthropic',
   DATE '2026-03-25', NULL,
   NULL, NULL, NULL,
   TRUE, NULL,
   'PLACEHOLDER ROW — NOT VENDOR PRICING. model_id OBSERVED 2026-08-26 as a session.shutdown.modelMetrics key in the Copilot CLI journal [V] — the 1.1.0 source. Rates intentionally NULL so cost_usd resolves to NULL and DQ-6 fires, per CONTRACT §4. Populate with sql/09_set_model_price.sql.',
   CURRENT_TIMESTAMP()),

  -- [LEGACY — pre-cutover only] Seen on the OTel span stream as
  -- gen_ai.response.model, 2026-08-19. That stream is frozen (see 01_raw.sql), so
  -- this id will not appear on a new event. It is KEPT because raw.otel_span rows
  -- inside the 90-day window still reprocess through the legacy branch of
  -- 04_transform_run.sql, and an unpriceable id there would restate historical cost
  -- to NULL. Delete it when the last pre-cutover partition expires, not before.
  ('gpt-4o-mini-2024-07-18', 'gpt-4o', 'openai',
   DATE '2026-03-25', NULL,
   NULL, NULL, NULL,
   TRUE, NULL,
   'PLACEHOLDER ROW — NOT VENDOR PRICING. LEGACY: model_id observed 2026-08-19 via gen_ai.response.model on the now-frozen span stream [V]. Copilot used this small model for internal housekeeping (conversation titling), so it appeared even when the agent declared a different model. Retained to keep pre-cutover reprocessing priceable. Populate with 09_set_model_price.sql.',
   CURRENT_TIMESTAMP()),

  -- Declared in agents/development/developer.implementer.agent.md frontmatter but
  -- NOT observed on either source. Kept so a run using it prices rather than
  -- silently failing DQ-6; remove if it never appears.
  ('GPT-5.3-Codex', 'gpt-5', 'openai',
   DATE '2026-03-25', NULL,
   NULL, NULL, NULL,
   TRUE, NULL,
   'PLACEHOLDER ROW — NOT VENDOR PRICING. model_id taken from agent frontmatter [V], NOT confirmed on the span stream or in the session journal. Populate with 09_set_model_price.sql.',
   CURRENT_TIMESTAMP());


-- =====================================================================================
-- core.dim_agent_version
-- =====================================================================================
-- Design §6.4: grain (agent_name, agent_version). The registry that makes the
-- "agent version before vs after a prompt change" experiment in design §9.1 possible.
--
-- model_declared_id is the frontmatter value. The transform compares it against the
-- model the runtime actually reported (since 1.1.0 the model.call `model_id`, i.e. the
-- session.shutdown.modelMetrics key; before the cutover, raw.otel_span
-- .gen_ai_response_model) to detect MODEL DRIFT — an agent declaring GPT-5.3-Codex whose runs are answered by something
-- else is a finding, not a rounding error, because the cost comparison in §9.1
-- silently becomes meaningless.
--
-- Skill versions are held in a repeated field rather than exploding the grain: a run
-- loads one agent but may load several skills, and design §9.1 wants to compare "runs
-- with step-reuse-detector loaded vs without".
-- =====================================================================================
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.core.dim_agent_version`
(
  agent_name         STRING NOT NULL OPTIONS (description = 'From .agent.md frontmatter `name`, e.g. "Platform Developer 2.0", "supervisor-test-spec". Bounded dimension (CONTRACT §1.5) — DQ-15 guards it.'),
  agent_version      STRING NOT NULL OPTIONS (description = 'Short git SHA of the .agent.md at load time. Together with agent_name this is the grain.'),

  agent_path         STRING          OPTIONS (description = 'Repository path of the .agent.md, e.g. agents/dev/developer.implementer.agent.md.'),
  agent_kind         STRING          OPTIONS (description = 'Bounded: supervisor | phase | standalone. AR-4 depends on knowing which runs are supervisors so their sub-agents outputs are never re-counted.'),
  model_declared_id  STRING          OPTIONS (description = 'Model declared in frontmatter. Compared against the runtime-reported model to detect drift (design §5.2 model_declared_id).'),

  declared_skills    ARRAY<STRUCT<
    skill_name       STRING,
    skill_version    STRING,
    load_mode        STRING
  >>                                 OPTIONS (description = 'Skills this agent version loads. load_mode: always | on_demand. Powers the design §9.1 "skill on vs off" comparison arm.'),

  base_context_tokens INT64          OPTIONS (description = 'Calibrated once per agent version from the .agent.md plus always-loaded SKILL.md sizes (design §8.2). A supervisor system prompt is a large, FIXED, and therefore highly predictable token cost — worth reporting on its own. Also the base term of the modelled-cost fallback.'),
  agent_md_lines      INT64          OPTIONS (description = 'Line count of the .agent.md at this version. Cheap proxy for prompt-size change between versions.'),

  first_seen_at      TIMESTAMP       OPTIONS (description = 'First run observed on this version. The reuse-rate denominator in §8.15 EXCLUDES assets created inside the reporting period — this column is how that exclusion is applied.'),
  last_seen_at       TIMESTAMP       OPTIONS (description = 'Most recent run observed on this version.'),
  is_shared_asset    BOOL            OPTIONS (description = 'TRUE when the asset meets the §8.15 shared-asset bar: distinct_users >= 2 AND invocations >= 5. Recomputed by the nightly job, not hand-set.'),
  updated_at         TIMESTAMP       OPTIONS (description = 'Last refresh of the derived columns.')
)
OPTIONS (
  description = 'Agent/skill version registry with declared model (design §6.4). Grain: (agent_name, agent_version). Enables the design §9.1 config-comparison arms — agent vs agent, version before/after a prompt change, skill on vs off — and supplies base_context_tokens for the modelled-cost fallback (§8.2).',
  labels = [('layer', 'dim'), ('domain', 'ai-telemetry')]
);


-- =====================================================================================
-- core.dim_task_benchmark  (supporting dimension for DQ-12)
-- =====================================================================================
-- Loaded from config.yaml `metrics.manual_time_benchmarks` / `automated_time_targets`
-- [V]. It exists for ONE reason: DQ-12 ("benchmark absent") in design §9.4 requires a
-- lookup to check against.
--
-- ⚠ SCOPE NOTE. Nothing in this warehouse computes `time_saved`. CONTRACT §1.6 and
-- design §9.1 Decision 2 forbid it as a headline, and §8.16 forbids any monetary
-- "value delivered" figure. This table therefore feeds a DATA-QUALITY CHECK only —
-- it does not feed a metric, and no query in 08_metrics.sql reads it. Its presence
-- must not be taken as licence to reintroduce time-saved reporting.
-- =====================================================================================
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.core.dim_task_benchmark`
(
  task_type              STRING NOT NULL OPTIONS (description = 'Bounded task-type key from config.yaml, e.g. framework_analysis, requirements_analysis, test_spec_creation, test_case_writing, script_generation.'),
  artifact_type          STRING          OPTIONS (description = 'Artifact type this benchmark applies to: code | test | spec | mock | doc | csv | config.'),
  agent_name             STRING          OPTIONS (description = 'Agent the benchmark was calibrated for, when it is agent-specific.'),
  manual_minutes         INT64           OPTIONS (description = 'config.yaml metrics.manual_time_benchmarks value.'),
  automated_target_min   INT64           OPTIONS (description = 'config.yaml metrics.automated_time_targets value.'),
  source_ref             STRING          OPTIONS (description = 'Provenance, e.g. "config.yaml#metrics.manual_time_benchmarks".'),
  updated_at             TIMESTAMP       OPTIONS (description = 'Load timestamp.')
)
OPTIONS (
  description = 'Task-type benchmark lookup, loaded from config.yaml [V]. Exists SOLELY so DQ-12 (design §9.4) has something to check against. No metric reads it — time_saved is deliberately not computed (CONTRACT §1.6, design §9.1 Decision 2, §8.16).',
  labels = [('layer', 'dim'), ('domain', 'ai-telemetry')]
);
