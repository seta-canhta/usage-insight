# Install and send data

One command. Takes about a minute.

```bash
curl -fsSL https://aeris-insight.seta-international.com/install | sh
```

It asks one thing:

```
Your work email:
```

Use your work address — `ngoc.nguyen@aeris.net`, `linh.hoang@aeris.net`,
`thao.nguyen@aeris.net`. Then it prints:

```
Registered with https://aeris-insight.seta-international.com. Nothing else to do.
```

That is the whole setup. Nothing to send anyone, nothing to wait for.

## Send what is already on your machine

Your Copilot history goes back as far as it goes, and installing this does not
start the clock. One command sends everything from a date:

```bash
insight backfill --since 2026-08-01
```

It prints a line per source, including the ones that found nothing:

```
[  ok  ] copilot   {"events": 0, "present": true}
[  ok  ] vscode    {"events": 12536, "sessions": 937}
[  ok  ] scan      {"commits": 453}
[  ok  ] pack      {"event_count": 485}
```

Add `--no-ship` first if you want to look before it goes.

After that it runs by itself — collection fires when you use Copilot, uploads
are batched hourly. Nothing else to remember.

## Check it

```bash
insight status     # what is buffered, what has been sent
insight help       # what is collected, and how to stop it
```

## What it collects

Counts, hashes, model ids, tool names, commit hashes, and paths relative to the
repository.

**Never** your prompts, Copilot's replies, your code, your diffs, file contents,
or secrets. Your email is stored as a hash and travels only in the upload
header. Your username never leaves — paths are repo-relative.

You can read any bundle before it is sent (`insight pack`, then look in
`~/.seta-insight/.reports/`), and delete everything held locally with
`insight purge --yes`.

These numbers describe how a way of working is going. They are not a
performance record.

## If something goes wrong

| what you see | what to do |
|---|---|
| `insight: command not found` | add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc`, open a new terminal |
| `python3 not found` | macOS: `xcode-select --install`. Ubuntu: `sudo apt-get install -y python3` |
| `not expected by this endpoint` | your address is not on the roster yet — tell Canh, then `insight enroll` |
| `already enrolled with a different machine` | a previous machine claimed it — tell Canh to reset it |
| an upload fails | nothing is lost. The bundle stays in `~/.seta-insight/.reports/` and the next run sends it |

Anything in a bundle that looks wrong: ask before sending it.
