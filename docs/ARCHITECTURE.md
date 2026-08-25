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
│              (NDJSON, contract events)   │  ship │             ▲             │
└──────────────────────────────────────────┘   │   └─────────────│─────────────┘
                                               ▼                 │
                                        proxy ──► S3 ──── pull.py ┘
                                     docs/TRANSPORT.md
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
./insight ship       # upload the sealed bundle -- docs/TRANSPORT.md
./insight whoami     # the allow-list line to send to whoever runs the server
./insight status    # what is buffered, what was last packed, what was sent
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

Run it once a week, then `ship`. They stay two commands because the consent model
rests on the engineer reading their own bundle before deciding to send it, and an
upload folded into `pack` would remove that without saying so.

`purge` exists because a person who cannot delete their own telemetry has not
consented to it. The file exporter helps here too: the engineer can read exactly what
Copilot recorded before deciding to hand it over.

`purge` is local, and says so when it runs: it cannot reach a bundle already
uploaded. Implying otherwise would be a worse failure of the consent model than
not offering the command at all.

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
  config.json          machine id, consent, salt, work email, upload secret (0600)
  buffer/*.ndjson      append-only, one file per day
  .reports/            packed bundles, kept after upload so they stay readable
  shipped.json         receipts: which bundle went where, and when
```

**Home directory, not the project.** A `.reports/` folder inside a working repo gets
committed by accident, ends up in a PR diff, and reaches a system nobody intended.

Each bundle carries a **manifest**: machine id, collector version, the window covered,
event counts by type, and a checksum.

---

## Collection is voluntary and per-week, which has consequences either way

`ship` replaced the person attaching a file, but none of the following changed --
they are consequences of *voluntary, per-week* collection, not of how the file
travels. They were designed for before there was a transport, which is why
adding one changed nothing below this line.

| Risk | Handling |
|---|---|
| The same week handed over twice | deterministic `event_id` — already required by `CONTRACT.md §1.3` |
| A week never handed over | the manifest declares its window; a missing window renders as **"no data"**, never as zero |
| Someone was on leave | a bundle with zero events is a *measured* zero and looks different from an absent bundle |
| Clock skew between machines | client stamps `event_time` in UTC; the collector stamps `ingested_at` on receipt; never sort by one alone |
| Bundle edited before handover | out of scope to prevent — see below |
| Bundle altered in transit | `X-Insight-Digest` over the whole file, recomputed by the proxy; the manifest's own checksum is re-verified again at import |

**On tamper-evidence:** the engineer can read and edit their bundle before sending it.
That is not a flaw to engineer away — it is what makes the collection consensual, and
it is the reason `purge` exists. But it does mean the data is **not** an audit trail
and must never be used as one. Report it as what it is: a voluntary record. Anyone who
wants to use these numbers for individual performance assessment should be told the
data does not support it, before they try.

**The "absent is not zero" rule matters most here.** With API polling, a gap is a bug.
With voluntary bundles, a gap is the normal case — someone forgets, someone is on
leave, someone joins mid-quarter. A report that renders those as `0` will show a team
getting worse when it is only getting quieter. Every aggregate carries how many machine
weeks it actually covers.

**Automation made this worse, and had to be paid for.** A bundle that never
arrived by email was visible: no email came. A bundle that never arrived over
HTTP is silence, and silence reads as zero unless something insists otherwise.
That is why `importers/pull.py` takes a roster of work emails and names who did
not report — coverage derived from bundles that *did* arrive cannot see someone
who has never sent one.

---

## Transport — built, and it changed nothing else

The prediction above held. `pack` already produced a sealed, manifested bundle, so
shipping became one `PUT` and nothing about the local design moved. The bundle
format, the allow-list, the import path and the coverage accounting are all
untouched — `importers/bundle.py` verifies a checksum written on another machine a
week earlier exactly as it did when the file arrived by email.

The full wire contract is **[`docs/TRANSPORT.md`](TRANSPORT.md)**. In brief:

- The laptop `PUT`s to a small proxy that owns the S3 credentials. No SigV4 on a
  machine full of source code, no SDK, no OAuth — the stdlib-only rule survives.
- The **proxy chooses the object key** from the authenticated identity, so one
  laptop can neither overwrite another's bundle nor list the team's.
- Identity is a work email plus a secret minted on the laptop; the server's
  `.env` holds only `sha256(secret)`. A leaked whitelist uploads nothing.
- The email is transport only. `CONTRACT.md §1.1` forbids raw addresses in
  collected data and nothing writes one into a bundle.
- `409` on a duplicate, because the key is the content digest and writes use
  `IfNoneMatch`. Idempotency is a property of the storage, not code that can rot.

Keeping the bundle format self-describing turned out to be the load-bearing
decision: the transport carries an opaque blob and needs to understand none of it.
