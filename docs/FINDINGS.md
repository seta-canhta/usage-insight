# Findings — measured baseline

**Measured 2026-08-24.** Scope: Jira `PRJ`, Bitbucket `acme/qa-automation`,
AIO TCMS project `PRJ`, 60-day window (2026-06-21 → 2026-08-24).
23,427 events read.

Every number here came from a live pull, not from a design document. Where a number
contradicts something written earlier in the design spike, this
file wins — the spike was written before the data existed.

---

## 1. Volume actually collected

| Source | Events | Notes |
|---|---:|---|
| Jira transitions | 2,014 | 427 distinct issues |
| Bitbucket | 118 | 98 merged, 7 reviewed, 4 declined, 9 reverts |
| AIO test runs | 17,047 | 25 cycles |
| AIO case inventory | 4,248 | |
| CI | **0** | CI is self-hosted Jenkins; the Bitbucket status path returns nothing |

---

## 2. The correlation layer was empty; it is not now

**Superseded 2026-08-26** — see "Where the join breaks", below. The four joins were built and
measured: `scm.pr.created` now carries commit SHAs (740 across 162 PRs), sessions
bind to a commit range at `explicit`/1.0, and the ticket link is allow-listed
against the 218 real Jira projects. What follows is the state that prompted that
work, kept because the fix only makes sense against it.

### The state as found

| Field | Present |
|---|---|
| `run_id` / `trace_id` on any emitted event | **0** |
| `~/.aiep/telemetry/` on the emitter author's own machine | **does not exist** |
| Copilot OTel `model.call` events | **0** — export never switched on |

`emit.py` is wired into **3 of 14** agent definitions and has never produced a single
event. Everything currently reported is derived from third-party systems after the
fact. No token count, no model id, no cost, no agent attribution exists today.

> **Appended 2026-08-26.** The third row of that table — *Copilot OTel `model.call`
> events: 0 — export never switched on* — is now a **retired path**, and the reason
> it read zero is the reason it was retired. The exporter had to be enabled per
> machine, and a machine where the setting never landed collected nothing while being
> indistinguishable from a machine having a quiet month.
>
> The replacement is Copilot CLI's own session journal,
> `~/.copilot/session-state/<id>/events.jsonl`, written whether or not anything is
> watching. Measured the same day on one real tree of 22 journals: **2,935 contract
> events**, and **7 git repositories discovered** with nothing registered by hand.
> Two of the 22 sessions ended without `session.shutdown` and so carry no usage
> totals — their tokens are **unknowable, not zero**.
>
> The first two rows of the table are unaffected: `emit.py` still has to emit, and
> `run.bound` naming a `copilot_session_id` is still the only thing that earns
> `link.method = 'explicit'`. What changed is that a zero measured here is now a
> zero somebody could have observed.

---

## 3. AI attribution — what works and what does not

### The marker map in the agent flow

| Marker | Form | Applied by | Issues in window | Poller reads it |
|---|---|---|---:|:--:|
| `PLANNED_BY_COPILOT` | Jira label | `architect.planner:65`, `supervisor step_1`, `test-spec-generator:41` | **58** | yes |
| `AUTH_BY_COPILOT` | Jira label **and** commit prefix | `developer.implementer` phase_5, `supervisor` step_3b, `test-executor-committer` step 6, `test-script-generator:52` | **55** | yes |
| `[GEN_BY_COPILOT]` | commit prefix **only** — no Jira label exists | `supervisor:958,1181`, `test-executor-committer:267,270` | not readable | **no — see §3.2** |
| `REVIEW_BY_COPILOT` | intended Jira label | **nothing applies it** | **0** | no |

Union of AI-labelled issues: **93 of 427 (21.8%)**, across **19 people**.
Adoption by month created: Apr 2, May 8, Jun 7, **Jul 54**, Aug 22.

### 3.1 SCM attribution is empty

```
ai_commit_count      0 / 102 PRs
ai_run_ids           0 / 102 PRs
ai_model_ids         none
PR title marker      3 / 109
```

### 3.2 Cause: the marker regex is incomplete (our bug)

`pollers/common.py` matches only `AUTH_BY_COPILOT`:

```python
AI_COMMIT_MARKER_RE = re.compile(
    r"(?<![A-Za-z0-9_])AUTH_BY_COPILOT(?![A-Za-z0-9_])", re.IGNORECASE
)
```

But the QA agents commit with `[GEN_BY_COPILOT]`
(`test-executor-committer.agent.md:267`). `tools/diagnostics/README.md:36` claims both are
matched; the code that runs does not. This is the most likely explanation for
`ai_commit_count = 0` in the QA repo, and it means the earlier conclusion
"no AI attribution exists in SCM" was drawn from a regex with a hole in it.

**Re-polled 2026-08-24 with the marker set corrected.** The hole was real, and closing
it recovered data — but not much:

| | AI commits found | PRs carrying a marker |
|---|---:|---:|
| `AUTH_BY_COPILOT` only | 0 | 4 |
| `AUTH_BY_COPILOT` + `GEN_BY_COPILOT` | **5** | **8** |

So §3.1 stands directionally: five AI-marked commits across 104 pull requests is still
close to no SCM attribution. The bug suppressed real data and is fixed; it does not
overturn the finding, and should not be reported as though it did.

### 3.3 Label drift has started

```
PLANNER_BY_COPILOT   1    (typo of PLANNED)
DEV_BY_COPILOT       1
COPILOT_TESTING      1
```

The poller matches a closed set, so these three issues are counted as non-AI. Drift
in hand-typed labels always biases the AI figure **downward** — the direction that
under-reports the thing being measured.

---

## 3.4 Copilot's OTel spans carry prompt content, and the setting to stop it does not work

Measured 2026-08-24 on copilot-chat **0.62.0**, during a real agent run:

| Attribute | Occurrences | Characters |
|---|---:|---:|
| `gen_ai.input.messages` | 6 | 11,203 |
| `copilot_chat.user_request` | 3 | 8,440 |
| `gen_ai.system_instructions` | 2 | 6,009 |
| `gen_ai.output.messages` | 6 | 3,724 |
| `gen_ai.tool.call.result` | 5 | 824 |

`tool.call.result` held real terminal output, so anything an agent runs lands in
the stream.

**This is a known, open upstream defect** — [microsoft/vscode#326254](https://github.com/microsoft/vscode/issues/326254),
*"the log and metric paths honor the setting; the span path does not"*, filed
against 0.57.0 and still reproducing on 0.62.0. Setting `captureContent: false`
does not fix it. `copilot-otel-setup.md` tags the false default **[E]
vendor-documented**; the default is documented correctly and the implementation
does not honour it, which is a worse failure than a wrong default because it
reads as safe.

Mitigations in place: `maxAttributeSizeChars: 256` truncates long attributes,
and `cli/otel_read.py` keeps 22 named fields and drops everything else before
storage. The upstream advice is to disable span capture entirely, which also
discards the cache-token detail only spans carry — so the content is dropped on
read instead.

> **Appended 2026-08-26 — this mitigation is no longer ours to run.** The span path
> was retired. `cli/otel_read.py`, `cli/otel_capture.py`, `collector/otel_config.yaml`
> and `collector/copilot-otel-setup.md` are deleted, and `cli/vscode_setup.py` now
> actively **removes** the `github.copilot.chat.otel.*` settings from any machine that
> still carries them — an exporter left switched on would go on writing prompts to a
> file nobody reads.
>
> The defect above is unchanged and still open upstream. What changed is that nothing
> here depends on it any more. Usage comes from
> `~/.copilot/session-state/<id>/events.jsonl`, which never leaves the machine on its
> own, so no redacting collector has to stand between a leak and the internet.
>
> That is a smaller exposure and a **larger** concentration of content: the whole
> conversation sits next to the numbers. `cli/copilot_read.py` therefore names the
> fields it keeps, by dotted path, and drops everything else without inspection —
> allow-list, not exclusion list, because an exclusion list is only as good as today's
> knowledge of a format that gains keys without asking. Verified by sweep on 22 real
> journals: **zero absolute paths and zero usernames** across 2,935 events.
>
> One thing the journal fixed that this section could not: `tool.call.status` was
> structurally NULL under spans, so the tool-failure count was always 0 **by
> construction, not by measurement**. The journal reports **2,000 ok / 62 error**.

### Also corrected here

`copilot-otel-setup.md` names the exporter setting `otel.protocol`. It is
`otel.exporterType`, and the file exporter needs `otel.outfile` to say where.
With those two set there is no listener and no daemon: Copilot writes JSON
lines itself.

---

## 4. P1/P2/P3 lives in Jira, not AIO

```
P1-Urgent            25
P2-Must Have Soon    60
P3-Must-Have        315
P4-Try For It         5
(legacy: Medium 13, High 7, Critical 2)
```

AIO's own scale is High / Medium / Low and is a **different axis**. Any earlier
coverage split by AIO priority answered a question nobody asked.

**AIO test cases carry no Jira key at all** — 0 of 4,248 cases and 0 of 17,047 runs.
Until that join exists, coverage cannot be cut by P1/P2.

---

## 5. Automation coverage is self-declared, not verifiable

```
automation_status = Automated    3,993 / 4,248
has_automation_key = False       4,248 / 4,248   (100%)
script_type = Classic            4,248 / 4,248   (100%)
automation_owner set               317 / 4,248   (7%)
```

Not one case carries a key pointing at a real script. The headline coverage figure
measures how diligently a dropdown is filled in, not whether automation exists.

Reconciled against the AIO dashboard for cycle `PRJ-CY-199` — exact match:
`3858 / 3946 = 97.8%`, buckets Automated 3858 · Not Assigned 66 · To Be Automated 15
· Manual 7. The number is faithfully reported; it is the *definition* that is weak.

---

## 6. Flaky rate is not measurable

```
case+cycle pairs with more than one run:   0 / 17,047
```

AIO stores one run per case per cycle and overwrites on re-execution. There is no
rerun history, so flakiness cannot be derived. Any cross-cycle comparison spans
different code baselines and is not flakiness — an earlier "2.6%" figure produced
this way is withdrawn.

Observed failure rate is `104 / 17,047 = 0.6%`, which is itself worth questioning.

---

## 7. Review happens in the Jira workflow, not in Bitbucket

Bitbucket review data is almost empty: 5 PRs have a first review, 4 approvals across
102 PRs. But the Jira workflow carries the real review chain:

```
Resolved - Need Review → In Review → Reviewed → Ready for QA → In QA → Fix Accepted
                      ↘ Reopened (28 transitions out of a done-ish status)
```

| | n | First-pass | Issues reopened |
|---|---:|---:|---:|
| **All** | 233 | **93.1%** | 46 |
| AI-labelled | 55 | 94.5% | 10 (18.2%) |
| non-AI | 178 | 92.7% | 36 (20.2%) |

This measures **human** review of the work. It does not measure AI review quality —
nothing does yet, because `REVIEW_BY_COPILOT` is never applied.

---

## 8. Cohort comparison — AI vs non-AI cycle time

Median days from issue created to resolved:

| Issue type | AI-labelled | non-AI | n (AI / non-AI) |
|---|---:|---:|---|
| Task | **7.3** | **25.0** | 42 / 99 |
| Bug | 1.3 | 1.8 | 20 / 90 |
| Story | 26.7 | 13.9 | 7 / 21 |

**This is a cohort comparison, not a controlled experiment.** People choose when to
reach for AI, and that choice is very likely correlated with how tractable the work
already was. Report it as an observed difference with the selection effect stated;
never as "AI made tasks 3.4× faster".

---

## 9. What the two example engineers looked like

Reported to establish that the pipeline works end to end, not as a performance review.
The two do different jobs — one's output lands in Jira and AIO, the other's in
Bitbucket. Read down the columns, not across them.

Detail lives in the generated report, not in git (it names individuals).

---

# Trail gaps

## The gap table, cheapest first

| # | Missing trail | Blocks | Trail to add | Who writes it | Marginal cost |
|---|---|---|---|---|---|
| G1 | Agent run — **0 events ever emitted** | which agent, which task, burn | wire `emit.py` into the 11 remaining agent definitions | us, once | **zero** |
| G2 | Commit ↔ AI run | separating AI output from human output | `AI-Run-Id:` git trailer written by the agent | the agent | **zero** |
| G3 | `GEN_BY_COPILOT` unreadable | QA-side AI attribution | add it to `AI_COMMIT_MARKER_RE`; add the matching Jira label to the QA agents | us + agent | **zero** (bug fix) |
| G4 | Token / cost | metrics 9, 10 | `COPILOT_OTEL_ENABLED=1` per developer | developer, once | **~10 min** |
| G5 | Daily report unstructured | metrics 1, 2, 5 | fix the Excel columns (see `importers/README.md`) | **already written daily** | **~zero** |
| G6 | AIO case ↔ Jira issue — 0/4,248 | P1/P2 split, metric 5 | Jira key on the AIO case | QA at case design | 5s/case + 4,248 backfill |
| G7 | AIO case ↔ script file — 0/4,248 | verifying metric 2 | automation key = repo path | QA when automating | 10s/case + backfill |
| G8 | `REVIEW_BY_COPILOT` never applied | AI review quality | the external review system must tag back | **outside our control** — see below |
| G9 | Test rerun history — 0 pairs | metric 8 flaky | publish each execution instead of overwriting | CI / AIO config | depends on CI |

G1–G5 are essentially free. G6–G7 are the ones that cost real human time. G8 depends
on a system we do not own.

> **Appended 2026-08-26 — G4 closed differently than costed.** The `COPILOT_OTEL_ENABLED=1`
> path in the row above was **retired**, not completed. It is replaced by Copilot CLI's
> own session journal at `~/.copilot/session-state/<id>/events.jsonl`, which is written
> whether or not anything is watching — so the trail costs the developer **zero**
> minutes rather than ten, and there is no setting whose absence makes a machine
> silently collect nothing. `insight copilot` reads it; `cli/copilot_read.py` takes named
> fields off it by allow-list.
>
> Two of this document's own rules landed harder than expected here. The design rule at
> the top — *a trail is only worth adding if it is a by-product of work already being
> done* — is exactly what the span exporter failed and the journal passes. And the new
> trail carries what G4 never promised: which agent ran, which skill, real tool
> success/failure, real gate verdicts, and **premium requests**, the unit Copilot bills.
>
> It also costs something the original row did not price: the journal covers the
> **Copilot CLI / agent surface only**. The VS Code Chat panel and inline completions
> write nothing to it, so they are now a trail gap of their own — unmeasured, which is
> not the same as unused, and every read names them.

---

## G8 — the review trail is owned by someone else

`REVIEW_BY_COPILOT` means: **the team triggers an AI code review in a different
system, and that system tags the ticket back when it finishes.**

Consequences:

1. We cannot instrument it. No agent applies it, and adding one would not help —
   the trigger is elsewhere.
2. We can only **consume** it, which makes the tag-back a hard dependency. If that
   system tags inconsistently, the metric silently under-reports and looks like
   "AI review is rarely used" rather than "the tag is unreliable".
3. Before anything is built on it, verify the tag-back actually fires. Today it has
   fired **zero** times in 427 issues, which is either no adoption or no tagging —
   and those two need different fixes.

**Open question — must be answered before Phase 3:** which system, who owns it, and
does it apply the label on every completed review or only on request?

What it unlocks once reliable: pairing `REVIEW_BY_COPILOT` against the Jira first-pass
and reopen rates already computed in `FINDINGS.md §7` — i.e. do AI-reviewed issues
bounce back less often than the 19.7% baseline.

---

## The marker set, closed

Four markers exist in the flow. Treat this as a closed enum; anything else is drift.

| Marker | Form | Meaning |
|---|---|---|
| `PLANNED_BY_COPILOT` | Jira label | AI produced the plan |
| `AUTH_BY_COPILOT` | Jira label + commit prefix | AI authored the change |
| `GEN_BY_COPILOT` | commit prefix (Jira label **to be added**) | AI generated test artifacts |
| `REVIEW_BY_COPILOT` | Jira label (**nothing applies it yet**) | AI reviewed the change |

Three drifted variants are already in the data — `PLANNER_BY_COPILOT`,
`DEV_BY_COPILOT`, `COPILOT_TESTING` — and each one silently subtracts from the AI
figure. Agents must validate against this enum **before** applying a label rather than
after, and the poller must count unrecognised `*_BY_COPILOT` labels as a data-quality
signal instead of dropping them.

---

## Self-reported trails: use for mapping, never for counting

The daily Excel report is the cheapest trail available because it is already being
written. It is also **self-reported, retrospective, and human**.

- Use it to **build** the AIO ↔ Jira mapping and to **cross-check** machine trails.
- Never use it to **count** output.

Counting from self-report produces a metric that ranks people by how carefully they
write reports. Someone who works hard and logs little will look worse than someone who
works less and logs everything — and that failure is invisible in the output, which is
what makes it dangerous.

The reconciliation is the real value:

```
Excel:  person X worked on PRJ-6384 and PRJ-TC-2891 on 2026-08-12
Jira:   PRJ-6384 transitioned on 2026-08-12          ✓
AIO:    PRJ-TC-2891 has a run on 2026-08-12          ✓
     → mapping PRJ-6384 ↔ PRJ-TC-2891, high confidence
```

Agreement across three sources builds the mapping G6 needs without editing 4,248
cases by hand. Disagreement is itself a finding — either work is not reaching Jira, or
the report overstates. Both are worth surfacing, and neither should be silently
smoothed over.

This also yields a metric not in the original ten:

> **Trail completeness** — the share of self-reported work that left a machine-readable
> trace. It measures the measurement system, and it should be reported alongside every
> other figure so a reader knows how much of reality the numbers cover.

---

## Where the join breaks

**Linking audit, 2026-08-26.** Tracing one path — a Copilot session to the ticket
it served — across four systems.

Measured against 22 real Copilot session journals, **12 live pull requests** in
`aeriscom/wt-playwrite-taf`, the **218 projects** on `aeriscom.jira.com`, and the
sample tree at `samples/ngocnguyen`. AIO could not be queried — see §7.

Four joins were found broken. Two were silent structural zeros, one was
discarding every token this pilot has measured, and one was **inventing links to
tickets that do not exist**.

---

### 1. The chain

To say what an AI session cost a ticket, four joins have to hold in sequence.
Each is only as good as the key it stands on.

| # | Hop | Key it stands on | Before | Now |
|---|---|---|---|---|
| 1 | Session → repo | `context.repository`, `context.branch` | weak | **exact** |
| 2 | Session → commit | `baseCommit..headCommit` | *not collected* | **exact** |
| 3 | Commit → PR | `scm.pr.created.commit_shas` | *never emitted* | **exact** |
| 4 | PR → ticket | branch / PR title, allow-listed | **fabricating** | **honest** |

Hop 4 was not weak. It was producing confident, plausible, wrong answers.

---

### 2. Hop 4 was inventing tickets

`extract_jira_key` accepted anything matching `[A-Z][A-Z0-9]+-\d+`. Run against
12 live pull requests, it produced seven distinct keys. **Three of the projects
do not exist** among the 218 on `aeriscom.jira.com`:

| Extracted from | Key | What it actually is |
|---|---|---|
| branch `fix/AUG-25` | `AUG-25` | **August 25.** A date. |
| branch `fix/AUG-24` | `AUG-24` | A date. |
| branch `CY-199` | `CY-199` | Not a project. |
| PR title `TC-12018` | `TC-12018` | Not a project. |
| branch `IML-6546/release/26.8` | `IML-6546` | Real. |
| commit subject | `IML-6500` | Real. |
| PR title | `APR-2016` | Real. |

In events emitted, the fabricated keys **outnumbered the real ones — 14 to 5.**

A neighbouring branch, `fix/Aug-21`, escaped only because of its lowercase
letters. That is luck, not a rule.

`CONTRACT.md` §2.4 says *"Never synthesise a `run_id` to force a join — that
manufactures a join key and breaches AR-1."* A fabricated `jira_issue_key` is the
same offence in a different column, and worse for being plausible: `AUG-25` looks
exactly like a ticket, so nothing downstream has any reason to doubt it. It
attributes real engineering work to a ticket that does not exist.

**Fixed** with an allow-list — not a deny-list. A deny-list here would have to
anticipate every month abbreviation, every `TC-`, every `CY-`, and every
convention a team invents next quarter. The allow-list only has to know what Jira
says exists, and Jira will tell you.

Re-polled against the same 12 PRs with `JIRA_PROJECT_KEYS=IML,APR`:

| | Before | After |
|---|---|---|
| Fabricated links | **14** | **0** |
| Real links (`IML-6500`, `IML-6546`, `APR-2016`) | 5 | **5** |
| Honest nulls | 11 | 25 |

The extractor also now scans past a plausible-but-unknown prefix rather than
stopping at the first match, so `fix/AUG-25 for IML-6500` resolves to `IML-6500`
instead of nothing.

---

### 3. The collector was rejecting every token we have measured

Every `model.call` event built from the Copilot journal — **57 of 57** — was
refused at ingest with `envelope.missing_field: run_id`. Those rows carry every
token count, every cached-read figure and every premium request: the entire Cost
section of the weekly report.

The cause is a contradiction between the contract and the code enforcing it.
`CONTRACT.md` §2.4 is explicit —

> Poller events carry `run_id = null` unless the commit carries an `AI-Run-Id` trailer.

— while the collector's envelope check demanded a **non-null** `run_id` for every
event type. The collector rejected the exact shape the contract mandates.

It was not only Copilot usage. On the same rule, every Jira transition, every
Bitbucket PR without an AI trailer, and every AIO test run would be rejected too.

It survived 798 passing tests because the collector's own `make_event` fixture
always stamped a run id. **The contract's mandated case was the one case never
exercised.**

| | |
|---|---|
| Real journal events, before the fix | **57 rejected** |
| Real journal events, after | **2,943 accepted · 0 rejected** |
| An *absent* `run_id` — still rejected, deliberately | unchanged |

Present-but-null and absent are now distinguished. Null is a claim: *no run owns
this*. Absent is a client that forgot the field, and reading that as "no run"
would turn a client bug into a silently unattributed row.

---

### 4. Two joins that had never returned a row

Not sparse — never populated. Downstream, an empty result and a never-populated
one look identical, which is why these survived.

#### 4.1 `scm.pr.created` was in the contract, in the collector, in the SQL — and emitted by nothing

`sql/05_transform_output.sql:164` unnests `scm.pr.created.commit_shas` to decide
which PR an output was first reviewed in. The event type sits in the collector's
allow-list. **No poller had ever emitted it.** The commit→PR edge — hop 3 of four
— resolved to nothing from the day the warehouse was written.

#### 4.2 `commit_shas` was never computed either

`summarise_pr_commits()` walked every commit on a PR to count AI markers, then
returned counts only. Even had the event existed, the array it joins on would
have been absent.

**Both fixed, and verified against the live repository:** 12 of 12 pull requests
now emit `scm.pr.created` carrying commit SHAs — 76 in total, where there were
none.

---

### 5. What the Copilot journal can and cannot prove

Measured across 22 session journals.

| Signal | Measured | Consequence |
|---|---|---|
| Branch names carrying a Jira key | **0 of 37** | The Copilot→Jira link the reader has always shipped resolves to `NULL` on every real session |
| Context blocks with `baseCommit` + `headCommit` | **37 of 37** | An exact key was present in every record and was being dropped unread |
| Sessions that moved the tree | 8 of 22 | Now emit `run.bound` at `explicit` / 1.0. The other 14 committed nothing and correctly bind nothing |
| Named `gitRoot`s that still exist on disk | **1 of 7** | The link cannot be reconstructed later |
| Sessions whose usage is unknowable | 2 of 22 | No `session.shutdown`. Unavailable, not zero |

That fourth row is the design rule the rest follows from: **evidence about a
repository has to be taken while the repository is still there.** Worktrees are
deleted when their branch merges — precisely the sessions that produced
something. A commit range recomputed next week from a clone that no longer exists
is not a weaker link; it is no link.

---

### 6. A correction, and what the scope actually is

An earlier draft of this audit claimed the pollers and the telemetry were pointed
at different companies, on the basis that the Jira site I could reach
(`all-it.atlassian.net`, projects FUT/WV/AIDLC) contained no `IML` project.

**That comparison was against the wrong Jira.** The site this system targets is
`aeriscom.jira.com` — 218 projects, including both `IML` and the configured
`APR`. `all-it` is a separate, unrelated instance that happened to be the one my
Atlassian connection was attached to. It has no bearing on this pilot.

The real picture is coherent:

| | |
|---|---|
| Jira | `aeriscom.jira.com` · `JIRA_PROJECT_KEY=APR`, `IML` also live |
| Bitbucket | `aeriscom/wt-playwrite-taf` |
| Sample user's workspace | `…/NgocNguyen/wt-playwrite-taf` — **the same repo** |

The `Seta-International` / GitHub repositories in §5 are from *my own* machine's
journals, not the pilot cohort's. There is no cross-company mismatch to resolve.

Credentials for Jira and Bitbucket live at
a gitignored, untracked `.env` outside this repo (verified).

---

### 7. AIO cannot reuse the Jira credential

The premise that the Jira token could be reused for AIO is contradicted by this
codebase's own documentation —
`tools/ai-telemetry/pollers/README.md:447`:

> **AIO does not accept the Jira API token** … hence `AIO_API_TOKEN` rather than
> a reuse of `JIRA_API_TOKEN`. Get one from **AIO Tests → API Keys**.

It authenticates with `Authorization: AioAuth <token>`, not Basic auth. The Jira
credential returns `401 Invalid or missing API Token`.

So AIO stays unqueried, and with it the finding that needs it:

**`poll_aio.py:438` hard-codes `jira_issue_key = None` on every `test.run.completed`.**
Every test execution this system records is unattributable to a ticket by
construction, so test outcomes can never be joined to the work that caused them.
Whether AIO exposes a requirement link on a cycle or case needs a token to
answer. Flagged rather than guessed at.

---

### 8. The sample machine produces no telemetry at all

`samples/ngocnguyen/.copilot` has **no `session-state/` directory**. No journals,
no events, nothing for the reader to parse. Of its 253 files, 1.9 MB of 2.0 MB is
an installed plugin.

**Corrected 2026-08-26.** An earlier reading blamed the mode: every process log
opens with `Starting CLI in server mode (stdio)` and closes with `Destroying 0
active sessions`, and that was taken to mean the mode journals nothing. It does
journal. The machine this was investigated on shows the *identical* pattern — 29
server-mode starts in August, `Destroying 0 active sessions` every time — and has
28 session directories from April–June, plus **0** from August.

The real reason is simpler: VS Code starts the CLI server whenever the editor
opens, and a session directory is created only when a Copilot CLI session is
actually run. On these machines, none ever has been.

| | |
|---|---|
| Contract events this machine would produce | **0** |
| Only signal present — `ide/*.lock` workspace folders | 3 repos |
| …and those are absolute paths carrying the username | unusable as-is |

This matters more than a coverage footnote, because §6 establishes that this
person works in `wt-playwrite-taf` — **the pilot repository**. If the cohort is QA
engineers working this way, the collection rate is not low; it is **zero**, and it
will report as a quiet week rather than as an absent one.

---

### 9. What changed

| File | Change | Effect |
|---|---|---|
| `pollers/common.py` | `extract_jira_key(..., projects=)` allow-list; `Config.jira_project_keys` from `JIRA_PROJECT_KEYS` | **14 fabricated links → 0** on live data |
| `collector/main.py` | `run_id` may be null; the key must still be present | **57 → 0 rejected** |
| `cli/copilot_read.py` | Keeps `baseCommit`, `repositoryHost`; emits `run.bound` with the commit range, one row per branch | 8 exact links where there were none |
| `pollers/poll_bitbucket.py` | Emits `scm.pr.created` carrying `commit_shas` | 12 of 12 live PRs, 76 SHAs |
| `pollers/poll_ci.py` | Allow-listed key extraction | no fabricated pipeline links |
| `cli/insight.py` | `scan` and `copilot` pass `config["jira_projects"]` | no fabricated commit links |
| `schema/CONTRACT.md` | Row 2 documents the range; the envelope documents null vs absent | contract and collector agree |
| tests | 16 added | **798 → 814 passing** |

One correctness detail, because the first version had it wrong. A session can
move between branches — one here starts on `main` and resumes on `feat/uiux`.
Taking the first base and the last head across that produces a range spanning two
unrelated lines of work, charging the session for every commit in the gap. Ranges
are keyed **by branch** instead, so each is bounded by commits that are genuinely
ancestors of one another. Both `agent-platform` ranges were then verified to
resolve against the one surviving clone.

`run.bound` publishes `jira_issue_key = null` even where a key *is* derivable from
the branch name. Resolving a range to a ticket is the warehouse's job, where the
SCM side of the join lives — and a key that is right by luck is not evidence.

---

### 10. Still open

- **Set `JIRA_PROJECT_KEYS` wherever the pollers run.** Without it the allow-list
  is absent and the old permissive behaviour returns. `poll_jira.py` talks to
  Jira and could fetch the list itself; seeding the laptop config at `setup` is
  the remaining gap.
- **The proxy is still not redeployed.** Uploads have been failing hourly since
  05:14 with `HTTP 400 — unknown schema_version '1.1.0'`. No data is lost; the
  bundles are on disk. All of this work rides that same deployment.
- **AIO needs its own key** (§7) before the test→ticket link can be assessed.
- **`--max-prs 12`** bounded the live sample. A full pass over the window would
  give a firmer ratio than 14:5, though the direction is not in doubt.

---

# Metric status — August 2026

### Status — window: **August 2026 only** (2026-08-01 →)

The reporting window is August onward. Everything below is measured inside it.
A metric that cannot be computed is listed as such, never quietly replaced by a
nearby one that can.

| # | State | August figure | Note |
|---|---|---|---|
| 1 | 🟡 partial | **195** automation scripts changed (35 merged PRs) | AI-marked: **2 of 35** |
| 2 | 🟡 **declared, not verified** | **80.2%** = 1,483 / 1,848 | 1,150 cases have no status set — excluded, not counted as un-automated. And `has_automation_key` is false on **100%** of cases: not one points at a real script, so this measures how a dropdown is filled in, not whether automation exists |
| 3 | 🔴 no signal | **5** review events on 41 PRs | cannot compute from 5 samples |
| 4 | 🔴 no signal | — | same denominator problem |
| 5 | 🟡 partial | anchors reachable | AIO case ↔ Bitbucket merge join not built |
| 6 | ⛔ out of scope | — | counterfactual; needs a controlled manual arm |
| 7 | 🟢 **live** | **66.6%** = 8,653 / 12,983 | 13 cycles; 8,028 automated; 557 defects |
| 8 | 🔴 **not measurable** | — | AIO stores **one run per case per cycle** and overwrites on re-execution: measured, **0** case+cycle pairs have more than one run. A cross-cycle comparison (which gives 33/4,157 = 0.8%) counts genuine fixes and regressions as flakes. Needs rerun history AIO does not keep |
| 9 | 🔴 blocked | — | **0 AI-Run-Id events**, and **0 AI telemetry in August** |
| 10 | ⛔ out of scope | — | "value" is not observable |

**One of ten is live** (Execution Rate). One more is reportable with a stated
caveat (Coverage). Two are permanently out of scope; the rest are blocked.

That is a worse scorecard than an earlier draft of this file claimed, and it is
the true one: two figures were marked live before `docs/FINDINGS.md` §5 and §6
were read against them.

### What is missing, precisely

**1. There is no AI-usage telemetry in August from any source.** Not thin —
zero.

| Source | Newest data | Gap |
|---|---|---|
| `~/.copilot/session-state` (this machine) | 2026-06-26 | 2 months |
| VS Code chatSessions (this machine) | 2026-02-05 | 6 months |
| `samples/ngocnguyen` | **no `session-state` at all** | total |
| `samples/linhhoang` | **no `session-state` at all** | total |

Verified against file mtimes, so this is real staleness and not a parse bug.
Every metric with "AI" in its numerator therefore has no numerator in August.

**2. Scope: one repository out of 32.** `aeriscom` has 45 repositories, **32
active in August**; only `wt-playwrite-taf` is polled. And on Jira, the project
with the most AI attribution is not the one being measured:

| Project | Issues updated in Aug | `AUTH_BY_COPILOT` | `PLANNED_BY_COPILOT` |
|---|---:|---:|---:|
| **AERLABS** | 719 | **100** | **157** |
| IML | 185 | 24 | 44 |
| APR | 55 | 0 | 0 |

AERLABS carries four times the AI attribution of IML and has never been pulled.

**3. Review practice.** 5 review events on 41 August PRs. Metrics 3 and 4 cannot
be manufactured from a process that does not produce the signal.

### What to do next, in order

1. **Widen the scope.** Poll AERLABS (Jira) and the 32 August-active
   repositories, not just `wt-playwrite-taf`. The AI attribution is largely
   somewhere we are not looking.
2. **Get collection onto the QA machines.** Both sampled machines produce zero
   — **because no Copilot CLI session has ever been run on them**, not because
   the journal is unwritable there. Verified 2026-08-26: this machine shows the
   identical log pattern (29 server-mode starts in August, `Destroying 0 active
   sessions` every time) and also created **0** session dirs in August, while
   creating 28 in April–June. The CLI server starts whenever VS Code opens;
   `session-state/` appears only when a session actually runs.
   `cli/vscode_read.py` covers the chat panel, and must run **daily**: 24 of 27
   workspace folders were already deleted when read retrospectively. Note the
   CLI's own logs retain only ~7 days, so they are not a durable signal either.
3. **Turn on AI markers.** `AI-Run-Id` trailers and `[AUTH_BY_COPILOT]` on
   commits. Currently zero anywhere. This is the only route to metric 9.
4. **Document `insight update`.** It landed after the last documentation pass
   and no file mentions it.
5. **In the agent-platform repo** (not this one): pin its copy of the contract
   and add a drift check; delete the directories that moved here.
6. **Agree the daily-report column spec** with the team (`importers/README.md`)
   before more days are filled in by hand.
