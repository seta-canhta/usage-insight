# Turning on GitHub Copilot's native OpenTelemetry export

**Audience:** platform operators and whoever owns the org's Copilot configuration.
**Scope:** switching the export on, pointing it at this repo's collector, keeping
content capture off, and proving that token attributes actually arrive.

**Related:**
`collector/otel_config.yaml` (the collector that receives this),
`schema/CONTRACT.md` §3 (`model.call`, `tool.call`),
`docs/spikes/ai-effectiveness-observability.md` §4.4a, §6.5, §11.1, §11.3.

---

## 0. How to read the evidence tags

Every factual claim below carries one of these. Do not silently promote a tag.

| Tag | Meaning |
|---|---|
| **[E]** | **Vendor-documented.** Stated in Microsoft/GitHub product documentation or changelog, recorded in design §4.4a with sources, verified 2026-08-19. |
| **[A]** | **Assumption.** Plausible, consistent with the docs, but **not** confirmed for *this organisation's* Copilot plan, client versions, or models. Must be validated in the POC before anything depends on it. |
| **[D]** | **This project's recorded decision.** Not a vendor behaviour at all — a choice we made and are accountable for. |

A one-page summary of every tag in this document is in [§8](#8-vendor-documented-vs-assumed--the-summary-table).

---

## 1. What you are switching on

**[E]** Copilot Chat (the VS Code extension) and the Copilot CLI agent host both ship
their own OTLP exporter emitting the OpenTelemetry **GenAI semantic conventions**.
There is no instrumentation library to add, no proxy to build, no sidecar. This is a
product feature to configure, not software to write.

**[E]** What arrives:

| Signal | Detail |
|---|---|
| Spans | `invoke_agent` (whole orchestration) → `chat` (per LLM call) → `execute_tool` (per tool) → `execute_hook` |
| Token attributes | `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, plus cached-input and reasoning token counters |
| Identity attributes | `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.agent.name`, `gen_ai.conversation.id`, `gen_ai.operation.name` (`invoke_agent`\|`chat`\|`execute_tool`\|`embeddings`), `gen_ai.tool.name` |
| Metrics | Operation duration, token usage, tool-call counts, agent invocation latency |
| Events | Session start, tool calls, user feedback |

**[E] There is no `gen_ai.usage.total_tokens`.** Copilot does not emit it. Total is
computed as `input_tokens + output_tokens` by the `transform/total_tokens` processor in
`otel_config.yaml`, which also stamps `aiep.total_tokens_basis = "computed_input_plus_output"`
so `raw.otel_span` never claims a measured total it never received.

**[A]** The exact spelling of the cached and reasoning counters. Design §4.4a names
them as "cached-input and reasoning tokens" without pinning the attribute keys.
`otel_config.yaml` therefore accepts **both** observed shapes —
`gen_ai.usage.cached_input_tokens` / `gen_ai.usage.input_tokens.cached` and
`gen_ai.usage.reasoning_tokens` / `gen_ai.usage.output_tokens.reasoning` — and
normalises onto the CONTRACT.md §3 `model.call` names. **Confirm the real spelling in
step [§6](#6-verification-procedure) and then delete the branch you do not need.**

**[E] What it does *not* solve: correlation.** OTel knows `gen_ai.conversation.id`. It
knows nothing about `jira_issue_key`, `pr_id` or `commit_sha`. The join stays this
repository's problem, bridged by the `run.bound` event (CONTRACT.md §3 row 2) that
`emit.py` produces. Switching OTel on does not make the emitter optional.

---

## 2. Configuration layers, and which one wins

**[E] Precedence, highest first:**

```
1. Enterprise-managed settings   (MDM / server-managed / managed-settings.json)
2. Environment variables         (COPILOT_OTEL_ENABLED, OTEL_EXPORTER_OTLP_ENDPOINT)
3. User settings                 (VS Code settings.json)
```

**A managed value always wins over env vars and user settings.** That is the whole
reason the managed layer is worth the effort: it is the only layer an engineer cannot
override, deliberately or accidentally.

### 2.1 User layer — VS Code settings

```jsonc
// settings.json — the developer-local, lowest-precedence layer
{
  "github.copilot.chat.otel.enabled": true,        // [E] default: false
  "github.copilot.chat.otel.otlpEndpoint": "https://<collector-host>/v1/traces",
  "github.copilot.chat.otel.captureContent": false // [E] default: false — KEEP IT
}
```

**[E]** `github.copilot.chat.otel.enabled` defaults to `false`; nothing is exported
until someone turns it on.
**[A]** That the endpoint setting accepts a path-suffixed OTLP HTTP URL rather than a
bare base URL. Design §4.4a names the setting but not its URL grammar. Try the base
URL first (`https://<collector-host>`) and fall back to the `/v1/traces` suffix; the
verification in §6 tells you which one your client build wants.

Use this layer for **one machine during the POC only**. It does not scale, it cannot
be audited, and it can be switched off by the person you are measuring.

### 2.2 Environment layer

```bash
export COPILOT_OTEL_ENABLED=true
export OTEL_EXPORTER_OTLP_ENDPOINT="https://<collector-host>"
```

**[E]** Both variables are documented configuration points for the Copilot exporter.
**[E]** This layer is the one the Copilot CLI agent host reads most naturally, since a
CLI has no VS Code `settings.json`.
**[A]** That the standard OTel SDK companions (`OTEL_EXPORTER_OTLP_HEADERS`,
`OTEL_EXPORTER_OTLP_PROTOCOL`, `OTEL_RESOURCE_ATTRIBUTES`) are honoured. They are
conventional for OTLP exporters, but design §4.4a documents only the two above. **If
`OTEL_EXPORTER_OTLP_HEADERS` is not honoured, the bearer auth on the collector's OTLP
receiver cannot be satisfied from this layer** — which is one more reason to use the
managed layer, whose exporter headers *are* documented (§2.3).

**[E] Security note that matters here:** managed exporter headers are applied **only**
to the extension's own OTLP exporter and are **never** passed through environment
variables. Collector credentials therefore do not leak into subprocess tools that the
agent spawns — a real risk with an agent that runs `run_in_terminal` as heavily as
`agents/development/developer.implementer.agent.md` does.

### 2.3 Enterprise-managed layer — the one that counts

**[E]** Three delivery mechanisms, all outranking everything else:

| Mechanism | Where it lives |
|---|---|
| **MDM — Windows** | Windows Registry policy keys pushed by Intune/Group Policy |
| **MDM — macOS** | Managed preferences (a configuration profile / `.mobileconfig` payload) |
| **Server-managed settings** | Applied to signed-in GitHub accounts by the enterprise, no endpoint agent needed |
| **File-based** | `managed-settings.json` placed by the device management tool |

Shape of `managed-settings.json` — **[A]** the exact key names and nesting for the
file-based variant are not pinned by design §4.4a; treat this as a template to confirm
against the current admin documentation before rollout:

```jsonc
{
  "github.copilot.chat.otel.enabled":        { "value": true,  "userCanOverride": false },
  "github.copilot.chat.otel.otlpEndpoint":   { "value": "https://<collector-host>",
                                               "userCanOverride": false },
  "github.copilot.chat.otel.protocol":       { "value": "otlp-http", "userCanOverride": true },
  "github.copilot.chat.otel.captureContent": { "value": false, "userCanOverride": false },
  "github.copilot.chat.otel.exporterHeaders":{ "value": { "Authorization": "Bearer <token>" },
                                               "userCanOverride": false }
}
```

**[E]** That enterprise-managed settings can *enforce* a value **and control whether
users may override it** is documented behaviour and is the entire basis of §5.
**[A]** The literal JSON keys above (`value` / `userCanOverride`, the `protocol` and
`exporterHeaders` setting ids). Validate before rollout.

Never commit a real endpoint or a real token into this repository or into any file
under version control. The token belongs in the MDM payload, sourced from the same
Secret Manager secret the collector reads (`deploy/deploy.sh`, IAM note A).

---

## 3. Protocol options

**[E]**

| Value | Use it for |
|---|---|
| `otlp-http` | **Default, and what we use.** Matches the collector's `:4318` receiver in `otel_config.yaml`. Survives corporate proxies that mangle gRPC. |
| `otlp-grpc` | Lower overhead at volume. The collector's `:4317` receiver is configured and ready; switch only if HTTP proves to be a bottleneck. |
| `console` | Prints spans to the extension's output channel. **The fastest way to debug §6 step 2** — you see the attributes without any network path at all. |
| `file` | Writes spans to a local file. Useful for capturing a sample to attach to a ticket. |

**[D]** Use `otlp-http` in production. Use `console` for verification and for the
privacy audit in §5.3, because it lets you read exactly what the client would have
sent before it is sent anywhere.

---

## 4. Pointing it at this repo's collector

The OTel collector defined by `otel_config.yaml` listens on:

* `:4318` — OTLP/HTTP, bearer-authenticated
* `:4317` — OTLP/gRPC, bearer-authenticated
* `:13133` — health check

Set the Copilot endpoint to the collector's public address (whatever the load balancer
in front of it ends up being) and supply the bearer token via the managed
`exporterHeaders` payload. **[A]** Both the address and the header mechanism depend on
the network decision that has not been made yet — no endpoint is recorded in this repo
on purpose.

Do **not** point Copilot at the JSON ingest service in `main.py`. That endpoint
(`POST /v1/events`) speaks the CONTRACT.md envelope, not OTLP. They are two different
streams that meet in BigQuery, joined on `gen_ai.conversation.id` via `run.bound`.

---

## 5. Privacy — `captureContent` stays off

### 5.1 The vendor behaviour

**[E]** No prompt content, responses, or tool arguments are captured unless
`github.copilot.chat.otel.captureContent` / `COPILOT_OTEL_CAPTURE_CONTENT` is
explicitly enabled. The default is **off**.

**[E]** Enterprise-managed settings can enforce it off **and deny user override**.

### 5.2 The recorded decision

> **[D] DECISION.** `captureContent` is **off**, everywhere, permanently, and is
> enforced through the enterprise-managed layer with `userCanOverride: false`.
> Enabling it on any machine, for any duration, for any investigation, requires a
> written exception from the AI Platform Owner *and* the data-privacy owner.
>
> Rationale: CONTRACT.md §1.1 — "Never store content. No prompts, no responses, no
> source code, no diffs, no secrets, no error message bodies, no raw email addresses."
> Design §11.3 lists the same prohibition and names the alternative for every category.
> Turning `captureContent` on would put this platform in breach of its own contract on
> the first span.

This is recorded here because design §11.3 is explicit that keeping it off is
**"a design decision recorded here, not a default to be relied on silently."** A
default can change in a client release. A managed setting with `userCanOverride: false`
cannot change without someone deciding to change it.

### 5.3 Defence in depth — three independent layers

The decision is enforced at three places, on purpose, because any one of them can be
misconfigured:

| Layer | Mechanism | What it survives |
|---|---|---|
| 1. Client | Managed `captureContent: false`, `userCanOverride: false` | A curious engineer |
| 2. **Collector** | `transform/drop_content` + `redaction/allowlist` in `otel_config.yaml` | An unmanaged laptop, a new client default, an attribute name we have never seen |
| 3. Warehouse | Allow-listed column set on `raw.otel_span` | A collector misconfiguration |

Layer 2 is the one this repository owns end to end. `redaction/allowlist` runs with
`allow_all_keys: false`, so it is a **key allow-list**: an attribute Copilot invents in
a future release is dropped until someone adds it deliberately. That is the same
allow-list-not-deny-list posture `main.py` applies to the JSON stream.

**Audit procedure (run quarterly):** set `protocol: console` on one machine, run one
agent invocation, and read the emitted spans. If any attribute contains natural
language, escalate immediately. Record the result against the quarterly access review
in design §11.4.

---

## 6. Verification procedure

The goal is a specific, falsifiable claim: **`gen_ai.usage.*` is populated for both
models this repository's agents declare.**

| Agent file | Declared model |
|---|---|
| `agents/development/developer.implementer.agent.md` | `GPT-5.3-Codex` |
| `agents/qualdev/test-executor-committer.agent.md` | `Claude Sonnet 4.6` |

**[A] This is the open item design §4.4a flags as "to validate": whether the org's
Copilot plan and client versions expose enterprise-managed settings, and whether
`gen_ai.usage.*` is populated for these two specific models.** It is the *first* POC
step. Until it passes, token cost stays `cost_basis = 'modelled'` per design §8.2 and
the "measured cost" claim is not available.

### Step 1 — Console-only smoke test (no network)

1. On one machine, set `github.copilot.chat.otel.enabled: true` and
   `protocol: console`. Leave `captureContent` at its default.
2. Open Copilot Chat and invoke the implementer agent on a throwaway task.
3. In the extension's output channel, confirm you see an `invoke_agent` span with at
   least one nested `chat` span.

**Pass:** spans appear. **Fail:** nothing appears → the client version does not support
the export, or the setting id has changed. Stop and check the client version before
touching the network path.

### Step 2 — Attribute presence, per model

For each of the two models, capture one `chat` span and check:

```
gen_ai.operation.name          == "chat"
gen_ai.request.model           == "GPT-5.3-Codex"   (then "Claude Sonnet 4.6")
gen_ai.response.model          present
gen_ai.conversation.id         present and stable across the whole session
gen_ai.usage.input_tokens      present, integer, > 0
gen_ai.usage.output_tokens     present, integer, > 0
cached-input token counter     present?   -> RECORD THE EXACT KEY NAME
reasoning token counter        present?   -> RECORD THE EXACT KEY NAME
gen_ai.usage.total_tokens      ABSENT     -> expected; confirms design §4.4a
```

Record the result per model in a table like this and attach it to the POC ticket:

| Attribute | GPT-5.3-Codex | Claude Sonnet 4.6 |
|---|---|---|
| `gen_ai.usage.input_tokens` | ☐ present ☐ absent | ☐ present ☐ absent |
| `gen_ai.usage.output_tokens` | ☐ present ☐ absent | ☐ present ☐ absent |
| cached-input counter (exact key) | ______________ | ______________ |
| reasoning counter (exact key) | ______________ | ______________ |
| `gen_ai.usage.total_tokens` | ☐ absent (expected) | ☐ absent (expected) |

**Why per model matters:** the two models are served by different providers behind the
same Copilot surface. **[A]** A provider that does not return a usage block leaves the
attributes empty, and there is no reason to assume both behave identically. If one
model reports and the other does not, that model falls back to the §8.2 estimation
model with `cost_basis = 'modelled'`, and every dashboard must say so rather than
silently mixing measured and modelled cost.

### Step 3 — End to end, through the collector

1. Switch `protocol` back to `otlp-http` and set the endpoint to the collector.
2. Run one agent invocation.
3. Collector-side, with `LOG_LEVEL=debug` and the `debug` exporter enabled, confirm
   the span count is non-zero. **The `debug` exporter runs at `verbosity: basic` — it
   prints counts, never attribute values. Do not raise it to `detailed` on a machine
   handling real traffic.**
4. In BigQuery:

```sql
-- Did the spans land, and is total_tokens synthesised rather than measured?
SELECT
  conversation_id,
  attributes['gen_ai.request.model']            AS request_model,
  attributes['gen_ai.usage.input_tokens']       AS input_tokens,
  attributes['gen_ai.usage.output_tokens']      AS output_tokens,
  attributes['gen_ai.usage.total_tokens']       AS total_tokens,
  attributes['aiep.total_tokens_basis']         AS total_basis
FROM `raw.otel_span`
WHERE DATE(start_time) = CURRENT_DATE()
  AND attributes['gen_ai.operation.name'] = 'chat'
ORDER BY start_time DESC
LIMIT 20;
```

**Pass:** `total_tokens = input_tokens + output_tokens` and
`total_basis = 'computed_input_plus_output'` on every row.

### Step 4 — Prove the correlation bridge

Copilot's stream alone proves nothing about delivery. Run one agent invocation that
also produces a `run.bound` event, then:

```sql
-- One conversation, joined to one Jira ticket. This is the POC's real exit criterion.
SELECT
  s.conversation_id,
  e.attributes.jira_issue_key,
  COUNT(*)                                                   AS spans,
  SUM(CAST(s.attributes['gen_ai.usage.input_tokens']  AS INT64)) AS input_tokens,
  SUM(CAST(s.attributes['gen_ai.usage.output_tokens'] AS INT64)) AS output_tokens
FROM `raw.otel_span` s
JOIN `raw.ai_run_event` e
  ON e.event_type = 'run.bound'
 AND e.attributes.otel_conversation_id = s.conversation_id
WHERE DATE(s.start_time) = CURRENT_DATE()
GROUP BY 1, 2;
```

**Pass:** at least one row with a non-null `jira_issue_key`. **[E/A]** The OTel half of
this join is vendor-documented; the `run.bound` half is entirely ours and is the thing
design §6.5 calls "the core of the POC".

### Step 5 — Privacy regression check

Run the last three steps again with a prompt containing an obvious marker string, then:

```sql
SELECT COUNT(*) AS leaks
FROM `raw.otel_span`
WHERE DATE(start_time) = CURRENT_DATE()
  AND TO_JSON_STRING(attributes) LIKE '%<your-marker-string>%';
```

**Pass is `leaks = 0` and nothing else.** Any non-zero result is a P1: stop the rollout,
disable the export, and fix layer 2 before re-enabling.

---

## 7. Rollout order

1. **One machine, console protocol.** Prove the client emits (§6 steps 1–2).
2. **One machine, through the collector.** Prove the pipeline lands rows (§6 step 3).
3. **Prove the bridge.** `run.bound` joins the two streams (§6 step 4).
4. **Privacy regression.** §6 step 5, before any wider rollout.
5. **Managed rollout to a pilot team**, `captureContent` enforced off, with the
   engineers told what is collected and why (design §11.5).
6. **Org-wide managed rollout.**

Do not skip to step 5. **[A]** Whether the org's Copilot plan exposes the managed layer
at all is unconfirmed; if it does not, the whole rollout falls back to the environment
layer, coverage becomes voluntary, and the emission-bypass risk (design R2) returns.
Find that out at step 1, not at step 6.

---

## 8. Vendor-documented vs assumed — the summary table

| # | Statement | Tag |
|---|---|---|
| 1 | Copilot Chat and Copilot CLI ship a native OTLP exporter using GenAI semantic conventions | **[E]** |
| 2 | Span hierarchy `invoke_agent` → `chat` → `execute_tool` → `execute_hook` | **[E]** |
| 3 | `gen_ai.usage.input_tokens` / `output_tokens` are emitted, plus cached-input and reasoning counters | **[E]** |
| 4 | **`gen_ai.usage.total_tokens` is NOT emitted**; total must be computed | **[E]** |
| 5 | Identity attributes `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.agent.name`, `gen_ai.conversation.id`, `gen_ai.operation.name`, `gen_ai.tool.name` | **[E]** |
| 6 | `gen_ai.operation.name` ∈ {`invoke_agent`, `chat`, `execute_tool`, `embeddings`} | **[E]** |
| 7 | `github.copilot.chat.otel.enabled` defaults to `false`; `otlpEndpoint` sets the target | **[E]** |
| 8 | `COPILOT_OTEL_ENABLED` and `OTEL_EXPORTER_OTLP_ENDPOINT` configure it from the environment | **[E]** |
| 9 | Managed settings arrive via MDM (Windows Registry / macOS managed preferences), server-managed settings, or `managed-settings.json` | **[E]** |
| 10 | **A managed value always wins over env vars and user settings** | **[E]** |
| 11 | Protocols: `otlp-http` (default), `otlp-grpc`, `console`, `file` | **[E]** |
| 12 | `captureContent` defaults to OFF; nothing content-bearing is exported unless it is explicitly enabled | **[E]** |
| 13 | Managed settings can enforce `captureContent` off and deny user override | **[E]** |
| 14 | Managed exporter headers apply only to the extension's exporter and are never passed via env vars | **[E]** |
| 15 | OTel carries no `jira_issue_key` / `pr_id` / `commit_sha`; correlation stays ours | **[E]** |
| 16 | `captureContent` stays off permanently, managed, no user override | **[D]** |
| 17 | `otlp-http` is the production protocol; `console` for verification | **[D]** |
| 18 | Total = input + output, with cached and reasoning tokens NOT re-added (already counted inside input/output respectively) | **[D]** — an accounting convention, chosen and documented; validate against real invoices |
| 19 | **This org's Copilot plan and client versions expose the enterprise-managed layer** | **[A]** — design §4.4a explicitly flags this to validate |
| 20 | **`gen_ai.usage.*` is populated for `GPT-5.3-Codex`** | **[A]** — §6 step 2 |
| 21 | **`gen_ai.usage.*` is populated for `Claude Sonnet 4.6`** | **[A]** — §6 step 2, separately from #20 |
| 22 | Exact attribute keys for the cached-input and reasoning counters | **[A]** — both spellings accepted until confirmed |
| 23 | The `otlpEndpoint` URL grammar (base URL vs `/v1/traces` suffix) | **[A]** |
| 24 | `OTEL_EXPORTER_OTLP_HEADERS` / `_PROTOCOL` / `OTEL_RESOURCE_ATTRIBUTES` are honoured | **[A]** — conventional for OTLP, not documented for this exporter |
| 25 | The literal JSON schema of `managed-settings.json` (`value` / `userCanOverride`, `protocol`, `exporterHeaders` ids) | **[A]** |
| 26 | Both models behave identically with respect to usage reporting | **[A]** — explicitly do not assume; test each |
| 27 | Metrics (not just spans) pass through the same content-stripping chain in our collector | **[D]** — our configuration, not vendor behaviour |

**Sources for every [E] row** (external, verified 2026-08-19, recorded in design §4.4a):
VS Code "Monitor agent usage with OpenTelemetry" docs; GitHub Changelog 2026-07-08
"Enterprise-managed OpenTelemetry export for VS Code and CLI"; GitHub Changelog
2026-07-27 (JetBrains OTel configuration); SigNoz "GitHub Copilot OpenTelemetry
Monitoring"; Amazon CloudWatch "Set up OpenTelemetry for GitHub Copilot".
