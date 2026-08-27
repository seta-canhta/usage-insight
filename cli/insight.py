#!/usr/bin/env python3
"""``insight`` -- the local collector that runs on an engineer's own machine.

Three signals never leave a developer's laptop, and no amount of API polling
recovers them: what Copilot actually spent (it journals every session under
``~/.copilot``), which platform agent ran, and the ``AI-Run-Id`` trailer written
at commit time. This reads those and packs them into one bundle to hand over.

**There is no daemon.** Copilot CLI writes its own session journal whether or
not anything is watching -- no exporter, no setting, nothing listening -- and
this reads it when asked. ``pack`` is a batch read of local files, run once a
week.

Standard library only, Python 3.9+. No virtualenv, nothing to install, nothing
to resolve on someone else's machine.

    ./insight init         consent, machine id, salt
    ./insight install-hook write AI-Run-Id trailers at commit time
    ./insight scan         read git history in a repository
    ./insight copilot   read Copilot's own session journals
    ./insight collect   read the emit.py buffer
    ./insight pack      seal a bundle to hand over
    ./insight ship      send a sealed bundle to the collection endpoint
    ./insight whoami    the whitelist line to send to the server admin
    ./insight schedule  collect hourly instead of remembering to
    ./insight auto      one unattended run, for the scheduler
    ./insight rotate-token  mint a new upload secret
    ./insight status    what is buffered, what was packed
    ./insight purge     delete everything collected

The contract is shared with the central pipeline rather than reimplemented --
``pollers/common.py`` builds the envelope and ``collector/main.py`` owns the
attribute allow-list. A second implementation of that allow-list, in this
process that sits on a machine full of source code and secrets, is exactly
where a content leak would come from.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, os.path.join(_ROOT, "collector")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import common  # noqa: E402  (path set above)
import main as collector_main  # noqa: E402
import version as version_mod  # noqa: E402


def _archive() -> Optional[str]:
    """The ``.pyz`` this is running from, or None in a checkout.

    ``_ROOT`` is whatever sits above ``cli/``. In a checkout that is the
    repository and it is a directory; inside a zipapp it is the archive file
    itself, because ``zipimport`` lets a ``sys.path`` entry run straight through
    a file into it. So one ``os.path.isfile`` answers "am I packaged", with no
    build-time flag that could fall out of step with reality.
    """
    return _ROOT if os.path.isfile(_ROOT) else None


def _read_bundled(path: str) -> str:
    """Read a file that ships beside this module -- checkout or zipapp.

    In a checkout ``path`` is a real file. Inside ``insight.pyz`` it is not:
    the name looks like a path, but there is no directory called
    ``insight-0.3.0.pyz`` and ``open()`` raises ``NotADirectoryError``.
    ``zipimport`` hands the bytes over through the loader that imported this
    module, keyed by that same name, so one extra call covers both.

    Not ``importlib.resources``: ``read_text`` was removed in 3.13 and
    ``files()`` needs a package anchor. ``cli/`` is a directory on ``sys.path``,
    not a package, and making it one would break every ``import insight`` in
    the test suite.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        get_data = getattr(globals().get("__loader__"), "get_data", None)
        if get_data is None:
            raise
        return get_data(path).decode("utf-8")


def _executable() -> str:
    """The absolute path a scheduler should invoke to run ``auto`` hourly.

    Three answers, in the order they can be trusted:

    ``$INSIGHT_EXE`` -- exported by the launcher ``install.sh`` writes. That
    launcher is the only thing that knows both where it put itself and which
    interpreter it validated.

    ``<checkout>/insight`` -- the bash wrapper, when this is a git clone. Still
    correct there, and it is what the plists already in the field carry.

    ``sys.argv[0]``, made absolute. Last, and absolute for a reason: a launchd
    agent runs with ``PATH=/usr/bin:/bin:/usr/sbin:/sbin``, so a bare name here
    would resolve to something else or to nothing.

    Getting this wrong fails in the worst available way -- ``schedule --hourly``
    reports success, launchd accepts the plist, and the job then fires every
    hour forever against a path that does not exist, with nobody watching.
    """
    exe = os.environ.get("INSIGHT_EXE")
    if exe:
        return os.path.abspath(exe)
    checkout = os.path.join(_ROOT, "insight")
    if _archive() is None and os.path.isfile(checkout):
        return checkout
    return os.path.abspath(sys.argv[0])


def version_line() -> str:
    """Everything a support question needs, in one pasteable line.

    The digest is of the file actually running, not of a build recorded
    somewhere else. ``0.3.0`` answers *which release*; the digest answers
    *whether this laptop has that release's bytes*, and those two come apart
    exactly when it matters -- a half-finished upgrade, a copy taken from a
    colleague, an archive edited to see what would happen.
    """
    path = _archive() or os.path.abspath(__file__)
    try:
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()[:12]
    except OSError:
        digest = "unreadable"
    return "insight {} (schema {}, python {}.{}.{}, {} sha256:{})".format(
        version_mod.VERSION, common.SCHEMA_VERSION,
        sys.version_info[0], sys.version_info[1], sys.version_info[2],
        path, digest)

HOME = os.environ.get("SETA_INSIGHT_HOME") or os.path.join(
    os.path.expanduser("~"), ".seta-insight"
)  # everything this tool ever writes lives under here, and nowhere else
CONFIG_PATH = os.path.join(HOME, "config.json")
BUFFER_DIR = os.path.join(HOME, "buffer")
REPORTS_DIR = os.path.join(HOME, ".reports")
#: Which bundles have already gone, so `ship` does not resend megabytes and
#: `status` can answer "did last week get through?" without a network call.
RECEIPTS_PATH = os.path.join(HOME, "shipped.json")

#: Where ``emit.py`` writes, per ai-engineering-platform.
EMIT_BUFFER = os.path.join(os.path.expanduser("~"), ".aiep", "telemetry")

BUNDLE_FORMAT = "seta-insight-bundle/1"

#: Where `ship` uploads unless told otherwise. Defaulted rather than asked for,
#: because an engineer who is never told about `--endpoint` collects diligently
#: for a month and then finds out nothing ever arrived. `--endpoint` overrides
#: it and `--no-endpoint` opts out into handing bundles over by hand.
#: SETA_INSIGHT_ENDPOINT exists so tests and a staging proxy do not have to
#: patch the module.
SETA_ENDPOINT = "https://aeris-insight.seta-international.com"
DEFAULT_ENDPOINT = os.environ.get("SETA_INSIGHT_ENDPOINT") or SETA_ENDPOINT

#: What this machine collects, in the words of somebody being asked to agree to
#: it. Kept honest deliberately and at some cost: it named "latency" for a
#: fortnight after the source stopped carrying it, and a consent record that
#: describes the wrong thing is not a weaker consent record, it is a false one.
#: Anything added to what `copilot`, `scan` or `collect` read has to appear
#: here in the same commit.
CONSENT_TEXT = """
This collects, from this machine:

  - which agent ran, on which ticket, and how long it took
  - how much Copilot spent: token counts, model ids, premium requests
  - which tools and quality gates ran, and whether they passed
  - files an agent changed, by path and line count -- never their contents
  - commit hashes, line counts, and AI provenance markers

It reads Copilot's own session journal in ~/.copilot, which it never alters or
deletes, and takes named fields out of it. Everything else in that file --
prompts, replies, source code, command output -- is dropped without being
stored, and a second check refuses the whole batch if anything unnamed slips
through.

It never collects prompts, responses, source code, diffs, file contents, or
secrets. Counts, hashes and fixed categories only. File paths are recorded
relative to the repository, so your home directory and username are not.

It does not see everything. Copilot Chat in the VS Code panel, and inline
completions, write nothing to that journal and are not collected. Every upload
says so, so that work nobody measured is never read as work nobody did.

{transport}

These figures describe how a way of working is going. They are not a
performance record and do not support individual assessment.
"""

#: Said differently depending on whether an endpoint is configured, because the
#: promise is different. "Nothing is sent anywhere" is true of a machine with no
#: endpoint and false of one with `ship` wired up, and a consent text that is
#: false in the second case is worse than no consent text at all.
TRANSPORT_MANUAL = """Nothing is sent anywhere. Everything stays in {home} until you run `pack`
and hand the bundle over yourself. You can read any bundle before you send it,
and `purge` deletes everything at any time."""

TRANSPORT_ENDPOINT = """Everything stays in {home} until you run `ship`, which uploads a sealed
bundle to:

  {endpoint}

`ship` never runs on its own -- nothing leaves this machine until you type it,
and you can read any bundle first. `purge` deletes everything held here; a
bundle you have already sent is already sent, and removing that is a request to
whoever runs the pipeline."""

#: The honest version of the above once collection is scheduled. Setup turns
#: hourly upload on by default, so the consent text has to lead with that --
#: describing a manual handover to someone whose machine will upload on its own
#: is not a consent record, it is a wrong one.
TRANSPORT_HOURLY = """**This machine will upload on its own, every hour.** A sealed bundle of the
day's events goes to:

  {endpoint}

Bundles are kept in {home} so you can read anything that was sent, `status`
lists every upload, and nothing is uploaded in an hour where nothing changed.

You are not locked in: `insight schedule --off` stops the hourly run and
returns this to upload-only-when-you-say-so, and `purge` deletes everything
held here. A bundle already sent is already sent -- removing that is a request
to whoever runs the pipeline."""

#: Appended to the hourly text when this machine may also replace its own
#: binary. Separate constant, and only added when the answer was yes, because a
#: consent record that describes self-updating on a machine that does not do it
#: is as wrong as one that omits it on a machine that does.
TRANSPORT_AUTO_UPDATE = """
Once a day it will also ask {endpoint} whether a newer version of this tool
exists, and install it if there is one. Every download is checked against a
sha256 published by the endpoint over TLS and refused if it does not match, and
a major version is never installed on its own -- that one waits for you.

`insight update --off` stops it, `insight update --status` shows what happened,
and every change is written to the log with both version numbers."""


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config() -> Optional[Dict[str, Any]]:
    if not os.path.exists(CONFIG_PATH):
        return None
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def remember_repo(path: str) -> None:
    config = load_config()
    if config is None:
        return
    resolved = os.path.abspath(os.path.expanduser(path))
    repos = config.get("repos") or []
    if resolved not in repos:
        repos.append(resolved)
        config["repos"] = sorted(repos)
        write_json(CONFIG_PATH, config)


def known_repos() -> List[str]:
    config = load_config() or {}
    return list(config.get("repos") or [])


def require_config() -> Dict[str, Any]:
    config = load_config()
    if config is None:
        raise SystemExit("not initialised -- run `insight init` first")
    return config


class NoTerminal(SystemExit):
    """Raised when a prompt is reached with nothing to read from.

    Not the same thing as a non-tty stdin. `curl ... | sh` leaves stdin holding
    the script while a terminal is still attached, so the questions are asked on
    /dev/tty -- see ``_tty``. This is for the case where there is genuinely
    nobody there: cron, CI, a container.
    """

    def __init__(self) -> None:
        super().__init__(
            "`insight setup` asks questions and needs a terminal.\n"
            "Run it directly, or non-interactively with "
            "`insight setup --email you@seta-international.vn --yes`.")


_TTY = None  # type: Optional[Any]


def _tty():
    """The terminal, whatever stdin happens to be. ``None`` if there is none.

    The installer pipes itself into `sh` and then runs `setup`, which inherits
    that pipe as stdin. A terminal is still attached to the process; it is just
    not on fd 0. Reaching it through /dev/tty is what lets the install be one
    command instead of two without ever passing --yes on someone's behalf.

    Two handles rather than one `r+`: a tty is not seekable, and Python's
    buffered read-write mode demands that it be, so `open("/dev/tty", "r+")`
    raises `io.UnsupportedOperation` on macOS and takes the terminal with it.
    """
    global _TTY
    if _TTY is not None:
        return _TTY or None
    if sys.stdin.isatty():
        _TTY = (sys.stdin, sys.stdout)
        return _TTY
    try:
        _TTY = (open("/dev/tty", "r"), open("/dev/tty", "w"))
    except (OSError, IOError, ValueError):
        _TTY = False
        return None
    return _TTY


def ask(prompt: str, default: str = "") -> str:
    """One wizard question. Refuses to guess when there is nobody to ask."""
    stream = _tty()
    if stream is None:
        raise NoTerminal()
    reader, writer = stream
    try:
        writer.write(prompt)
        writer.flush()
        answer = reader.readline()
        if answer == "":
            raise EOFError
    except EOFError:
        raise NoTerminal()
    return answer.strip() or default


def ask_yes(prompt: str, default: bool = False) -> bool:
    answer = ask(prompt, "y" if default else "n").lower()
    return answer in ("y", "yes")


def ask_email(allow_cancel: bool = False) -> str:
    """Ask until the address parses.

    A typo costs a retry, not a restart. `normalise_email` raising
    ``SystemExit`` mid-wizard would discard every answer already given.

    With ``allow_cancel`` an empty line means no, which is what makes this the
    only question `setup` asks: the address is both the answer and the consent.
    """
    import identity

    while True:
        raw = ask("Your work email: ")
        if not raw:
            if allow_cancel:
                return ""
            print("  An address is needed -- `ship` uploads under it.")
            continue
        try:
            return identity.normalise_email(raw)
        except identity.IdentityError as exc:
            print("  {}".format(exc))


def cmd_init(args: argparse.Namespace) -> int:
    existing = load_config()
    if existing and not args.force:
        print("already initialised at {}".format(CONFIG_PATH))
        print("consent recorded {}".format(existing.get("consent_at")))
        return 0

    import identity

    endpoint = (None if getattr(args, "no_endpoint", False) else
                (getattr(args, "endpoint", None)
                 or (existing or {}).get("endpoint")
                 or DEFAULT_ENDPOINT))
    email = getattr(args, "email", None) or (existing or {}).get("email")
    if email:
        try:
            email = identity.normalise_email(email)
        except identity.IdentityError as exc:
            raise SystemExit(str(exc))
    if endpoint and getattr(args, "hourly", False):
        transport = TRANSPORT_HOURLY.format(home=HOME, endpoint=endpoint)
    elif endpoint:
        transport = TRANSPORT_ENDPOINT.format(home=HOME, endpoint=endpoint)
    else:
        transport = TRANSPORT_MANUAL.format(home=HOME)
    print(CONSENT_TEXT.format(home=HOME, transport=transport))
    if not args.yes and not email:
        # One question, and it is this one. Typing the address is the consent:
        # the text above says what is collected, and there is nothing to upload
        # under without it. A separate y/N in front of it asked the same person
        # the same thing twice.
        print("Enter your work email to start collecting, or press Enter to "
              "stop here.")
        email = ask_email(allow_cancel=True)
        if not email:
            print("nothing was written; no data will be collected")
            return 1

    os.makedirs(BUFFER_DIR, exist_ok=True)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    config = {
        "format": BUNDLE_FORMAT,
        "machine_id": (existing or {}).get("machine_id") or uuid.uuid4().hex,
        # The salt makes an email hash unlinkable across organisations. It is
        # generated here and never leaves the machine; rotating it breaks every
        # historical join, so it is written once and reused.
        "salt": (existing or {}).get("salt") or uuid.uuid4().hex,
        "consent_at": now(),
        "schema_version": common.SCHEMA_VERSION,
        # Repositories this machine works in. Kept here so `scan` with no
        # arguments covers all of them: an engineer with four repos should not
        # have to remember four commands, because the one they forget is the
        # one that silently reports nothing.
        "repos": (existing or {}).get("repos") or [],
        # Where `ship` sends. Absent means bundles are handed over by hand,
        # which stays a supported way to run this rather than a broken one.
        "endpoint": endpoint,
        # Transport identity. CONTRACT.md 1.1 forbids raw email addresses in
        # collected data, and nothing writes this into a bundle -- it travels
        # in the Authorization header and nowhere else.
        "email": email,
        # Minted here, kept here, and only here. The server is given sha256 of
        # it and never the value, so its whitelist is not a credential store.
        #
        # There used to be a ``--token`` that adopted a secret issued by the
        # server admin, for onboarding somebody before their laptop had been
        # touched. It is gone. That path put the live secret through Slack --
        # `cli/identity.py` argues the point and calls it an exception granted
        # only for pre-provisioning. Removing the exception means every secret
        # on every machine is one that has never travelled, which is a
        # stronger property than the convenience was worth.
        "endpoint_token": (existing or {}).get("endpoint_token") or (
            identity.mint_secret() if email else None),
        "endpoint_token_previous": (existing or {}).get("endpoint_token_previous"),
    }
    write_json(CONFIG_PATH, config)
    os.chmod(CONFIG_PATH, 0o600)
    print("initialised. machine id {}".format(config["machine_id"][:8]))
    if config.get("endpoint_token"):
        # `setup` prints this itself, at the end, after the step table. Printing
        # it here too puts the same paragraph on screen twice, and a paragraph
        # somebody has already scrolled past is one they stop reading.
        if not getattr(args, "quiet_whitelist", False):
            print()
            print_whitelist_line(config)
    elif endpoint:
        # Warned, not refused. Collecting without being able to upload is still
        # a useful state -- the bundles are on disk and can be handed over by
        # hand -- and refusing here would block anyone not yet enrolled.
        print()
        print("No work email recorded, so `ship` has nothing to upload under.")
        print("Run `insight setup` when you have one.")
    return 0


def print_whitelist_line(config: Dict[str, Any]) -> None:
    """The one thing setup cannot do for the engineer, said plainly.

    Uploading fails until someone adds this line to the server, and the failure
    is a 401 that looks like a bug rather than a missing step. Printing it here,
    in full, is what keeps that from becoming a support conversation.

    This is now the only direction a secret ever travels: minted on the machine,
    fingerprint sent out. The `--token` path that went the other way is gone.
    """
    import identity

    email = config.get("email")
    line = (identity.whitelist_line(email, config["endpoint_token"]) if email
            else identity.fingerprint(config["endpoint_token"]))
    print("Send this line to whoever maintains the collection server, so they")
    print("can add you to INSIGHT_ALLOWED in its .env:")
    print()
    print("    " + line)
    print()
    print("It is a hash, not a secret -- the secret stays in {}.".format(
        CONFIG_PATH))
    # Only true before there is a working secret. During a rotation the old one
    # still uploads, and saying otherwise would send someone chasing an outage
    # that is not happening.
    if config.get("endpoint_token_previous"):
        print("Uploads keep working on the previous secret until it is.")
    else:
        print("Uploading with `ship` will fail until that line is in place.")


# --------------------------------------------------------------------------
# buffer
# --------------------------------------------------------------------------

def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


#: Copilot CLI's own home. It keeps one journal per session under
#: ``session-state/<id>/events.jsonl``, written with no exporter, no setting and
#: nothing listening. See cmd_copilot.
COPILOT_ROOT = os.environ.get("COPILOT_HOME") or os.path.join(
    os.path.expanduser("~"), ".copilot")

#: The VS Code user directory, or None to let `vscode_read` find it. Named here
#: for the same reason `COPILOT_ROOT` is: discovery reads a real directory on a
#: real machine, so anything testing it has to be able to point it somewhere
#: empty. Leaving it None is production behaviour on all four supported paths.
VSCODE_ROOT: Optional[str] = os.environ.get("VSCODE_HOME") or None


def buffer_path(day: Optional[str] = None) -> str:
    return os.path.join(BUFFER_DIR, "{}.ndjson".format(day or now()[:10]))


def partition_of(event: Dict[str, Any]) -> str:
    """The day an event belongs to is the day it happened, not the day we read it.

    A span produced on Friday and collected on Monday belongs in Friday. Filing
    it under Monday would move work between weeks, which is exactly the sort of
    quiet error a weekly report cannot survive: the totals stay right and every
    week is wrong.
    """
    stamp = event.get("event_time")
    if isinstance(stamp, str) and len(stamp) >= 10 and stamp[4] == "-":
        return stamp[:10]
    return now()[:10]


def buffer_days() -> List[str]:
    """Which days the buffer holds, oldest first."""
    if not os.path.isdir(BUFFER_DIR):
        return []
    return sorted(name[:-len(".ndjson")] for name in os.listdir(BUFFER_DIR)
                  if name.endswith(".ndjson"))


def read_buffer(since: Optional[str] = None,
                until: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read the buffer, optionally one window of days.

    Bounds are inclusive dates, `YYYY-MM-DD`. Filtering by partition rather than
    by re-reading everything and discarding is what makes the day layout worth
    having.
    """
    events: List[Dict[str, Any]] = []
    if not os.path.isdir(BUFFER_DIR):
        return events
    for name in sorted(os.listdir(BUFFER_DIR)):
        if not name.endswith(".ndjson"):
            continue
        day = name[:-len(".ndjson")]
        if (since and day < since) or (until and day > until):
            continue
        with open(os.path.join(BUFFER_DIR, name), "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    return events


def append_events(events: Iterable[Dict[str, Any]]) -> Tuple[int, int]:
    """Append to today's buffer, dropping anything already there.

    Returns ``(written, duplicates)``. Dedup is on ``event_id``, which the
    contract requires to be deterministic for anything derived from a
    re-readable fact -- so re-running ``scan`` over the same window is safe and
    is expected to be the normal case.
    """
    seen = {event["event_id"] for event in read_buffer()}
    written = 0
    duplicates = 0
    os.makedirs(BUFFER_DIR, exist_ok=True)
    handles: Dict[str, Any] = {}
    try:
        for event in events:
            if event["event_id"] in seen:
                duplicates += 1
                continue
            seen.add(event["event_id"])
            day = partition_of(event)
            if day not in handles:
                handles[day] = open(buffer_path(day), "a", encoding="utf-8")
            handles[day].write(json.dumps(event, sort_keys=True) + "\n")
            written += 1
    finally:
        for handle in handles.values():
            handle.close()
    return written, duplicates


def check_allowed(event: Dict[str, Any]) -> List[str]:
    """Validate against the collector's own allow-list, before anything is stored.

    Checked on write rather than on ingest at the far end, where it would
    already be too late: this process runs on a machine full of exactly what
    the contract forbids collecting.
    """
    event_type = event.get("event_type")
    allowed = collector_main.ATTRIBUTE_ALLOWLIST.get(event_type)
    if allowed is None:
        return ["event_type {!r} is not in the contract enum".format(event_type)]
    return [
        "{}.{} is not in the allow-list".format(event_type, key)
        for key in sorted(event.get("attributes") or {})
        if key not in allowed
    ]


# --------------------------------------------------------------------------
# setup -- one command, because doing it by hand goes wrong silently
# --------------------------------------------------------------------------

def cmd_setup(args: argparse.Namespace) -> int:
    """Set this machine up. Asks for one thing: the work email.

    Everything else is on by default -- automatic collection, self-update, and
    the commit hook -- because every other answer either has one sensible value
    or silently empties a metric when refused. `--no-schedule`,
    `--no-auto-update` and `--no-commit-hook` opt out, and each is reversible
    afterwards.

    The consent text is printed above the question, so typing the address is
    the consent. Prompts go to /dev/tty rather than stdin, which is what lets
    the installer run this itself at the end of a `curl | sh`.

    The flags still work (``--email``, ``--yes``) so this is scriptable and so
    the test suite does not need a terminal.
    """
    import vscode_setup

    steps: List[Dict[str, Any]] = []
    wizard = not (args.yes or args.email or args.dry_run)

    if wizard:
        print()
        print("This sets up collection on this machine. One question.")
        print()

    # Beside the repo is only a meaningful default when there is a repo. From
    # a zipapp, `_ROOT/..` is whatever directory the archive was installed
    # into, and probing it is how this ends up registering nothing while
    # reporting success -- `desired()` returns {} for aiep=None, `apply()`
    # writes no keys, and the step still prints [ ok ] vscode.
    aiep = args.aiep or os.environ.get("INSIGHT_AIEP")
    if not aiep and _archive() is None:
        aiep = os.path.join(_ROOT, "..", "ai-engineering-platform")
    aiep = os.path.abspath(aiep) if aiep and os.path.isdir(
        os.path.join(os.path.abspath(aiep), "agents")) else None

    # Skills must be a flat directory of skill folders; the platform's own
    # setup.sh builds one, and doing it here keeps the two in step.
    if aiep:
        staging = os.path.join(aiep, ".skills")
        os.makedirs(staging, exist_ok=True)
        source = os.path.join(aiep, "skills")
        if os.path.isdir(source):
            for name in sorted(os.listdir(source)):
                target = os.path.join(staging, name)
                if os.path.isdir(os.path.join(source, name)) and \
                        not os.path.exists(target):
                    os.symlink(os.path.join(source, name), target)

    path = vscode_setup.settings_path()
    if not path:
        # Not a failure, and it used to be. Nothing about collection depends on
        # VS Code any more: usage comes from Copilot CLI's own journal, which is
        # written whether or not an editor is involved. This step registers the
        # platform's agents and skills for people who use the chat panel, and
        # its absence on a terminal-only machine is a fact about that machine.
        # Left as a failure it would make `setup` exit non-zero on every such
        # machine, which teaches people that a red line at the end is normal.
        steps.append({"step": "vscode", "ok": True,
                      "detail": "not installed -- nothing to configure"})
    else:
        changed, detail, keys = vscode_setup.apply(
            path, vscode_setup.desired(HOME, aiep), args.dry_run)
        steps.append({"step": "vscode", "ok": True, "changed": changed,
                      # Said out loud rather than left to `agents_registered`
                      # in a dict nobody prints. Half of what this step exists
                      # to do did not happen, and "ok" alone would hide that.
                      "detail": detail if aiep else detail +
                      " (no ai-engineering-platform found, so no agent or "
                      "skill locations were registered -- pass --aiep <path> "
                      "or set INSIGHT_AIEP)",
                      "settings": path,
                      "keys": sorted(keys), "agents_registered": bool(aiep)})

    if not args.dry_run:
        email = args.email
        if load_config() is None:
            # `init` prints the consent text and asks for the address. That is
            # the only question in the whole of setup; everything below it is
            # a default with a flag to turn it off.
            if cmd_init(argparse.Namespace(
                    yes=args.yes, force=False, endpoint=args.endpoint,
                    no_endpoint=args.no_endpoint, email=email,
                    quiet_whitelist=True,
                    hourly=not args.no_schedule)) != 0:
                return 1
        steps.append({"step": "consent", "ok": True,
                      "detail": "recorded" if load_config() else "declined"})

        if wizard and not (load_config() or {}).get("email"):
            # Reached only when a config already existed without an address --
            # a machine set up before the endpoint did.
            print()
            email = ask_email()

        # Also settable on a machine set up before there was an endpoint, so
        # turning transport on later is one command rather than a re-init.
        config = load_config()
        if config is not None and (args.endpoint or email or args.no_endpoint
                                   or not config.get("endpoint")):
            import identity
            if args.no_endpoint:
                config["endpoint"] = None
            elif args.endpoint or not config.get("endpoint"):
                config["endpoint"] = args.endpoint or DEFAULT_ENDPOINT
            if email:
                try:
                    config["email"] = identity.normalise_email(email)
                except identity.IdentityError as exc:
                    raise SystemExit(str(exc))
            if config.get("email") and not config.get("endpoint_token"):
                config["endpoint_token"] = identity.mint_secret()
            write_json(CONFIG_PATH, config)
            os.chmod(CONFIG_PATH, 0o600)
        if config is not None:
            steps.append({"step": "endpoint", "ok": True,
                          "detail": config.get("endpoint")
                          or "not set -- bundles are handed over by hand"})
            if config.get("email"):
                steps.append({"step": "identity", "ok": True,
                              "detail": config["email"]})

        # Hourly by default. An engineer who has to remember a weekly command
        # is an engineer whose quiet weeks are indistinguishable from their
        # busy ones, and `pull` cannot tell those apart from the outside.
        # `--no-schedule` opts out; `schedule --off` reverses it later.
        hourly = not args.no_schedule
        # Asked, not assumed, and only where hourly was taken -- the check
        # lives inside `auto`, so on a machine that runs things by hand there
        # is nothing to switch on. Default yes, on the same reasoning that
        # makes hourly default yes: a fleet on fourteen versions is a fleet
        # nobody can support, and staying current is what the person answering
        # would want if they thought about it. It is still a question, and the
        # consent text records the answer.
        if hourly and config is not None:
            auto_update = not args.no_auto_update
            fresh = load_config() or {}
            fresh["auto_update"] = bool(auto_update)
            write_json(CONFIG_PATH, fresh)
            steps.append({"step": "updates", "ok": True,
                          "detail": "automatic" if auto_update else "manual "
                                    "-- re-run the installer to upgrade"})

        # The Copilot hook, always, and without asking. It is not a separate
        # decision from "collect automatically": it is *how* that is done now,
        # and a question whose only sensible answer is yes is a question that
        # wastes somebody's attention. `insight schedule --off` removes both.
        #
        # Collection fires on Copilot activity rather than on a clock, because
        # the evidence perishes -- measured 2026-08-26, 24 of 27 VS Code
        # workspace folders and 6 of 7 Copilot `gitRoot`s were already deleted
        # by the time anything read them. Uploads stay batched on their own
        # interval so a busy day sends a handful of full bundles, not a stream
        # of nearly-empty ones.
        if hourly and config is not None:
            try:
                detail = install_copilot_hook()
                steps.append({"step": "hook", "ok": True,
                              "detail": "collection runs on Copilot activity "
                                        "({})".format(detail["status"])})
            except OSError as exc:
                # Not fatal: the hourly timer below is the fallback, and a
                # machine with one of the two still reports.
                steps.append({"step": "hook", "ok": False,
                              "detail": "{} -- the hourly run still "
                                        "collects".format(exc)})

        if hourly and config is not None:
            import schedule as schedule_mod
            try:
                detail = schedule_mod.install(_executable(), LOG_PATH)
                steps.append({"step": "schedule", "ok": True,
                              "detail": "hourly via {}".format(detail["kind"])})
            except schedule_mod.ScheduleError as exc:
                # Not fatal. Everything still works by hand, and saying so
                # beats a setup that reports success and quietly never runs.
                steps.append({"step": "schedule", "ok": False,
                              "detail": "{} -- collection still works with "
                                        "`./insight pack && ./insight ship`"
                                        .format(exc)})

        # The span file the retired exporter wrote. Nothing reads it now, and
        # it is not somebody's own file -- it exists only because this tool
        # asked Copilot to write it, so this tool cleans it up. Removing the
        # settings without removing what they produced would leave a document
        # of somebody's work sitting in a directory nobody looks at, for the
        # rest of the machine's life. Measured on the first machine migrated:
        # 135 KB still there, and still growing until VS Code was restarted.
        legacy_spans = os.path.join(HOME, "copilot-spans.jsonl")
        if os.path.exists(legacy_spans):
            size = os.path.getsize(legacy_spans)
            try:
                os.remove(legacy_spans)
                steps.append({"step": "cleanup", "ok": True,
                              "detail": "removed the retired span file "
                                        "({:,} bytes)".format(size)})
            except OSError as exc:
                steps.append({"step": "cleanup", "ok": True,
                              "detail": "could not remove {} ({})".format(
                                  legacy_spans, exc)})

        # Repositories are no longer asked for. Copilot's journals record
        # `context.gitRoot` for every session, so `scan` discovers the trees an
        # agent actually worked in -- see `copilot_read.discover_repos`. The
        # old `--repo` flag existed because the repository somebody forgot to
        # name was the one that silently reported nothing; not having to ask
        # removes the failure rather than warning about it.
        import copilot_read
        import vscode_read
        # Both surfaces. A QA engineer who works entirely in the VS Code chat
        # panel has no Copilot CLI journal at all, and asking only that one
        # told them "none yet" on a machine with a repository open.
        discovered = list(copilot_read.discover_repos(COPILOT_ROOT))
        for path in vscode_read.discover_repos(VSCODE_ROOT):
            if path not in discovered:
                discovered.append(path)
        steps.append({
            "step": "repos", "ok": True,
            "detail": "{} found in Copilot's session history".format(
                len(discovered)) if discovered else
            "none yet -- they are discovered as Copilot is used"})

        # The commit hook is the one thing discovery cannot replace. It writes
        # an `AI-Run-Id` trailer at commit time, and that trailer is the only
        # evidence that earns `link.method = 'explicit'` (CONTRACT.md §2.4) --
        # which is in turn the only thing admitted to cost-per-output. Without
        # it every link stays heuristic and that metric stays empty. So it is
        # offered, once, with what it buys said plainly.
        if discovered and not args.no_commit_hook:
            # Installed, not offered. It writes an `AI-Run-Id` trailer at commit
            # time, and that trailer is the only evidence that earns
            # `link.method = 'explicit'` (CONTRACT.md 2.4), which is the only
            # thing admitted to cost-per-output. A question whose no answer
            # silently empties a metric is a question worth not asking.
            # `--no-commit-hook` opts out; the hook is a file in .git/hooks and
            # deleting it reverses this.
            installed, failed = 0, []
            for repo in discovered:
                try:
                    cmd_install_hook(argparse.Namespace(repo=repo, force=False))
                    installed += 1
                except SystemExit as exc:
                    failed.append("{}: {}".format(os.path.basename(repo), exc))
            steps.append({
                "step": "trailer", "ok": True,
                "detail": "commit hook in {} of {} repositories{}".format(
                    installed, len(discovered),
                    "" if not failed else " ({})".format("; ".join(failed)))})

    print()
    for step in steps:
        mark = "  ok  " if step.get("ok") else " FAIL "
        print("[{}] {:<9} {}".format(
            mark, step["step"], step.get("detail") or step.get("repo") or ""))
    print()
    if args.dry_run:
        print("Dry run: nothing was written.")
    else:
        # The one thing this cannot do for them, said once and plainly.
        print("Restart VS Code (quit fully, not Reload Window) so the settings "
              "take effect.")
        final = load_config() or {}
        if any(s["step"] == "schedule" and s["ok"] for s in steps):
            print("Collection runs hourly from now on. Nothing else to remember.")
            print("`insight schedule --status` shows the last run, `--off` stops it.")
        else:
            tail = " && insight ship" if final.get("endpoint") else ""
            print("Then: insight copilot && insight scan && insight pack" + tail)
        if final.get("endpoint_token"):
            # Registered here rather than printed for somebody to relay. The
            # fingerprint travels; the secret does not.
            result = enroll_identity(final)
            record_enrolment(result)
            print()
            if result.get("ok"):
                print("Registered with {}. Nothing else to do.".format(
                    final.get("endpoint")))
            elif result.get("status") == 403:
                print("Not registered: nobody is expecting {} at the endpoint "
                      "yet.".format(final.get("email")))
                print("Ask whoever runs the pipeline to add it. This retries "
                      "by itself,")
                print("so there is nothing to send them and nothing to re-run.")
            elif result.get("status") == 409:
                print("Not registered: that address is enrolled from another "
                      "machine.")
                print("A replacement laptop needs the old entry reset by "
                      "whoever runs the pipeline.")
            else:
                print("Not registered yet ({}). Collection has started; "
                      "`insight enroll`".format(
                          result.get("outcome")))
                print("retries, and so does every scheduled run.")
    return 0 if all(s.get("ok") for s in steps) else 1


# --------------------------------------------------------------------------
# copilot hook -- collection triggered by activity, not by a clock
# --------------------------------------------------------------------------

#: Where Copilot CLI reads hook definitions.
COPILOT_HOOKS = os.path.join(COPILOT_ROOT, "hooks")
COPILOT_HOOK_PATH = os.path.join(COPILOT_HOOKS, "seta-insight.json")

#: Do not collect more than once in this window.
#:
#: `PreToolUse` fires before **every** tool call -- 2,062 of them in 22 sessions
#: on the machine this was measured on. Without a debounce, one Copilot session
#: would spawn a thousand collectors. With it, a busy hour costs one run and a
#: quiet hour costs nothing at all, which is the whole reason to prefer a hook
#: over a timer.
HOOK_DEBOUNCE_S = 10 * 60
HOOK_STAMP = os.path.join(HOME, "hook.stamp")

#: Collection and upload run on **different clocks**, and this is the point of
#: the split.
#:
#: Evidence perishes: measured 2026-08-26, 24 of 27 VS Code workspace folders
#: and 6 of 7 Copilot `gitRoot`s were already deleted when read after the fact.
#: So collection has to be frequent -- it is local, cheap, and idempotent.
#:
#: Upload has the opposite constraint. Shipping on every trigger would put
#: dozens of objects a day per machine on the endpoint, most of them a few
#: events apart. Batched to this interval instead, a busy day sends a handful
#: of full bundles rather than a stream of nearly-empty ones.
#:
#: Nothing is lost by waiting: `pack` is idempotent over the day, and a bundle
#: that misses one window is picked up whole by the next.
SHIP_MIN_INTERVAL_S = 60 * 60
SHIP_STAMP = os.path.join(HOME, "ship.stamp")


def ship_due(now_s: Optional[float] = None) -> bool:
    """Has enough time passed to justify another upload?

    A first run, or a missing stamp, is always due -- a machine that has never
    reported must not stay silent for an hour after being set up.
    """
    try:
        return (now_s or time.time()) - os.path.getmtime(SHIP_STAMP) \
            >= SHIP_MIN_INTERVAL_S
    except OSError:
        return True


def copilot_hook_body(command: str) -> Dict[str, Any]:
    """The hook definition, in both spellings Copilot has been seen to accept.

    `rtk` -- found already installed on both pilot machines -- registers
    `PreToolUse` and `preToolUse` with differently-named timeout fields. That
    is a tool hedging across two Copilot versions, and since those files are
    demonstrably accepted in the field, this matches their shape rather than
    inventing a cleaner one that might be ignored in silence.
    """
    return {
        "version": 1,
        "hooks": {
            "PreToolUse": [
                {"type": "command", "command": command, "cwd": ".",
                 "timeout": 5},
            ],
            "preToolUse": [
                {"type": "command", "bash": command, "powershell": command,
                 "cwd": ".", "timeoutSec": 5},
            ],
        },
    }


def cmd_hook(args: argparse.Namespace) -> int:
    """Called by Copilot before a tool runs. Must return immediately.

    This sits in the latency path of somebody's editor. It does no collection
    of its own: it decides whether a run is due and, if so, detaches one. The
    budget is milliseconds, and the rule is that **nothing here may fail
    loudly** -- a collector that breaks a tool call would deserve to be
    uninstalled within the hour.
    """
    try:
        if load_config() is None:
            return 0                       # not set up; silently do nothing

        os.makedirs(HOME, exist_ok=True)
        now_s = time.time()
        try:
            if now_s - os.path.getmtime(HOOK_STAMP) < HOOK_DEBOUNCE_S:
                return 0
        except OSError:
            pass                           # no stamp yet: this is the first run

        # Written *before* the run, not after. If collection is slow or dies,
        # the next tool call must not immediately start another one.
        with open(HOOK_STAMP, "w", encoding="utf-8") as handle:
            handle.write(str(int(now_s)))

        if args.now:
            return cmd_auto(argparse.Namespace())

        # Detached, output discarded, no wait. `insight auto` already holds its
        # own lock, so a second trigger during a long run is a no-op there too.
        with open(os.devnull, "wb") as devnull:
            subprocess.Popen(
                [_executable(), "auto"],
                stdout=devnull, stderr=devnull, stdin=devnull,
                start_new_session=True,
            )
    except Exception:  # noqa: BLE001 -- see the docstring; never break a tool call
        return 0
    return 0


def install_copilot_hook(force: bool = False) -> Dict[str, Any]:
    """Register the hook with Copilot, keeping anybody else's.

    Written as its own file rather than merged into an existing one: both pilot
    machines already carry `rtk-rewrite.json`, and Copilot reads the directory,
    so two files coexist where two edits to one file would eventually clobber
    each other.
    """
    command = "{} hook".format(_executable())
    if os.path.exists(COPILOT_HOOK_PATH) and not force:
        try:
            with open(COPILOT_HOOK_PATH, "r", encoding="utf-8") as handle:
                if json.load(handle) == copilot_hook_body(command):
                    return {"status": "already installed",
                            "path": COPILOT_HOOK_PATH}
        except (OSError, ValueError):
            pass
    os.makedirs(COPILOT_HOOKS, exist_ok=True)
    with open(COPILOT_HOOK_PATH, "w", encoding="utf-8") as handle:
        json.dump(copilot_hook_body(command), handle, indent=2, sort_keys=True)
        handle.write("\n")
    return {"status": "installed", "path": COPILOT_HOOK_PATH,
            "command": command}


def remove_copilot_hook() -> Dict[str, Any]:
    try:
        os.remove(COPILOT_HOOK_PATH)
        return {"status": "removed", "path": COPILOT_HOOK_PATH}
    except OSError:
        return {"status": "not installed", "path": COPILOT_HOOK_PATH}


# --------------------------------------------------------------------------
# install-hook
# --------------------------------------------------------------------------

#: The hook's bytes, addressed the same way in a checkout and in the archive.
#: `_read_bundled` is what makes the second case work -- see there.
HOOK_SOURCE = os.path.join(_HERE, "hooks", "prepare-commit-msg")
HOOK_MARKER = "AI-Run-Id"


def cmd_install_hook(args: argparse.Namespace) -> int:
    require_config()
    repo = os.path.abspath(args.repo)
    hooks = os.path.join(repo, ".git", "hooks")
    if not os.path.isdir(hooks):
        raise SystemExit("{} has no .git/hooks -- not a git repository".format(repo))

    target = os.path.join(hooks, "prepare-commit-msg")
    if os.path.exists(target) and not args.force:
        with open(target, "r", encoding="utf-8", errors="replace") as handle:
            existing = handle.read()
        if HOOK_MARKER in existing:
            remember_repo(repo)
            print(json.dumps({"repo": repo, "status": "already installed"}))
            return 0
        # Somebody else's hook. Overwriting it silently would break whatever it
        # does, and this is not important enough to cost anyone that.
        raise SystemExit(
            "{} already has a prepare-commit-msg hook that is not ours. "
            "Merge them by hand, or re-run with --force to replace it.".format(target))

    body = _read_bundled(HOOK_SOURCE)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.chmod(target, 0o755)
    remember_repo(repo)
    print(json.dumps({"repo": repo, "status": "installed", "hook": target}))
    return 0


# --------------------------------------------------------------------------
# scan -- local git history
# --------------------------------------------------------------------------

def git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo] + list(args),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            "git failed in {}: {}".format(repo, result.stderr.decode().strip())
        )
    return result.stdout.decode("utf-8", "replace")


def repo_full_name(repo: str) -> Optional[str]:
    try:
        url = git(repo, "config", "--get", "remote.origin.url").strip()
    except SystemExit:
        return None
    if not url:
        return None
    url = url[:-4] if url.endswith(".git") else url
    parts = [p for p in url.replace(":", "/").split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


#: Map a path to a CONTRACT.md §3 artifact_type. The path is a permitted field;
#: what the file contains is not, and is never read.
ARTIFACT_RULES = (
    ("test", re.compile(r"\.feature$|(\.|_)(spec|test)\.(ts|js|tsx|jsx|py)$"
                        r"|(^|/)tests?/|(^|/)test_[^/]+\.py$|[._-]test\.py$"
                        r"|Test\.java$|[._-]steps?\.(ts|js|py|java)$", re.I)),
    ("spec", re.compile(r"(^|/)(specs?|requirements?)/.*\.md$"
                        r"|[-_.](spec|specification)\.md$", re.I)),
    ("mock", re.compile(r"(^|/)(mocks?|stubs?|fixtures?)/|[._-]mock\.", re.I)),
    ("config", re.compile(r"\.(ya?ml|toml|ini|cfg|json|properties)$"
                          r"|(^|/)(Dockerfile|Makefile)$", re.I)),
    ("csv", re.compile(r"\.csv$", re.I)),
    ("doc", re.compile(r"\.(md|rst|adoc)$", re.I)),
)


def artifact_type(path: str) -> str:
    for kind, pattern in ARTIFACT_RULES:
        if pattern.search(path):
            return kind
    return "code"


def scan_outputs(repo: str, sha: str, run_id: Optional[str],
                 name: Optional[str], branch: Optional[str],
                 event_time: str, jira_key: Optional[str]) -> List[Dict[str, Any]]:
    """One output.generated per file the commit touched.

    Derived from git rather than from the agent. The agents are supposed to emit
    these and in practice emit run.started and run.completed and nothing between
    -- measured 2026-08-24, phases_completed 0. Waiting for the wiring to be
    obeyed would leave the artifact trail permanently empty; git already knows
    what was written.
    """
    out: List[Dict[str, Any]] = []
    stats = git(repo, "show", "--numstat", "--format=", sha)
    for row in stats.splitlines():
        cols = row.split("\t")
        if len(cols) != 3:
            continue
        added, removed, path = cols
        if not added.isdigit() or not removed.isdigit():
            continue  # binary files report "-"
        out.append(common.build_event(
            event_type="output.generated",
            event_time=event_time,
            natural_key=(name or repo, sha, path),
            attributes={
                "output_id": common.deterministic_id("out", sha, path),
                "artifact_type": artifact_type(path),
                "file_path": path,
                "lines_added": int(added),
                "lines_removed": int(removed),
            },
            context=common.make_context(
                jira_issue_key=jira_key, repo_full_name=name, branch_name=branch),
            agent=common.make_agent("client.insight"),
            link=common.make_link("explicit", 1.0) if run_id
            else common.make_link("heuristic", 0.4),
            run_id=run_id,
        ))
    return out


def scan_commits(repo: str, since_days: int, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Read commit metadata. Subjects classify and are then discarded.

    The subject is never emitted. It is read to decide whether a marker is
    present and to find a Jira key, and neither of those is content.
    """
    sep = "\x1e"
    log = git(
        repo, "log", "--all", "--no-merges",
        "--since={} days ago".format(since_days),
        "--numstat",
        "--pretty=format:%x1e%H%x1f%aI%x1f%ae%x1f%B%x1f",
    )
    name = repo_full_name(repo)
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() or None
    events: List[Dict[str, Any]] = []

    for block in log.split(sep):
        block = block.strip("\n")
        if not block:
            continue
        head, _, stats = block.partition("\x1f\n")
        fields = head.split("\x1f")
        if len(fields) < 4:
            # Trailing record with no numstat section.
            fields = block.split("\x1f")
            stats = ""
        if len(fields) < 4:
            continue
        sha, authored, email, message = fields[0], fields[1], fields[2], fields[3]

        added = removed = 0
        for row in stats.splitlines():
            cols = row.split("\t")
            if len(cols) == 3 and cols[0].isdigit() and cols[1].isdigit():
                added += int(cols[0])
                removed += int(cols[1])

        subject = message.splitlines()[0] if message else ""
        trailers = common.parse_ai_trailers(message)
        run_id = trailers.get("ai-run-id")
        jira_key = common.extract_jira_key(
            subject, branch,
            projects=common.validated_projects(config.get("jira_projects"),
                                               source="jira_projects"))
        marker = common.has_ai_commit_marker(subject)

        # Artifacts only for commits AI had a hand in. Emitting them for every
        # human commit would inflate "AI output" with work AI never touched --
        # the exact error this whole system exists to avoid.
        if run_id or marker:
            events += scan_outputs(repo, sha, run_id, name, branch,
                                   common.to_rfc3339(authored), jira_key)

        events.append(common.build_event(
            event_type="scm.commit",
            event_time=authored,
            natural_key=(name or repo, sha),
            attributes={
                "commit_sha": sha,
                "lines_added": added,
                "lines_removed": removed,
                "has_ai_marker": marker,
            },
            actor=common.make_actor(
                person_id=None,
                person_email_hash=common.hash_email(email, config.get("salt")),
            ),
            context=common.make_context(
                jira_issue_key=jira_key,
                repo_full_name=name,
                branch_name=branch,
            ),
            agent=common.make_agent("client.insight"),
            link=common.make_link("explicit", 1.0) if run_id
            else common.make_link("marker_only", 0.3),
            run_id=run_id,
            trace_id=trailers.get("ai-trace-id"),
        ))
    return events


def cmd_scan(args: argparse.Namespace) -> int:
    config = require_config()

    if args.repo:
        targets = [os.path.abspath(os.path.expanduser(args.repo))]
        remember_repo(targets[0])
    else:
        # Registered first, then whatever Copilot's journals name. Nothing has
        # to be registered any more: `session.start` records `context.gitRoot`,
        # so the trees an agent actually worked in are already written down.
        # Asking somebody to list them was never the point -- and the one they
        # forgot was the one that silently reported nothing.
        targets = list(known_repos())
        # Both surfaces, because a machine can have either. Asking only the
        # Copilot CLI journal is how a VS Code-only laptop came to report
        # `repos: 0` while holding 512 chat events in a git tree -- measured
        # 2026-08-26. No commits scanned means no `AI-Run-Id` trailer read,
        # and that trailer is the only evidence earning `method='explicit'`.
        for module, discover in (
                ("copilot_read", lambda m: m.discover_repos(COPILOT_ROOT)),
                ("vscode_read", lambda m: m.discover_repos(VSCODE_ROOT))):
            try:
                for path in discover(__import__(module)):
                    if path not in targets:
                        targets.append(path)
            except (ImportError, OSError):
                continue
        if not targets:
            # Not an error. A machine where Copilot has not run in a git tree
            # has nothing to scan, and saying so beats failing hourly.
            print(json.dumps({"repos": 0, "discovered": 0, "events": 0},
                             sort_keys=True))
            return 0

    results = []
    failed = False
    for repo in targets:
        try:
            results.append(scan_one(repo, args.since_days, config))
        except SystemExit as exc:
            # One unreachable repository must not stop the others. A machine
            # where somebody deleted a clone should still report the rest.
            failed = True
            results.append({"repo": repo, "error": str(exc)})

    for line in results:
        print(json.dumps(line, sort_keys=True))
    return 1 if failed else 0


def scan_one(repo: str, since_days: int, config: Dict[str, Any]) -> Dict[str, Any]:
    if not os.path.isdir(os.path.join(repo, ".git")):
        raise SystemExit("not a git repository (or the clone is gone)")

    events = scan_commits(repo, since_days, config)
    rejected = [(e, check_allowed(e)) for e in events]
    bad = [(e, why) for e, why in rejected if why]
    if bad:
        for event, why in bad[:5]:
            print("REJECTED {}: {}".format(event["event_id"], "; ".join(why)), file=sys.stderr)
        raise SystemExit(
            "{} events failed the allow-list; nothing written".format(len(bad))
        )

    written, duplicates = append_events(events)
    # scan_commits returns two kinds of event now, so count each by type rather
    # than assuming every one carries a commit's attributes.
    commits = [e for e in events if e["event_type"] == "scm.commit"]
    outputs = [e for e in events if e["event_type"] == "output.generated"]
    return {
        "repo": repo_full_name(repo) or repo,
        "commits": len(commits),
        "artifacts": len(outputs),
        "written": written,
        "already_buffered": duplicates,
        "with_ai_marker": sum(
            1 for e in commits if e["attributes"]["has_ai_marker"]),
        "with_run_id": sum(1 for e in commits if e.get("run_id")),
    }


# --------------------------------------------------------------------------
# collect -- the emit.py buffer
# --------------------------------------------------------------------------

def cmd_collect(args: argparse.Namespace) -> int:
    require_config()
    source = args.source or EMIT_BUFFER
    if not os.path.isdir(source):
        # Not an error. The emitter simply has not run on this machine yet,
        # and that is a measured fact worth reporting rather than a failure.
        print(json.dumps({"source": source, "present": False, "events": 0}))
        return 0

    # Walk, do not list: emit.py writes into pending/ and moves files to
    # shipped/. A top-level listing finds the directories and no events.
    paths = []
    for root, _dirs, files in os.walk(source):
        paths += [os.path.join(root, f) for f in sorted(files) if f.endswith(".ndjson")]

    events: List[Dict[str, Any]] = []
    malformed = 0
    for path in sorted(paths):
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    malformed += 1

    kept, rejected = [], []
    for event in events:
        (rejected if check_allowed(event) else kept).append(event)

    written, duplicates = append_events(kept)
    print(json.dumps({
        "source": source,
        "present": True,
        "events": len(events),
        "written": written,
        "already_buffered": duplicates,
        "rejected": len(rejected),
        "malformed_lines": malformed,
    }, sort_keys=True))
    if rejected:
        print(
            "{} events did not match the allow-list and were not stored".format(
                len(rejected)),
            file=sys.stderr,
        )
    return 0


# --------------------------------------------------------------------------
# copilot -- read Copilot's own session journals
# --------------------------------------------------------------------------

def cmd_copilot(args: argparse.Namespace) -> int:
    """Read ``~/.copilot`` and buffer what the contract permits.

    **Nothing is deleted.** The command this replaced truncated Copilot's span
    file after every read, and that was right: the file existed only because we
    asked for it, and it held prompts. A session journal is different in kind --
    it is Copilot's own history of the user's work, it is what ``/resume`` reads,
    and it is not ours to clear. Re-reading is safe instead: every ``event_id``
    is derived from the journal record's own uuid, so a session that stays open
    for a week is read hourly and buffered once.
    """
    config = require_config()
    root = args.root or COPILOT_ROOT
    if not os.path.isdir(os.path.join(root, "session-state")):
        # Not an error. Copilot CLI may not be installed, or may not have run
        # yet. Reporting it as a fact beats failing on a machine that is fine.
        print(json.dumps({"root": root, "present": False, "events": 0},
                         sort_keys=True))
        return 0

    import copilot_read  # local: keeps `insight` importable without it

    # The journal names no author -- it is one machine's own history, so the
    # person is whoever this install belongs to. `scan` reads an address per
    # commit because a repository holds several people's work; this does not.
    email = config.get("email")
    actor = common.make_actor(
        person_id=None,
        person_email_hash=common.hash_email(email, config.get("salt")),
    ) if email else None

    result = copilot_read.to_events(
        root, since=args.since, actor=actor,
        jira_projects=common.validated_projects(
            config.get("jira_projects"), source="jira_projects"))
    problems = copilot_read.verify_no_content(result["events"])
    if problems:
        for problem in problems[:5]:
            print("REJECTED " + problem, file=sys.stderr)
        raise SystemExit("attributes outside the allow-list; nothing written")

    written, duplicates = append_events(result["events"])

    print(json.dumps({
        "root": root, "present": True,
        "sessions_read": result["sessions_read"],
        "sessions_skipped": result["sessions_skipped"],
        "events": len(result["events"]),
        "written": written, "already_buffered": duplicates,
        # Carried into the output every time, not only when it looks bad. A
        # reader who cannot see the denominator reads a small total as light
        # usage rather than as partial measurement.
        "coverage": result["coverage"],
    }, sort_keys=True))
    return 0


def cmd_vscode(args: argparse.Namespace) -> int:
    """Read VS Code's Copilot Chat store.

    The surface the CLI journal cannot see. On both pilot machines it is the
    **only** surface with anything on it: neither has ever created a Copilot
    CLI session, so `insight copilot` returns zero there for ever.
    """
    config = require_config()
    import vscode_read

    email = config.get("email")
    actor = common.make_actor(
        person_id=None,
        person_email_hash=common.hash_email(email, config.get("salt")),
    ) if email else None

    result = vscode_read.to_events(
        args.root, actor=actor,
        jira_projects=common.validated_projects(
            config.get("jira_projects"), source="jira_projects"))
    if not result["present"]:
        print(json.dumps({"present": False, "events": 0}, sort_keys=True))
        return 0

    problems = vscode_read.verify_no_content(result["events"])
    if problems:
        for problem in problems[:5]:
            print("REJECTED " + problem, file=sys.stderr)
        raise SystemExit("attributes outside the allow-list; nothing written")

    written, duplicates = append_events(result["events"])
    print(json.dumps({
        "present": True, "sessions": result["sessions"],
        "requests": result["requests"], "events": len(result["events"]),
        "written": written, "already_buffered": duplicates,
        "coverage": result["coverage"],
    }, sort_keys=True))
    return 0


def cmd_rtk(args: argparse.Namespace) -> int:
    """Read rtk's history, if this machine has one.

    A second, independent token source. Absent on most machines and present on
    the pilot ones -- which is why it is read rather than assumed either way.
    """
    config = require_config()
    import rtk_read

    if args.probe:
        print(json.dumps(rtk_read.probe(), indent=2, sort_keys=True))
        return 0

    email = config.get("email")
    actor = common.make_actor(
        person_id=None,
        person_email_hash=common.hash_email(email, config.get("salt")),
    ) if email else None

    result = rtk_read.to_events(actor=actor)
    if not result["present"]:
        print(json.dumps({"present": False, "events": 0}, sort_keys=True))
        return 0

    written, duplicates = append_events(result["events"])
    print(json.dumps({
        "present": True, "records": result["records"],
        "unparsed": result["unparsed"], "source": result["source"],
        "events": len(result["events"]),
        "written": written, "already_buffered": duplicates,
        "coverage": result["coverage"],
    }, sort_keys=True))
    return 0


# --------------------------------------------------------------------------
# pack
# --------------------------------------------------------------------------

def _vscode_present() -> bool:
    try:
        import vscode_read
        return bool(vscode_read.default_root())
    except Exception:  # noqa: BLE001 -- a status line may not crash
        return False


def sources_of(config: Dict[str, Any]) -> Dict[str, Any]:
    """Which local sources this machine is set up to read.

    Deliberately not "did the last run succeed": that changes hour to hour and
    would make an idle afternoon look like a broken install. This is the slower
    fact -- whether there is anything here to read at all -- which is what
    separates a quiet day from a setup that never finished.
    """
    return {
        # Registered repositories. Zero means `scan` has nothing to walk, and
        # every bundle from this machine will be empty until one is added.
        "repos": len(config.get("repos") or []),
        # Copilot CLI has run on this machine at least once. Nothing had to be
        # configured for this to be true -- which is the point, and the reason
        # it replaced a span exporter that had to be switched on per machine
        # and silently collected nothing when it was not.
        "copilot": os.path.isdir(os.path.join(COPILOT_ROOT, "session-state")),
        # VS Code's own chat store. Independent of `copilot` above: a machine
        # that has never run a CLI session still has this, and on the pilot
        # machines it is the only surface with anything on it.
        "vscode": bool(_vscode_present()),
        # A second token source, present on some machines only.
        "rtk": bool(shutil.which("rtk")),
        # Collection now fires on Copilot activity rather than on a clock.
        "hook": os.path.exists(COPILOT_HOOK_PATH),
        # The platform emitter's buffer directory, created on its first run.
        "agent": os.path.isdir(EMIT_BUFFER),
    }


def iso_week_of(day: str) -> str:
    year, week, _ = date.fromisoformat(day[:10]).isocalendar()
    return "{}-W{:02d}".format(year, week)


def week_bounds(week: str) -> Tuple[str, str]:
    """``2026-W34`` -> the Monday and the Sunday, as dates."""
    year, number = week.split("-W")
    monday = date.fromisocalendar(int(year), int(number), 1)
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def weeks_between(since: Optional[str], until: Optional[str]) -> List[str]:
    """Every ISO week touched by an inclusive date range."""
    if not since or not until:
        return []
    start, stop = date.fromisoformat(since[:10]), date.fromisoformat(until[:10])
    weeks: List[str] = []
    cursor = start
    while cursor <= stop:
        week = iso_week_of(cursor.isoformat())
        if week not in weeks:
            weeks.append(week)
        cursor += timedelta(days=7 - cursor.weekday())
    return weeks


def cmd_pack(args: argparse.Namespace) -> int:
    """One bundle per ISO week. Never one bundle across several.

    A bundle is filed by the endpoint under a folder derived from its
    `window_start` alone (`server/proxy.py:object_key`), and the report
    pipeline asks for a week at a time. So a bundle whose window straddles a
    week boundary is filed under the earlier of the two, and the later week's
    pull finds nothing -- not an error anywhere, just a week that reads as
    quiet.

    `insight backfill --since 2026-08-01` is where this bites: four weeks of
    events in one bundle, all of them filed under 2026-W31, and W32 through W34
    read as weeks nobody sent. The hourly `auto` is not affected -- it packs
    `--since today --until today`, which is one day and therefore one week --
    but every wide `pack` by hand is, and backfill is the command this project
    is about to ask two people to run.

    Splitting here rather than in the endpoint is deliberate: the client is the
    only party that knows which days it meant to cover, and a server that
    re-filed a straddling bundle would have to guess.
    """
    config = require_config()
    events = read_buffer(args.since, args.until)

    by_week: Dict[str, List[Dict[str, Any]]] = {}
    for event in events:
        by_week.setdefault(iso_week_of(partition_of(event)), []).append(event)

    # A requested window declares every week in it, including the empty ones.
    # That is what separates "this week was quiet" from "nobody sent this
    # week", and only the person packing knows which window they meant.
    requested = weeks_between(args.since, args.until)
    for week in requested:
        by_week.setdefault(week, [])

    # No events and no window: one bundle declaring nothing, as before. There
    # is no week to file it under and inventing one would be a claim.
    weeks = sorted(by_week) if by_week else [None]

    written: List[Dict[str, Any]] = []
    for week in weeks:
        group = by_week.get(week, []) if week else []
        times = sorted(e["event_time"] for e in group if e.get("event_time"))
        counts: Dict[str, int] = {}
        for event in group:
            counts[event["event_type"]] = counts.get(event["event_type"], 0) + 1

        if week:
            monday, sunday = week_bounds(week)
            # Clamped to what was asked for, so a bundle never declares
            # coverage of days outside the request.
            start = max(monday, args.since) if args.since else monday
            stop = min(sunday, args.until) if args.until else sunday
            window_start = start + "T00:00:00Z"
            window_end = stop + "T23:59:59Z"
        else:
            window_start = args.window_start or (times[0] if times else None)
            window_end = args.window_end or (times[-1] if times else None)

        body = "".join(json.dumps(e, sort_keys=True) + "\n" for e in group)
        manifest = {
            "format": BUNDLE_FORMAT,
            "schema_version": common.SCHEMA_VERSION,
            "machine_id": config["machine_id"],
            "packed_at": now(),
            # The window is declared even when it is empty. A bundle covering a
            # week with no activity is a measured zero; a week with no bundle
            # is missing data. Reports must be able to tell those apart.
            "window_start": window_start,
            "window_end": window_end,
            "days_covered": sorted({partition_of(e) for e in group}),
            "event_count": len(group),
            "event_counts_by_type": counts,
            # What this machine was in a position to measure at all.
            #
            # Without it a zero is ambiguous in the one way that matters. A
            # bundle from a machine with no repository registered is well
            # formed: it declares its window and reports no events, which is
            # exactly what a genuinely quiet day looks like. Read as a
            # measurement it says the person did no work -- a wrong answer, not
            # a missing one, and the only failure this whole design exists to
            # prevent.
            #
            # Counts and booleans, never paths. `importers/bundle.py` uses this
            # to separate a measured zero from a machine that measured nothing.
            "sources": sources_of(config),
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        }

        os.makedirs(REPORTS_DIR, exist_ok=True)
        stem = "{}-{}".format(config["machine_id"][:8],
                              now().replace(":", "").replace("-", ""))
        if week:
            stem = "{}-{}".format(stem, week)
        path = os.path.join(REPORTS_DIR, stem + ".ndjson")
        # The stamp has second resolution, so two packs in the same second
        # collide. Rare by hand and routine once a scheduler is driving this --
        # and the failure is silent: the second bundle overwrites the first,
        # which may not have been uploaded yet.
        if os.path.exists(path):
            serial = 2
            while os.path.exists(path):
                path = os.path.join(REPORTS_DIR,
                                    "{}-{}.ndjson".format(stem, serial))
                serial += 1
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"_manifest": manifest}, sort_keys=True) + "\n")
            handle.write(body)
        written.append({"bundle": path, "week": week, **manifest})

    if args.clear:
        # Only the partitions that were packed. Clearing everything would throw
        # away days the bundle does not cover.
        for day in {partition_of(e) for e in events}:
            buffered = buffer_path(day)
            if os.path.exists(buffered):
                os.remove(buffered)

    # The last line stays one JSON object because three callers parse it that
    # way. `bundle`/`window_start` describe the newest bundle so a single-week
    # pack reads exactly as it did; `bundles` is the whole truth.
    newest = written[-1]
    print(json.dumps({
        **newest,
        "bundles": [{"bundle": b["bundle"], "week": b["week"],
                     "event_count": b["event_count"],
                     "window_start": b["window_start"],
                     "window_end": b["window_end"]} for b in written],
        "bundle_count": len(written),
        "event_count": sum(b["event_count"] for b in written),
        "days_covered": sorted({d for b in written for d in b["days_covered"]}),
    }, sort_keys=True))
    return 0


# --------------------------------------------------------------------------
# ship
# --------------------------------------------------------------------------

def cmd_ship(args: argparse.Namespace) -> int:
    """Send sealed bundles to the collection endpoint.

    Separate from ``pack`` on purpose. The consent model rests on an engineer
    being able to read their bundle before deciding to hand it over, and folding
    the upload into ``pack`` would remove that without saying so.
    """
    import ship as ship_mod

    config = require_config()
    endpoint = args.endpoint or config.get("endpoint")
    if not endpoint:
        raise SystemExit(
            "no collection endpoint configured -- run `insight setup "
            "--endpoint https://...` or pass --endpoint. See docs/TRANSPORT.md")

    if not (args.token or config.get("endpoint_token")):
        raise SystemExit(
            "no upload secret on this machine -- run `insight setup --email "
            "you@seta-international.vn`, then send the line it prints to "
            "whoever maintains the server whitelist")

    receipts = ship_mod.load_receipts(RECEIPTS_PATH)
    if args.bundle:
        pending = [os.path.abspath(os.path.expanduser(args.bundle))]
    else:
        pending = ship_mod.unshipped(REPORTS_DIR, receipts)
        # Newest only unless asked otherwise: the weekly ritual sends this
        # week. Someone catching up after leave asks for --all and means it.
        if pending and not args.all:
            pending = pending[-1:]

    if not pending:
        print(json.dumps({"shipped": 0, "detail": "nothing to ship",
                          "endpoint": endpoint}, sort_keys=True))
        return 0

    if args.dry_run:
        for path in pending:
            manifest = ship_mod.read_manifest(path)
            body, digest = ship_mod.digest_of(path)
            print(json.dumps({
                "would_ship": os.path.basename(path),
                "endpoint": endpoint.rstrip("/") + "/v1/bundle",
                "window": manifest.get("window_start"),
                "events": manifest.get("event_count"),
                "bytes": len(body), "sha256": digest,
            }, sort_keys=True))
        return 0

    sent, failed = 0, 0
    for path in pending:
        try:
            receipt = ship_mod.ship_bundle(
                path, endpoint,
                token=args.token or config.get("endpoint_token"),
                previous_token=(None if args.token
                                else config.get("endpoint_token_previous")),
                timeout=args.timeout)
        except ship_mod.ShipError as exc:
            print("FAILED {}: {}".format(os.path.basename(path), exc),
                  file=sys.stderr)
            failed += 1
            continue
        receipts[os.path.basename(path)] = receipt
        # Written per bundle, not at the end. A crash halfway through --all must
        # not lose the record of what already went, or the next run resends it.
        ship_mod.save_receipts(RECEIPTS_PATH, receipts)
        sent += 1
        print(json.dumps(receipt, sort_keys=True))

    if failed:
        print(json.dumps({"shipped": sent, "failed": failed}, sort_keys=True),
              file=sys.stderr)
    return 1 if failed else 0


# --------------------------------------------------------------------------
# auto -- the unattended hourly run
# --------------------------------------------------------------------------

LOCK_PATH = os.path.join(HOME, "auto.lock")
LOG_PATH = os.path.join(HOME, "auto.log")

#: Buffer partitions older than this are removed once a bundle covering them
#: has been uploaded. An hourly job runs forever; without this the buffer on an
#: engineer's laptop grows for as long as they work here.
KEEP_DAYS = 30


def _log(record: Dict[str, Any]) -> None:
    record["at"] = now()
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass  # a full disk must not turn a collection run into a crash loop


def _quietly(step: str, fn, args: argparse.Namespace) -> Tuple[Optional[str], str]:
    """Run one step. Returns ``(error or None, whatever it printed)``.

    Unattended, so a step that fails must not stop the ones after it: Copilot
    not having written a span file is not a reason to skip uploading the agent
    events that are already buffered. Every failure is logged; none is fatal.

    The captured output is how ``auto`` learns which bundle ``pack`` wrote.
    Guessing it from the directory instead would mean guessing wrong exactly
    when two bundles share a timestamp.
    """
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            fn(args)
    except SystemExit as exc:
        return "{}: {}".format(step, exc), buffer.getvalue()
    except Exception as exc:  # noqa: BLE001 -- an hourly job may not crash
        return "{}: {}: {}".format(step, type(exc).__name__, exc), buffer.getvalue()
    return None, buffer.getvalue()


def _prune_buffer(receipts: Dict[str, Any]) -> List[str]:
    """Drop old partitions, but only ones already covered by an upload.

    Pruning on age alone would delete a week that never left the machine
    because shipping had been broken the whole time -- silently turning a fixable
    outage into permanent data loss.
    """
    covered = set()
    for receipt in receipts.values():
        if not isinstance(receipt, dict):
            continue
        window = str(receipt.get("window") or "")
        if "/" in window:
            start, end = window.split("/", 1)
            covered.add((start[:10], end[:10]))

    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    dropped = []
    for day in buffer_days():
        if day >= cutoff:
            continue
        if not any(start <= day <= end for start, end in covered):
            continue
        path = buffer_path(day)
        if os.path.exists(path):
            os.remove(path)
            dropped.append(day)
    return dropped


def cmd_auto(args: argparse.Namespace) -> int:
    """One collection run, for a scheduler rather than a person.

    Reads the local sources, packs the current day, and uploads it only if the
    result differs from what has already gone. A quiet hour therefore costs
    nothing: no object, no request, no line in the report suggesting activity
    that did not happen.
    """
    import ship as ship_mod

    config = load_config()
    if config is None:
        _log({"event": "skipped", "reason": "not initialised"})
        return 0

    # A slow scan on a big repository can outlast the hour. Overlapping runs
    # would pack the same events twice and race on the buffer.
    os.makedirs(HOME, exist_ok=True)
    try:
        lock = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        age = time.time() - os.path.getmtime(LOCK_PATH)
        if age < 6 * 3600:
            _log({"event": "skipped", "reason": "another run holds the lock",
                  "lock_age_s": int(age)})
            return 0
        # Older than any real run: the previous process died without cleaning up.
        os.remove(LOCK_PATH)
        lock = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)

    problems: List[str] = []
    try:
        os.write(lock, str(os.getpid()).encode())
        os.close(lock)

        # First, and before any collection. Not because it is important -- it
        # is the least important thing in this function -- but because every
        # path below can return early on a quiet machine, and an update check
        # that only runs on busy days is one that never runs on the laptops
        # most likely to be behind.
        #
        # It cannot affect this run. The archive it swaps is resolved through
        # `current.pyz` at exec time, so a new version takes effect on the next
        # invocation; and `update.check` is written never to raise, so an
        # endpoint that is down or a manifest that is nonsense is a line in the
        # log and nothing else. The laptop on the plane still collects.
        try:
            import update as update_mod
            outcome = update_mod.check(
                HOME, config.get("endpoint"), version_mod.VERSION,
                _archive(), enabled=bool(config.get("auto_update")))
            if outcome.get("action") not in ("off", "not_due", "current"):
                _log(outcome)
        except Exception as exc:  # noqa: BLE001 -- an hourly job may not crash
            _log({"event": "update_check", "action": "failed",
                  "detail": "{}: {}".format(type(exc).__name__, exc)})

        for step, fn, ns in (
            ("copilot", cmd_copilot, argparse.Namespace(root=None, since=None)),
            # The surface the pilot machines actually use. Ordered after
            # `copilot` only because that one is cheaper when it finds nothing.
            ("vscode", cmd_vscode, argparse.Namespace(root=None)),
            ("rtk", cmd_rtk, argparse.Namespace(probe=False)),
            ("collect", cmd_collect, argparse.Namespace(source=None)),
            ("scan", cmd_scan, argparse.Namespace(repo=None, since_days=7)),
        ):
            problem, _ = _quietly(step, fn, ns)
            if problem:
                problems.append(problem)

        # The current day, declared. Re-packing it as the day fills is what
        # makes an hourly run idempotent: identical content hashes identically,
        # and the proxy already refuses a digest it holds.
        today = now()[:10]
        pack_problem, packed = _quietly("pack", cmd_pack, argparse.Namespace(
            window_start=None, window_end=None,
            since=today, until=today, clear=False))
        if pack_problem:
            problems.append(pack_problem)
            _log({"event": "failed", "problems": problems})
            return 0
        try:
            newest = json.loads(packed)["bundle"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            problems.append("pack: unreadable result: {}".format(exc))
            _log({"event": "failed", "problems": problems})
            return 0

        # Deduped on the events, never on the file name and never on the whole
        # file. The name has second resolution, so two packs in one second
        # collide; the file carries `packed_at`, so it differs on every run even
        # when nothing happened. The manifest's own checksum covers the events
        # alone and is the only one of the three that stays still when the hour
        # was genuinely quiet.
        receipts = ship_mod.load_receipts(RECEIPTS_PATH)
        content = ship_mod.read_manifest(newest).get("sha256")
        already = any(isinstance(r, dict) and r.get("content_sha256") == content
                      for r in receipts.values())
        if already:
            # Nothing new since the last upload. Drop the duplicate rather than
            # leaving 24 identical bundles a day in .reports/.
            os.remove(newest)
            _log({"event": "no_change", "problems": problems})
            return 0

        if not config.get("endpoint") or not config.get("endpoint_token"):
            _log({"event": "packed_not_sent", "bundle": os.path.basename(newest),
                  "reason": "no endpoint or no upload secret", "problems": problems})
            return 0

        # Until it sticks. A laptop set up before the admin added the address
        # would otherwise 401 forever with nobody watching; retrying here costs
        # one request an hour and turns that into a wait.
        #
        # Also when the machine is enrolled but holds no board list. That is
        # not a hypothetical: every laptop enrolled before projects existed is
        # in exactly that state, and a machine with no allow-list does not fail
        # quietly -- it invents Jira keys from anything key-shaped, which is
        # the AR-1 fabrication this project has already shipped twice. One
        # request an hour until it has an answer is the cheapest way to reach
        # a fleet nobody can log into.
        if not config.get("enrolled_at") or not config.get("jira_projects"):
            outcome = enroll_identity(config)
            record_enrolment(outcome)
            if outcome.get("ok"):
                _log({"event": "enrolled", "outcome": outcome.get("outcome")})
                config = load_config() or config

        # Collected, sealed, and deliberately held. The bundle stays on disk and
        # the next due run ships the day whole -- `pack` re-seals the same day,
        # so waiting costs nothing but the wait.
        if not (getattr(args, "force_ship", False) or ship_due()):
            _log({"event": "batched", "bundle": os.path.basename(newest),
                  "reason": "within the upload interval",
                  "next_due_s": int(SHIP_MIN_INTERVAL_S -
                                    (time.time() - os.path.getmtime(SHIP_STAMP))),
                  "problems": problems})
            return 0

        try:
            receipt = ship_mod.ship_bundle(
                newest, config["endpoint"],
                token=config.get("endpoint_token"),
                previous_token=config.get("endpoint_token_previous"))
        except ship_mod.ShipError as exc:
            # Kept on disk. The next run retries it, and a week of failed
            # uploads still ends with every bundle recoverable by hand.
            _log({"event": "ship_failed", "bundle": os.path.basename(newest),
                  "error": str(exc), "problems": problems})
            return 0

        receipts[os.path.basename(newest)] = receipt
        ship_mod.save_receipts(RECEIPTS_PATH, receipts)
        # Only on success. A failed upload must leave the next run due, not
        # push it an hour away.
        with open(SHIP_STAMP, "w", encoding="utf-8") as handle:
            handle.write(str(int(time.time())))
        dropped = _prune_buffer(receipts)
        _log({"event": "shipped", "bundle": os.path.basename(newest),
              "status": receipt["status"], "key": receipt.get("key"),
              "pruned_days": dropped, "problems": problems})
        return 0
    finally:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)



def cmd_backfill(args: argparse.Namespace) -> int:
    """Everything on this machine since a date, in one command.

    The six-command sequence this replaces was six commands because each of
    them is a different reader with a different notion of "since": the journals
    take a date, `scan` takes a number of days, `pack` takes a range and `ship`
    takes a flag. A person backfilling a laptop should not have to hold that.

    Safe to run twice. Every event id is derived from the fact it describes
    rather than minted per run, so a day read, packed and shipped twice is one
    day at the far end -- the proxy answers 409 to a digest it already holds.
    """
    config = require_config()
    since = args.since
    try:
        start = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise SystemExit("--since takes a date: YYYY-MM-DD")
    today = datetime.now(timezone.utc)
    if start > today:
        raise SystemExit("--since is in the future")
    days = max(1, (today - start).days + 1)

    steps: List[Dict[str, Any]] = []
    problems: List[str] = []

    def run(step: str, fn, ns: argparse.Namespace) -> str:
        problem, out = _quietly(step, fn, ns)
        if problem:
            problems.append(problem)
            steps.append({"step": step, "ok": False, "detail": problem})
        else:
            detail = ""
            try:
                payload = json.loads(out.strip().splitlines()[-1])
                detail = json.dumps({
                    k: payload[k] for k in
                    ("events", "written", "already_buffered", "present",
                     "sessions_read", "sessions", "repos", "commits",
                     "event_count", "days_covered")
                    if k in payload}, sort_keys=True)
            except (ValueError, IndexError):
                detail = out.strip().splitlines()[-1] if out.strip() else ""
            steps.append({"step": step, "ok": True, "detail": detail})
        return out

    # The readers. `--since` is an optimisation for the journal reader and a
    # requirement for none of them: skipping a journal wrongly costs a re-read,
    # never a lost day, because ids are deterministic.
    run("copilot", cmd_copilot, argparse.Namespace(root=None, since=since))
    run("vscode", cmd_vscode, argparse.Namespace(root=None))
    run("rtk", cmd_rtk, argparse.Namespace(probe=False))
    run("collect", cmd_collect, argparse.Namespace(source=None))
    run("scan", cmd_scan, argparse.Namespace(repo=None, since_days=days))

    until = today.strftime("%Y-%m-%d")
    packed = run("pack", cmd_pack, argparse.Namespace(
        window_start=None, window_end=None, since=since, until=until,
        clear=False))

    bundle = None
    try:
        bundle = json.loads(packed.strip().splitlines()[-1])
    except (ValueError, IndexError):
        pass

    shipped = None
    if args.no_ship:
        pass
    elif not config.get("endpoint") or not config.get("endpoint_token"):
        steps.append({"step": "ship", "ok": True,
                      "detail": "no endpoint -- the bundle is in {}".format(
                          REPORTS_DIR)})
    else:
        shipped = run("ship", cmd_ship, argparse.Namespace(
            bundle=None, all=True, endpoint=None, token=None, timeout=60,
            dry_run=args.dry_run))

    print()
    for step in steps:
        print("[{}] {:<9} {}".format(
            "  ok  " if step["ok"] else " FAIL ", step["step"],
            step.get("detail") or ""))
    print()
    if bundle:
        made = bundle.get("bundles") or []
        print("{:,} events, {} day(s), {} -> {}".format(
            bundle.get("event_count", 0), len(bundle.get("days_covered") or []),
            since, until))
        if len(made) > 1:
            # Said out loud because the alternative used to be silent. One
            # bundle per ISO week is what keeps each week in its own folder at
            # the endpoint; a single bundle spanning the range filed the whole
            # backfill under its first week.
            print("{} bundles, one per week: {}".format(
                len(made), ", ".join(
                    "{} ({:,})".format(b["week"], b["event_count"])
                    for b in made if b.get("week"))))
    if args.no_ship or args.dry_run:
        print("Nothing was uploaded. `insight ship --all` sends it.")
    if problems:
        print()
        print("{} step(s) had a problem; the rest still ran.".format(
            len(problems)), file=sys.stderr)
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    import schedule as schedule_mod

    if args.status or not (args.hourly or args.off):
        state = {
            "installed": schedule_mod.installed(),
            "platform": schedule_mod.system(),
            "interval_seconds": schedule_mod.INTERVAL_SECONDS,
            "log": LOG_PATH if os.path.exists(LOG_PATH) else None,
        }
        if os.path.exists(LOG_PATH):
            with open(LOG_PATH, "r", encoding="utf-8") as handle:
                lines = handle.readlines()[-1:]
            if lines:
                try:
                    state["last_run"] = json.loads(lines[0])
                except json.JSONDecodeError:
                    pass
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0

    if args.off:
        result = schedule_mod.remove()
        print(json.dumps({"scheduled": False, **result}, sort_keys=True))
        return 0

    config = require_config()
    insight = _executable()

    if not args.yes:
        # Said before it is switched on, not in a file someone may never open.
        print()
        print("Every hour, this machine will:")
        print("  - read Copilot's span file, the agent buffer, and git history")
        print("  - pack today's events into a bundle")
        print("  - upload it to {}".format(config.get("endpoint") or "(no endpoint set)"))
        print()
        print("It uploads nothing when nothing has changed, and it never")
        print("collects prompts, replies, code or diffs -- the same limits as")
        print("the manual commands.")
        print()
        print("What you give up by turning this on: today you read a bundle")
        print("before sending it. After this, it goes on its own. Everything")
        print("sent stays readable in {} and `purge` still works.".format(REPORTS_DIR))
        print()
        print("`./insight schedule --off` reverses this at any time.")
        print()
        if not ask_yes("Collect and upload hourly from this machine? [y/N] "):
            print("nothing was scheduled")
            return 1

    try:
        result = schedule_mod.install(insight, LOG_PATH)
    except schedule_mod.ScheduleError as exc:
        raise SystemExit(str(exc))
    print(json.dumps({"scheduled": True, **result}, sort_keys=True))
    print("Runs hourly. `./insight schedule --status` shows the last run, "
          "`--off` stops it.")
    return 0


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# enrol -- the step that used to be a person
# --------------------------------------------------------------------------

def enroll_identity(config: Dict[str, Any], timeout: int = 15) -> Dict[str, Any]:
    """Register this machine's fingerprint with the endpoint.

    This replaces the worst step in the old setup: read a fingerprint off your
    screen, send it to an admin over chat, wait for them to edit a file and
    restart a service, and until all of that happens every upload is a 401 that
    looks like a bug. The endpoint accepts the first fingerprint offered for an
    address somebody has put on its roster, so the admin's work is adding an
    address once -- which they already did, for the coverage report.

    Only ever sends the fingerprint. The secret does not travel, here or
    anywhere: `identity.py` argues that point at length and this does not make
    an exception to it.
    """
    import identity

    endpoint = config.get("endpoint")
    email = config.get("email")
    secret = config.get("endpoint_token")
    if not endpoint or not email or not secret:
        return {"ok": False, "outcome": "not_configured"}

    url = endpoint.rstrip("/")
    url = url[:-len("/v1/bundle")] if url.endswith("/v1/bundle") else url
    payload = json.dumps({
        "email": email, "fingerprint": identity.fingerprint(secret),
    }).encode("utf-8")

    import urllib.error
    import urllib.request
    request = urllib.request.Request(
        url + "/v1/enroll", data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "insight/" + version_mod.VERSION})
    # Sent when there is one. A machine that is already enrolled and is
    # rotating proves who it is this way, and skips the roster entirely.
    if config.get("endpoint_token_previous") or config.get("enrolled_at"):
        request.add_header("Authorization", "Bearer " + secret)
    try:
        with urllib.request.urlopen(
                request, timeout=timeout,
                context=common.ssl_context()) as response:
            body = json.loads(response.read().decode("utf-8") or "{}")
            boards = body.get("jira_projects")
            return {"ok": True, "status": response.status,
                    "outcome": body.get("outcome") or "enrolled",
                    # The endpoint knows which Jira boards are real; this
                    # machine cannot. Without them every reader here runs with
                    # no allow-list and mints keys from anything key-shaped --
                    # `fix/AUG-25` became ticket "AUG-25" on 28 of 28 events
                    # from a laptop enrolled the morning of 2026-08-26.
                    "jira_projects": [str(b) for b in boards]
                    if isinstance(boards, list) else None}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error") or ""
        except Exception:  # noqa: BLE001 -- an error page is not JSON
            pass
        return {"ok": False, "status": exc.code, "outcome": "rejected",
                "detail": detail}
    except Exception as exc:  # noqa: BLE001 -- offline is not a failed setup
        return {"ok": False, "outcome": "unreachable",
                "detail": "{}: {}".format(type(exc).__name__, exc)}


def record_enrolment(result: Dict[str, Any]) -> None:
    config = load_config()
    if config is None or not result.get("ok"):
        return
    config["enrolled_at"] = now()
    # Refreshed on every successful enrolment, so an admin adding a board does
    # not need anyone to reinstall. A response that carries no list at all
    # leaves whatever is configured alone -- an older endpoint saying nothing
    # must not silently disarm a machine that already knows its boards.
    boards = result.get("jira_projects")
    if isinstance(boards, list):
        config["jira_projects"] = boards
    write_json(CONFIG_PATH, config)
    os.chmod(CONFIG_PATH, 0o600)


def cmd_enroll(args: argparse.Namespace) -> int:
    """Register with the endpoint, or say exactly why it did not work."""
    config = require_config()
    result = enroll_identity(config)
    record_enrolment(result)
    print(json.dumps(result, sort_keys=True))
    if result.get("ok"):
        print()
        print("This machine can upload. Nothing else to do.")
        return 0
    if result.get("status") == 403:
        print()
        print("Nobody is expecting {} yet. Ask whoever runs the pipeline to "
              "add it;".format(config.get("email")))
        print("no fingerprint needs to travel -- this retries by itself on the "
              "next run.")
    elif result.get("status") == 409:
        print()
        print("That address is enrolled from another machine. A replacement "
              "laptop needs")
        print("the old entry reset by whoever runs the pipeline.")
    elif result.get("outcome") == "unreachable":
        print()
        print("The endpoint could not be reached. Collection continues; this "
              "retries on the next run.")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    """Print the whitelist line again.

    Exists because the line printed at setup scrolls away, and the failure it
    prevents -- a 401 an engineer cannot diagnose -- costs more than the command.
    """
    config = require_config()
    if not config.get("endpoint_token"):
        raise SystemExit("no upload secret on this machine -- run "
                         "`insight rotate-token` to mint one")
    print_whitelist_line(config)
    if config.get("endpoint_token_previous"):
        import identity
        print()
        print("A rotation is still in flight. The previous line stays valid "
              "until it is removed:")
        print()
        print("    " + (identity.whitelist_line(
            config["email"], config["endpoint_token_previous"])
            if config.get("email")
            else identity.fingerprint(config["endpoint_token_previous"])))
    return 0


def cmd_rotate_token(args: argparse.Namespace) -> int:
    """Mint a new upload secret without breaking the old one.

    The new fingerprint has to reach a ``.env`` maintained by someone else
    before it works, so the previous secret is retained and ``ship`` falls back
    to it on a 401. Rotation costs no downtime and needs no scheduling, which is
    the only way it happens more than once.
    """
    import identity

    config = require_config()
    if not (config.get("email") or config.get("endpoint_token")):
        raise SystemExit(
            "nothing to rotate -- this machine has no upload secret. Run "
            "`insight setup --token <the secret you were sent>`, or "
            "`insight setup --email you@example.com` to mint one here")

    if args.finish:
        if not config.get("endpoint_token_previous"):
            print("no rotation in flight")
            return 0
        config["endpoint_token_previous"] = None
        write_json(CONFIG_PATH, config)
        os.chmod(CONFIG_PATH, 0o600)
        print("previous secret discarded. Ask for its line to be removed from "
              "INSIGHT_ALLOWED.")
        return 0

    new_secret, previous = identity.rotate(config)
    config["endpoint_token"] = new_secret
    config["endpoint_token_previous"] = previous or None
    write_json(CONFIG_PATH, config)
    os.chmod(CONFIG_PATH, 0o600)

    print_whitelist_line(config)
    if previous:
        print()
        print("Add it ALONGSIDE the existing one rather than replacing it -- "
              "INSIGHT_ALLOWED takes")
        print("both as `{}:<new>:<old>`. Uploads keep working throughout."
              .format(config.get("email") or "<your address>"))
        print("Once it is in place, `insight rotate-token --finish` drops the "
              "old secret here.")
    return 0


# --------------------------------------------------------------------------
# status / purge
# --------------------------------------------------------------------------

def cmd_update(args: argparse.Namespace) -> int:
    """Look at, change, or explain this machine's update setting.

    Deliberately its own command rather than a flag on `schedule`. Replacing
    the binary and uploading on a timer are two different promises, and someone
    switching one off should not have to discover that it was spelled as an
    option to the other.
    """
    import update as update_mod
    config = require_config()

    if args.on or args.off:
        config["auto_update"] = bool(args.on)
        write_json(CONFIG_PATH, config)
        print(json.dumps({"auto_update": bool(args.on)}))
        if args.on and not update_mod.installation(_archive()):
            print("Recorded -- but this is a checkout, not an installed "
                  "archive, so nothing will actually update. Use `git pull`.",
                  file=sys.stderr)
        return 0

    state = update_mod.load_state(HOME)
    if args.pin or args.unpin:
        pin = None if args.unpin else args.pin
        if pin and not update_mod.parse_version(pin):
            raise SystemExit("{!r} is not a version like 0.3.0".format(pin))
        state["pinned"] = pin
        update_mod.save_state(HOME, state)
        print(json.dumps({"pinned": pin}))
        return 0

    if args.check or args.now:
        outcome = update_mod.check(
            HOME, config.get("endpoint"), version_mod.VERSION, _archive(),
            enabled=True, apply_it=bool(args.now), force=True)
        print(json.dumps(outcome, indent=2, sort_keys=True))
        # Non-zero only when the check itself failed. "held", "pinned" and
        # "refused" are this working as designed, and a scripted caller should
        # not have to special-case them.
        return 1 if outcome.get("action") == "failed" else 0

    print(json.dumps(update_mod.status(
        HOME, version_mod.VERSION, _archive(),
        bool(config.get("auto_update"))), indent=2, sort_keys=True))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config()
    if config is None:
        print(json.dumps({"initialised": False, "home": HOME}))
        return 0
    events = read_buffer()
    counts: Dict[str, int] = {}
    for event in events:
        counts[event["event_type"]] = counts.get(event["event_type"], 0) + 1
    days = buffer_days()
    bundles = sorted(
        f for f in os.listdir(REPORTS_DIR) if f.endswith(".ndjson")
    ) if os.path.isdir(REPORTS_DIR) else []
    import identity
    import schedule as schedule_mod
    import ship as ship_mod
    import update as update_mod
    receipts = ship_mod.load_receipts(RECEIPTS_PATH)
    print(json.dumps({
        "initialised": True,
        "home": HOME,
        "machine_id": config["machine_id"],
        "consent_at": config.get("consent_at"),
        "buffered_events": len(events),
        "buffered_by_type": counts,
        "days_buffered": days,
        "repos": known_repos(),
        "bundles": len(bundles),
        "last_bundle": bundles[-1] if bundles else None,
        "endpoint": config.get("endpoint"),
        "email": config.get("email"),
        # The fingerprint, never the secret. `status` output gets pasted into
        # tickets and chat when something is wrong, so it must stay safe to paste.
        "token_fingerprint": (
            identity.fingerprint(config["endpoint_token"])
            if config.get("endpoint_token") else None),
        "rotation_in_flight": bool(config.get("endpoint_token_previous")),
        "hourly": schedule_mod.installed(),
        "shipped": len(receipts),
        # Named, not counted. "3 bundles waiting" is a number to ignore; the
        # file names are what someone acts on.
        # Which version this is, whether it replaces itself, and when it last
        # looked. Folded in here rather than left to `update --status` because
        # "what is this machine doing on its own" is one question.
        "update": update_mod.status(HOME, version_mod.VERSION, _archive(),
                                    bool(config.get("auto_update"))),
        "unshipped": [os.path.basename(p)
                      for p in ship_mod.unshipped(REPORTS_DIR, receipts)],
        "last_shipped_at": max(
            (r.get("shipped_at") for r in receipts.values()
             if isinstance(r, dict) and r.get("shipped_at")), default=None),
    }, indent=2, sort_keys=True))
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    if not args.yes:
        if not ask_yes(
                "Delete every event, bundle and the config in {}? [y/N] ".format(
                    HOME)):
            print("nothing deleted")
            return 1
    import ship as ship_mod
    shipped = len(ship_mod.load_receipts(RECEIPTS_PATH))
    removed = 0
    for directory in (BUFFER_DIR, REPORTS_DIR):
        if not os.path.isdir(directory):
            continue
        for entry in os.listdir(directory):
            os.remove(os.path.join(directory, entry))
            removed += 1
    if os.path.exists(RECEIPTS_PATH):
        os.remove(RECEIPTS_PATH)
        removed += 1
    import update as update_mod
    for path in (LOCK_PATH, LOG_PATH, update_mod.state_path(HOME)):
        if os.path.exists(path):
            os.remove(path)
            removed += 1
    if args.all:
        # Someone purging everything expects the collection to stop, not to
        # keep running hourly against a config they just deleted.
        import schedule as schedule_mod
        schedule_mod.remove()
    if os.path.exists(CONFIG_PATH) and args.all:
        os.remove(CONFIG_PATH)
    print(json.dumps({"files_removed": removed, "config_removed": bool(args.all)}))
    # Said plainly rather than implied. `purge` has always been a local command
    # and cannot reach a bundle already sent; letting someone believe otherwise
    # would be a worse failure of the consent model than not offering it.
    if shipped:
        print("{} bundle(s) had already been sent and are not affected by this "
              "-- deleting them is a request to whoever runs the pipeline."
              .format(shipped), file=sys.stderr)
    return 0


# --------------------------------------------------------------------------
# help -- the guide, in the tool
# --------------------------------------------------------------------------

GUIDE = """\
insight -- what your AI assistance actually produces, measured from the
records Copilot already keeps on this machine.

SETUP

    curl -fsSL {endpoint}/install | sh

    One command. It installs, then asks for your work email, then prints one
    line to send to whoever runs the pipeline. That line is a fingerprint, not
    a secret.

WHAT IT READS

    ~/.copilot/session-state/     premium requests, token counts, model ids,
                                  tool names and their verdicts
    VS Code chatSessions/         per-call latency, tool names, tokens
    rtk history, if installed     per-command token counts
    git, in repos Copilot used    commit hashes, line counts, AI trailers

WHAT IT NEVER READS

    Prompts, replies, source code, diffs, file contents, secrets. Counts,
    hashes and fixed categories only. Paths are repo-relative, so your
    username never leaves. Your email travels in one header and is stored as
    a salted hash.

WHEN IT RUNS

    When Copilot runs. A hook at ~/.copilot/hooks/seta-insight.json fires
    before each tool call, returns in milliseconds, and collects at most once
    every 10 minutes. Uploads are batched at most once an hour. No daemon, no
    open port.

SEEING WHAT LEAVES

    insight status                what is buffered, what has been sent
    insight pack                  seal what is buffered
    insight ship --dry-run        what would be sent, without sending
    insight ship                  send it

SENDING NOW, AND BACKFILLING

    insight backfill --since 2026-08-01     everything since that day: read,
                                            packed and sent, in one command
    insight auto --force-ship               send what is buffered now, without
                                            waiting for the hourly batch

    Installing this does not start the clock -- the readers see every journal
    already on disk, so a machine with months of history can send all of it.
    Add --no-ship to pack it and look first.

    Running it twice is safe. Event ids are derived from the fact rather than
    minted per run, so a day collected twice is one day at the far end.

    A backfill spanning weeks makes one bundle per ISO week and says so. The
    endpoint files a bundle by the week its window starts in, so one bundle
    across four weeks would put all four in the first week's folder and the
    other three would read as weeks nobody sent.

STAYING CURRENT

    insight update --status       which version this is, and when it last looked
    insight update --now          take a new release now
    insight update --off          stop replacing itself

    It updates itself on the hourly run by default: it checks the endpoint,
    verifies the digest, swaps the archive atomically and rolls back if the new
    one fails its own smoke test. Re-running the installer does the same thing
    and is the answer when the tool itself will not start:

        curl -fsSL {endpoint}/update | sh

CONTROL

    insight whoami                your allow-list line, again
    insight rotate-token          replace the upload secret
    insight schedule --off        stop collecting automatically
    insight purge --yes           delete every event and bundle held here
    insight purge --yes --all     that, and forget this machine entirely

    Everything here is local. A bundle already uploaded is already uploaded;
    removing one is a request to whoever runs the pipeline.

These figures describe how a way of working is going. They are not a
performance record and do not support assessing anyone individually.

`insight <command> --help` for any command. Full list: `insight --help`.
"""


def cmd_help(args: argparse.Namespace) -> int:
    """The guide, in the tool.

    `--help` lists commands; it does not say what the thing does or what it
    keeps off the machine. Somebody deciding whether to run this should not
    have to find a web page to answer that.
    """
    config = load_config() or {}
    print(GUIDE.format(
        endpoint=config.get("endpoint") or DEFAULT_ENDPOINT))
    return 0


# --------------------------------------------------------------------------

class _VersionAction(argparse.Action):
    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS,
                         **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        print(version_line())
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="insight",
        description="Collect AI-effectiveness telemetry from this machine.",
    )
    # `action="version"` would build the line -- and hash the archive -- on
    # every single invocation, including the hourly `auto` run. This defers it
    # to the one call that asks.
    parser.add_argument("--version", action=_VersionAction,
                        help="version, schema, interpreter and this file's digest")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("help", help="what this collects, and how to control it")
    p.set_defaults(func=cmd_help)

    p = sub.add_parser("init", help="record consent and create the local store")
    p.add_argument("--yes", action="store_true", help="skip the consent prompt")
    p.add_argument("--force", action="store_true", help="rewrite an existing config")
    p.add_argument("--endpoint",
                   help="where `ship` sends bundles (default: {})".format(
                       DEFAULT_ENDPOINT))
    p.add_argument("--email", help="your work email -- identifies you to the "
                                   "server whitelist, never stored in a bundle")
    p.add_argument("--no-endpoint", action="store_true",
                   help="do not upload; bundles are handed over by hand")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser(
        "setup", help="set this machine up, by asking (the usual first command)")
    # No --repo. Copilot's journals name every git tree it has worked in, so
    # `scan` discovers them -- see copilot_read.discover_repos.
    p.add_argument("--aiep", help="path to ai-engineering-platform "
                                  "(default: beside this repo)")
    p.add_argument("--endpoint",
                   help="where `ship` sends bundles (default: {})".format(
                       DEFAULT_ENDPOINT))
    p.add_argument("--email", help="your work email -- identifies you to the "
                                   "server whitelist, never stored in a bundle")
    p.add_argument("--no-endpoint", action="store_true",
                   help="do not upload; bundles are handed over by hand")
    p.add_argument("--no-schedule", action="store_true",
                   help="do not collect hourly; run the commands by hand")
    p.add_argument("--no-auto-update", action="store_true",
                   help="do not install new versions automatically")
    p.add_argument("--no-commit-hook", action="store_true",
                   help="do not write AI-Run-Id trailers into commits")
    p.add_argument("--yes", action="store_true",
                   help="skip the questions and accept the defaults")
    p.add_argument("--force", action="store_true",
                   help="replace an existing prepare-commit-msg hook")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would change and write nothing")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser(
        "install-hook",
        help="write AI-Run-Id trailers into commits made during an agent run")
    p.add_argument("--repo", default=".", help="repository path (default: .)")
    p.add_argument("--force", action="store_true",
                   help="replace an existing prepare-commit-msg hook")
    p.set_defaults(func=cmd_install_hook)

    p = sub.add_parser(
        "scan", help="read commit metadata; with no --repo, every registered one")
    p.add_argument("--repo", help="a repository to scan and remember "
                                  "(default: every one already registered)")
    p.add_argument("--since-days", type=int, default=7)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser(
        "copilot", help="read Copilot's session journals (~/.copilot)")
    p.add_argument("--root", help="override Copilot's home (default ~/.copilot)")
    p.add_argument("--since", help="skip journals untouched since YYYY-MM-DD. "
                                   "An optimisation only -- event ids are "
                                   "deterministic, so a wrongly skipped journal "
                                   "costs a re-read and never a duplicate")
    p.set_defaults(func=cmd_copilot)

    p = sub.add_parser("collect", help="read the emit.py buffer")
    p.add_argument("--source", help="override the emit.py buffer path")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("pack", help="seal a bundle to hand over")
    p.add_argument("--window-start", help="override the declared window start")
    p.add_argument("--window-end", help="override the declared window end")
    p.add_argument("--since", help="first day to pack, YYYY-MM-DD (inclusive)")
    p.add_argument("--until", help="last day to pack, YYYY-MM-DD (inclusive)")
    p.add_argument("--clear", action="store_true",
                   help="remove the partitions that were packed")
    p.set_defaults(func=cmd_pack)

    p = sub.add_parser(
        "ship", help="send a sealed bundle to the collection endpoint")
    p.add_argument("--bundle", help="a specific bundle file "
                                    "(default: the newest not yet sent)")
    p.add_argument("--all", action="store_true",
                   help="send every bundle that has not gone yet")
    p.add_argument("--endpoint", help="override the configured endpoint")
    p.add_argument("--token", help="override the configured bearer token")
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be sent, and send nothing")
    p.set_defaults(func=cmd_ship)

    p = sub.add_parser(
        "enroll", help="register this machine with the endpoint")
    p.set_defaults(func=cmd_enroll)

    p = sub.add_parser(
        "whoami", help="print the whitelist line to send to the server admin")
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser(
        "rotate-token", help="mint a new upload secret, keeping the old valid")
    p.add_argument("--finish", action="store_true",
                   help="discard the previous secret once the new line is live")
    p.set_defaults(func=cmd_rotate_token)

    p = sub.add_parser(
        "auto", help="one unattended collection run (what the scheduler calls)")
    p.add_argument("--force-ship", action="store_true",
                   help="upload now, ignoring the batching interval")
    p.set_defaults(func=cmd_auto)

    p = sub.add_parser(
        "backfill",
        help="read, pack and send everything on this machine since a date")
    p.add_argument("--since", required=True, metavar="YYYY-MM-DD",
                   help="first day to collect")
    p.add_argument("--no-ship", action="store_true",
                   help="pack it, upload nothing")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be uploaded, and upload nothing")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser(
        "schedule", help="collect hourly instead of remembering to")
    p.add_argument("--hourly", action="store_true", help="turn it on")
    p.add_argument("--off", action="store_true", help="turn it off")
    p.add_argument("--status", action="store_true",
                   help="is it on, and when did it last run")
    p.add_argument("--yes", action="store_true", help="skip the prompt")
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser(
        "update", help="what version this is, and whether it updates itself")
    p.add_argument("--status", action="store_true",
                   help="version, setting, and when it last looked (default)")
    p.add_argument("--check", action="store_true",
                   help="look now and report; install nothing")
    p.add_argument("--now", action="store_true",
                   help="look now and install if there is something newer")
    p.add_argument("--on", action="store_true", help="check daily from now on")
    p.add_argument("--off", action="store_true", help="stop checking")
    p.add_argument("--pin", metavar="VERSION",
                   help="stay on this version until --unpin")
    p.add_argument("--unpin", action="store_true")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser(
        "vscode", help="read VS Code's Copilot Chat store")
    p.add_argument("--root", help="override the VS Code User directory")
    p.set_defaults(func=cmd_vscode)

    p = sub.add_parser(
        "rtk", help="read rtk's per-command history, if installed")
    p.add_argument("--probe", action="store_true",
                   help="report what rtk is on this machine and exit")
    p.set_defaults(func=cmd_rtk)

    p = sub.add_parser(
        "hook", help="called by Copilot before a tool runs; returns immediately")
    p.add_argument("--now", action="store_true",
                   help="collect in the foreground instead of detaching")
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("status", help="what is buffered and what was packed")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("purge", help="delete everything collected on this machine")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--all", action="store_true", help="also remove the config")
    p.set_defaults(func=cmd_purge)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # A bare `insight` used to be an argparse error on stderr with exit 2.
        # The person who typed it is asking what this is; that is the guide.
        return cmd_help(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
