# usage-insight

Measures how much AI assistance actually contributes to software delivery, by
reading systems that already record the work: Jira, Bitbucket, a test management
tool, and GitHub Copilot's own telemetry.

It answers three questions a weekly report cannot otherwise answer — **which
agent ran, on which ticket, and what it cost** — and it refuses to answer the
ones the data cannot support.

---

## How it works

Two halves that never talk to each other directly.

```
engineer's machine                        central
──────────────────                        ───────
Copilot  ──writes spans──┐                pollers  ──►  Jira
agents   ──emit.py──────┐│                              Bitbucket
git hook ──AI-Run-Id───┐││                              test management
                       ▼▼▼
                   ./insight  ──weekly bundle──►  importers ──► report
```

Three signals never leave a laptop, and no amount of API polling recovers them:
Copilot's token usage, which agent ran, and the run id stamped into a commit.
That is what the local client is for. Everything else is read centrally through
APIs.

Bundles are handed over by hand, one file a week. There is **no daemon and no
listening port** — Copilot writes its own span file and the client reads it when
asked.

## Install

```bash
git clone git@github.com:seta-canhta/usage-insight.git
cd usage-insight
./insight setup --repo ~/work/repo-one --repo ~/work/repo-two
```

Python 3.9+, standard library only. Nothing to install, no virtualenv.

`setup` configures VS Code, records consent, and installs the commit hook. It
backs up your settings, keeps what you already have, and **restores the backup
if the result does not parse** — the file ends up correct or untouched.

Repeat `--repo` for every repository you work in, or add one later with
`./insight install-hook --repo <path>`. Each is remembered — an engineer with
four repositories should not have to remember four commands, because the one
they forget is the one that silently reports nothing.

Then restart VS Code. Once a week:

```bash
./insight otel      # Copilot's tokens
./insight collect   # which agent, which ticket
./insight scan      # every registered repository
./insight pack --since 2026-08-17 --until 2026-08-23
```

`scan` with no `--repo` covers all of them, and keeps going if one clone has
since been deleted.

`pack` prints one file. Send it. `./insight purge --yes` deletes everything.

Engineer-facing guide: [`docs/SETUP.md`](docs/SETUP.md) ·
[tiếng Việt](docs/SETUP.vi.md)

---

## Rules it will not bend

**No content is ever stored.** No prompts, responses, source code or diffs —
counts, hashes, bounded categories and paths only. The allow-list is checked
before a write, not on ingest, because the client runs on a machine full of
exactly what it must not collect.

**Absent is never rendered as zero.** With hand-collected data a gap is the
normal case, and a report that shows a missing week as `0` shows a team getting
worse when it is only getting quieter.

**Self-reported data builds mappings, never counts.** Counting from it would
rank people by how carefully they write reports.

**Nothing synthesises a join key.** Where two sources cannot be linked with
evidence, the answer is "cannot attribute" — not the nearest match.

**These figures are not a performance record.** Local collection is voluntary
and reversible, which is what makes it consensual and also what makes it useless
as an audit trail. Say so before someone tries.

## Before you turn it on

Copilot's OTel spans carry prompt content, and `captureContent: false` does not
stop it — [microsoft/vscode#326254](https://github.com/microsoft/vscode/issues/326254),
open. Measured on copilot-chat 0.62.0: system instructions, full conversations
and command output all present.

`cli/otel_read.py` keeps 22 named fields and drops everything else before
storage. That is the mitigation, not a second line of defence behind a setting
that works.

## Layout

| | |
|---|---|
| `cli/` | `./insight` — the local client, stdlib only |
| `pollers/` | Jira, Bitbucket, test management |
| `importers/` | weekly bundles, daily spreadsheet reconciliation |
| `report/` | weekly Markdown, per-person workbooks |
| `collector/` | the attribute allow-list, enforced |
| `schema/` | `CONTRACT.md` — the single source of truth |
| `docs/` | [what we measure](docs/WHAT-WE-MEASURE.md), findings, architecture |

355 tests: `for s in pollers report collector cli importers; do python3 -m unittest discover -s $s/tests; done`
