# `./insight` — the local collector

Runs on an engineer's own machine. Python 3.9+, standard library only — no
virtualenv, no `pip install`, nothing to resolve. macOS and Ubuntu.

```bash
git clone git@github.com:seta-canhta/usage-insight.git
cd usage-insight
./insight init            # consent, machine id, salt
./insight otel            # read Copilot's span file, then truncate it
./insight collect         # read the emit.py buffer
./insight scan            # every registered repository
./insight pack --since 2026-08-17 --until 2026-08-23
./insight ship            # upload it -- docs/TRANSPORT.md
./insight whoami          # the allow-list line for the server's .env
./insight rotate-token    # replace the upload secret, no outage
./insight status
./insight purge           # delete everything collected here
```

## Why it exists

Three signals never leave a laptop, and no amount of API polling recovers them:

| Signal | Why it is local-only |
|---|---|
| Copilot tokens, model, latency | its OTel exporter writes on the developer's machine |
| Which platform agent ran, on which task | `emit.py` writes a local NDJSON buffer |
| `AI-Run-Id` at commit time | the trailer is written before the push |

## Several repositories

`install-hook` and `scan --repo` remember the repository. After that `scan` with
no arguments covers every one of them, and a deleted clone is reported without
stopping the rest.

An engineer with four repositories should not have to remember four commands:
the one they forget is the one that silently reports nothing.

## Partitioned by day

The buffer is one file per day, and an event lands in the day it **happened**,
not the day it was read. A span produced on Friday and collected on Monday
belongs in Friday; filing it under Monday moves work between weeks, and the
totals stay right while every week is wrong.

`pack --since --until` then packs exactly one window, and `--clear` removes only
the partitions that were packed. A requested window is declared in the manifest
even when it turned up nothing, because "that week was quiet" and "nobody sent
that week" are opposite findings.

## No daemon

Copilot's exporter has a `file` mode, so it writes its own spans to disk and
`pack` reads them. No listening port, no background process, nothing to keep
alive. Run `pack` once a week before handing the bundle over.

Configure it with `otel.exporterType: "file"` and `otel.outfile: "<path>"` —
the setting is `exporterType`, not `protocol`. It appends and never rotates, so
`insight otel` truncates the file after reading it. The raw file can hold
prompts (microsoft/vscode#326254), and keeping copies multiplies that exposure
for no gain once the events exist; `--keep-raw` is there if you want it and is
a deliberate choice.

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

`init` records it, and refuses to collect anything until it has. `purge`
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

## Identity, and what it is not

`setup --email` mints a secret that stays on the machine, and prints
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
