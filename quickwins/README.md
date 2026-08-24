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

## A. `scan_ai_commits.sh`

```bash
./scan_ai_commits.sh                                # this repo
./scan_ai_commits.sh ~/src/repo-a ~/src/repo-b      # several, plus a grand total
./scan_ai_commits.sh --tsv > monthly.tsv            # machine-readable
MARKERS='AUTH_BY_COPILOT' ./scan_ai_commits.sh      # narrow the marker set
```

Prints total commits, AI-marked commits, share %, date range, distinct author
identities, the per-author breakdown, and a month-by-month table.

**Detection.** Only the explicit **commit** markers the agents actually write —
`AUTH_BY_COPILOT`, `GEN_BY_COPILOT` — matched anywhere in the commit subject, because
real history places them three different ways (design §2.3):

```
prefix   [AUTH_BY_COPILOT] feat(x): …
infix    [AUTH_BY_COPILOT] : refactor(x): …      AMS-1856: docs: [AUTH_BY_COPILOT] …
suffix   feat(x): … [GEN_BY_COPILOT] [TICKET-123]
```

That pair is the commit-marker closed set of **CONTRACT.md §3.1**, and it is the same
set `pollers/common.py::AI_COMMIT_MARKERS` uses — the two must not drift apart. (They
did: until 2026-08-24 the poller matched `AUTH_BY_COPILOT` only, so this README was
right and the code that ran was wrong. A 60-day pull of `acme/qa-automation`
reported `ai_commit_count = 0` on all 102 PRs as a result.)

`PLANNED_BY_COPILOT` and `REVIEW_BY_COPILOT` are the other two markers in §3.1. They
are **Jira labels only** — nothing writes them onto a commit subject — so this script
does not look for them and adding them to `MARKERS` would only match commits that
*talk about* the labels.

It deliberately does **not** use the pattern from the `aiep-impact-report-generator`
skill on `origin/feature/ATSP-22288`, which adds `^feat|^fix|^chore`. Conventional
Commits are mandated repo-wide by `.github/copilot-instructions.md` §5, so those
alternations classify essentially every human commit as AI-authored. See the comment
block at the top of the script.

**Actual output on one real repository:** 43 raw identities → 35 distinct emails.
Eight of those emails carried more than one display name.

```
dev1@example.com    Dev.One (23) / dev1 (23)
dev2@example.com    Dev Two (1)  / dev2 (1)
dev3@example.com    Dev Three (55) / Three, Dev (5)
...

F2  dev4alias   -> dev4alias@bitbucket.org  and  dev4@example.com
F4  git stash <git@stash>  ·  NoEmail <NoEmail>            (unhashable)
F5  git@stash · someone@othercompany.com · personal@gmail.com
```

The real output is not reproduced here. It is a list of named individuals and
their email addresses, which is precisely the data `CONTRACT.md` §1.1 forbids
storing — reproducing it in a README to illustrate a tool that exists to hash it
would be an odd way to make the point. Run the script to see your own.

**What it means.** The share of commits an agent *claimed*. **Not** how much code AI
wrote (a marked commit may be one line), whether it was reviewed, merged, reworked or
reverted, or what it cost. The marker is applied by policy, not enforced by a hook, so
opt-out is silent — **treat 12.1% as a lower bound.**

---

## B. `identity_collisions.sh`

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

## C. `pr_lead_time.py`

```bash
export BITBUCKET_USERNAME=... BITBUCKET_ACCESS_TOKEN=...
./pr_lead_time.py --repo workspace/repo
./pr_lead_time.py --repo ws/a --repo ws/b --since 2026-01-01 --exact-merge --json out.json
./pr_lead_time.py --repo ws/a --no-diffstat        # faster; drops the per-100-lines column
```

Python 3, stdlib only — no `pip install`. **Strictly read-only: HTTP GET only.** It
never creates, approves, merges, comments on or declines anything. Credentials are read
from the environment and are never printed, logged, or written to the JSON output;
query strings are stripped from any URL that reaches stderr.

Reports, for merged PRs, split by whether the title carries `[Authored By Copilot]`:
median and p85 lead time (`created_on` → merged), and the same normalised per 100
changed lines from `/diffstat`.

**Not run against live Bitbucket here** — this sandbox has no outbound TLS path, so the
network call was exercised only to confirm it fails cleanly without leaking anything.
The parsing, percentile and formatting logic is unit-tested; run it yourself with real
credentials.

**What it means and does not mean** (also printed after every run):

1. **n is small.** This repo has 3 PRs carrying the marker. Design §9.1 requires
   n ≥ 20 per arm before reporting a difference. Below that the table is descriptive.
2. **This is not AI vs human.** There is no non-AI control group (design §9.1
   Decision 2). Unmarked PRs are mostly AI-assisted work where the marker was not
   applied. The split is "marker present" vs "marker absent" and nothing more.
3. Lead time is confounded by PR size, reviewer availability, weekends and freezes.
   The per-100-lines column controls for size only.
4. Without `--exact-merge`, `merged_at` is approximated by `updated_on`, which moves on
   any late edit. Use `--exact-merge` for a figure anyone will act on.
5. Merged-only: declined and open PRs are invisible, so this view is survivor-biased.

---

## D. `retain_metrics.sh`

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

## Honest limits of everything in this directory

- **Every number is descriptive, not causal.** These scripts describe what happened.
  None of them establishes that AI caused it.
- **Markers are policy, not enforcement.** Commit and PR markers are applied by agent
  instructions, so every marker-based share is a lower bound with an unknown gap. The
  `prepare-commit-msg` hook in `../hooks/` is what eventually closes it.
- **No cost anywhere.** Nothing here sees tokens. Cost arrives with the Copilot OTel
  export (design §4.4a, §14.1 item 5) — configuration, not code, and the other genuine
  same-week win.
- **Do not build a dashboard on these.** They are a factual starting point and a
  worklist. The pipeline described in `../README.md` is what replaces them.
