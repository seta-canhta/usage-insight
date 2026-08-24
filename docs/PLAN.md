# Implementation plan

Read `FINDINGS.md` first — it is the evidence this plan rests on, and
`ARCHITECTURE.md` for the client/central split. Gap references (G1…G9) are to
`TRAIL-GAPS.md`; metric numbers to `WHAT-WE-MEASURE.md`.

---

## Why this repo is separate from `ai-engineering-platform`

AIEP is **distributed to engineers**: `setup.sh` copies the whole repository into every
developer's `$HOME` and registers it with VS Code. Measurement code does not belong in
something that ships to the people being measured.

| | `ai-engineering-platform` | `usage-insight` |
|---|---|---|
| Audience | every engineer, on their machine | SETA internal, one scheduled runner |
| Contains | agents, skills, prompts, `emit.py` | pollers, collector, importers, reports |
| Credentials | none | Jira, Bitbucket, AIO tokens |
| Output | none | reports naming individuals |
| Cadence | changes with the agent flow | changes with the measurement question |

The current `reports/.gitignore` blocks `*` — a repository fighting its own contents is
a repository holding the wrong contents.

**`emit.py` stays in AIEP.** It runs inside the agent flow; it is instrumentation, not
measurement. Only the collection and reporting side moves.

### The shared contract

`schema/CONTRACT.md` is the single source of truth for both repos. It moves here, and
AIEP keeps a **pinned copy** with its `schema_version`. Two editable copies would drift,
and a drifted contract is worse than a duplicated one because both sides keep believing
they agree.

---

## Phase 0 — Repository foundation

**Goal:** this repo runs the existing pipeline unchanged.

- [x] Repo skeleton, `.gitignore`, `.env.example`
- [x] Findings, trail gaps and metric feasibility written down
- [x] Move `schema/`, `pollers/`, `collector/`, `report/`, `sql/`, `quickwins/` from AIEP
- [ ] Add `cli/` — the local client (Python, stdlib only, same language as the pipeline)
- [ ] Pin the contract copy left behind in AIEP; add a drift check
- [x] Move the test suites — **131 + 78 + 37 green here**; `emit`'s 51 stay in AIEP
- [x] CI: run the suites on push, on 3.10 and 3.12, with a stdlib guard on `cli/`
- [ ] AIEP: delete the moved directories, leave `emit.py` and a README pointing here

**Done when:** every suite is green here and AIEP no longer contains a poller.

---

## Phase 1 — Free trails (G1, G2, G3, G4)

Nothing here costs an engineer anything. All four are code or one-time config.

Two of them (1b, 1d) only produce data once the **npx client** exists to catch it —
see Phase 1e and `ARCHITECTURE.md`. Build the client first if you want 1b/1d to yield
anything on a real machine.

### 1a. Fix the marker regex — G3
`AI_COMMIT_MARKER_RE` matches only `AUTH_BY_COPILOT`; the QA agents commit
`[GEN_BY_COPILOT]`. Add it, re-poll Bitbucket, and **re-check the claim** that SCM has
no AI attribution — that conclusion came from the incomplete regex and may be wrong.

Also: add `GEN_BY_COPILOT` as a **Jira label** in the QA agents. Today it exists only
as a commit prefix, which makes it invisible to the Jira poller.

### 1b. Wire `emit.py` into the remaining 11 agents — G1
Three of fourteen call it, and `~/.aiep/telemetry/` does not exist on any machine
checked. `run-start` already carries `--agent`, `--agent-file`, `--jira`, `--model`,
`--mode` — the design is complete; only the wiring is missing.

This is what answers *"which platform agent are they using, on which task"*.

### 1c. `AI-Run-Id:` git trailer — G2 ✅
`cli/hooks/prepare-commit-msg`, installed by `./insight install-hook`. It reads the
newest *open* run from the `emit.py` buffer and appends the trailer; the Bitbucket
poller already knows how to read it (`AI_TRAILER_RE`). This is the only **explicit**
commit ↔ run link; markers are heuristics.

Three constraints, because a commit hook that gets any of them wrong is worse than no
hook: it never fails a commit (any error exits 0 — telemetry that can block a commit
gets deleted within the week), never touches the subject line (that is where the
provenance markers other tooling parses live), and never invents a run when none is
open (a fabricated id would put a human commit's cost on an agent's account). A run
older than four hours is treated as stale — someone left a terminal open, and a wrong
join is worse than a missing one.

### 1d. Copilot OTel, local pilot — G4
Per-developer, no admin needed:

```bash
export COPILOT_OTEL_ENABLED=1
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

Two developers, one afternoon, and metric 9 has real token counts.

For the pilot prefer the **`file`** exporter over `otlp-http`: no collector to stand
up, and the engineer can read what was recorded before it goes anywhere. `console` is
the documented way to run the privacy audit — it shows exactly what the client would
have sent. `collector/otel_config.yaml` stays for the eventual central path.

⚠️ `OTEL_EXPORTER_OTLP_HEADERS` / `_PROTOCOL` are **[A]**-tagged — conventional for
OTLP but not vendor-documented for this exporter. A local collector without auth avoids
depending on them, which is a further reason to pilot locally before an org rollout.

Org-managed rollout buys **enforcement** and **cannot-be-switched-off**. Ask for it
*after* the pilot produces numbers — the numbers are the argument.

### 1e. The `npx` client — the thing that catches 1b and 1d

`emit.py` writing to a local buffer and Copilot exporting OTLP to localhost both
produce data that never leaves the machine. The client is what turns them into
something SETA can read.

- [ ] **First: verify the OTel `file` exporter** — path, format, rotation. The whole
      no-daemon design rests on it (`ARCHITECTURE.md`). If it appends without bound,
      stop and reconsider before writing any client code
- [ ] `cli/` — Python 3.9+, **stdlib only**, run as `./insight` from a clone. macOS and
      Ubuntu only. No daemon: `pack` reads the Copilot span file, the `emit.py` buffer
      and `git log`, then writes one bundle
- [ ] Reuse the envelope, `event_id` derivation and allow-list from the existing code —
      do not reimplement them; a second implementation of the allow-list is where a
      content leak would come from
- [ ] `init` — consent prompt, machine id, salt, point Copilot at the file exporter
- [ ] `pack` — sealed bundle with manifest and checksum
- [ ] **allow-list check on write**, not on ingest — the client sits on a machine full
      of exactly what `CONTRACT.md §1.1` forbids collecting
- [ ] `purge` — a person who cannot delete their own telemetry has not consented
- [ ] `status` — what is buffered, what was last packed

**Done when:** one agent run on an engineer's machine produces a `run_id` that joins to
a commit, a PR, and a token count, and arrives at SETA inside a packed bundle.

---

## Phase 2 — Intake

Two things arrive by hand every week and both need an importer that refuses bad input
loudly rather than quietly.

### 2a. Bundle intake

- [x] `importers/bundle.py` — verify checksum, read manifest, dedup on `event_id`
- [x] Track **coverage**: which machine, which window, per week
- [x] A missing week renders "no data"; an empty bundle renders a measured zero.
      With hand-collected data a gap is the normal case, not a bug — see
      `ARCHITECTURE.md`
- [x] Publish machine-weeks covered alongside every aggregate

### 2b. The daily Excel importer (G5)

The cheapest trail with real leverage, because it is already being written.

- [ ] Agree the column spec with the team (`IMPORT-SPEC.md`) — **before** more days
      accumulate in free text; nobody goes back to fix old spreadsheets
- [x] `importers/daily_report.py`: read → validate → **mapping table, not events**
- [x] Reject loudly. An unparseable row is reported with its row number, never dropped
- [x] Hash emails per `CONTRACT.md §11.3`; source files land in `inbox/`, gitignored
- [x] Three-way reconciliation: Excel × Jira × AIO → the AIO ↔ Jira mapping table
- [x] Publish **trail completeness** as a first-class metric — `daily_report.py`, and a coverage section in the combined report

**Constraint that must not erode:** the Excel builds mappings and cross-checks. It
never counts output. See `TRAIL-GAPS.md`.

**Done when:** a mapping table exists with a stated confidence per row, built without
hand-editing 4,248 AIO cases.

---

## Phase 3 — The expensive trails (G6, G7, G8, G9)

These need people, process, or systems we do not own. Sequence them behind evidence
from Phase 2 — Phase 2 will show how much of the mapping can be inferred, and therefore
how much manual backfill is genuinely left.

| | Work | Owner | Note |
|---|---|---|---|
| G6 | Jira key on the AIO case | QA | Phase 2 reduces the backlog; new cases from day one |
| G7 | Automation key = repo path | QA | unlocks verifying metric 2 |
| G8 | `REVIEW_BY_COPILOT` tag-back | **external system owner** | identify the system first; verify it tags at all |
| G9 | Publish each execution, stop overwriting | CI / AIO admin | unlocks metric 8 |

**G8 first, because it is a question, not a task.** Zero of 427 issues carry the label.
That is either no adoption or no tagging, and the two need opposite responses. Find out
before designing anything on top of it.

---

## Phase 4 — Report surface

- [x] Combined weekly Markdown (people + project) — ported
- [x] Per-person workbook — ported
- [x] Management view — `docs/WHAT-WE-MEASURE.md`, `docs/EMAIL-WEEKLY.md`
- [ ] Then, and only then, the skill layer: a manager asks in chat and the pipeline
      answers

The skill layer is deliberately last. A skill over an unreliable pipeline makes wrong
numbers easier to reach.

---

## Sequencing

```
Phase 0 ──► Phase 1 ─┬─► Phase 2 ──► Phase 3 ──► Phase 4
                     └─► (1d unblocks metric 9 independently)
```

Phase 1 and Phase 2 can overlap: 1 is ours, 2 waits on the team agreeing a column spec.

**Do not start Phase 4's skill layer before Phase 1 lands.** Until `emit.py` produces
events, the reports measure engineering output and label it AI effectiveness — which is
a confident answer to a question nobody asked.

---

## Open questions

| # | Question | Owner | Blocks |
|---|---|---|---|
| OQ-1 | Which system runs the AI code review, and does it tag back reliably? | user | G8, metric 3 for AI |
| OQ-2 | Where is the daily Excel delivered, in what format today? | user | Phase 2 |
| OQ-3 | Do AIO cases relate to Jira issues by any existing convention (folder ↔ epic)? | QA lead | G6 |
| OQ-4 | Who defines "value" for ROI? | management | metric 10 |
| OQ-5 | Is a 0.6% test failure rate real, or are failures not being recorded? | QA lead | trust in metric 7 |

OQ-1 and OQ-2 block work that is otherwise ready to start.
