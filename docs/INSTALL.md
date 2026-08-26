# Install

For engineers taking part in the pilot. macOS and Ubuntu. Takes a few minutes,
once.

## One command

```bash
curl -fsSL https://aeris-insight.seta-international.com/install | sh
```

It installs a launcher at `~/.local/bin/insight` and a zipapp under
`~/.local/share/seta-insight/`, then runs `insight setup`, which asks one thing:

```
Your work email:
```

Typing it is the consent; the text above the question says what is collected.
Press Enter alone to stop there and nothing is written.

Everything else is on by default: automatic collection, self-update, and a
commit hook that records which agent run produced each commit. `insight setup
--no-schedule`, `--no-auto-update` and `--no-commit-hook` opt out, and each is
reversible afterwards.

Setup then registers this machine with the endpoint by itself and says so:

```
Registered with https://aeris-insight.seta-international.com. Nothing else to do.
```

Only a fingerprint travels. The secret stays in `~/.seta-insight/` and never
leaves the machine.

If it says nobody is expecting your address yet, that is a wait, not an errand:
ask whoever runs the pipeline to add it, and every scheduled run retries until
it sticks. `insight enroll` retries on demand.

You do not register repositories. Copilot records the git root of every session,
so `insight scan` finds them.

With no terminal attached — CI, a container — the installer skips setup and says
so. Run it later, or non-interactively:

```bash
insight setup --email you@seta-international.vn --yes
insight setup --dry-run      # show what would change, write nothing
```

## What runs, and when

`setup` installs `~/.copilot/hooks/seta-insight.json`. Copilot calls it before
each tool use. The hook returns in milliseconds and detaches a collector, at most
once per 10 minutes. Uploads are batched at most once per hour.

If the hook cannot be installed, an hourly timer does the same work.

Nothing runs as a daemon and nothing listens on a port.

## What leaves the machine

Counts, hashes, model ids, repo-relative paths, and fixed categories. Never
prompts, replies, source code, diffs, file contents or secrets.

Read a bundle before it goes:

```bash
insight pack              # seal what is buffered
insight status            # what is buffered, what was sent
insight ship --dry-run    # what would be sent
insight ship              # send it
```

`pack` and `ship` are separate on purpose. Nothing is uploaded until the second.

## Sending now, and backfilling

Installing this does not start the clock. The readers see every journal already
on disk, so one command sends everything since a date:

```bash
insight backfill --since 2026-08-01
```

It runs every reader, packs the range and uploads it, and prints a line per step
including the ones that found nothing. `--no-ship` packs it and stops so you can
read it first.

To push what is already buffered without waiting for the hourly batch:

```bash
insight auto --force-ship
```

Running either twice is safe. Event ids are derived from the fact rather than
minted per run, so a day collected twice arrives as one day, and the server
answers `409` to a bundle it already holds.

## Controls

`insight help` prints all of this in the terminal.

| | |
|---|---|
| `insight status` | what is buffered, and what has been uploaded |
| `insight enroll` | register with the endpoint again |
| `insight rotate-token` | replace the upload secret; uploads keep working |
| `insight schedule --off` | stop the automatic run |
| `insight purge --yes` | delete every event and bundle held here |
| `insight purge --yes --all` | that, and forget this machine entirely |

## Upgrading

Re-run the installer. It replaces the launcher and the archive only.
`~/.seta-insight/` is untouched, so your config, secret and buffered events
survive, and an existing schedule is repointed automatically.

## Uninstalling

```bash
curl -fsSL -o install.sh https://aeris-insight.seta-international.com/install
sh install.sh --uninstall
```

Run `insight purge --yes --all` first if you want the collected data gone, since
afterwards the command is no longer there.

Three things are left behind deliberately: the `prepare-commit-msg` hooks in your
repositories (remove them yourself), `~/.copilot` (never ours), and anything
already uploaded. Ask whoever runs the pipeline to remove an uploaded bundle.

## If something fails

| what you see | what to do |
|---|---|
| `python3 not found` | macOS: `xcode-select --install`. Ubuntu: `sudo apt-get install -y python3` |
| `python3 is too old` | needs 3.9+. Install one, or `INSIGHT_PYTHON=/path/to/python3 sh install.sh` |
| `checksum mismatch` | nothing was installed. Do not retry; report it |
| `insight: command not found` | add `export PATH="$HOME/.local/bin:$PATH"` to your shell rc |
| `401 not on the whitelist` | this machine is not registered. `insight enroll` says why |
| certificate cannot be verified | on a python.org macOS build, run `/Applications/Python 3.x/Install Certificates.command` |

A failed upload does not lose the bundle. It stays in `~/.seta-insight/.reports/`
and the next run sends it.

## What this is not

These figures describe how a way of working is going. They are not a performance
record and do not support assessing anyone individually. Collection is voluntary,
you can read everything before it leaves, and you can delete what is held here.
