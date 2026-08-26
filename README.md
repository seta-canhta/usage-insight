# usage-insight

Measures how much AI assistance contributes to QA test automation, by reading
systems that already record the work: Copilot's own journals on the laptop,
Jira, Bitbucket, and AIO TCMS.

Output is one Markdown report a week, written to `reports/<week>/`. That
directory is gitignored -- the reports name individuals and carry live issue
keys.

The ten metrics it exists to produce, and which ones are actually live today,
are in [`docs/METRICS.md`](docs/METRICS.md). Read that before quoting a number.

## Install

On an engineer's laptop, one command:

```bash
curl -fsSL https://aeris-insight.seta-international.com/install | sh
```

It installs, then asks for a work email and nothing else. The machine registers
itself with the endpoint -- no fingerprint to relay, no admin to wait for, as
long as the address is on the roster. Automatic collection, self-update and the
commit hook are on by default; each has a flag to turn it off.

Python 3.9+, standard library only. The whole client is one zipapp.

`insight help` explains what is collected and how to stop it, from the terminal.
Longer version: [`docs/INSTALL.md`](docs/INSTALL.md).

Running the pipeline instead: [`docs/OPERATE.md`](docs/OPERATE.md).

## What it collects

From the laptop:

| Source | What is read |
|---|---|
| `~/.copilot/session-state/*/events.jsonl` | premium requests, token counts, model ids, tool names and verdicts |
| VS Code `chatSessions/*.jsonl` | per-call latency, tool names, prompt/output tokens |
| `rtk` history, where installed | per-command token counts |
| git, in repos Copilot has worked in | commit hashes, line counts, AI trailers |

From the servers: Jira issues and transitions, Bitbucket PRs and reviews, AIO
test cycles and runs.

Collection runs when Copilot runs. `insight setup` installs a hook at
`~/.copilot/hooks/seta-insight.json` that Copilot calls before each tool use.
The hook returns in milliseconds and detaches a collector, debounced to 10
minutes. Uploads are batched hourly. An hourly timer stays as a fallback for
machines where the hook cannot be installed.

Field-by-field definitions: [`schema/CONTRACT.md`](schema/CONTRACT.md).

## What it never collects

- **Content.** No prompts, replies, source, diffs, file contents. Readers name
  the fields they keep and never look at the rest.
- **Absolute paths.** Paths are repo-relative, so no username ships.
- **Raw emails.** Only a salted hash.
- **Self-reported counts.** The daily sync sheet maps person to team to ticket.
  It is never a source for a number.
- **Client-side cost.** `premium_requests` is measured on the laptop; `cost_usd`
  is derived in the warehouse against dated pricing.

A gap is never rendered as zero. VS Code stores no token counts for some calls,
so those fields stay NULL.

## Layout

| | |
|---|---|
| `cli/` | `insight`, the local client |
| `common/` | shared library the other packages import |
| `pollers/` | Jira, Bitbucket, AIO, CI |
| `importers/` | weekly bundles, daily reconciliation |
| `report/` | weekly Markdown, per-person workbooks |
| `collector/` | ingest service; enforces the attribute allow-list |
| `server/` | collection endpoint; S3 behind one PUT |
| `schema/CONTRACT.md` | the single source of truth |
| `sql/` | the warehouse |

## Tests

```bash
for s in pollers report collector cli importers server; do
  python3 -m unittest discover -s $s/tests
done
python3 -m unittest discover -s tests
```
