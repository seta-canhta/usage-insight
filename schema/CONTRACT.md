# AI Telemetry — Schema Contract v1.0.0

**This file is the single source of truth.** Every component (emitter, collector,
pollers, warehouse, metric SQL) MUST conform to it. Do not redefine these structures
locally. If a change is needed, change it here and bump `schema_version`.

Design reference: `docs/spikes/ai-effectiveness-observability.md` §5, §6, §8, §9, §11.

---

## 1. Non-negotiable rules

1. **Never store content.** No prompts, no responses, no source code, no diffs, no
   secrets, no error message bodies, no raw email addresses. Store hashes, counts,
   bounded enums, and file paths only. (Design §11.3)
2. **Fail open.** Telemetry must never block, slow, or fail an agent run. Every
   client-side error is swallowed and logged locally.
3. **Idempotent.** `event_id` is the dedup key. Re-delivery must be safe.
4. **Append-only.** Never update or delete a raw event. Corrections are new events.
5. **Bounded dimensions.** Any field used as a metric dimension must have < 100
   distinct values. High-cardinality ids live in the payload, not in metric labels.
   (Design §6.1, enforced by DQ-15)
6. **No ROI, no counterfactual.** Per design §9.1 Decision 2 there is no non-AI
   control group. Do not compute `time_saved` as a headline, and do not emit any
   monetary "value delivered" field. The economic metric is cost per accepted output.

---

## 2. Envelope

Every event is one JSON object with exactly these top-level keys.

```jsonc
{
  "schema_version": "1.0.0",          // string, semver, required
  "event_id":       "evt_<uuid4hex>", // string, required, dedup key. Live-emitted
                                      //   events use uuid4. BACKFILL events from
                                      //   pollers MUST instead use a deterministic
                                      //   sha256 of the fact's natural key, so that
                                      //   re-polling a window is idempotent (rule §1.3)
                                      //   and a watermark rewind cannot duplicate rows.
  "event_type":     "run.completed",  // enum, required, see §3
  "event_time":     "RFC3339 UTC",    // required, when it happened on the client
  "ingested_at":    "RFC3339 UTC",    // set by the collector, never by the client

  "trace_id":       "trc_<uuid4hex>", // required — one user-initiated workflow
  "run_id":         "run_<uuid4hex>", // required — one agent invocation
  "parent_run_id":  null,             // string|null — sub-agent -> supervisor
  "span_id":        "spn_<uuid4hex>", // string|null — one phase
  "workflow_id":    "wf-PRJ-6383-20260819", // string|null — legacy bridge

  "actor":   { ... },                 // §2.1, required
  "context": { ... },                 // §2.2, required
  "agent":   { ... },                 // §2.3, required
  "attributes": { ... },              // §3, event-type specific
  "link":    { "method": "explicit", "confidence": 1.0 }  // §2.4, required
}
```

### 2.1 `actor`

| Field | Type | Req | Notes |
|---|---|---|---|
| `person_id` | string\|null | ✓ | Atlassian `accountId`. **Canonical person key.** |
| `person_email_hash` | string | ✓ | `sha256(salt + lower(git_author_email))`, hex. **Never the raw email.** |
| `team_id` | string\|null | ✓ | From directory; null until OQ-6 resolves |
| `role` | enum\|null | ✓ | `dev` `qa` `devops` `po` `lead` |

### 2.2 `context`

| Field | Type | Req | Notes |
|---|---|---|---|
| `jira_issue_key` | string\|null | ✓ | `^[A-Z][A-Z0-9]+-\d+$` |
| `jira_project_key` | string\|null | ✓ | Derived from the key |
| `repo_full_name` | string\|null | ✓ | `{workspace}/{repo_slug}` |
| `branch_name` | string\|null | ✓ | |
| `product_profile` | string\|null | ✓ | `watchtower` `automotive` … |
| `environment` | enum\|null | ✓ | `dev` `sit` `pre` `prd` `local` |

### 2.3 `agent`

| Field | Type | Req | Notes |
|---|---|---|---|
| `agent_name` | string | ✓ | From `.agent.md` frontmatter `name` |
| `agent_version` | string\|null | ✓ | Git SHA (short) of the `.agent.md` at load time |
| `skill_name` | string\|null | | |
| `skill_version` | string\|null | | Git SHA of the `SKILL.md` |
| `surface` | enum | ✓ | `vscode-copilot-chat` `copilot-cli` `headless` `unknown` |

### 2.4 `link`

| Field | Type | Req | Notes |
|---|---|---|---|
| `method` | enum | ✓ | `explicit` \| `heuristic` \| `marker_only` |
| `confidence` | float | ✓ | 0.0–1.0. `explicit` ⇒ 1.0 |

**Rule:** only `method='explicit'` rows may be used for cost-per-output metrics.

**Poller events carry `run_id = null` unless the commit carries an `AI-Run-Id` trailer.**
Never synthesise a `run_id` to force a join — that manufactures a join key and breaches
AR-1. The trailer is the only thing that earns `method='explicit'` on a backfilled row;
everything else is `heuristic` or `marker_only`.

---

## 3. Event types and their `attributes`

`event_type` is a closed enum. Unknown values are rejected by the collector.

| # | `event_type` | Source | `attributes` |
|---|---|---|---|
| 1 | `run.started` | emitter | `invocation_mode` (`jira_driven`\|`direct_plan`\|`unknown`), `model_declared_id`, `input_source` |
| 2 | `run.bound` | emitter | **`otel_conversation_id`** (= `gen_ai.conversation.id`), `jira_issue_key`. ⭐ The bridge between the OTel stream and the correlation stream — **but see the warning below: it is NOT a join key on its own** |
| 3 | `run.phase.started` | emitter | `phase_name` |
| 4 | `run.phase.completed` | emitter | `phase_name`, `duration_ms`, `status` (`ok`\|`failed`\|`skipped`) |
| 5 | `model.call` | **OTel** | `model_id`, `input_tokens`, `output_tokens`, `cached_input_tokens`, `reasoning_tokens`, `latency_ms`, `retry_count`, `finish_reason`. **Wire attribute names, measured 2026-08-19:** `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read.input_tokens` (→ `cached_input_tokens`), `gen_ai.usage.cache_creation.input_tokens`, `gen_ai.usage.reasoning.output_tokens` (→ `reasoning_tokens`), `gen_ai.response.model` (→ `model_id` — authoritative), `gen_ai.response.finish_reasons`. There is no `gen_ai.usage.total_tokens` |
| 6 | `tool.call` | **OTel** | `tool_name`, `tool_kind` (`mcp`\|`terminal`\|`file`\|`http`\|`other`), `duration_ms`, `status`, `error_class` |
| 7 | `human.turn` | emitter | `turn_index`, `turn_kind` (`clarification`\|`correction`\|`approval`\|`rejection`), `chars` |
| 8 | `output.generated` | emitter | `output_id`, `artifact_type` (`code`\|`test`\|`spec`\|`mock`\|`doc`\|`csv`\|`config`), `file_path`, `lines_added`, `lines_removed`, `output_content_hash`, `reuse_source` |
| 9 | `gate.evaluated` | emitter | `gate_name` (`build`\|`test`\|`lint`\|`secrets`\|`coverage`), `status` (`pass`\|`fail`\|`skipped`), `quality_score`, `coverage_pct`, `attempt_index` |
| 10 | `run.completed` | emitter | `duration_ms`, `phases_completed` |
| 11 | `run.failed` | emitter | `duration_ms`, `failure_class`, `dependency_failed` (`vpn`\|`network`\|`jira`\|`bitbucket`\|`mcp`\|`aio`\|`ci`\|`none`) |
| 12 | `run.timeout` | emitter | `duration_ms`, `timeout_policy` |
| 13 | `run.abandoned` | **DQ job** | `last_seen_at` |
| 14 | `scm.commit` | git hook | `commit_sha`, `output_ids`, `lines_added`, `lines_removed`, `has_ai_marker` |
| 15 | `scm.pr.created` | emitter | `pr_id`, `commit_shas`, `pr_title_has_ai_marker` |
| 16 | `scm.pr.reviewed` | **poller** | `pr_id`, `reviewer_person_id`, `action` (`approved`\|`changes_requested`\|`commented`), `comment_count`, `reviewed_at` |
| 17 | `scm.pr.merged` | **poller** | `pr_id`, `merged_at`, `merge_commit_sha` |
| 18 | `scm.pr.declined` | **poller** | `pr_id`, `declined_at`, `decline_reason_class` |
| 19 | `scm.revert` | **poller** | `reverted_commit_sha`, `revert_commit_sha`, `days_to_revert` |
| 20 | `ci.pipeline.completed` | **poller** | `pipeline_id`, `commit_sha`, `status`, `duration_ms`, `tests_total`, `tests_passed`, `tests_failed`, `coverage_pct`, `ci_provider`, `ci_kind` (`build`\|`deploy`), `job_name`, `job_branch`, `build_number`. **Measured 2026-08-19:** CI is self-hosted **Jenkins**, not Bitbucket Pipelines. The source is `/commit/{sha}/statuses`, which needs no Jenkins credential. `tests_*` and `coverage_pct` are NOT available on that path and stay NULL — never 0 |
| 21 | `jira.transition` | **poller** | `from_status`, `to_status`, `transitioned_at`, `status_category`, `issue` (snapshot sub-object: status, assignee accountId, issue type, labels, parent, estimates), `attribution` (AR-3 evidence sub-object: `rule`, `ai_labels`, `has_ai_labels`, `label_authored_by_ai`, `label_planned_by_ai`, `label_generated_by_ai`, `label_reviewed_by_ai`, `unrecognised_ai_labels`, `has_ai_label_drift`, `is_delivery_ticket_candidate`, `delivery_ticket_key`, `feature_ticket_key`, `feature_ticket_source`, `resolution_confidence`, `linked_issues`, `parent_key`). Issue creation is synthesised as a transition so every issue yields ≥1 event — the enum is closed, so there is no separate snapshot event. **`unrecognised_ai_labels` is a DQ signal, never a count:** it carries label *names* outside the §3.1 closed set, and those tickets are deliberately excluded from every AI figure |
| 22 | `test.run.completed` | **poller** | `test_case_key`, `test_cycle_key`, `test_run_id`, `status` (AIO run status name), `status_category` (`passed`\|`failed`\|`blocked`\|`skipped`\|`in_progress`\|`not_run`\|`other`), `is_automated`, `executed_by_person_id`, `assigned_to_person_id`, `executed_at`, `effort_seconds`, `defect_count`, `folder_name`, `priority`. **Added 2026-08-20**, when an AioAuth key first made AIO TCMS reachable — the enum stopped at 21 because this source was blocked (`401 Invalid or missing API Token`), not because test execution did not matter. For a QA engineer the test cycle *is* the delivery record; pull requests are not. `test_case_title` is deliberately **not** carried: titles are free text and belong to the §11.3 exclusions. A run that has never been executed emits `status_category = not_run` with a NULL `executed_at` — it is **not** a failure and must never be counted as one |
| 23 | `test.case.snapshot` | **poller** | `test_case_key`, `automation_status` (AIO's own value, e.g. `Automated` / `To Be Automated`, or NULL when nobody has set it), `automation_owner_person_id`, `has_automation_key`, `test_case_status`, `script_type`, `folder_name`, `priority`, `is_archived`, `created_at`, `updated_at`. **Added 2026-08-20.** This is the *inventory* event, and it exists because the denominator of Automation Coverage is invisible to event 22: a test case nobody has ever executed emits no run, and those are exactly the un-automated cases the coverage metric has to count. `automation_status` is passed through verbatim and is **NULL for roughly half the estate** — an unset field is not "not automated", so a coverage figure must state its known-status denominator or it is measuring how diligently the field is filled in |

### 3.1 Provenance markers — two closed sets, not one

The AIEP flow applies **four** markers, and they do not all live in the same place.
Detection is by **name**, never by shape: a `*_BY_COPILOT`-looking pattern match would
reintroduce the §3.5 defect where every Conventional Commit reads as AI-authored.

| Marker | Jira label | Commit subject | Applied by |
|---|---|---|---|
| `AUTH_BY_COPILOT` | ✓ | ✓ | `developer.implementer` phase_5, `supervisor-test-spec` step_3b, `test-executor-committer` step 6, `test-script-generator` |
| `GEN_BY_COPILOT` | — *(no writer yet)* | ✓ | `supervisor-test-spec`, `test-executor-committer` |
| `PLANNED_BY_COPILOT` | ✓ | — | `architect.planner`, `supervisor-test-spec` step_1, `test-spec-generator` |
| `REVIEW_BY_COPILOT` | ✓ | — | **External** AI code-review system; nothing in this repository writes it |

* **Commit-marker set** = `{AUTH_BY_COPILOT, GEN_BY_COPILOT}` — drives
  `scm.commit.has_ai_marker` and `pr.ai_commit_count`. `PLANNED_`/`REVIEW_` are
  excluded: no agent writes them onto a subject, so a commit *mentioning* one is a human
  commit about the feature.
* **Label set** = all four — drives `attribution.label_*_by_ai`. `GEN_BY_COPILOT` stays
  in the label set so that the day a writer appears it is counted, not dropped.

Matching is bounded on both sides (`(?<![A-Za-z0-9_])…(?![A-Za-z0-9_])`) so a longer
identifier that merely contains a marker never matches.

**Drift is surfaced, never silently dropped.** A label that looks like a provenance
marker but is outside the closed set (measured live: `PLANNER_BY_COPILOT`,
`DEV_BY_COPILOT`, `COPILOT_TESTING`) lands in `attribution.unrecognised_ai_labels` and
sets `attribution.has_ai_label_drift`. It is **not** counted as AI — counting it would
make the AI figure depend on whatever anyone typed — but every one of them subtracts
from the AI figure until reconciled, so the names must reach a report.

**Forbidden attribute names** (emitter and collector both reject the whole event):
`prompt`, `response`, `content`, `message`, `code`, `diff`, `body`, `text`,
`stack_trace`, `error_message`, `token`, `password`, `secret`, `api_key`, `email`.

> ⚠️ **Matching is EXACT on the lowercased key, never substring.** Substring matching
> would reject mandated §3 fields — `output_content_hash` (contains "content"),
> `input_tokens` / `output_tokens` / `cached_input_tokens` (contain "token"),
> `error_class` (contains "error"). Walk nested dicts and lists; compare each key
> exactly. Every implementation MUST carry a regression test asserting those four legal
> names survive the guard.
>
> Key-name screening is only the first layer. Both emitter and collector MUST also
> screen string *values* against the secret patterns in `skills/dev-quality-gates/SKILL.md`
> (`password\s*=`, `api[_-]?key`, `BEGIN * PRIVATE KEY`, `AKIA[0-9A-Z]{16}`), plus
> Atlassian `ATATT` and GitHub `gh[pousr]_` tokens. On rejection log the **field name
> and the check that fired — never the value**.

---

> ### 🔴 Copilot exports content by DEFAULT — the collector must strip it
>
> Measured 2026-08-19 with no content setting enabled: `gen_ai.input.messages`,
> `gen_ai.output.messages`, `gen_ai.system_instructions`, `gen_ai.tool.call.arguments`,
> `gen_ai.tool.call.result` and `copilot_chat.user_request` carried prompts, replies,
> the system prompt, absolute paths with the username, and **source file contents**.
> Vendor docs say `captureContent` defaults off; it was off, and content still arrived.
>
> Never point Copilot's OTLP exporter at an endpoint that does not redact.
> `collector/otel_config.yaml` is that redactor and its behaviour is verified by
> replaying real captured spans through it.
>
> **Key-pattern sweeps in that config MUST be anchored to a whole dot-segment**, for
> exactly the reason stated for the JSON path above. An unanchored sweep containing
> `response` silently deleted `gen_ai.response.model` — the field `dim_model_pricing`
> joins on — which would have priced every run to NULL. Found by measurement, not review.

> ### ⚠️ `otel_conversation_id` is not a join key
>
> A Copilot `gen_ai.conversation.id` identifies a chat **session**, not a run. One
> session hosts sequential runs, and a supervisor's nine sub-agents all share it. A
> plain equijoin on conversation id is therefore **many-to-many** and silently
> multiplies every token in the session by the number of runs in it.
>
> The bind MUST be conversation id **plus a time window**:
> 1. Build each bound run's active window `[started_at − 300s, terminal_at + 300s]`,
>    capped at 24h for a run with no terminal event (matching the DQ-2 orphan window,
>    so an abandoned run stops absorbing later tokens).
> 2. Join spans on `conversation_id` AND `start_time` inside that window.
> 3. Force one span to exactly one run:
>    `QUALIFY ROW_NUMBER() OVER (PARTITION BY span_id ORDER BY started_at DESC, run_id ASC) = 1`
>
> `PARTITION BY span_id` is load-bearing — it makes double-counting structurally
> impossible rather than merely unlikely. `started_at DESC` assigns a span to the most
> recently started run whose window contains it, so tokens land on the **sub-agent**
> that issued the call; AR-4 then rolls the supervisor up by `trace_id`.

---

## 4. Cost derivation

Cost is **never** emitted by a client. It is computed in the warehouse:

```
cost_usd = input_tokens        /1000 * input_per_1k_usd
         + output_tokens       /1000 * output_per_1k_usd
         + cached_input_tokens /1000 * cached_input_per_1k_usd
```

joined to `dim_model_pricing` on `model_id` AND
`event_time BETWEEN effective_from AND COALESCE(effective_to, '9999-12-31')`.

**Price per call, not per run** — a run can span two models or straddle a midnight
price change. If **any** call in a run is unpriceable, the run's `cost_usd` is `NULL`:
summing only the priceable calls yields a number that looks complete and is
systematically low.

`cost_basis ∈ {measured, modelled, seat_allocated}`. OTel spans ⇒ `measured`; the same
event types arriving on the correlation stream (non-exporting surfaces) ⇒ `modelled`.
**Never mix the two within one run.**
An unpriced `model_id` ⇒ `cost_usd = NULL` (**never 0**) + DQ-6 finding.

---

## 5. Acceptance state machine

Computed nightly on `fct_ai_output`. (Design §8.7, §9.2)

```
generated ──> in_flight ──┬──> accepted   (merged AND post_review_change_ratio <= 0.25
                          │                AND NOT reverted within 30d)
                          ├──> reworked   (merged AND ratio > 0.25)
                          ├──> rejected   (PR declined OR never committed within 7d)
                          └──> reverted   (was merged, revert detected within 30d)
```

`post_review_change_ratio = lines_changed_after_first_review / lines_generated`

**Maturity window:** outputs whose PR is younger than 7 days stay `in_flight` and are
excluded from acceptance-rate denominators.

**Precedence when rules conflict** (maturity does NOT override terminal facts):
`reverted` > `rejected (declined)` > `rejected (never committed within 7d)` > maturity.
A revert or a decline is definitive; the maturity window exists to avoid judging work
still in motion, and declined or reverted work has stopped moving. Record which rule
fired in `acceptance_state_reason`.

**Zero-line merged output:** ratio denominator is 0 ⇒ ratio `NULL` ⇒ `accepted` with
reason `merged_ratio_undefined`, never silently pooled with ordinary accepted rows.

**`post_review_change_ratio` is computed once per PR** and propagated to its outputs.
Splitting a post-review commit across individual outputs would require git-blame line
attribution, which the Bitbucket API alone cannot provide; pro-rata splitting would
invent precision.

---

## 6. Attribution rules (enforced in the transform layer, not by convention)

| Rule | Enforcement |
|---|---|
| AR-1 one output → one run | `fct_ai_output` unique on `output_id`; conflicts → DQ-16 quarantine |
| AR-3 `qd_jira_key` resolves to the feature ticket | Transform resolves; `delivery_ticket_key` kept separately |
| AR-4 supervisor does not inherit sub-agent outputs | Roll up by `trace_id`, never re-count |
| AR-5 deduplicated artifact is `reused`, not `generated` | `reuse_source` non-null ⇒ excluded from output volume |
| AR-7 a PR's AI share is fractional | `ai_line_share = ai_lines / total_changed_lines` |
| AR-8 seat/infra cost allocated once per person-month | Pro rata by run count |
| AR-9 revert withdraws credit | State → `reverted` in the period the revert occurs |

---

## 7. Table names

| Layer | Dataset.table | Partition | Cluster |
|---|---|---|---|
| Raw | `raw.ai_run_event` | `DATE(event_time)` | `person_id, agent_name` |
| Raw | `raw.otel_span` | `DATE(start_time)` | `conversation_id` |
| Core | `core.fct_ai_run` | `DATE(started_at)` | `agent_name, model_id` |
| Core | `core.fct_ai_output` | `DATE(generated_at)` | `acceptance_state, artifact_type` |
| Core | `core.fct_pull_request` | `DATE(created_on)` | `repo_full_name` |
| Core | `core.fct_ci_run` | `DATE(started_at)` | `repo_full_name` |
| Core | `core.fct_jira_issue` | `DATE(created_at)` | `jira_project_key` |
| Dim | `core.dim_person` | — | — |
| Dim | `core.dim_model_pricing` | — | — |
| Dim | `core.dim_agent_version` | — | — |
| Mart | `marts.agg_daily_person_agent` | `day` | — |
| DQ | `dq.dq_findings` | `DATE(detected_at)` | `check_id` |

---

## 8. Local buffer format

Emitter writes newline-delimited JSON to `~/.aiep/telemetry/`:

```
~/.aiep/telemetry/          # override root with $AIEP_TELEMETRY_DIR
  current-run               # plain text: active run_id (the git hook reads this)
  runs/<run_id>.env         # KEY=VALUE sidecar: trace_id, agent, agent_version, model,
                            #   context, turn_index, phases_completed, started_at.
                            #   current-run stays a bare run_id per the rule above;
                            #   the sidecar carries everything the §9 trailers need.
  pending/*.ndjson          # one file per day, append-only, awaiting ship
  shipped/*.ndjson          # moved here after successful ship; pruned after 7 days
  emit.log                  # client-side diagnostics only, never event content
```

**`run.phase.started` (event 3)** is emitted by `phase --start`; `phase --status ...`
emits `run.phase.completed` (event 4).

**Salt:** `$AIEP_TELEMETRY_SALT`, default constant `aiep-telemetry-v1`. The salt is not
a secret — it exists to stop trivial rainbow-table reversal of the email hash, not to
protect it cryptographically.

## 9. Git commit trailers

Appended by `prepare-commit-msg`. **Never modify the commit subject** — the existing
`[AUTH_BY_COPILOT] [TICKET]` / `[GEN_BY_COPILOT] [TICKET]` convention (§3.1) must
remain intact.

```
AI-Run-Id: run_01hq8f3zk9m2nx7b4c6d
AI-Trace-Id: trc_01hq8f3zk1aabbccddee
AI-Agent: Platform Developer 2.0@a3f21c9
AI-Model: GPT-5.3-Codex
```
