#!/usr/bin/env python3
"""``insight`` -- the local collector that runs on an engineer's own machine.

Three signals never leave a developer's laptop, and no amount of API polling
recovers them: Copilot's token usage (its OTel exporter writes locally), which
platform agent ran (``emit.py`` writes a local NDJSON buffer), and the
``AI-Run-Id`` trailer written at commit time. This reads those and packs them
into one bundle to hand over.

**There is no daemon.** Copilot's exporter has a ``file`` mode, so it writes its
own spans to disk and this reads them when asked. ``pack`` is a batch read of
local files, run once a week. See ``docs/ARCHITECTURE.md``.

Standard library only, Python 3.9+. No virtualenv, nothing to install, nothing
to resolve on someone else's machine.

    ./insight init         consent, machine id, salt
    ./insight install-hook write AI-Run-Id trailers at commit time
    ./insight scan         read git history in a repository
    ./insight collect   read the emit.py buffer
    ./insight pack      seal a bundle to hand over
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
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (os.path.join(_ROOT, "pollers"), os.path.join(_ROOT, "collector")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import common  # noqa: E402  (path set above)
import main as collector_main  # noqa: E402

HOME = os.environ.get("SETA_INSIGHT_HOME") or os.path.join(
    os.path.expanduser("~"), ".seta-insight"
)  # everything this tool ever writes lives under here, and nowhere else
CONFIG_PATH = os.path.join(HOME, "config.json")
BUFFER_DIR = os.path.join(HOME, "buffer")
REPORTS_DIR = os.path.join(HOME, ".reports")

#: Where ``emit.py`` writes, per ai-engineering-platform.
EMIT_BUFFER = os.path.join(os.path.expanduser("~"), ".aiep", "telemetry")

BUNDLE_FORMAT = "seta-insight-bundle/1"

CONSENT_TEXT = """
This collects, from this machine:

  - which platform agent ran, on which ticket, and how long it took
  - Copilot token counts, model ids and latency
  - commit hashes, line counts, and AI provenance markers

It never collects prompts, responses, source code, diffs, file contents, or
secrets. Counts, hashes and fixed categories only.

Nothing is sent anywhere. Everything stays in {home} until you run `pack`
and hand the bundle over yourself. You can read any bundle before you send it,
and `purge` deletes everything at any time.

These figures describe how a way of working is going. They are not a
performance record and do not support individual assessment.
"""


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


def cmd_init(args: argparse.Namespace) -> int:
    existing = load_config()
    if existing and not args.force:
        print("already initialised at {}".format(CONFIG_PATH))
        print("consent recorded {}".format(existing.get("consent_at")))
        return 0

    print(CONSENT_TEXT.format(home=HOME))
    if not args.yes:
        answer = input("Collect telemetry from this machine? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
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
    }
    write_json(CONFIG_PATH, config)
    print("initialised. machine id {}".format(config["machine_id"][:8]))
    return 0


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


#: Where Copilot's file exporter writes, per otel.outfile. It appends and never
#: rotates, so something has to. See cmd_otel.
COPILOT_SPANS = os.path.join(HOME, "copilot-spans.jsonl")


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
    import vscode_setup

    steps: List[Dict[str, Any]] = []

    aiep = args.aiep or os.path.join(_ROOT, "..", "ai-engineering-platform")
    aiep = os.path.abspath(aiep) if os.path.isdir(
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
        steps.append({"step": "vscode", "ok": False,
                      "detail": "no VS Code settings directory found"})
    else:
        changed, detail, keys = vscode_setup.apply(
            path, vscode_setup.desired(HOME, aiep), args.dry_run)
        steps.append({"step": "vscode", "ok": True, "changed": changed,
                      "detail": detail, "settings": path,
                      "keys": sorted(keys), "agents_registered": bool(aiep)})

    if not args.dry_run:
        if load_config() is None:
            cmd_init(argparse.Namespace(yes=args.yes, force=False))
        steps.append({"step": "consent", "ok": True,
                      "detail": "recorded" if load_config() else "declined"})

        for repo in args.repo or []:
            try:
                cmd_install_hook(argparse.Namespace(repo=repo, force=args.force))
                steps.append({"step": "hook", "ok": True, "repo": repo})
            except SystemExit as exc:
                steps.append({"step": "hook", "ok": False, "repo": repo,
                              "detail": str(exc)})

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
        print("Then: ./insight otel && ./insight collect && ./insight pack")
    return 0 if all(s.get("ok") for s in steps) else 1


# --------------------------------------------------------------------------
# install-hook
# --------------------------------------------------------------------------

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

    with open(HOOK_SOURCE, "r", encoding="utf-8") as handle:
        body = handle.read()
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
        jira_key = common.extract_jira_key(subject, branch)
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
        targets = known_repos()
        if not targets:
            raise SystemExit(
                "no repositories registered. Run `insight scan --repo <path>` "
                "or `insight install-hook --repo <path>` once per repository "
                "and they will be remembered.")

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
# otel -- read Copilot's own file, then rotate it
# --------------------------------------------------------------------------

def cmd_otel(args: argparse.Namespace) -> int:
    require_config()
    source = args.source or COPILOT_SPANS
    if not os.path.exists(source):
        # Not an error: the exporter has not written yet, or is not configured.
        # Reporting it as a fact beats failing on a machine that is fine.
        print(json.dumps({"source": source, "present": False, "events": 0}))
        return 0

    import otel_read  # local: keeps `insight` importable without it

    result = otel_read.to_events(source)
    problems = otel_read.verify_no_content(result["events"])
    if problems:
        for problem in problems[:5]:
            print("REJECTED " + problem, file=sys.stderr)
        raise SystemExit("attributes outside the allow-list; nothing written "
                         "and the source left untouched")

    written, duplicates = append_events(result["events"])

    rotated = False
    if not args.keep_raw:
        # Truncated, not archived. The raw file can hold prompts, system
        # instructions and command output -- microsoft/vscode#326254 -- and
        # keeping copies multiplies that exposure on someone's laptop for no
        # gain, since the events carry everything we are allowed to keep.
        with open(source, "w", encoding="utf-8"):
            pass
        rotated = True

    print(json.dumps({
        "source": source, "present": True,
        "spans_read": result["spans_read"], "events": len(result["events"]),
        "written": written, "already_buffered": duplicates,
        "source_truncated": rotated,
    }, sort_keys=True))
    return 0


# --------------------------------------------------------------------------
# pack
# --------------------------------------------------------------------------

def cmd_pack(args: argparse.Namespace) -> int:
    config = require_config()
    events = read_buffer(args.since, args.until)
    times = sorted(e["event_time"] for e in events if e.get("event_time"))

    counts: Dict[str, int] = {}
    for event in events:
        counts[event["event_type"]] = counts.get(event["event_type"], 0) + 1

    body = "".join(json.dumps(e, sort_keys=True) + "\n" for e in events)
    manifest = {
        "format": BUNDLE_FORMAT,
        "schema_version": common.SCHEMA_VERSION,
        "machine_id": config["machine_id"],
        "packed_at": now(),
        # The window is declared even when it is empty. A bundle covering a week
        # with no activity is a measured zero; a week with no bundle is missing
        # data. Reports must be able to tell those apart.
        # A requested window is declared even when it turned up nothing. That
        # is what separates "this week was quiet" from "nobody sent this week",
        # and only the person packing knows which window they meant.
        "window_start": (args.window_start
                         or (args.since + "T00:00:00Z" if args.since else None)
                         or (times[0] if times else None)),
        "window_end": (args.window_end
                       or (args.until + "T23:59:59Z" if args.until else None)
                       or (times[-1] if times else None)),
        "days_covered": sorted({partition_of(e) for e in events}),
        "event_count": len(events),
        "event_counts_by_type": counts,
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }

    os.makedirs(REPORTS_DIR, exist_ok=True)
    name = "{}-{}.ndjson".format(config["machine_id"][:8], now().replace(":", "").replace("-", ""))
    path = os.path.join(REPORTS_DIR, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"_manifest": manifest}, sort_keys=True) + "\n")
        handle.write(body)

    if args.clear:
        # Only the partitions that were packed. Clearing everything would throw
        # away days the bundle does not cover.
        for day in {partition_of(e) for e in events}:
            path = buffer_path(day)
            if os.path.exists(path):
                os.remove(path)

    print(json.dumps({"bundle": path, **manifest}, sort_keys=True))
    return 0


# --------------------------------------------------------------------------
# status / purge
# --------------------------------------------------------------------------

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
    }, indent=2, sort_keys=True))
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    if not args.yes:
        answer = input(
            "Delete every event, bundle and the config in {}? [y/N] ".format(HOME)
        ).strip().lower()
        if answer not in ("y", "yes"):
            print("nothing deleted")
            return 1
    removed = 0
    for directory in (BUFFER_DIR, REPORTS_DIR):
        if not os.path.isdir(directory):
            continue
        for entry in os.listdir(directory):
            os.remove(os.path.join(directory, entry))
            removed += 1
    if os.path.exists(CONFIG_PATH) and args.all:
        os.remove(CONFIG_PATH)
    print(json.dumps({"files_removed": removed, "config_removed": bool(args.all)}))
    return 0


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="insight",
        description="Collect AI-effectiveness telemetry from this machine.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="record consent and create the local store")
    p.add_argument("--yes", action="store_true", help="skip the consent prompt")
    p.add_argument("--force", action="store_true", help="rewrite an existing config")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser(
        "setup", help="configure VS Code, record consent, install hooks")
    p.add_argument("--repo", action="append",
                   help="repository to install the commit hook into (repeatable)")
    p.add_argument("--aiep", help="path to ai-engineering-platform "
                                  "(default: beside this repo)")
    p.add_argument("--yes", action="store_true", help="skip the consent prompt")
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
        "otel", help="read Copilot's span file, then truncate it")
    p.add_argument("--source", help="override otel.outfile")
    p.add_argument("--keep-raw", action="store_true",
                   help="do not truncate afterwards. The raw file can hold "
                        "prompts; keeping it is a deliberate choice")
    p.set_defaults(func=cmd_otel)

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

    p = sub.add_parser("status", help="what is buffered and what was packed")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("purge", help="delete everything collected on this machine")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--all", action="store_true", help="also remove the config")
    p.set_defaults(func=cmd_purge)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
