# `insight` — the local collector

Runs on an engineer's own machine. Python 3.9+, standard library only — no
virtualenv, no `pip install`, nothing to resolve. macOS and Ubuntu.

```bash
curl -fsSL https://aeris-insight.seta-international.com/install | sh
insight setup             # the wizard: consent, email, schedule, commit hook
insight copilot           # read ~/.copilot's session journals -- never deletes them
insight collect           # read the emit.py buffer
insight scan              # every repository Copilot has worked in
insight pack --since 2026-08-17 --until 2026-08-23
insight ship              # upload it -- docs/TRANSPORT.md
insight whoami            # the allow-list line for the server's .env
insight rotate-token      # replace the upload secret, no outage
insight status
insight purge             # delete everything collected here
```

`init` still exists underneath `setup`, for a machine being configured without a
terminal.

From a checkout it is `git clone`, then `./insight` in place of `insight` —
identical behaviour, and how anyone develops on this. The shipped artifact is a
stdlib-only zipapp built from `cli/`, `pollers/common.py` and
`collector/main.py`, so both forms run the same modules.

## Why it exists

Three signals never leave a laptop, and no amount of API polling recovers them:

| Signal | Why it is local-only |
|---|---|
| Copilot tokens, premium requests, agent name, tool and gate outcomes | Copilot CLI writes its session journal to `~/.copilot` and sends it nowhere |
| Which platform agent ran, on which task | `emit.py` writes a local NDJSON buffer |
| `AI-Run-Id` at commit time | the trailer is written before the push |

## Several repositories, none of them registered by hand

`session.start` carries `context.gitRoot`, so `discover_repos()` reads every git
tree Copilot has been run in straight out of the journals. `scan` with no
arguments covers all of them, and a deleted clone is reported without stopping
the rest. Measured 2026-08-26: **7 repositories found with nothing registered.**

`setup --repo` used to exist, repeatable, and its failure mode was structural:
the repository somebody forgot to name was the one that silently reported
nothing, and a bundle missing it looked exactly like a quiet week. Asking was
never the point — knowing which trees to walk was. `install-hook --repo` and
`scan --repo` still take an explicit path, and both remember it.

Linked worktrees come back alongside their parent, which is correct rather than
duplication: they are separate checkouts on separate branches. `scan` keys
commits by `(repo_full_name, sha)`, so a commit reachable from both yields one
event id and de-duplicates on the way into the buffer.

## Partitioned by day

The buffer is one file per day, and an event lands in the day it **happened**,
not the day it was read. A journal record written on Friday and collected on
Monday belongs in Friday; filing it under Monday moves work between weeks, and
the totals stay right while every week is wrong.

`pack --since --until` then packs exactly one window, and `--clear` removes only
the partitions that were packed. A requested window is declared in the manifest
even when it turned up nothing, because "that week was quiet" and "nobody sent
that week" are opposite findings.

## No daemon, and nothing to switch on

Copilot CLI keeps a per-session journal at
`~/.copilot/session-state/<session-id>/events.jsonl` and writes it whether or not
anything is watching. No exporter, no setting, no listening port, no background
process, nothing to keep alive.

That last property is why this replaced the OTel span exporter on 2026-08-26. The
exporter had to be configured per machine, and a machine where the setting never
landed collected **nothing** while being indistinguishable from a machine having
a quiet month. It also carries what spans never did: which agent ran
(`subagent.started.agentName`), which skill, the git context, per-edit line
counts, real tool success/failure, real gate verdicts, and **premium requests** —
the unit Copilot actually bills.

**`insight copilot` never deletes the journal.** `insight otel` truncated its span
file every run, and that was right: the file existed only because we asked for it
and it held prompts. This one is Copilot's own session history and what `/resume`
reads. Re-reading is safe instead — every `event_id` is derived from the journal
record's own uuid, so a session open for a week is read hourly and buffered once.

## Content stays behind, by allow-list

The journal is full of exactly what `CONTRACT.md §1.1` forbids collecting:
prompts, replies, file contents, command output, absolute paths carrying the
username. It never leaves the machine on its own, so the exposure is smaller than
the span stream's — but it is a larger concentration of content in one place, and
`copilot_read.py` is the only thing between it and a bundle.

So it **names the fields it keeps**, by dotted path, per event type, and drops
everything else without inspection. An exclusion list is only as good as today's
knowledge of a file format that gains keys without asking. Nested values are
refused outright: a `dict` or `list` leaf returns nothing even when its path is on
the keep-list, because free text hides in structure.

Three fields are read to classify and then discarded, the same rule the commit
subject parser follows — the shell command (which gate?), the tool error message
(which failure class?), the anchored tail of command output (which exit code?).
None of the three is stored. `file_path` is made repo-relative, and a path under
no known `gitRoot` or `cwd` is dropped rather than truncated.

Measured 2026-08-26 over 22 journals: 2,935 contract events, zero absolute paths
and zero usernames in the output.

## What it cannot see

The journal covers the **Copilot CLI / agent surface only**. VS Code's Copilot
Chat panel and inline completions write nothing to it, so they are unmeasured.
`coverage()` publishes `surfaces_not_covered` on every read and `insight copilot`
prints the block every time, not only when it looks bad — a reader who cannot see
the denominator reads a small total as light usage rather than partial
measurement. An unmeasured surface must never read as an unused one.

The same block carries `sessions_without_usage`: a session that ended without
`session.shutdown` records no usage totals at all, so its tokens are **unknowable,
not zero**. Measured 2026-08-26: 2 of 22.

## What it will not do

`CONTRACT.md §1.1` — no prompts, no responses, no source code, no diffs, no
secrets, no raw email addresses. Counts, hashes, bounded categories and paths
only.

Every event is checked against the collector's own allow-list **before it is
written**, not on ingest at the far end where it would already be too late.
This process runs on a machine full of exactly what it is forbidden to collect,
and `check_allowed()` is the only thing standing between the two. It reuses
`collector/main.py`'s list rather than restating it — a second copy is where a
leak would come from.

Commit subjects are read to classify and then discarded. They are never stored.

## Consent

`setup` records it — that is the first question it asks, before the one about an
email address, because agreeing to collection is the decision and an address is
only bookkeeping once it is made. Nothing is collected until it has. `purge`
removes every event, every bundle and optionally the config: someone who cannot
delete their own telemetry has not consented to it.

Bundles are plain NDJSON with a manifest on the first line — readable before you
send them, and readable in six months without this tool.

## `pack` and `ship` are two commands

The consent model rests on an engineer being able to read their own bundle
before deciding to send it. Folding the upload into `pack` would remove that
property without ever mentioning it, so `ship` stays a thing someone types.

`ship` never alters the bundle. A file that arrives is byte-for-byte the file
that was sealed, which is what lets `importers/bundle.py` verify a checksum
computed on another machine a week earlier.

Re-running it is safe: the proxy keys objects by content digest and writes with
`IfNoneMatch`, so a bundle it already holds comes back `409` and is reported as
*already handed over*. Someone unsure whether last week went through will run it
again, and it has to be safe when they do.

## `--token` is gone, and that is the security improvement

There used to be an `init --token` / `setup --token` that adopted a secret issued
by the server admin, for onboarding somebody whose laptop had not been touched
yet. `cli/identity.py` describes that direction as putting "the live secret
through Slack", supported "only because pre-provisioning a new joiner sometimes
needs it" — an exception, argued for as an exception.

The flag is deleted. Removing the exception means **every secret on every machine
is now one that has never travelled**, which is a stronger property than the
convenience it cost.

What it costs, stated rather than hidden: an admin can no longer pre-provision
anyone. Every engineer runs `insight setup`, mints locally, and sends the
`email:fingerprint` line. `rotate-token` remains for anyone still holding a secret
that was issued to them under the old flow — an issued secret travelled over some
channel to get here, and a minted one never does. The old one keeps working until
the server's line catches up, so nothing has to be coordinated in the same minute.

## Identity, and what it is not

`setup` asks for a work email, mints a secret that stays on the machine, and prints
`email:sha256(secret)` for the server's `INSIGHT_ALLOWED`. The secret is never
transmitted to whoever keeps that list, so the list is not a credential store.

`rotate-token` keeps the previous secret working — `ship` tries the new one and
falls back on `401`. A rotation needing both people in the same minute is a
rotation that happens once.

The email is transport identity. `CONTRACT.md §1.1` forbids raw addresses in
collected data and nothing here writes one into a bundle; it exists so a missing
week can be chased by name instead of by machine id.

## Hourly, by default

`setup` installs a launchd agent (macOS) or systemd user timer (Linux) that runs
`insight auto` once an hour. `schedule --off` removes it; `--no-schedule` at
setup never installs it.

`auto` is the same work the manual commands do, with the properties an
unattended job needs. Each is tested, because every one of these failures is
invisible for weeks:

- **Dedupes on the events, not the file.** The manifest carries `packed_at`, so
  the file changes every hour even when nothing happened. Keying on it would
  upload 24 near-identical bundles a day.
- **A quiet day still uploads once.** An empty bundle with a declared window is
  a measured zero; a day with no bundle is missing data. Collapsing the two is
  the failure `ARCHITECTURE.md` cares about most.
- **A failing step does not stop the others.** Copilot not being installed is
  not a reason to skip uploading buffered agent events.
- **A failed upload keeps its bundle** for the next run.
- **A stale lock is broken after six hours**, so one crash does not stop
  collection permanently and silently.
- **An uninitialised machine exits quietly** — the scheduler outlives
  `purge --all`.
- **Old buffer days are pruned only once a bundle covering them was uploaded.**
  Pruning on age alone turns a fixable outage into permanent data loss.
