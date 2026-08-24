# Setup — 20 minutes, once

For engineers taking part in the AI effectiveness pilot. macOS and Ubuntu.

*Tiếng Việt: [`SETUP.vi.md`](SETUP.vi.md)*

**What this does:** records which platform agent ran, on which ticket, and what it
cost in Copilot tokens. All of it stays on your machine until you hand a file
over yourself.

**What it never records:** your prompts, Copilot's replies, your code, your
diffs, file contents, or anything you type. Counts, hashes and fixed categories
only. You can read every file before you send it, and delete the lot with one
command.

---

## 1 · One command

```bash
git clone git@github.com:seta-canhta/usage-insight.git
cd usage-insight
./insight setup --repo ~/work/repo-one --repo ~/work/repo-two
```

Repeat `--repo` for each repository you work in. Each one is remembered, so `scan` later needs no arguments — and the repository you would have forgotten is the one that would have silently reported nothing.

That configures VS Code, records your consent, and installs the commit hook.
Add `--dry-run` first if you want to see what it would change.

It backs up your `settings.json`, keeps every setting you already have, and
**restores the backup if the result does not parse** — so the file is either
correct or untouched. Doing this by hand went wrong three times in one
afternoon here, and none of the three failures showed up as an error.

Then **quit VS Code fully and reopen it** (not Reload Window).

## 2 · ⚠️ Know what `captureContent` does not do

**It does not stop your prompts reaching the file.** That is a known, open VS
Code defect — [microsoft/vscode#326254](https://github.com/microsoft/vscode/issues/326254):
*"the log and metric paths honor the setting; the span path does not."* Still
reproducing on copilot-chat 0.62.0.

So `~/.seta-insight/copilot-spans.jsonl` **may contain your prompts, Copilot's
replies, and the output of commands it ran.** Treat it as you would your own
notes.

Two things protect the data that leaves your machine:

- `maxAttributeSizeChars: 256` truncates long attributes, gutting the
  content-bearing ones while leaving ids and token counts intact.
- The collector reads only 22 named fields. Everything else is dropped before
  anything is stored, a test asserts it, and `./insight otel` empties the raw
  file once it has been read.

**Read that file yourself** before the first `pack`. It is JSON lines. If
something is in there that should not be, say so before sending anything.

## 3 · Link your commits to the agent that made them

In each repository you work in:

```bash
cd ~/path/to/qa-automation
~/usage-insight/insight install-hook --repo .
```

This adds a line like `AI-Run-Id: run_abc123` to the end of commit messages made
while an agent is running. It never touches your commit subject, and it can
never fail a commit — if anything goes wrong it does nothing and gets out of the
way.

It refuses to overwrite a `prepare-commit-msg` hook you already have. If it
stops with that message, tell us rather than forcing it.

---

## Every Friday — about a minute

```bash
cd ~/usage-insight
./insight otel                                   # Copilot's tokens
./insight collect                                # which agent, which ticket
./insight scan      # every repository you have registered
./insight pack --since 2026-08-17 --until 2026-08-23
```

`otel` empties Copilot's span file after reading it — it grows without limit
otherwise, and it is the file that may hold your prompts.

`pack` prints the path of one file under `~/.seta-insight/.reports/`. Send that
file.

**Open it first if you want to.** It is plain text: a summary line, then one
line per event. Nothing is hidden and nothing is compressed.

**A quiet week still needs its file.** A week with no events is a real zero; a
week with no file is missing data, and the report has to be able to tell those
apart. Sending an empty bundle is a useful answer.

---

## Your controls

| | |
|---|---|
| `./insight status` | what is buffered right now |
| `./insight purge --yes` | delete every event and bundle |
| `./insight purge --yes --all` | that, and forget this machine entirely |
| Turn it all off | set `otel.enabled` back to `false` and stop running `pack` |

## What this is not

These figures describe how a way of working is going — whether AI-assisted work
is accepted at review, whether tests get run, what a session costs. They are
**not a performance record**, and they do not support assessing anyone
individually. Collection is voluntary, you can read everything before it leaves
your machine, and you can delete it at any point. That is deliberate, and it is
also the reason the data could never serve as an audit trail.

Questions, or anything in a bundle that looks wrong: ask before sending it.
