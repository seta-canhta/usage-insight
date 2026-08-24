# AI-telemetry backfill pollers

Three pollers that read the systems of record — Bitbucket, Jira, CI — and emit
telemetry events conforming to [`../schema/CONTRACT.md`](../schema/CONTRACT.md) v1.0.0.

| File | Emits (CONTRACT.md §3) | Cadence (design §7.11) |
|---|---|---|
| `poll_bitbucket.py` | 16 `scm.pr.reviewed`, 17 `scm.pr.merged`, 18 `scm.pr.declined`, 19 `scm.revert` | **hourly** |
| `poll_jira.py` | 21 `jira.transition` (+ issue snapshot) | **daily, 02:00** |
| `poll_ci.py` | 20 `ci.pipeline.completed` | **daily, 02:00** — *conditional on OQ-3* |
| `common.py` | shared config, HTTP, envelope, NDJSON, watermarks | — |

They are read-only: no poller writes to Bitbucket or Jira.

---

## 1. Configuration

Environment variables, reusing the names this repo already uses
(`skills/bitbucket-ops/commands.md`, `skills/qualdev/jira-attach/SKILL.md`). A
`.env` file found in any parent directory is read as a fallback; **real
environment variables always win**, and values are never logged.

| Variable | Required for | Notes |
|---|---|---|
| `BITBUCKET_USERNAME` | `poll_bitbucket`, `poll_ci` | Bitbucket **username**, not the email |
| `BITBUCKET_ACCESS_TOKEN` | `poll_bitbucket`, `poll_ci` | App password / access token |
| `JIRA_URL` | `poll_jira` | `https://your-org.atlassian.net` |
| `JIRA_USERNAME` | `poll_jira` | Atlassian account email |
| `JIRA_API_TOKEN` | `poll_jira` | Jira API token |
| `AIEP_TELEMETRY_SALT` | all (optional but strongly recommended) | Per-deployment salt for `person_email_hash`. **Without it no hash is emitted at all** — an unsalted SHA-256 of a corporate address is trivially reversible, so the field is left `null` rather than pseudo-anonymous |
| `AIEP_POLLER_STATE` | all (optional) | Watermark file path. Default `~/.aiep/telemetry/poller-state.json` |
| `AIEP_HTTP_TIMEOUT` | all (optional) | Seconds, default `30` |
| `AIEP_HTTP_MAX_RETRIES` | all (optional) | Default `5` |

**Auth is HTTP Basic everywhere.** `skills/bitbucket-ops/commands.md` is explicit
that Bearer returns 401 with these credentials; Jira REST v3 uses the same Basic
pattern. Do not "modernise" this to Bearer.

### Dependencies

Standard library only. `requests` is used if it happens to be importable and
`urllib` otherwise — detected lazily at first use, in the same style as
`skills/bigquery/bq_tool.py`. Python 3.8+.

---

## 2. Running them

```bash
# Bitbucket — PR review/merge/decline + reverts, incremental since the watermark
python3 poll_bitbucket.py --workspace acme --repo watchtower --out /var/telemetry/bb.ndjson

# ...a specific window, ignoring the watermark
python3 poll_bitbucket.py --workspace acme --repo watchtower \
  --since 2026-07-01T00:00:00Z --state MERGED,DECLINED --no-watermark

# Jira — status transitions + issue snapshots for one project
python3 poll_jira.py --project PRJ --out /var/telemetry/jira.ndjson \
  --delivery-project QD          # QD issues are QualDev delivery tickets (AR-3)

# CI — ANSWER OQ-3 FIRST. This is the deliverable, not the poll.
python3 poll_ci.py --workspace acme --repo watchtower --probe --out oq3-report.md

# CI — only once the probe says Pipelines is real
python3 poll_ci.py --workspace acme --repo watchtower --out /var/telemetry/ci.ndjson
```

Output is newline-delimited JSON, one event per line, to `--out` or stdout.
Diagnostics (counts, HTTP statuses, hints) go to **stderr** as single-line JSON,
so `--out -`-style piping stays clean.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. Watermark advanced. |
| `2` | Configuration error (missing env vars, bad `--state`). Nothing ran. |
| `3` | API error. **No watermark advance, no partial output file.** Safe to re-run. |
| `4` | *(poll_ci only)* CI not available — 403/404 on pipelines. This is an **answer to OQ-3**, not a crash. Re-run with `--probe`. |
| `130` | Interrupted. Watermark not advanced. |

### Useful flags

* `--no-pr-commits` (Bitbucket) — skips one call per PR, but loses the `AI-Run-Id`
  trailers, so every link degrades from `explicit` to `marker_only` and the PRs
  drop out of cost-per-output metrics. Only for large ad-hoc backfills.
* `--no-reverts` (Bitbucket) — skips the commit scan.
* `--no-test-reports` (CI) — much cheaper; `tests_*` become `null`.
* `--search-api {auto,jql,legacy}` (Jira) — Jira Cloud is mid-migration between
  `POST /rest/api/3/search/jql` and `GET /rest/api/3/search`. `auto` tries the
  new one and falls back.
* `--jql` (Jira) — full JQL override; `--project` still names the watermark.

---

## 3. Scheduling

Per design §7.11:

```cron
# Hourly — PR throughput, review lead time, comment counts
17 * * * *  cd /opt/aiep/pollers && python3 poll_bitbucket.py --workspace acme --repo watchtower --out /var/telemetry/bb-$(date +\%Y\%m\%dT\%H).ndjson

# Daily 02:00 — Jira changelog, then CI (only if OQ-3 resolved positively)
0 2 * * *   cd /opt/aiep/pollers && python3 poll_jira.py --project PRJ --out /var/telemetry/jira-$(date +\%Y\%m\%d).ndjson
10 2 * * *  cd /opt/aiep/pollers && python3 poll_ci.py  --workspace acme --repo watchtower --out /var/telemetry/ci-$(date +\%Y\%m\%d).ndjson
```

Why these cadences: PR review dynamics are the fastest-moving signal that feeds a
team-level view, so hourly. Jira changelogs and CI feed the weekly view and are
reconciled overnight with the acceptance-state job, so daily. Nothing here is
event-driven — that is the emitter's job, not a poller's.

**Reconciliation lag is expected.** An output's acceptance state is not final
until its PR merges or is declined, typically days later. Re-polling the same
window is safe and is how late-arriving facts get picked up (see §4).

Run one poller per repo/project. They share the watermark file safely as long as
two instances of *the same* repo/project are not run concurrently.

---

## 4. The watermark / incremental model

Each poller keeps a **last successful poll timestamp per source** in a JSON file
(`AIEP_POLLER_STATE`, default `~/.aiep/telemetry/poller-state.json`):

```json
{
  "version": 1,
  "watermarks": {
    "bitbucket:pullrequests:acme/watchtower": {
      "last_success_at": "2026-08-19T14:00:00.000Z", "updated_at": "...", "runs": 41
    },
    "bitbucket:commits:acme/watchtower":  { "last_success_at": "..." },
    "bitbucket:pipelines:acme/watchtower": { "last_success_at": "..." },
    "jira:issues:PRJ":                      { "last_success_at": "..." }
  }
}
```

Rules:

1. **The watermark advances only on a fully successful run.** It is written
   inside a `checkpoint()` context manager whose commit step runs only when the
   block exits without an exception. Any HTTP failure, any crash, any Ctrl-C
   leaves the previous value in place and the next run re-reads the same window.
2. **It never moves backwards**, and never to a null.
3. **Re-reading is safe** because `event_id` is deterministic — a SHA-256 of the
   fact's natural key (repo + PR id + terminal state + timestamp, and so on),
   rendered as `evt_<32 hex>`. Re-polling the same PR produces byte-identical
   ids, and CONTRACT.md §1 rule 3 makes `event_id` the dedup key. Nothing
   downstream double-counts.
4. **Output files are atomic.** `--out` is written to a temp file and renamed on
   clean close, so a failed run never leaves a half-written batch that looks
   complete.
5. **First run has no watermark**, so it uses `--lookback-days` (default 30).
   For a full historical backfill, pass an explicit `--since` with
   `--no-watermark`.

The Bitbucket poller keeps *two* watermarks per repo (pull requests and commits)
because the two streams page differently: PRs are filtered server-side on
`updated_on`, commits are filtered client-side on the way down the newest-first
stream.

---

## 5. Contract notes — read before changing anything

**`run_id` is `null` unless a commit carries an `AI-Run-Id` trailer.** A poller
observes facts that exist whether or not an agent was involved. Fabricating a
`run_id` would manufacture a join key and breach AR-1 (one output, one run). It
is populated only from the git trailer of CONTRACT.md §9 — which is also the only
thing that earns `link.method = 'explicit'`, and therefore the only poller data
admissible to cost-per-output metrics.

**`trace_id` is either the `AI-Trace-Id` trailer or a deterministic synthetic id**
(`trc_<sha256 of source+entity>`), which groups every event about one PR / issue /
pipeline without pretending an agent trace existed. Synthetic traces are always
accompanied by `run_id = null` and `link.method != 'explicit'`.

**`ingested_at` is always `null`.** The collector sets it. A client never does.

**Link confidence ladder** (design §5.3):

| Evidence | `link.method` | `confidence` |
|---|---|---|
| `AI-Run-Id` trailer on a commit | `explicit` | 1.0 |
| `AUTH_BY_COPILOT` / `GEN_BY_COPILOT` / `[Authored By Copilot]` marker, no trailer | `marker_only` | 0.3 |
| Jira key parsed from branch/title only | `heuristic` | 0.5 |
| Nothing | `heuristic` | 0.0 |

**No `jira.issue.snapshot` event exists.** CONTRACT.md §3 is a closed enum and
the collector rejects unknown types, so the issue snapshot rides on
`jira.transition` events as `attributes.issue`. Every issue produces at least one
transition because issue creation is synthesised as the transition into its first
status (Jira's changelog never records it), flagged
`attributes.is_synthesised_creation`.

**Redaction is two layers, and they are not the same shape** (CONTRACT.md §3):

1. *Key names* — **exact match** on the lowercased key, walking nested dicts and
   lists. Never substring: substring matching would reject `output_content_hash`
   ("content"), `input_tokens` / `output_tokens` / `cached_input_tokens`
   ("token") and `error_class` ("error"), silently dropping the two most
   important event types in the system. There is a regression test for exactly
   this.
2. *String values* — regex screening for secrets (`password\s*=`, `api[_-]?key`,
   `BEGIN … PRIVATE KEY`, `AKIA…`, Atlassian `ATATT…`, GitHub `gh[pousr]_…`) and
   for anything shaped like a raw email address. Rejections name the field and
   the check that fired, **never the value**.

Both run on every event before it is written. A violation raises rather than
emitting.

---

## 6. AI marker detection — the narrow one

Detection is by **name**, from the two closed sets in CONTRACT.md §3.1 — never
by shape. It matches **only**:

* `AUTH_BY_COPILOT` or `GEN_BY_COPILOT` anywhere on the **commit subject line** —
  all four real placements occur in this org's history and all four match:
  `AUTH_BY_COPILOT: …`, `[GEN_BY_COPILOT] [PRJ-6383] …`,
  `[PRJ-6383] [AUTH_BY_COPILOT] : …`, `… GEN_BY_COPILOT`;
* `[Authored By Copilot]` in a **PR title** (mandated by
  `skills/bitbucket-ops/commands.md` §9);
* the `AI-Run-Id:` **git trailer**;
* the Jira labels `AUTH_BY_COPILOT` / `PLANNED_BY_COPILOT` / `GEN_BY_COPILOT` /
  `REVIEW_BY_COPILOT`.

`PLANNED_BY_COPILOT` and `REVIEW_BY_COPILOT` are **labels only** — no agent
writes them onto a commit subject, so they are deliberately absent from the
commit regex and a commit saying `feat: PLANNED_BY_COPILOT label handling` stays
a human commit. `GEN_BY_COPILOT` is the reverse: a commit marker with no Jira
label writer today. Omitting it from the commit regex is why a 60-day pull of
`acme/qa-automation` reported `ai_commit_count = 0` across all 102 PRs.

> **Do not widen this to a pattern.** The `aiep-impact-report-generator` skill on
> `origin/feature/ATSP-22288` ends its keyword pattern with
> `…|^feat[\(:]|^feat\b|^fix[\(:]|^fix\b|^chore[\(:]|^chore\b`. Conventional
> Commits are mandated repo-wide by `.github/copilot-instructions.md` §5, so that
> clause classifies **every conventionally-formatted human commit as
> AI-authored** — design §3.5 measured 43 genuinely marked commits against 354
> total. `feat(api): add endpoint` is a human commit, and there is a test
> asserting it stays that way. Adding a marker means adding a **name** to
> `AI_COMMIT_MARKERS` / `AI_LABELS` in `common.py`; the boundary guards
> (`(?<![A-Za-z0-9_])…(?![A-Za-z0-9_])`) never come off.

### Marker drift

A Jira label shaped like a provenance marker but outside the closed set —
measured live: `PLANNER_BY_COPILOT`, `DEV_BY_COPILOT`, `COPILOT_TESTING` — is a
data-quality finding, not a silent drop. `poll_jira.py` emits the names in
`attributes.attribution.unrecognised_ai_labels` and sets
`attributes.attribution.has_ai_label_drift`. They are **not** counted as AI
(counting a shape is the mistake above), but each one subtracts from the AI
figure until it is reconciled, so `people_workbook.py` and `combined_weekly.py`
both report the count and the workbook lists the names per issue.

---

## 7. The qd_jira_key attribution hazard (AR-3)

`supervisor-test-spec.agent.md:1518` sends *all* delivery comments and label
updates to a separate QualDev delivery ticket (`qd_jira_key`) rather than the
feature ticket. **The ticket wearing the AI label is often not the ticket
describing the work.** A naive rollup either double-counts (one run marks two
tickets) or mis-attributes (credits the QA tracking ticket's assignee).

`poll_jira.py` does not resolve this — CONTRACT.md §6 puts attribution rules in
the transform layer, not in convention. It emits the evidence:

```jsonc
"attribution": {
  "rule": "AR-3",
  "ai_labels": ["AUTH_BY_COPILOT", "PLANNED_BY_COPILOT"],
  "label_authored_by_ai": true,
  "label_planned_by_ai": true,
  "label_generated_by_ai": false,
  "label_reviewed_by_ai": false,
  "unrecognised_ai_labels": ["DEV_BY_COPILOT"],
  "has_ai_label_drift": true,
  "is_delivery_ticket_candidate": true,
  "delivery_ticket_key": "QD-12",
  "feature_ticket_key": "PRJ-6383",
  "feature_ticket_source": "issue_link:Relates",
  "resolution_confidence": 0.7,
  "linked_issues": [ { "issue_key": "PRJ-6383", "link_type": "Relates", "direction": "outward" } ],
  "parent_key": null
}
```

A ticket is flagged as a delivery candidate when it carries AI labels **and** has
a resolvable link to another ticket **and** looks like a delivery ticket (its
project is in `--delivery-project`, or its issue type is in
`--delivery-issue-type`, or the link crosses projects). Cross-project links score
highest. The transform makes the final call and keeps `delivery_ticket_key`
separately, per AR-3.

---

## 8. Identity

`person_id` is the Atlassian `accountId` and nothing else. Display names,
nicknames and email addresses are **never** emitted. Design §9.4 measured the
consequences on this repository — `"Ann Lee"` vs `"Lee, Ann"` (same
address, 17 commits vs 1), `"Bob Smith"` vs `"Bob Smtih"`, a committer
with no address at all — so a name-keyed rollup splits one engineer across three
rows and produces at least one row that cannot be joined to Jira.

Bitbucket user objects carry `account_id`, which *is* the Atlassian id, so PR
authors and reviewers join to Jira directly. Git commit authors only expose a raw
`Name <email>` string; the address is used to compute
`sha256(salt + lower(email))` and is then discarded. Display names are read
locally for bot detection and for nothing else.

`team_id` and `role` are `null` — there is no directory (OQ-6).

---

## 9. Testing

```bash
python3 -m unittest discover -s pollers/tests -v
```

117 tests, no network. Every HTTP interaction is served by a fake transport over
synthetic payloads shaped like real Bitbucket v2.0 / Jira v3 responses. The
suite covers, among other things: `first_review_at` excluding the PR author's own
comments and bot noise; all four `AUTH_BY_COPILOT` / `GEN_BY_COPILOT` placements
matching while conventional commits and the label-only markers do not; marker
drift being surfaced rather than dropped; revert resolution and `days_to_revert`; pagination
followed to completion on four different collections; the watermark advancing
only on full success; the exact-match redaction guard letting `input_tokens` and
`output_content_hash` through; and the OQ-3 probe making exactly one call per
endpoint.

---

## 10. UNVERIFIED endpoints

Nothing here has been exercised against this organisation's live API. These are
the specific dependencies to validate during the POC (design §12):

**Bitbucket — high confidence, never exercised by any skill in this repo**
(design §4.2 marks approvals/comments retrievability as **[A]**):

| Endpoint | Risk if unavailable |
|---|---|
| `GET /2.0/repositories/{ws}/{repo}/pullrequests/{id}/activity` | **No `first_review_at`, no review lead time, no approver identity.** The single richest source; nothing replaces it |
| `GET …/pullrequests/{id}/comments` | No review-comment counts (the primary rework/quality proxy) |
| `GET …/pullrequests/{id}/diffstat` | No PR size / churn |
| `GET …/pullrequests/{id}/commits` | No AI trailers ⇒ every link degrades to `marker_only` ⇒ no cost-per-output |
| `GET …/pullrequests?q=updated_on >= "…"` | Server-side date filtering unconfirmed; if `q` is rejected the poller must page the whole history |
| `GET …/commits` | No revert detection |

**Jira — changelog expansion is marked [A] "validate" in design §4.1:**

| Endpoint | Risk if unavailable |
|---|---|
| `POST /rest/api/3/search/jql` **or** `GET /rest/api/3/search` with `expand=changelog` | **No transitions at all** — no cycle time, no WIP, no blocked time. Both variants implemented; which one this instance serves is unknown |
| `GET /rest/api/3/issue/{key}/changelog` | Long-lived tickets silently lose their earliest transitions |
| `GET /rest/api/3/status` | `status_category` falls back to a hard-coded name map and returns `null` for custom statuses |
| Story-point custom field | **Not implemented** — the field id is unknown per project (design §4.1 **[A]**) |

**CI — the whole integration is unconfirmed (OQ-3):**

Every endpoint in `poll_ci.py` is an inference from Bitbucket Cloud being the
SCM. There is no pipeline file in this repo and no skill or agent calls a CI API.
`pipelines/`, `pipelines/{uuid}/steps/`, `steps/{uuid}/test_reports` — none
verified; the test-report payload shape in particular is thinly documented and is
parsed defensively. `coverage_pct` is **always `null`**: no coverage publishing
exists anywhere in this org (design §4.3 **[G]**), and a `0` would read as "0%
covered".

**Run `poll_ci.py --probe` and paste its report into the spike doc. That report
is the answer to OQ-3.** It also probes `commit/{sha}/statuses`, which is the one
endpoint that reveals a *non*-Bitbucket CI — Jenkins, GitHub Actions and
TeamCity all post build statuses there.

Until OQ-3 is answered, the interim pre-merge signal is the local quality-gate
output that already exists (design §4.3 **[V]**): `.tmp/quality-gates-*.log` and
`.tmp/test-results/cucumber-report.json`.

---

## `poll_aio.py` — AIO TCMS test execution

Emits CONTRACT.md §3 event **22** (`test.run.completed`): one event per test case
per cycle, carrying the latest run's status, who executed it, whether it was
automated, and how many Jira defects it raised.

```bash
export AIO_API_TOKEN=...            # NOT the Jira token — see below
python3 poll_aio.py --project PRJ --probe        # reachability, one call
python3 poll_aio.py --project PRJ --since 2026-06-21T00:00:00Z --no-watermark \
        --out events.ndjson
```

| Flag | Meaning |
|---|---|
| `--include-not-run` | Emit never-executed rows too. Off by default |
| `--max-cycles N` | Stop after N cycles |
| `--progress-every N` | Progress line every N in-window cycles |
| `--pace SECONDS` | Sleep between requests. AIO 429s readily; a full inventory pass needs ~0.3–1.0 |
| `--max-retries N` | Defaults to 8, above the shared client's 5, for the same reason |

### Coverage mode

```bash
# Business as usual: P1+P2 across the cycles run in the window
python3 poll_aio.py --project PRJ --coverage --since 2026-06-21T00:00:00Z \
    --no-watermark --priority High --priority Medium --pace 0.3 \
    --out coverage.ndjson

# A release under test: P1+P2+P3, every cycle belonging to it
python3 poll_aio.py --project PRJ --coverage --release 26.8 --release-id 28771 \
    --pace 0.3 --out coverage-26.8.ndjson
```

| Flag | Meaning |
|---|---|
| `--coverage` | Emit `test.case.snapshot` instead of test runs |
| `--coverage-scope` | `cycles` (default) or `project` |
| `--priority NAME` | Repeatable. On this project **P1 = High, P2 = Medium, P3 = Low** — AIO uses its own scale and has no P1/P2 field |
| `--release TEXT` | Repeatable. Cycle **title** contains this |
| `--release-id ID` | Repeatable. Cycle carries this Jira release id |

**Why coverage needs a scope.** The project inventory is 10,515 test cases and
includes everything ever written, most of it retired or belonging to an old
release. A percentage over that answers a question about the archive. Scoped to
the cycles actually in flight, it answers "of what we are testing now, how much
is automated" — which is the question the metric is for.

**A release ignores `--since`.** A release's own test evidence can predate the
reporting window; intersecting the two would silently drop it.

**Title matches are reported separately from release-id matches.** The id is a
structured field an administrator set; the title is free text an engineer typed.
On this project two "26.8 Dev Integration Testing" cycles carry no release id, so
the title fallback is needed to see them — but a figure that depends on somebody's
spelling has to say so, and the run log names every cycle admitted that way.

### AIO does not accept the Jira API token

It issues its own key and returns `401 Invalid or missing API Token` for the Jira
credential — hence `AIO_API_TOKEN` rather than a reuse of `JIRA_API_TOKEN`. Get
one from **AIO Tests → API Keys**.

The app is also enabled **per Jira project**. A project without it returns 401
with a *different* body: "The app is not enabled for this project". `--probe`
tells the two apart, because the fixes are unrelated — one needs a new token, the
other needs a Jira administrator.

### Not-run is not failed, and it is not activity either

AIO seeds a cycle with one row per test case at status **Not Run** the moment the
cycle is created. On the first real run, 8,078 of 25,125 rows were seeded rows
that nobody had executed. Emitting them by default would:

* turn cycle *planning* into apparent test *activity*, and
* put a huge denominator under a pass rate nobody earned.

So they are skipped unless `--include-not-run` is passed, and when they are
included they carry `status_category = 'not_run'` with a **NULL** `executed_at`
and a **NULL** `executed_by_person_id` — AIO leaves a stale `executedByID` on
seeded rows, and trusting it would credit an execution that never happened.

### Cost of a run

The cycle-testcase list endpoint inlines `latestRun`, so a cycle of 200 cases
costs 2 requests, not 200. A 60-day window over 31 in-window cycles was 273
requests and about six minutes.

### Timestamps

AIO mixes epoch **seconds** (`testcycle.createdDate`) and epoch **milliseconds**
(`testrun.updatedDate`) across endpoints in the same API. Reading milliseconds as
seconds gives the year 57490 — which formats and sorts perfectly well, and is
wrong. `epoch_to_rfc3339()` infers the unit from magnitude.

### Coverage divides by known status only

About 42% of this estate has never had `automationStatus` set. Those cases are
emitted with a NULL status and **excluded from the rate** — an unset field is not
"not automated", and folding them in would measure how diligently the field is
filled in. Both the poller log and the report state the denominator, and flag the
figure when the unknowns outnumber the knowns.

### What it deliberately does not carry

Test case **titles**. They are free text and routinely quote customer names,
endpoints and device identifiers, which is exactly the class §11.3 keeps out of
the stream. The event carries `test_case_key`, `folder_name` (a controlled
taxonomy) and `priority` instead.
