# usage-insight

Measures what AI assistance actually contributes to software delivery — by
reading systems that already record the work, rather than asking anyone.

Three questions a weekly report cannot otherwise answer: **which agent ran, on
which ticket, and what it cost.** It refuses the ones the data cannot support.

## What you get

One Markdown report a week. A real section, from real data:

```
## 1. Adoption

| Metric                    | This week | Prior |
|---------------------------|----------:|------:|
| AI runs                   |        28 |    18 |
| ...started fresh          |         9 |     7 |
| ...resumed                |         8 |     9 |
| ...sub-agent invocations  |        11 |     2 |
| Active people             |         8 |     1 |
| Distinct skills used      |         3 |     2 |

## 6. Cost

| Metric                  | Value      | Basis                          |
|-------------------------|-----------:|--------------------------------|
| Premium requests        |         21 | measured — the unit Copilot bills |
| API requests            |        358 | not the billed unit            |
| Input tokens            | 22,431,999 | economic weight, not spend     |
| ...of which cache reads | 94.0%      | higher is better               |
| Token cost (modelled)   |          — | derived in the warehouse       |
| Seat cost               |   excluded | a contract term, not telemetry |
```

Ten sections: adoption, acceptance, speed, quality, test execution, cost,
reliability, human involvement, trend, data quality. Full sample:
[`reports/2026-W34/weekly-2026-W34.md`](reports/2026-W34/weekly-2026-W34.md).

## Install

```bash
curl -fsSL https://aeris-insight.seta-international.com/install | sh
insight setup
```

Python 3.9+, standard library only — the artifact is a single zipapp, nothing to
install and no virtualenv. The installer installs and stops; `setup` is a short
conversation (work email, hourly yes/no, commit hook yes/no) because consent
obtained from a pipe is consent nobody was shown.

`setup` prints one `email:fingerprint` line to send to whoever runs the
pipeline. It is a hash; the secret never leaves the machine.

Developing on it? `git clone` and `./insight` work unchanged, and behave
identically. See [`docs/SETUP.md`](docs/SETUP.md) · [tiếng Việt](docs/SETUP.vi.md).

## What runs

Hourly, on its own, from `setup`:

```bash
insight copilot   # Copilot's session journal — tokens, premium requests, tools
insight collect   # which agent, which ticket
insight scan      # every repository Copilot has worked in
insight pack && insight ship
```

`insight schedule --off` stops it. A quiet hour uploads nothing; a quiet day
still uploads one empty bundle, because a measured zero and missing data must
never look the same.

Nothing is registered by hand — Copilot records `context.gitRoot` for every
session, so `scan` finds the repositories itself. `pack` seals and `ship` sends,
two commands on purpose: nothing leaves until the second is typed, which is what
makes reading your own bundle first a real option rather than a claim.

Centrally, once a week:

```bash
python3 importers/pull.py --week 2026-W34 --inbox inbox/ --roster roster.txt
python3 importers/bundle.py --inbox inbox/ --out events.ndjson
```

`pull` names who did not report. Read that line before the numbers.

## What it can see

The source is `~/.copilot/session-state/<id>/events.jsonl`, the journal Copilot
CLI keeps for its own `/resume`. It is written whether or not anything is
watching — no exporter, no setting, no port — and `insight copilot` never alters
or deletes it.

Measured on one real tree of 22 journals:

| | |
|---|---:|
| Contract events produced | 2,935 |
| Absolute paths, or usernames, in the output | **0** |
| Repositories discovered with nothing registered | 7 |
| Tool calls, with a real verdict | 2,000 ok · 62 error |
| Shell-command gates carrying an exit-code verdict | ~88% |
| Sessions whose token usage is **unknowable** | 2 of 22 |

The last two rows are limits, not results. A gate with no exit code is a gate
with **no verdict**, not a pass. A session that ended without `session.shutdown`
carries no usage at all — unavailable, not zero.

**It does not see everything.** VS Code's Copilot Chat panel and inline
completions write nothing to this journal and are unmeasured. Pilot usage is
mixed across all three surfaces, so every run publishes a `coverage` block
naming what it could not see.

## What it will not do

**Never stores content.** No prompts, responses, source code or diffs — counts,
hashes, bounded categories and repo-relative paths only. The journal is *mostly*
content, so the reader **names the fields it keeps** and drops the rest without
looking: an exclusion list is only as good as today's knowledge of a format that
gains keys without asking. The allow-list is checked before a write, not on
ingest, because the client runs on a machine full of what it must not collect.

**Never renders absent as zero.** A gap is the normal case — leave, a new joiner,
a quiet week — and a missing week shown as `0` shows a team getting worse when it
is only getting quieter.

**Never synthesises a join key.** Where two sources cannot be linked with
evidence, the answer is "cannot attribute", not the nearest match.

**Never counts from self-reported data.** It builds mappings only; counting from
it would rank people by how carefully they fill in forms.

**Not a performance record.** Local collection is voluntary and reversible,
which is what makes it consensual and also what makes it useless as an audit
trail. Say so before someone tries.

## Layout

| | |
|---|---|
| `cli/` | `insight` — the local client, stdlib only |
| `pollers/` | Jira, Bitbucket, test management |
| `importers/` | weekly bundles, daily reconciliation |
| `report/` | weekly Markdown, per-person workbooks |
| `collector/` | the attribute allow-list, enforced |
| `server/` | the collection endpoint — S3 behind one `PUT` |
| `schema/CONTRACT.md` | the single source of truth |
| `docs/` | [what we measure](docs/WHAT-WE-MEASURE.md), [architecture](docs/ARCHITECTURE.md), [findings](docs/FINDINGS.md) |

798 tests: `for s in pollers report collector cli importers server; do python3 -m unittest discover -s $s/tests; done`
