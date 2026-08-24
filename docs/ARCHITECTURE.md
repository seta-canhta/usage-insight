# Architecture

Two sides that never talk to each other directly.

```
┌─ engineer's machine ────────────────────┐      ┌─ SETA ──────────────────────┐
│                                          │      │                             │
│  Copilot / VS Code ──OTLP──┐             │      │  pollers/   Jira            │
│  AIEP agents ──emit.py────┐│             │      │             Bitbucket       │
│  git hook ────────────────┤│             │      │             AIO             │
│                           ▼▼             │      │                             │
│              ./insight  (python3, stdlib) │      │  importers/ daily Excel     │
│                     │                    │      │             .reports bundles│
│                     ▼                    │      │                             │
│              ~/.seta-insight/.reports/  │─────►│  collector/ ──► report/     │
│              (NDJSON, contract events)   │ weekly, by hand      │             │
└──────────────────────────────────────────┘      └─────────────────────────────┘
                                                      later: S3 instead of by hand
```

The engineer's machine holds everything the central side cannot see. The central side
holds everything the engineer's machine cannot see. Neither is sufficient alone.

---

## Why the client has to exist at all

Three signals are **only** visible locally, and no amount of API polling recovers them:

| Signal | Why it is local-only |
|---|---|
| Copilot token usage, model id, latency | OTLP export goes to an endpoint on the developer's machine |
| Which AIEP agent ran, which phase, which task | `emit.py` writes to a local NDJSON buffer |
| `AI-Run-Id` at commit time | the trailer is written before the push |

`FINDINGS.md §2` measured all three as **zero**. That is the entire reason the current
report measures engineering output and calls it AI effectiveness.

---

## Distribution — Python, cloned from the private repo

**Target: SETA internal only, macOS and Ubuntu.** No Windows path is designed, built
or tested; if that changes it is new work, not a flag.

```bash
git clone git@github.com:seta-canhta/usage-insight.git
cd usage-insight && ./insight pack
```

No package manager, no registry, no publish step, no `npx`, no `pipx`, no `uv`.

### Why Python and not Node or Go

The deciding question is not which runtime is installed. It is **how many times the
contract gets implemented.**

The client's job overlaps `emit.py` almost exactly: build the envelope, derive a
deterministic `event_id`, check the attribute allow-list, append to an NDJSON buffer.
That already exists, tested, in Python — roughly 3,200 lines across `emit.py`,
`pollers/common.py` and `collector/main.py`.

Choosing Node or Go means writing a second implementation of the same contract in
another language. The piece most likely to drift is also the safety-critical one: the
**allow-list check** is the only thing standing between a machine full of prompts,
source code and secrets and a file that gets handed to management. Two implementations
of that check, in two languages, is precisely where a leak comes from.

`PLAN.md` already argues that two editable copies of `CONTRACT.md` would drift. Two
implementations of it drift for the same reason.

| | Python | Node | Go |
|---|:--:|:--:|:--:|
| Reuses the existing contract code | **yes** | no | no |
| One implementation of the allow-list | **yes** | no | no |
| Runtime present on macOS + Ubuntu | yes | via nvm, per-user | n/a — static binary |
| Build matrix | none | none | darwin-arm64/x64, linux-x64 |
| Languages in the system | **1** | 2 | 3 |

Go's real advantage — a static binary needing no runtime — only pays when the runtime
is missing. `python3` ships with Ubuntu and arrives on macOS with the Xcode command
line tools, which every engineer already has because git needs them.

### The constraint that keeps this simple

**Standard library only. No third-party dependencies.** No virtualenv, no
`pip install`, no dependency resolution on someone else's machine, nothing to break
when a transitive package changes. Target Python 3.9 so the macOS system interpreter
works unmodified.

The existing pipeline already follows this rule — `pollers/common.py` hand-writes its
own `.env` reader rather than take a dependency. Keep it.

### A side effect worth keeping

Engineers clone the measurement repository itself, so they can read exactly what is
collected and why. A tool that measures you should be one you can read. That supports
the consent model the rest of this document depends on, and it costs nothing.

The central pipeline is the same language and the same code. There is no client/server
translation layer to keep in sync — only the parts of the repo each side runs.

---

## What the client does

```
./insight init       # consent prompt, config, point Copilot at the file exporter
./insight pack       # read the three local sources, write the weekly bundle
./insight status    # what is buffered, what was last packed
./insight purge      # delete everything collected
```

**There is no daemon.** Copilot's OTel exporter supports a `file` mode — **[E]**,
`copilot-otel-setup.md §3` — so Copilot writes its own spans to disk and the client
reads them when asked. No listening port, no background process, no lifecycle to
manage, no "is it running?" support burden. `pack` reads three local sources and
writes one file:

```
pack
 ├── Copilot's span file        (OTel file exporter)
 ├── ~/.aiep/telemetry/*.ndjson (the emit.py buffer)
 └── git log                    (markers and AI-Run-Id trailers)
```

Run it once a week before handing the bundle over.

`purge` exists because a person who cannot delete their own telemetry has not
consented to it. The file exporter helps here too: the engineer can read exactly what
Copilot recorded before deciding to hand it over.

**Verified 2026-08-24.** The exporter is configured with
`github.copilot.chat.otel.exporterType: "file"` and
`github.copilot.chat.otel.outfile: "<path>"` -- the setting is `exporterType`,
not `protocol` as `copilot-otel-setup.md` had it. It writes one JSON line per
signal to the path you name, so the location is chosen rather than discovered.

It appends without rotating, which is the disk question answered: whoever runs
`pack` should truncate the file afterwards, and the client does not yet do that.

⚠️ **The file may contain prompt content.** `captureContent: false` does not
suppress it on the span path -- microsoft/vscode#326254, open. See
`FINDINGS.md` §3.4. The mitigation is `cli/otel_read.py`, which keeps 22 named
fields and drops the rest before anything is stored.

### What it must never capture

`CONTRACT.md §1.1`, restated because the client is where the temptation lives: no
prompts, no responses, no source code, no diffs, no secrets, no error message bodies,
no raw email addresses. Counts, hashes, bounded enums, and file paths only.

The client runs on a machine full of exactly the things it is forbidden to collect.
Every field it writes must be checked against the attribute allow-list before it lands
in the buffer — not on ingest at the far end, where it is already too late.

---

## Where the data lives

```
~/.seta-insight/
  config.json          machine id, consent record, salt
  buffer/*.ndjson      append-only, one file per day
  .reports/            packed bundles ready to hand over
```

**Home directory, not the project.** A `.reports/` folder inside a working repo gets
committed by accident, ends up in a PR diff, and reaches a system nobody intended.

Each bundle carries a **manifest**: machine id, collector version, the window covered,
event counts by type, and a checksum.

---

## Manual collection is a design constraint, not a temporary shortcut

Handing files over by hand every week has consequences that must be designed for now,
because they do not become easier once S3 arrives.

| Risk | Handling |
|---|---|
| The same week handed over twice | deterministic `event_id` — already required by `CONTRACT.md §1.3` |
| A week never handed over | the manifest declares its window; a missing window renders as **"no data"**, never as zero |
| Someone was on leave | a bundle with zero events is a *measured* zero and looks different from an absent bundle |
| Clock skew between machines | client stamps `event_time` in UTC; the collector stamps `ingested_at` on receipt; never sort by one alone |
| Bundle edited before handover | out of scope to prevent — see below |

**On tamper-evidence:** the engineer can read and edit their bundle before sending it.
That is not a flaw to engineer away — it is what makes the collection consensual, and
it is the reason `purge` exists. But it does mean the data is **not** an audit trail
and must never be used as one. Report it as what it is: a voluntary record. Anyone who
wants to use these numbers for individual performance assessment should be told the
data does not support it, before they try.

**The "absent is not zero" rule matters most here.** With API polling, a gap is a bug.
With hand-collected bundles, a gap is the normal case — someone forgets, someone is on
leave, someone joins mid-quarter. A report that renders those as `0` will show a team
getting worse when it is only getting quieter. Every aggregate carries how many machine
weeks it actually covers.

---

## Migration to S3, later

Nothing about the local design changes. `pack` already produces a sealed, manifested
bundle; shipping becomes `POST` instead of a person attaching a file. Deliberately not
built now — a transport with no bundles to carry is a guess about a format that does
not exist yet.

What **is** worth doing now, because retrofitting it is expensive: keep the bundle
format self-describing (`schema_version`, window, machine id, checksum inside the file)
so a bundle found on disk in six months is still readable without the tool that made it.
