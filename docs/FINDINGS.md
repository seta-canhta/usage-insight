# Findings — measured baseline

**Measured 2026-08-24.** Scope: Jira `PRJ`, Bitbucket `acme/qa-automation`,
AIO TCMS project `PRJ`, 60-day window (2026-06-21 → 2026-08-24).
23,427 events read.

Every number here came from a live pull, not from a design document. Where a number
contradicts something written earlier in `ai-engineering-platform/docs/spikes/`, this
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

## 2. The correlation layer is empty

| Field | Present |
|---|---|
| `run_id` / `trace_id` on any emitted event | **0** |
| `~/.aiep/telemetry/` on the platform author's own machine | **does not exist** |
| Copilot OTel `model.call` events | **0** — export never switched on |

`emit.py` is wired into **3 of 14** agent definitions and has never produced a single
event. Everything currently reported is derived from third-party systems after the
fact. No token count, no model id, no cost, no agent attribution exists today.

---

## 3. AI attribution — what works and what does not

### The marker map in the AIEP flow

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
(`test-executor-committer.agent.md:267`). `quickwins/README.md:36` claims both are
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
