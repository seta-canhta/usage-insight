# Setup — 10 minutes, once

For engineers taking part in the AI effectiveness pilot. macOS and Ubuntu.

*Tiếng Việt: [`SETUP.vi.md`](SETUP.vi.md)*

**What this does:** records which platform agent ran, on which ticket, and what it
cost in Copilot tokens and premium requests. All of it stays on your machine
until it is uploaded — hourly if you say yes, or when you type `ship` if you
would rather.

**What it never records:** your prompts, Copilot's replies, your code, your
diffs, file contents, or anything you type. Counts, hashes and fixed categories
only. You can read every file before you send it, and delete the lot with one
command.

**What it cannot see:** the VS Code Copilot **Chat panel** and inline
completions. The source is Copilot CLI's own session journal, and those two
surfaces write nothing to it. Every read says so out loud — see §2.

---

## 0 · Already collecting? Re-run the installer

If this machine has been running `insight` for a while — from a clone, or from an
earlier install — you do not start over.

```bash
curl -fsSL https://aeris-insight.seta-international.com/install | sh
insight status
```

**Nothing already collected is disturbed.** `~/.seta-insight/` is untouched by
the installer: your config, your machine id, your salt and your upload secret all
survive, so the events you have already buffered stay buffered and your line on
the server's whitelist keeps working. An existing hourly schedule is repointed at
the new binary automatically — you do not have to re-run `schedule`.

You do not need `insight setup` again unless `status` says something is missing.

**One thing did change, and it is worth knowing.** Local usage used to come from
Copilot's OTel span exporter, configured per machine through
`github.copilot.chat.otel.*` settings in VS Code. It now comes from Copilot CLI's
own session journal at `~/.copilot`, which is written whether or not anything is
watching. If you had those settings, `insight setup` **removes** them — a retired
exporter left switched on keeps writing prompts to a file nobody reads.

There is no longer any `--token`. If you were ever sent a secret over chat,
`insight rotate-token` replaces it with one that has never travelled.

---

## 1 · Two commands

```bash
curl -fsSL https://aeris-insight.seta-international.com/install | sh
insight setup
```

The first installs a single stdlib-only Python zipapp: a launcher at
`~/.local/bin/insight` and the archive under `~/.local/share/seta-insight/`.
Nothing is written to `~/.seta-insight/` — that is `insight setup`'s to create.
The second is a short conversation — four questions, about a minute:

```
$ insight setup

This sets up collection on this machine. Four questions.

This collects, from this machine:

  - which platform agent ran, on which ticket, and how long it took
  - Copilot token counts, premium requests, model ids
  - commit hashes, line counts, and AI provenance markers

It never collects prompts, responses, source code, diffs, file contents, or
secrets. Counts, hashes and fixed categories only.

**This machine will upload on its own, every hour.** ...

Collect telemetry from this machine? [y/N] y

Your work email: you@seta-international.vn

Collect and upload once an hour, automatically? [Y/n] y

Copilot has worked in 7 repositories on this machine.
A commit hook can stamp which agent run produced each commit.
Without it, cost per accepted output cannot be computed.
Install it? [y/N] y
```

Add `--dry-run` first if you want to see what it would change and write nothing.
`--email you@seta-international.vn --yes` skips the questions, for anyone
scripting it.

**Nothing is registered by hand.** Copilot writes the git root of every session
it starts, so `scan` discovers the repositories you have actually worked in —
that is where the "7 repositories" line above comes from. There used to be a
`--repo` flag you had to repeat, and its failure mode was structural: the
repository you forgot to name was the one that silently reported nothing, and a
bundle missing it looked exactly like a quiet week. `scan --repo` and
`install-hook --repo` still take an explicit path when you want one.

`setup` configures VS Code, records your consent, and installs the commit hook if
you say yes. It backs up your `settings.json`, keeps every setting you already
have, and **restores the backup if the result does not parse** — so the file is
either correct or untouched. Doing this by hand went wrong three times in one
afternoon here, and none of the three failures showed up as an error.

Then **quit VS Code fully and reopen it** (not Reload Window).

### The installer installs, and stops

It never runs `setup` for you. That looks like an omission, so here is why it is
not one.

Piping a script into `sh` means there is **no terminal attached**. `setup` cannot
ask you anything in that situation, so running it from the installer would mean
running it with `--yes` — which records a consent decision nobody was shown. A
consent record obtained by a flag on a pipe is not a consent record.

And the whitelist line `setup` prints at the end is the one thing here a person
has to read and act on. Text at the tail of a piped installer is text people
scroll past. Two commands puts it in front of someone who is looking at it.

### If you would rather not pipe a script into `sh`

`curl | sh` is trust-on-first-use over TLS: you are trusting that the host is who
DNS and the certificate say it is, and that what it served is what we published.
That is a real assumption, and it is worth one paragraph rather than a footnote.

The two-step form lets you check the script before it runs:

```bash
curl -fsSL -o install.sh https://aeris-insight.seta-international.com/install
shasum -a 256 install.sh
# compare against the digest published below, then:
sh install.sh
```

> **Published digest of `install.sh`:**
> `PLACEHOLDER — generated at release time, fill in when the artifact is cut`

Either way the script itself verifies what it downloads: the `insight.pyz`
artifact comes from GitHub Releases on the public `seta-canhta/usage-insight`
repo and is checked against a SHA-256 embedded in the install script. **On a
mismatch it refuses to install rather than installing something unverified.**
Read the script — it is short, and reading it is the point of the two-step form.

### One line to send

Setup finishes by printing a line like this:

```
    you@seta-international.vn:9f2ac41e8b...c3d1
```

**Send it to whoever runs the pipeline.** It goes into the server's allow-list,
and `insight ship` will refuse to upload until it is there.

That line is a hash, not a password — safe to paste in chat. Your actual upload
secret is generated on this machine, stays in `~/.seta-insight/config.json`, and
is never sent to anyone, including the person maintaining the allow-list.

Lost it? `insight whoami` prints it again.

**There is no way to be given a secret any more,** and that is deliberate. The
old `setup --token` let an admin mint a secret and send it to you, so that
somebody could be pre-provisioned before their laptop had been touched. It also
put a live credential through Slack. Removing the flag removes the exception:
every secret on every machine is now one that has never travelled anywhere. The
cost is that nobody can be set up before they run `setup` themselves — every
engineer mints locally and sends the `email:fingerprint` line.

Your email is used for one thing: telling the server who is uploading, so a
missing week can be chased by name. **It is never written into a bundle** — the
data itself carries no email addresses at all.

## 2 · Know what is on your machine, and what leaves it

Copilot CLI keeps a journal of every session at
`~/.copilot/session-state/<session-id>/events.jsonl`. It writes it for its own
`/resume`, whether or not anything is watching, and it never sends it anywhere.

**That file is mostly content.** Your prompts, Copilot's replies, file contents,
the output of every command it ran, and absolute paths carrying your username —
all of it sits next to the numbers. It is your file. Treat it as you would your
own notes.

What protects the data that *leaves* your machine is that the reader **names the
fields it keeps** rather than excluding the ones it knows about:

- 67 named fields in total, by exact path, across 16 kinds of journal record.
  Everything else is dropped without ever being looked at. An exclusion list is only as good as
  today's knowledge of a file format that gains new keys without asking.
- Anything structured is refused outright — free text hides inside structure.
- Three things are read to *classify* and then thrown away: the shell command
  (which gate was this?), a tool's error message (which kind of failure?), and
  the tail of a command's output (what exit code?). None of the three is stored.
- File paths are made repo-relative. A path that sits under no repository you
  work in is dropped, not shortened.
- Before anything is written, every event is re-checked against the collector's
  own allow-list, and the whole read is refused if one field is outside it.

Measured across 22 real journals on 2026-08-26: 2,935 events produced, and
**zero absolute paths and zero usernames** in the output.

**`insight copilot` never deletes your journal.** The command it replaced emptied
Copilot's span file after every read, because that file existed only because we
asked for it. This one is Copilot's own history of your work and what `/resume`
reads back. Re-reading it is safe: events are keyed so a session read every hour
for a week is still stored once.

### What it cannot see

The journal covers the **Copilot CLI and agent surface only**. The VS Code Chat
panel and inline completions write nothing to it. If you work mostly in the Chat
panel, most of your AI usage is currently **unmeasured**.

That is a limit, not a result, and every run says so:

```json
"coverage": {"sessions": 22, "sessions_with_usage": 20,
             "sessions_without_usage": 2, "usage_coverage": 0.909,
             "surfaces_not_covered": ["vscode-copilot-chat", "inline-completions"]}
```

`sessions_without_usage` is the other half of the same honesty. A session that
ended without a clean shutdown — crashed, killed, or still open — records no
usage totals at all. Its tokens are **unknowable, not zero**, and nothing
downstream may render them as `0`.

## 3 · Link your commits to the agent that made them

`setup` offers to do this for every repository Copilot has worked in. To add one
later:

```bash
cd ~/path/to/qa-automation
insight install-hook --repo .
```

This adds a line like `AI-Run-Id: run_abc123` to the end of commit messages made
while an agent is running. It never touches your commit subject, and it can
never fail a commit — if anything goes wrong it does nothing and gets out of the
way.

It is the one thing discovery cannot replace: that trailer is the only evidence
that links a commit to a specific agent run, and without it cost per accepted
output cannot be computed at all.

It refuses to overwrite a `prepare-commit-msg` hook you already have. If it
stops with that message, tell us rather than forcing it.

---

## It runs by itself, when you use Copilot

`setup` installs a Copilot hook. When Copilot runs a tool, the hook fires,
returns in milliseconds, and hands collection off in the background — at most
once every ten minutes, so a busy hour costs one run and a quiet hour costs
nothing.

Uploads are **batched separately**, at most hourly, and only if something
changed. Collection is frequent because the evidence perishes: a workspace
folder deleted after a branch merges takes its history with it. Uploading is
not, because a stream of nearly-empty bundles helps nobody.

| | |
|---|---|
| `insight schedule --status` | is it on, and when did it last run |
| `insight schedule --off` | stop the hook and the timer both |
| `~/.seta-insight/auto.log` | one line per run, including anything that failed |

Nothing runs as root and nothing is installed outside your home directory.

Everything that was ever uploaded stays readable in
`~/.seta-insight/.reports/`, so you can still open any bundle after the fact.
`purge` still deletes everything held on your machine.

---

## If you would rather do it by hand

```bash
insight copilot                                  # Copilot's own session journal
insight collect                                  # which agent, which ticket
insight scan        # every repository Copilot has worked in
insight pack --since 2026-08-17 --until 2026-08-23
insight ship        # upload it
```

`insight setup --no-schedule` opts out of the hourly run, and then it is yours to
remember.

`copilot` reads and does not delete: your session journals stay exactly as
Copilot left them, and re-running it does not double-count anything.

`pack` writes one file under `~/.seta-insight/.reports/` and `ship` uploads it.
They are two commands on purpose: nothing leaves your machine until you type the
second one.

**Open it first if you want to.** It is plain text: a summary line, then one
line per event. Nothing is hidden and nothing is compressed. `insight ship
--dry-run` shows what would be sent without sending it.

Running `ship` twice is safe — the server recognises a bundle it already has and
says so. If you are unsure whether last week went through, just run it again.

**A quiet week still needs its bundle.** A week with no events is a real zero; a
week with no bundle is missing data, and the report has to be able to tell those
apart. `pack --since ... --until ...` records the week you meant even when
nothing happened in it, which is why those dates are worth typing.

### From a checkout

The install is not the only way to run this. A clone works exactly as it always
did, and is how anyone develops on it:

```bash
git clone git@github.com:seta-canhta/usage-insight.git
cd ~/usage-insight
./insight copilot
```

Same code, same behaviour, no checksum to trust. Everything in this document
works with `./insight` in place of `insight`.

---

## Upgrading

Re-run the one-liner:

```bash
curl -fsSL https://aeris-insight.seta-international.com/install | sh
```

It replaces the launcher and the archive and nothing else. Your config, machine
id, salt, upload secret, buffered events and past bundles are all in
`~/.seta-insight/`, which the installer never writes to. An hourly schedule is
repointed at the new binary automatically, so there is nothing to re-enable.
`insight status` afterwards confirms it.

Already on the current version? It says so and stops. `--force` reinstalls
anyway; `--prefix DIR` installs somewhere other than `$HOME/.local`.

From a checkout, upgrading is `git pull`.

## Uninstalling

```bash
curl -fsSL -o install.sh https://aeris-insight.seta-international.com/install
sh install.sh --uninstall
```

That removes the launcher, the archive and the hourly schedule. It deliberately
leaves three things behind, because deleting them silently would be the wrong
default:

1. **The commit hooks.** Remove `prepare-commit-msg` from each repository's
   `.git/hooks/` yourself — the installer never edits a repository you own.
2. **Your collected data.** `insight purge --yes` deletes every event and bundle
   held on this machine; `insight purge --yes --all` also forgets the machine
   entirely, including the config and the upload secret. Run these **before**
   uninstalling if you want them gone, since afterwards the command is no longer
   there.
3. **Copilot's own session journals.** They were never ours; `~/.copilot` is
   left exactly as it was.

Removing your line from the server's whitelist is a request to whoever runs the
pipeline. A bundle you already uploaded is already uploaded.

---

## Your controls

| | |
|---|---|
| `insight status` | what is buffered, and what has been uploaded |
| `insight ship --dry-run` | what would be sent, without sending it |
| `insight whoami` | your allow-list line, again |
| `insight rotate-token` | replace your upload secret (uploads keep working) |
| `insight purge --yes` | delete every event and bundle held here |
| `insight purge --yes --all` | that, and forget this machine entirely |
| Turn it all off | `insight schedule --off`, then `insight purge --yes --all` |

`purge` is a local command. A bundle you already uploaded is already uploaded —
ask whoever runs the pipeline if you need one removed.

### If the install fails

| what you see | what it means |
|---|---|
| *python3 not found* | macOS: `xcode-select --install` puts one there. Ubuntu: `sudo apt-get install -y python3`. The installer prints whichever applies rather than guessing at another interpreter |
| *python3 is too old* (< 3.9) | every `python3` it found is older than the client targets. Install a newer one and re-run, or name one directly with `INSIGHT_PYTHON=/path/to/python3 sh install.sh`. It will not silently fall back to an unsupported interpreter |
| *checksum mismatch* | what was downloaded is not what we published. **Nothing is installed.** Do not retry past it — tell whoever runs the pipeline. It is either a corrupted download or something that needs looking at |
| `insight: command not found` after a successful install | `~/.local/bin` is not on your `PATH`. Add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc` (macOS) or `~/.bashrc` (Ubuntu) and open a new terminal. The installer prints this too, at the end |

### If an upload fails

`ship` says which of these it was, and none of them lose the bundle — it stays
in `~/.seta-insight/.reports/` and the next run sends it.

| what it says | what it means |
|---|---|
| *not on the whitelist* (`401`) | your line has not reached the server yet, or you rotated and the old secret has expired. `insight whoami` prints it again |
| *did not reach the endpoint* | something in front of the server — a CDN or proxy — refused the request. Nothing on your machine needs changing; tell whoever runs the pipeline |
| *a certificate this machine cannot verify* | your Python has no CA certificates. On a python.org install for macOS, run `/Applications/Python 3.x/Install Certificates.command` |
| *unreachable after 3 attempts* | the network, or the server is down. It will go with the next hourly run |

## What this is not

These figures describe how a way of working is going — whether AI-assisted work
is accepted at review, whether tests get run, what a session costs. They are
**not a performance record**, and they do not support assessing anyone
individually. Collection is voluntary, you can read everything before it leaves
your machine, you can stop the hourly run at any time, and you can delete what is
held here. That is deliberate, and it is also the reason the data could never
serve as an audit trail.

Questions, or anything in a bundle that looks wrong: ask before sending it.
