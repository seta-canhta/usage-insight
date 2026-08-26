# Quick wins — real numbers today, no infrastructure

Four scripts implementing **design §14.1, "Measure immediately — this week, no new
infrastructure"** (`docs/spikes/ai-effectiveness-observability.md`).

Everything here runs standalone, **before** the emitter, collector, pollers, warehouse
or dashboards exist. Nothing here depends on anything else in this repository.
Three of the four need no credentials at all.

| Script | Answers | Needs | Runtime |
|---|---|---|---|
| `scan_ai_commits.sh` | What share of commits carries an AI provenance marker, by whom, since when? | a cloned repo | seconds |
| `identity_collisions.sh` | Which git author identities are the same person? | a cloned repo | seconds |
| `pr_lead_time.py` | How long do merged PRs take, and does the AI marker correlate? | Bitbucket creds | minutes |
| `retain_metrics.sh` | *(not a report — an action)* Stop deleting the metrics the agents already compute. | a finished workflow | instant |

If you only do one thing, do `retain_metrics.sh`. Design §14.1 calls it the highest
value-per-hour action in the whole document: the numbers already exist and are being
thrown away, so every day it is not wired up is a day of measurement lost permanently.

---

## `identity_collisions.sh`

```bash
./identity_collisions.sh                     # this repo
./identity_collisions.sh ~/src/a ~/src/b     # union across repos
./identity_collisions.sh --map               # emit a hand-correctable identity-map stub
CORP_DOMAIN=example.com ./identity_collisions.sh
```

Six checks, each with a different confidence level and a different fix:

| | Finding | Action |
|---|---|---|
| F1 | One email, several display names | Merge automatically on lowercased email |
| F2 | One normalised name, several emails | Review — two people can share a short name |
| F3 | Near-duplicate names (edit distance ≤ 2) across different emails | Review — catches transposition typos |
| F4 | No `@`, or a synthetic address | Cannot be hashed (CONTRACT §2.1) — map by hand or exclude |
| F5 | Non-corporate domain | Will not join the directory; `person_id`/`team_id` stay null |
| F6 | Same email in several letter cases | Confirms lowercasing before hashing matters |

**Actual output on one real repository:** 43 raw identities → 35 distinct emails.
Eight of those carried more than one display name, and three used a non-corporate
domain.

The real list is not reproduced here. It is people's names and email addresses,
which is exactly what `CONTRACT.md` §1.1 forbids storing — printing it in a README
to illustrate a tool whose whole purpose is to hash it would be an odd way to make
the point. Run the script against your own repositories.

**What it means.** Git records a free-text name/email pair chosen by each developer's
local config. Until these are resolved, "distinct authors" counts git configs, not
people, and every per-person rate is wrong by an unknown factor. This script does not
fix anything — it produces the worklist for `core.dim_person`. `--map` emits a YAML
stub with `TODO_ATLASSIAN_ACCOUNT_ID` placeholders; the real `person_id` must come from
the Atlassian directory (CONTRACT §2.1), which is open question **OQ-6**.

**What it does not mean.** F1 is safe to merge; F2 and F3 are *candidates*, not
conclusions. Confirm with the people concerned before collapsing two identities into
one — a wrong merge silently attributes one engineer's work to another.

---

## `retain_metrics.sh`

```bash
./retain_metrics.sh --jira AUT-632                 # explicit key
./retain_metrics.sh                                # key auto-read from workflow-context.json
./retain_metrics.sh --dry-run                      # show what would be kept
AIEP_METRICS_HOME=/mnt/share/aiep ./retain_metrics.sh
```

**Run it at the end of a workflow, before `.tmp/` is cleaned.** Copies (never moves):

```
.tmp/test-spec/04-specifications/spec-metrics.json
.tmp/test-spec/workflow-context.json
.tmp/test-spec/execution-context.json
.tmp/test-spec/intent-analysis.json
.tmp/test-spec/workflow-summary.md
.tmp/test-results/cucumber-report.json
.tmp/quality-gates-*.log
.tmp/test-spec/08-metrics/*.json          # config.yaml:129 already reserves this path
```

to `${AIEP_METRICS_HOME:-~/.aiep/metrics}/{JIRA_KEY}/{UTC timestamp}/`, writes a
`manifest.json` (jira key, run id, profile, branch, git sha, remote, host, file list)
and appends one row to a flat `index.tsv`.

Exit codes: `0` retained or dry run, `3` nothing found — **never** non-zero for a
missing artifact, so an agent can call it unconditionally without risking its own run.
No `jq` dependency. Files are copied verbatim; nothing is summarised or transformed.

**What it means.** The agents already compute requirement coverage, automation
coverage, scenario counts, step reuse and a time-saved figure — and then
`tmp_management: on_success: "Clean all .tmp/"` deletes them. Changing the destination
is the difference between having a time series in six weeks and having nothing.

**What it does not mean.** These are the agents' *self-reported* figures, computed by
the agent that did the work. In particular the `time_saved` values inside
`spec-metrics.json` rest on a manual-effort benchmark nobody measured — design §9.1
Decision 2 and §8.10 demote that to a directional trend at best. Retain it, do not
publish it. The values worth trusting are the counts: scenarios, files, steps reused,
pass/fail.

---

## Honest limits of both scripts here

- **Every number is descriptive, not causal.** These scripts describe what happened.
  None of them establishes that AI caused it.
- **Markers are policy, not enforcement.** Commit and PR markers are applied by agent
  instructions, so every marker-based share is a lower bound with an unknown gap. The
  `prepare-commit-msg` hook in `../hooks/` is what eventually closes it.
- **No cost anywhere.** Nothing here sees tokens. Cost arrives with `insight copilot`,
  which reads Copilot CLI's own session journal at `~/.copilot` — installation, not
  configuration, and the other genuine same-week win. It carries premium requests too,
  which is the unit Copilot actually bills.
- **Do not build a dashboard on these.** They are a factual starting point and a
  worklist. The pipeline described in `../README.md` is what replaces them.
