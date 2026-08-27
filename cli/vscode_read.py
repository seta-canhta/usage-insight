#!/usr/bin/env python3
"""Read VS Code's Copilot Chat sessions -- the surface the CLI journal misses.

    python3 cli/vscode_read.py [--root <VS Code User dir>]

Why this exists
---------------
`copilot_read.py` reads ``~/.copilot/session-state``, which Copilot CLI writes
for its own ``/resume``. **The VS Code Copilot Chat panel writes nothing there.**

Measured on the pilot samples (``samples/ngocnguyen``, ``samples/linhhoang``,
2026-08-26): neither machine has a ``session-state`` directory at all.
**Not because server mode cannot journal** -- an earlier reading of this claimed
that and it is wrong. The machine these tests were written on shows the
identical log pattern (29 ``server mode (stdio)`` starts in August, ``Destroying
0 active sessions`` on every shutdown) and journals perfectly well; it simply
created 0 session dirs in August against 28 in April-June. The CLI server is
started by VS Code whenever the editor opens; a session directory appears only
when a Copilot CLI session actually runs.

So a QA engineer working entirely in the chat panel produces **zero** contract
events from the journal -- not because the journal is broken, but because the
chat panel is a different surface that never creates one. A zero that means "not
watched" is reported identically to a zero that means "did no work". That is the
failure this module exists to close.

VS Code does keep the sessions, in its own store:

    <User>/workspaceStorage/<hash>/chatSessions/<uuid>.json

Measured on a real machine: 145 workspaces, 55 session files, 27 of them
carrying requests, 93 requests, 603 tool calls.

Two storage formats
-------------------
`.json`  -- one object with a `requests` array. The older format.
`.jsonl` -- an append-only log: a header, then records that add or **patch**
            requests. This is the current format.

Reading only `.json` is how this module first shipped, and it was wrong in a
way worth recording: it saw **93** requests where the machine held **5,036**,
and reported the newest activity as six months old on a machine in use that
afternoon. A file-extension filter decided a finding. Both are read now, and
`.jsonl` records are merged by `requestId` because a request's `result` -- and
with it its token counts -- can arrive in a later record than the request.

What this can and cannot see
----------------------------
**It can see cost, on some requests.** `promptTokens` and `outputTokens` are
present in the `.jsonl` metadata. An earlier version of this docstring stated
the opposite in the strongest terms -- "cannot be recovered from this surface
at any later date" -- and that was read off the older format. Measured
2026-08-26 on a live request: promptTokens 31,991, outputTokens 170.

They are absent on requests whose `result` never landed, and there they are
NULL, never 0. What is genuinely unavailable is the cache split and the
premium-request count.

**It sees what the CLI journal cannot.** `result.timings.totalElapsed` is a
real per-request latency, per *call* rather than per session. The journal
totals usage at shutdown and stamps one row for a whole session, so it carries
no per-call latency at all. The two surfaces are complementary:

    journal   premium requests, cache split, per-session totals -- no latency
    chat      latency, per-call grain, tool names, prompt/output tokens

Content safety
--------------
These files are **mostly content**. A single request carries `message.text`
(the prompt), `response` (137 elements on one measured request),
`metadata.codeBlocks`, `metadata.renderedUserMessage` and the arguments of
every tool call. None of it may be stored.

So this module names what it keeps, exactly as `copilot_read.py` does, and
never inspects the rest. `KEEP` below is the whole surface. A field that is not
named there is not stored -- and with one exception, named below, not read
either. An exclusion list would have to keep pace with a format that gains keys
without asking; there is no version of that which stays correct.

**The one exception, and why it is drawn this narrowly.** `SCAN` names a single
path, `message.text`, that is *read* and never *kept*. It exists because a
session with no ticket cannot be joined to anything: measured 2026-08-26 on a
live laptop, all 877 events carried `jira_issue_key=null`, because this team
names branches `feature/26.8` and a release train is not a ticket.

What leaves `scan_for_key` is at most one string of the form `IML-1234`, and
only when a real project claims the prefix. The prompt is matched and dropped
inside that function; it is never returned, never stored, never logged, and
never reaches an event. With no allow-list the function reads nothing at all
and returns None -- AR-1's fabricated `AUG-25` came from exactly the permissive
path, and prose contains far more key-shaped noise than a branch name does.

A key found this way is `heuristic` at 0.5, not 0.9: naming a ticket in a
prompt is weaker evidence than working on its branch, and *mentioning* is not
*working on*. It is used only where the branch yielded nothing, so this can
turn a null into a key and can never change a key that was already there.

Tool names are the one dimension taken from inside a tool call, and they are
taken because they are bounded: 12 distinct values across 603 calls on the
measured machine (`read_file`, `grep_search`, `replace_string_in_file`, ...).
The *arguments* are never touched.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import (Any, Collection, Dict, Iterator, List, Optional,
                    Tuple)

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import common  # noqa: E402

#: CONTRACT.md §2.3. Named separately from `copilot-cli` because the whole point
#: of this module is that the two surfaces see different things; folding them
#: into one name would hide which half of a figure came from where.
SURFACE = "vscode-copilot-chat"

#: Where VS Code keeps per-workspace state, on the platforms we support.
DEFAULT_ROOTS = (
    "~/Library/Application Support/Code/User",          # macOS
    "~/.config/Code/User",                              # Linux
    "~/Library/Application Support/VSCodium/User",
    "~/.config/VSCodium/User",
)

#: Named because they are kept. Everything else in a request -- the prompt, the
#: response, the rendered context, the code blocks, every tool argument -- is
#: dropped without inspection. Paths are dotted, from one entry of `requests`.
KEEP: Tuple[str, ...] = (
    "requestId",
    "modelId",
    "timestamp",
    "responseTimestamp",
    "timeSpentWaiting",
    "result.timings.totalElapsed",
    "result.timings.firstProgress",
    "result.metadata.agentId",
    "result.metadata.maxToolCallsExceeded",
    # Present only in the `.jsonl` format, and only on some requests. Measured
    # 2026-08-26: 1 of 4 requests in a fresh session carried them.
    "result.metadata.promptTokens",
    "result.metadata.outputTokens",
    "result.metadata.resolvedModel",
)

#: Taken from inside each tool-call round. The name only.
KEEP_TOOL: Tuple[str, ...] = ("name",)

#: CONTRACT.md §3 `tool_kind`. Mapping is by exact name against a closed set --
#: never by substring, which would put `manage_todo_list` in `read` for
#: containing "list". An unmapped name is `other`, and stays visible as such.
TOOL_KINDS: Dict[str, str] = {
    "read_file": "read",
    "list_dir": "read",
    "get_errors": "read",
    "grep_search": "search",
    "file_search": "search",
    "semantic_search": "search",
    "create_file": "edit",
    "replace_string_in_file": "edit",
    "multi_replace_string_in_file": "edit",
    "run_in_terminal": "execute",
    "runSubagent": "delegate",
    "manage_todo_list": "plan",
}


def dig(data: Any, path: str) -> Any:
    """Follow a dotted path, returning None rather than raising."""
    node = data
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def keep(request: Dict[str, Any]) -> Dict[str, Any]:
    """Project one request onto `KEEP`. Nothing else is looked at."""
    return {path: dig(request, path) for path in KEEP}


#: Read, never kept. The counterpart to `KEEP`, and deliberately one path.
#:
#: `KEEP` is a projection: those values are stored. This is a match: the string
#: is compared against the project allow-list inside `scan_for_key` and dropped
#: there. Naming it separately is the point -- a reader of this module can see
#: at a glance the complete list of paths whose *text* is ever looked at.
#:
#: `message.text` is the human's own prompt. The model's `response`, the
#: rendered context, the code blocks and every tool argument stay unread: they
#: are larger, they are not the person's statement of intent, and they carry
#: far more incidental key-shaped noise -- pasted logs, stack traces, another
#: system's ticket references.
SCAN: Tuple[str, ...] = ("message.text",)


def scan_for_key(request: Dict[str, Any],
                 projects: Collection[str]) -> Optional[str]:
    """A Jira key named in the prompt, or None. The prompt itself is discarded.

    The allow-list is not optional here, and an empty one short-circuits before
    anything is read. `extract_jira_key(projects=None)` accepts anything
    key-shaped, which on a branch name already produced `AUG-25` -- a date --
    on 28 of 28 events from a live machine. Prose is a far richer source of
    that mistake than a branch name: a prompt quoting `ERR-500`, `CVE-2024`,
    `PY-311` or a colleague's `TC-12018` would mint all four. Constrained to
    the projects Jira says exist, the same regex is safe.

    What this cannot know is whether naming a ticket means working on it.
    "this is like IML-200 was" reads identically to "fix IML-200". That is why
    the caller records the result at confidence 0.5 and only where the branch
    gave nothing -- see CONTRACT.md §2.4, and note that `heuristic` rows are
    already barred from the cost metrics.
    """
    return scan_for_keys(request, projects)["jira_issue_key"]


def scan_for_keys(request: Dict[str, Any],
                  projects: Collection[str]) -> Dict[str, Optional[str]]:
    """Every key the prompt names -- Jira issue, AIO test case, AIO test cycle.

    The AIO pair is here because for a QA engineer that is the work unit: the
    test cycle is the delivery record and the pull request is not (CONTRACT.md
    §3 row 22). Reading only Jira meant a prompt saying "fix the flaky
    IML-TC-1234" named exactly the thing it was about, and we threw it away --
    and worse, `JIRA_KEY_RE` mined the tail of it and offered `TC-1234`, which
    is the fabrication `extract_jira_key`'s docstring already records.

    Everything `scan_for_key` says about the allow-list applies unchanged, and
    applies to the AIO prefixes too. The prompt is read and discarded; what
    survives is at most three keys, each matching a project somebody confirmed.
    """
    blank: Dict[str, Optional[str]] = {"jira_issue_key": None,
                                       "test_case_key": None,
                                       "test_cycle_key": None}
    if not projects:
        return blank
    for path in SCAN:
        text = dig(request, path)
        if not isinstance(text, str) or not text:
            continue
        found = dict(blank)
        found["jira_issue_key"] = common.extract_jira_key(
            text, projects=projects)
        found.update(common.extract_test_keys(text, projects=projects))
        if any(found.values()):
            return found
    return blank


def stamp(epoch_ms: Any) -> Optional[str]:
    """VS Code writes epoch milliseconds; the contract wants RFC3339 UTC."""
    if not isinstance(epoch_ms, (int, float)) or epoch_ms <= 0:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def normalise_model(model_id: Any) -> Optional[str]:
    """``copilot/claude-sonnet-4.5`` -> ``claude-sonnet-4.5``.

    The prefix names the *vendor route*, not the model, and the CLI journal
    records the same model without it. Left in place, one model would appear as
    two rows in every by-model breakdown, split by which surface it ran on --
    which is the opposite of what a by-model breakdown is for.
    """
    if not isinstance(model_id, str) or not model_id:
        return None
    name = model_id.split("/", 1)[1] if "/" in model_id else model_id
    # `resolvedModel` spells the version with a dash where `modelId` uses a
    # dot: `claude-sonnet-4-6` against `claude-sonnet-4.6`. Measured
    # 2026-08-26, preferring `resolvedModel` without this split one model into
    # two rows -- 12 under one spelling and 7 under the other. The CLI journal
    # writes the dot, so the dot is canonical.
    return re.sub(r"-(\d+)-(\d+)\b", r"-\1.\2", name)


def default_root() -> Optional[str]:
    """Where VS Code keeps user state, honouring an override.

    ``$VSCODE_HOME`` mirrors ``$COPILOT_HOME`` in `copilot_read`, and it is not
    only a convenience. Without it every test of the hourly run parsed the
    developer's real chat history -- 937 sessions and 12,536 events on the
    machine this was written on -- which made one test suite take four minutes
    and, worse, made tests read data they had no business touching.
    """
    override = os.environ.get("VSCODE_HOME")
    if override:
        return override if os.path.isdir(
            os.path.join(override, "workspaceStorage")) else None
    for candidate in DEFAULT_ROOTS:
        path = os.path.expanduser(candidate)
        if os.path.isdir(os.path.join(path, "workspaceStorage")):
            return path
    return None


def workspace_folder(storage_dir: str) -> Optional[str]:
    """The filesystem folder a workspaceStorage hash stands for.

    Measured 2026-08-26: 145 of 145 directories holding chat sessions carried a
    readable ``workspace.json``, so this is a reliable edge rather than a
    best-effort one. A ``workspace`` key (a ``.code-workspace`` file) rather
    than a ``folder`` key is a multi-root window; it names no single repository
    and is left unresolved instead of being attributed to one of them.
    """
    path = os.path.join(storage_dir, "workspace.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    uri = data.get("folder")
    if not isinstance(uri, str) or not uri.startswith("file://"):
        return None
    from urllib.parse import unquote
    return unquote(uri[len("file://"):])


def repo_of(folder: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """``(repo_full_name, branch)`` for a workspace folder, or ``(None, None)``.

    Read from the clone, which means it is read **now** -- and a folder that has
    been deleted since the chat happened resolves to nothing. That is the same
    perishability `copilot_read.run_bound` was built around, and the same
    answer applies: what cannot be evidenced is left NULL.

    The repository name survives that reading; the branch does not. A remote
    url is a property of the clone and does not change under a checkout, but
    "what branch is this on" answered now is not an answer about a session held
    three weeks ago. `checkout_history` is where that question goes, and this
    branch is the fallback for a caller that has no history to consult.
    """
    if not folder or not os.path.isdir(folder):
        return None, None

    def git(*args: str) -> Optional[str]:
        try:
            out = subprocess.run(("git", "-C", folder) + args,
                                 capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    url = git("config", "--get", "remote.origin.url")
    name = None
    if url:
        url = url[:-4] if url.endswith(".git") else url
        parts = [p for p in url.replace(":", "/").split("/") if p]
        name = "/".join(parts[-2:]) if len(parts) >= 2 else None
    return name, git("rev-parse", "--abbrev-ref", "HEAD")


#: A ref that is a raw object id rather than a branch. `git reflog` writes the
#: sha when HEAD goes detached, and "the branch was 4f2c9ab" is not a fact about
#: a branch.
_DETACHED = re.compile(r"\A[0-9a-f]{7,40}\Z")

#: `git reflog --date=iso-strict` line, as far as this needs to read it:
#:     <sha> HEAD@{2026-08-26T10:00:00+07:00}: checkout: moving from main to x
_REFLOG = re.compile(
    r"HEAD@\{(?P<when>[^}]+)\}:\s*(?P<what>.*)\Z")
_MOVED = re.compile(r"\Acheckout:\s*moving from (?P<src>\S+) to (?P<dst>\S+)")


def checkout_history(folder: Optional[str],
                     run=None) -> Optional[Dict[str, Any]]:
    """When HEAD pointed at which branch, from the reflog. None if unanswerable.

    This exists because the branch was being read at the wrong time. `repo_of`
    asks the clone what branch it is on **now**, and `insight backfill --since
    2026-08-01` then stamps that answer on every session in the window --
    three weeks of chats attributed to whatever happens to be checked out on
    the morning somebody runs the backfill. The value can be right by luck. The
    method never is, and `link.confidence` said 0.9 either way.

    The reflog is the record of the thing actually being asked about: it says
    when HEAD moved and where to. So the question "which branch was this
    session held on" gets answered from evidence, and a session older than the
    reflog gets no answer at all rather than today's.

    Returns ``{"floor": datetime, "timeline": [(when, branch), ...]}`` with the
    timeline ascending. ``floor`` is the oldest entry the reflog still holds;
    below it nothing is known, because git expires reflogs (90 days by
    default) and an expired range is not an empty one.
    """
    if not folder or not os.path.isdir(folder):
        return None

    def git(*args: str) -> Optional[str]:
        if run is not None:
            return run(*args)
        try:
            out = subprocess.run(("git", "-C", folder) + args,
                                 capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout if out.returncode == 0 else None

    raw = git("reflog", "show", "--date=iso-strict", "HEAD")
    if not raw:
        return None

    entries: List[Tuple[datetime, Optional[str], Optional[str]]] = []
    for line in raw.splitlines():
        found = _REFLOG.search(line)
        if not found:
            continue
        when = _parse_iso(found.group("when"))
        if when is None:
            continue
        moved = _MOVED.match(found.group("what").strip())
        if moved:
            entries.append((when, moved.group("src"), moved.group("dst")))
        else:
            entries.append((when, None, None))
    if not entries:
        return None

    entries.reverse()                                   # reflog is newest-first
    floor = entries[0][0]

    timeline: List[Tuple[datetime, Optional[str]]] = []
    for when, src, dst in entries:
        if dst is None:
            continue
        if not timeline and src:
            # Before the first recorded move, HEAD was where that move came
            # from -- but only back as far as the reflog goes.
            timeline.append((floor, None if _DETACHED.match(src) else src))
        timeline.append((when, None if _DETACHED.match(dst) else dst))

    if not timeline:
        # A clone that has never changed branch. Every entry in the reflog --
        # commits, pulls, resets -- happened on the branch it is on now.
        current = (git("rev-parse", "--abbrev-ref", "HEAD") or "").strip()
        if not current or current == "HEAD":
            return None
        timeline.append((floor, current))

    return {"floor": floor, "timeline": timeline}


def _parse_iso(text: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(text.strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def branch_at(history: Optional[Dict[str, Any]],
              when: Optional[str]) -> Optional[str]:
    """The branch HEAD pointed at, at ``when``. None where nothing is known.

    None rather than the current branch, and that is the whole point: a session
    older than the reflog is a session this machine cannot speak about. It
    leaves `jira_issue_key` null, `scan_for_key` gets its turn at the prompt,
    and if that finds nothing the row says so instead of guessing (§1, AR-1).
    """
    if not history or not when:
        return None
    moment = _parse_iso(when.replace("Z", "+00:00"))
    if moment is None or moment < history["floor"]:
        return None
    found = None
    for started, branch in history["timeline"]:
        if started > moment:
            break
        found = branch
    return found


def discover_repos(root: Optional[str] = None) -> List[str]:
    """Every git work tree a chat session was held in, deduplicated.

    `insight scan` learned repositories from the Copilot CLI journal alone, so
    a machine used only through the chat panel discovered none. Measured
    2026-08-26 on a live laptop: 512 chat events in `aeriscom/wt-playwrite-taf`
    and a bundle reporting `repos: 0`. No commits were scanned, so no
    `AI-Run-Id` trailer was ever read -- and CONTRACT.md §2.4 makes that
    trailer the only thing that earns `method='explicit'`, which is in turn the
    only thing the cost metrics may be computed from. A discovery gap was
    quietly holding two metrics shut.

    This module already resolves the folder for every session it reads, so the
    information was present and simply never asked for.

    Only directories that still exist and still hold a `.git` are returned.
    Evidence here perishes -- CLAUDE.md records 24 of 27 workspace folders
    already deleted when read after the fact -- and a path that has gone is
    left out rather than reported as a repository that cannot be scanned.
    """
    root = root or default_root()
    if not root:
        return []
    base = os.path.join(root, "workspaceStorage")
    if not os.path.isdir(base):
        return []
    found: List[str] = []
    seen = set()
    for storage, _session_id, _path in iter_sessions(root):
        if storage in seen:
            continue
        seen.add(storage)
        folder = workspace_folder(storage)
        if not folder or folder in found:
            continue
        if os.path.isdir(os.path.join(folder, ".git")):
            found.append(folder)
    return sorted(found)


def iter_sessions(root: str) -> Iterator[Tuple[str, str, str]]:
    """``(storage_dir, session_id, path)`` for every chat session under root."""
    base = os.path.join(root, "workspaceStorage")
    try:
        hashes = sorted(os.listdir(base))
    except OSError:
        return
    for name in hashes:
        storage = os.path.join(base, name)
        folder = os.path.join(storage, "chatSessions")
        if not os.path.isdir(folder):
            continue
        try:
            files = sorted(os.listdir(folder))
        except OSError:
            continue
        for filename in files:
            # `.jsonl` is the current format and `.json` the one before it.
            # Reading only `.json` is how this module first shipped, and it
            # made a live machine look six months idle -- every recent session
            # was in a file the loop skipped. Both are read.
            for suffix in (".jsonl", ".json"):
                if filename.endswith(suffix):
                    yield storage, filename[:-len(suffix)], \
                        os.path.join(folder, filename)
                    break


def load_requests(path: str) -> List[Dict[str, Any]]:
    """Every request in one chat session, from either storage format.

    `.json` is a single object with a `requests` array. `.jsonl` is an
    **append-only log**: a header record, then records that add or *patch*
    requests. Measured on a live session, one request's `result` -- and with it
    its token counts -- arrived in a later record than the request itself, so a
    reader that took the first sighting and stopped would report the tokens as
    absent. Records are therefore merged by `requestId`, later winning.

    A file VS Code is mid-write on yields whatever parsed; a truncated final
    line is skipped rather than losing the session.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return []

    if path.endswith(".jsonl"):
        merged: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue          # a half-written trailing line
            value = record.get("v")
            if isinstance(value, list):
                items = value
            elif isinstance(value, dict) and isinstance(value.get("requests"), list):
                items = value["requests"]
            else:
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                request_id = item.get("requestId")
                if not request_id:
                    continue
                if request_id not in merged:
                    order.append(request_id)
                    merged[request_id] = {}
                merged[request_id].update(item)
        return [merged[r] for r in order]

    try:
        data = json.loads(text)
    except ValueError:
        return []
    requests = data.get("requests")
    return [r for r in requests if isinstance(r, dict)] \
        if isinstance(requests, list) else []


def session_events(path: str, session_id: str,
                   repo: Optional[str], branch: Optional[str],
                   actor: Optional[Dict[str, Any]] = None,
                   jira_projects: Optional[Tuple[str, ...]] = None,
                   history: Optional[Dict[str, Any]] = None
                   ) -> List[Dict[str, Any]]:
    """Build contract events for one chat session file.

    ``branch`` is what the clone says today. ``history`` is what its reflog says
    it said at the time, and where both are available the second wins -- see
    `checkout_history`. Passing no history keeps the old behaviour, which is
    right for a caller that has already established the branch some other way.
    """
    requests = load_requests(path)
    if not requests:
        return []

    # validated_projects, never the raw argument: a caller that passes None
    # would otherwise turn branch `fix/AUG-25` into ticket "AUG-25" -- a date.
    # Measured on a live machine 2026-08-26, 28 of 28 events. AR-1.
    projects = common.validated_projects(jira_projects, source="jira_projects")

    #: One entry per distinct branch this session touched. A session is
    #: normally held on one, so this is normally one build; a session that
    #: spanned a checkout gets the right answer on both sides of it.
    built: Dict[Optional[str], Tuple[Dict[str, Any], bool]] = {}

    def for_branch(name: Optional[str]) -> Tuple[Dict[str, Any], bool]:
        if name not in built:
            keys = common.extract_test_keys(name, projects=projects)
            keys["jira_issue_key"] = common.extract_jira_key(
                name, projects=projects)
            built[name] = (common.make_context(
                repo_full_name=repo, branch_name=name, **keys),
                any(keys.values()))
        return built[name]

    context, branch_named_something = for_branch(branch)
    agent = common.make_agent("copilot.chat", surface=SURFACE)
    # The journal reader nulls this for the same reason: the agent's version is
    # not something the surface records, and the poller's own version number
    # would be a confident wrong answer.
    agent["agent_version"] = None

    events: List[Dict[str, Any]] = []

    #: Rebound once per request. The branch belongs to the session, but a
    #: prompt-derived key belongs to the request that named it -- a session that
    #: touches two tickets should not have both events attributed to whichever
    #: was mentioned first. `context` is per-event in the schema, so this costs
    #: nothing to represent honestly.
    current = {"context": context, "confidence": 0.9}

    def emit(event_type: str, when: Optional[str], attributes: Dict[str, Any],
             suffix: str) -> None:
        events.append(common.build_event(
            event_type=event_type,
            event_time=when,
            natural_key=(session_id, suffix),
            attributes=attributes,
            actor=actor,
            context=current["context"],
            agent=agent,
            # `heuristic` 0.9, not `explicit`: the request is a real record,
            # but nothing in it names a run, and the repository is resolved
            # from where the window happened to be open. 0.5 where the ticket
            # came from the prompt rather than the branch -- see `scan_for_key`.
            link=common.make_link("heuristic", current["confidence"]),
            trace_id=session_id,
            # No run: VS Code chat has no run concept, and inventing one to
            # satisfy a column would manufacture a join key (AR-1). Null is
            # accepted here -- CONTRACT.md §2.4.
            run_id=None,
        ))

    for index, request in enumerate(requests):
        if not isinstance(request, dict):
            continue
        attrs = keep(request)
        request_id = attrs.get("requestId") or "req-{}".format(index)
        when = stamp(attrs.get("timestamp"))

        # Which branch this request was made on, asked of the reflog rather
        # than of the working tree. `branch_at` returns None for a session
        # older than the reflog, and None is the answer -- see its docstring.
        if history is not None:
            here, here_named = for_branch(branch_at(history, when))
        else:
            here, here_named = context, branch_named_something

        # The branch wins where it has an answer, and the prompt is consulted
        # only where it does not. So this can turn a null into a key and can
        # never overwrite one -- no figure that exists today moves because of
        # it, which is the only safe way to introduce a weaker signal.
        if here_named:
            current["context"], current["confidence"] = here, 0.9
        else:
            named = scan_for_keys(request, projects)
            told = any(named.values())
            current["context"] = common.make_context(
                repo_full_name=repo,
                branch_name=here.get("branch_name"),
                **named
            ) if told else here
            current["confidence"] = 0.5 if told else 0.9

        # -- the human turn -------------------------------------------------
        # `chars` is deliberately NOT taken. It would require measuring the
        # prompt, and a length is a weak but real signal about content; the
        # turn happening is what this is for.
        emit("human.turn", when, {
            "turn_index": index,
            "turn_kind": "prompt",
            "chars": None,
        }, "turn:{}".format(request_id))

        # -- the model call -------------------------------------------------
        elapsed = attrs.get("result.timings.totalElapsed")
        prompt_tokens = attrs.get("result.metadata.promptTokens")
        output_tokens = attrs.get("result.metadata.outputTokens")
        emit("model.call", when, {
            # `resolvedModel` is what actually served the request; `modelId` is
            # what was asked for. They agree today, and when they disagree the
            # served one is the true answer.
            "model_id": normalise_model(
                attrs.get("result.metadata.resolvedModel")
                or attrs.get("modelId")),
            "latency_ms": elapsed if isinstance(elapsed, (int, float)) else None,
            # Present on this surface after all. An earlier version of this
            # module asserted they were "NULL forever and cannot be recovered
            # at any later date" -- that was read off the older `.json`
            # format, and the `.jsonl` format carries them. Measured
            # 2026-08-26 on a live request: promptTokens 31,991, outputTokens
            # 170.
            #
            # Still NULL when absent, which is the common case: only 1 of 4
            # requests in that session carried them, because the counts arrive
            # with the `result` and a request whose result never landed has
            # none. Zero would claim a request that cost nothing.
            "input_tokens": prompt_tokens if isinstance(prompt_tokens, int) else None,
            "output_tokens": output_tokens if isinstance(output_tokens, int) else None,
            # These genuinely are not recorded here: the surface reports a
            # prompt total, not its cache split, and no premium-request count.
            "cached_input_tokens": None,
            "cache_write_tokens": None,
            "reasoning_tokens": None,
            "premium_requests": None,
            "request_count": 1,
            "retry_count": None,
            "finish_reason": (
                "max_tool_calls"
                if attrs.get("result.metadata.maxToolCallsExceeded") else None),
        }, "model:{}".format(request_id))

        # -- the tool calls -------------------------------------------------
        rounds = dig(request, "result.metadata.toolCallRounds")
        if not isinstance(rounds, list):
            continue
        position = 0
        for round_ in rounds:
            if not isinstance(round_, dict):
                continue
            for call in (round_.get("toolCalls") or []):
                if not isinstance(call, dict):
                    continue
                name = call.get("name")
                if not isinstance(name, str) or not name:
                    continue
                emit("tool.call", when, {
                    "tool_name": name,
                    "tool_kind": TOOL_KINDS.get(name, "other"),
                    # The store records no per-tool outcome or duration. A
                    # status of "ok" would be an assumption that every tool
                    # succeeded, which is exactly the claim we cannot make.
                    "status": None,
                    "duration_ms": None,
                    "error_class": None,
                }, "tool:{}:{}".format(request_id, position))
                position += 1

    return events


def to_events(root: Optional[str] = None,
              actor: Optional[Dict[str, Any]] = None,
              jira_projects: Optional[Tuple[str, ...]] = None
              ) -> Dict[str, Any]:
    """Read every chat session under ``root``."""
    root = root or default_root()
    if not root or not os.path.isdir(os.path.join(root, "workspaceStorage")):
        return {"present": False, "root": root, "events": [], "sessions": 0,
                "requests": 0, "coverage": coverage(0, 0, 0)}

    events: List[Dict[str, Any]] = []
    sessions = with_requests = 0
    resolved: Dict[str, Tuple[Optional[str], Optional[str],
                              Optional[Dict[str, Any]]]] = {}

    for storage, session_id, path in iter_sessions(root):
        sessions += 1
        if storage not in resolved:
            folder = workspace_folder(storage)
            repo_name, current_branch = repo_of(folder)
            resolved[storage] = (repo_name, current_branch,
                                 checkout_history(folder))
        repo, branch, history = resolved[storage]
        found = session_events(path, session_id, repo, branch, actor,
                               jira_projects, history)
        if found:
            with_requests += 1
            events += found

    return {
        "present": True,
        "root": root,
        "events": events,
        "sessions": sessions,
        "sessions_with_requests": with_requests,
        "requests": sum(1 for e in events if e["event_type"] == "model.call"),
        "coverage": coverage(
            sessions, with_requests,
            sum(1 for e in events if e["event_type"] == "model.call"
                and e["attributes"].get("input_tokens") is not None)),
    }


def coverage(sessions: int, with_requests: int,
             tokens_seen: int = 0) -> Dict[str, Any]:
    """What this surface could not see, published with every run.

    CONTRACT.md §1: an unmeasured quantity and a measured zero must never look
    the same. The cost fields here are permanently unavailable, not pending.
    """
    return {
        "surface": SURFACE,
        "sessions_seen": sessions,
        "sessions_with_requests": with_requests,
        "sessions_empty": sessions - with_requests,
        "usage_available": tokens_seen > 0,
        "requests_with_tokens": tokens_seen,
        "unavailable_fields": [
            "cached_input_tokens", "cache_write_tokens", "reasoning_tokens",
            "premium_requests",
        ],
        "reason": (
            "Prompt and output token counts are present on requests whose "
            "`result` landed, and NULL on the rest -- not zero. The cache "
            "split and the premium-request count are not recorded on this "
            "surface at all."),
        "surfaces_not_covered": ["inline-completions"],
    }


def verify_no_content(events: List[Dict[str, Any]]) -> List[str]:
    """Refuse to ship anything that looks like content or a home directory.

    Checked before a write, not on ingest: this runs on a machine full of the
    things it must not collect.
    """
    problems: List[str] = []
    home = os.path.expanduser("~")
    for event in events:
        for key, value in (event.get("attributes") or {}).items():
            if isinstance(value, str):
                if home in value or value.startswith("/Users/") \
                        or value.startswith("/home/"):
                    problems.append("{}.{} carries an absolute path".format(
                        event["event_type"], key))
                elif len(value) > 200:
                    problems.append("{}.{} is {} chars -- content?".format(
                        event["event_type"], key, len(value)))
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Emit contract events from VS Code Copilot Chat sessions.")
    parser.add_argument("--root", help="VS Code User directory")
    parser.add_argument("--out", help="NDJSON output (default: summary only)")
    parser.add_argument("--jira-projects",
                        help="Comma-separated real Jira project keys")
    args = parser.parse_args(argv)

    projects = tuple(sorted({p.strip().upper()
                             for p in (args.jira_projects or "").split(",")
                             if p.strip()}))
    # Never None: without --jira-projects this reader used to accept anything
    # key-shaped and turned branch `fix/AUG-25` into ticket "AUG-25" (AR-1).
    result = to_events(args.root, jira_projects=common.validated_projects(
        projects, source="--jira-projects"))
    problems = verify_no_content(result["events"])
    if problems:
        for problem in problems[:5]:
            print("REJECTED " + problem, file=sys.stderr)
        return 2

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            for event in result["events"]:
                handle.write(json.dumps(event, sort_keys=True) + "\n")

    print(json.dumps({
        "present": result["present"],
        "sessions": result["sessions"],
        "sessions_with_requests": result.get("sessions_with_requests", 0),
        "requests": result["requests"],
        "events": len(result["events"]),
        "coverage": result["coverage"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
