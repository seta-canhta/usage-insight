#!/usr/bin/env python3
"""Turn Copilot's own session journal into contract events.

    python3 cli/copilot_read.py --root ~/.copilot

Copilot CLI keeps a per-session journal at
``~/.copilot/session-state/<session-id>/events.jsonl``. It is written whether or
not anything is watching, it needs no exporter, no setting and no listening
port, and it records things the OTel span stream never carried. This module
reads it and emits ``model.call``, ``tool.call``, ``gate.evaluated``,
``output.generated`` and ``human.turn`` per ``CONTRACT.md`` §3, so the weekly
pipeline reads it like any other source.

Why this replaced the OTel path
-------------------------------
The span stream answered "what did it cost" and almost nothing else, and it
answered even that at the price of a content leak we had to mitigate ourselves
(microsoft/vscode#326254 -- ``captureContent: false`` is honoured on the log and
metric paths and ignored on the span path). The journal is strictly better on
every axis the weekly report actually uses:

============================  ==================  =====================================
Weekly report section         From spans          From the journal
============================  ==================  =====================================
1 Adoption                    chat *mode* name    ``subagent.started.agentName``, and
                                                  the session itself is the run
4 Quality (gates)             ``status`` NULL     a real verdict -- see `exit_code`
6 Cost                        tokens only         tokens **plus premium requests**,
                                                  which is the unit Copilot bills
7 Reliability                 ``status`` NULL,    ``success`` and ``error.code``, so
                              so 0 tool failures  the failure count is measured
8 Human involvement           request text only   turn count, kind and length
============================  ==================  =====================================

What it costs: the journal covers the **Copilot CLI/agent surface only**. VS
Code's Copilot Chat panel and inline completions write nothing here. That is a
deliberate scope decision, not an oversight, and `coverage()` states it so a
surface nobody measured never reads as a surface nobody used.

Content is dropped here, by allow-list
--------------------------------------
The journal is full of exactly what §11.3 forbids collecting: prompts, replies,
file contents, command output, absolute paths carrying the username. Unlike the
span stream it never leaves the machine on its own, so the exposure is smaller
to begin with -- but this module still **names the fields it keeps** rather than
excluding the ones it knows about. An exclusion list is only as good as today's
knowledge of a file format that gains keys without asking. Anything not named in
`KEEP` never reaches an event, whatever it is called.

Three fields are read to classify and then discarded, the same rule the commit
subject parser follows: the shell command (which gate is this?), the tool error
message (which failure class?), and the tail of a command's output (which exit
code?). None of the three is ever stored.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, os.path.join(_ROOT, "collector")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import common  # noqa: E402
import main as collector_main  # noqa: E402

#: Where Copilot CLI keeps its journals. One directory per session, each holding
#: an append-only ``events.jsonl``.
DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), ".copilot")

#: CONTRACT.md §2.3. The journal is written by the CLI agent and by nothing
#: else, so the surface is known rather than guessed.
SURFACE = "copilot-cli"

#: Named because they are kept. Every other key in the journal -- and there are
#: hundreds, including whole file contents -- is dropped without inspection.
#: Paths are given from ``data``, dotted.
KEEP: Dict[str, Tuple[str, ...]] = {
    "session.start": (
        "sessionId", "startTime", "selectedModel", "reasoningEffort",
        "context.repository", "context.branch", "context.gitRoot",
        "context.cwd", "context.headCommit", "context.baseCommit",
        "context.repositoryHost", "context.hostType",
    ),
    "session.resume": (
        "sessionId", "resumeTime",
        "context.repository", "context.branch", "context.gitRoot",
        "context.cwd", "context.headCommit", "context.baseCommit",
        "context.repositoryHost", "context.hostType",
    ),
    "session.shutdown": (
        "shutdownType", "sessionStartTime", "totalPremiumRequests",
        "totalApiDurationMs", "totalNanoAiu", "currentModel",
        "toolDefinitionsTokens", "systemTokens", "conversationTokens",
        "codeChanges.linesAdded", "codeChanges.linesRemoved",
        # modelMetrics is a map keyed by model id, so it is walked rather than
        # named -- see `model_metrics`. Only the leaves listed there are read.
    ),
    "assistant.message": ("model", "outputTokens", "requestId", "turnId"),
    "assistant.turn_start": ("turnId", "interactionId"),
    "assistant.turn_end": ("turnId",),
    "tool.execution_start": (
        "toolCallId", "toolName", "parentToolCallId",
        "mcpServerName", "mcpToolName",
    ),
    "tool.execution_complete": (
        "toolCallId", "success", "model", "parentToolCallId",
        "toolTelemetry.metrics.linesAdded", "toolTelemetry.metrics.linesRemoved",
        "toolTelemetry.properties.executionMode",
    ),
    "subagent.started": ("toolCallId", "agentName", "agentDisplayName"),
    "subagent.completed": ("toolCallId",),
    "subagent.failed": ("toolCallId",),
    "skill.invoked": ("name", "source", "pluginVersion"),
    "user.message": ("interactionId", "source", "agentMode"),
    "permission.requested": ("permissionRequest.kind",),
    "permission.completed": ("result.kind",),
    "abort": ("reason",),
}

#: Leaves read out of ``session.shutdown.modelMetrics.<model>``.
USAGE_KEEP = (
    "requests.count", "requests.cost",
    "usage.inputTokens", "usage.outputTokens", "usage.cacheReadTokens",
    "usage.cacheWriteTokens", "usage.reasoningTokens",
    "totalNanoAiu",
)

#: CONTRACT.md §3 row 9 -- the closed gate set. The command is content: it is
#: read to decide which gate a call represents and is then dropped.
GATE_PATTERNS = (
    ("test", re.compile(r"\b(pytest|jest|vitest|mocha|go\s+test|mvn\s+test|"
                        r"npm\s+(run\s+)?test|yarn\s+test|python\s+-m\s+unittest|"
                        r"playwright\s+test|cucumber)\b", re.I)),
    ("lint", re.compile(r"\b(eslint|ruff|flake8|pylint|golangci-lint|"
                        r"npm\s+run\s+lint|prettier\s+--check)\b", re.I)),
    ("build", re.compile(r"\b(tsc|npm\s+run\s+build|mvn\s+(package|compile)|"
                         r"gradle\s+build|go\s+build|cargo\s+build|make\b)", re.I)),
    ("coverage", re.compile(r"\b(coverage|--cov|nyc|jacoco)\b", re.I)),
    ("secrets", re.compile(r"\b(gitleaks|trufflehog|detect-secrets)\b", re.I)),
)

#: Copilot's bash tool appends this to the output it hands back to the model.
#: Anchored to the end so a command that merely *prints* the phrase cannot forge
#: a verdict -- and only the integer is taken; the output it is attached to is
#: never read past this match.
EXIT_TRAILER = re.compile(r"<exited with exit code (-?\d+)>\s*$")

#: Tool error messages are free text. They are read to pick one of these bounded
#: classes and are then discarded -- `error_class` is a §3 row 6 field, the
#: message is not.
ERROR_CLASSES = (
    ("permission_denied", re.compile(r"user permission response|denied", re.I)),
    ("not_found", re.compile(r"no match found|does not exist|not found", re.I)),
    ("ambiguous_match", re.compile(r"multiple matches", re.I)),
    ("invalid_input", re.compile(r"invalid (inputs?|shell id)|is required", re.I)),
    ("timeout", re.compile(r"timed? ?out", re.I)),
)

#: CONTRACT.md §3 row 6 `tool_kind`. MCP tools are detected structurally, by the
#: presence of `mcpServerName`, rather than by matching their names -- the names
#: are whatever a server chose to call itself.
TOOL_KINDS = {
    "bash": "terminal", "read_bash": "terminal", "write_bash": "terminal",
    "stop_bash": "terminal", "list_bash": "terminal",
    "view": "file", "edit": "file", "create": "file",
    "glob": "file", "grep": "file",
    "web_fetch": "http",
    "sql": "other", "skill": "other", "task": "other",
    "report_intent": "other", "ask_user": "other",
}

#: Tools that write files. Only these produce `output.generated`.
WRITE_TOOLS = ("edit", "create")


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def dig(data: Any, path: str) -> Any:
    """Follow a dotted path, returning None rather than raising on any miss."""
    for part in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def kept(record: Dict[str, Any]) -> Dict[str, Any]:
    """Project one journal record onto the keep-list for its type.

    Returns a flat dict keyed by the dotted path. A record whose type is not in
    `KEEP` yields nothing at all, which is how a newly added event type stays
    out of the stream until somebody has looked at it.
    """
    paths = KEEP.get(record.get("type") or "")
    if not paths:
        return {}
    data = record.get("data") or {}
    out: Dict[str, Any] = {}
    for path in paths:
        value = dig(data, path)
        if value is not None and not isinstance(value, (dict, list)):
            out[path] = value
    return out


def scalar_leaf(value: Any) -> Any:
    """Accept a number or a string; refuse anything nested.

    A dict or a list is where free text hides. Refusing them here means a
    keep-list entry that is wrong about a field's shape drops the field instead
    of smuggling a structure through.
    """
    return value if isinstance(value, (int, float, str, bool)) else None


def model_metrics(record: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Read ``session.shutdown.modelMetrics``, one entry per model.

    The map is keyed by model id, so it cannot be named in `KEEP` -- the keys
    are data. The *leaves* are named instead, in `USAGE_KEEP`, which gives the
    same guarantee: a new field under a model id is dropped, not carried.
    """
    metrics = dig(record.get("data") or {}, "modelMetrics")
    if not isinstance(metrics, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for model, block in metrics.items():
        if not isinstance(model, str) or not isinstance(block, dict):
            continue
        picked = {}
        for path in USAGE_KEEP:
            value = scalar_leaf(dig(block, path))
            if value is not None:
                picked[path] = value
        out[model] = picked
    return out


def classify_gate(command: Optional[str]) -> Optional[str]:
    if not command:
        return None
    for name, pattern in GATE_PATTERNS:
        if pattern.search(command):
            return name
    return None


def classify_error(message: Optional[str]) -> Optional[str]:
    if not message:
        return None
    for name, pattern in ERROR_CLASSES:
        if pattern.search(message):
            return name
    return "other"


def exit_code(content: Any) -> Optional[int]:
    """The integer from ``<exited with exit code N>``, or None.

    This is the one thing that makes a gate verdict possible, and it is worth
    saying why reading it is not a content read. The trailer is written by
    Copilot's bash tool, not by the command; it is matched anchored at the end
    of the string; and the only thing extracted is a small integer. The output
    it was appended to is never inspected, stored, or logged.

    Measured 2026-08-26: present on 780 of 887 bash results. Absent on the rest
    -- a command still running, or output truncated -- and absent means None,
    never 0. A missing verdict and a passing verdict are different facts.
    """
    if not isinstance(content, str):
        return None
    match = EXIT_TRAILER.search(content)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def iter_sessions(root: str) -> Iterator[Tuple[str, str]]:
    """Yield ``(session_id, journal_path)`` for every session on disk."""
    state = os.path.join(root, "session-state")
    if not os.path.isdir(state):
        return
    for name in sorted(os.listdir(state)):
        journal = os.path.join(state, name, "events.jsonl")
        if os.path.isfile(journal):
            yield name, journal


def iter_records(path: str) -> Iterator[Dict[str, Any]]:
    """Yield parsed records, skipping any line that does not parse.

    The journal is appended to live. Reading it while Copilot is mid-write can
    catch a partial last line, and a torn line is not a reason to lose the
    session.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def stamp(value: Any) -> Optional[str]:
    """Journal timestamps are RFC3339 with milliseconds; the contract wants seconds."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def elapsed_ms(start: Optional[str], end: Optional[str]) -> Optional[int]:
    """Milliseconds between two contract timestamps, or None if either is missing."""
    if not start or not end:
        return None
    try:
        began = datetime.fromisoformat(start.replace("Z", "+00:00"))
        ended = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = int((ended - began).total_seconds() * 1000)
    return delta if delta >= 0 else None


def relative(path: Any, roots: Iterable[Optional[str]]) -> Optional[str]:
    """Express a touched file relative to its repository.

    Absolute paths out of the journal begin ``/Users/<name>/`` -- a raw
    identifier, which §11.3 keeps out of the stream. `file_path` is a permitted
    field; somebody's home directory is not. A path that sits under none of the
    known roots is dropped rather than truncated, because a half-path is not
    worth the guess about which prefix was safe to remove.
    """
    if not isinstance(path, str) or not path:
        return None
    if not os.path.isabs(path):
        return path
    for root in roots:
        if root and path.startswith(root.rstrip("/") + "/"):
            return path[len(root.rstrip("/")) + 1:]
    return None


# --------------------------------------------------------------------------
# one session -> events
# --------------------------------------------------------------------------

class Session:
    """Running state while walking one journal, front to back.

    A journal is a stream, not a table: a tool's name arrives on
    ``tool.execution_start`` and its outcome on ``tool.execution_complete``,
    which may be hundreds of lines later. This holds the little that has to
    survive between the two.
    """

    def __init__(self, session_id: str) -> None:
        self.id = session_id
        self.repo: Optional[str] = None
        self.branch: Optional[str] = None
        self.host: Optional[str] = None
        #: Real Jira project keys, when the machine has been told any.
        self.jira_projects: Optional[Tuple[str, ...]] = None
        #: branch -> [first base seen on it, latest head seen on it].
        #:
        #: Keyed by branch, not one pair per session, because a session can
        #: move between branches -- measured here: one starts on `main`,
        #: resumes on `feat/uiux`. First-base-to-last-head across that is a
        #: range spanning two unrelated lines of work, and every commit in the
        #: gap would be charged to this session. Per branch, each range is
        #: bounded by commits that are actually ancestors of one another.
        self.ranges: Dict[str, List[Optional[str]]] = {}
        self.roots: List[str] = []
        self.model: Optional[str] = None
        #: toolCallId -> what its start line said.
        self.calls: Dict[str, Dict[str, Any]] = {}
        #: toolCallId of a `task` call -> the subagent it launched. A tool run
        #: by a subagent carries `parentToolCallId` pointing back at it, which
        #: is how a nested call is attributed to the agent that made it rather
        #: than to the session.
        self.subagents: Dict[str, str] = {}
        #: How many times each gate has been evaluated -- §3 row 9
        #: `attempt_index`, which is only meaningful within one session.
        self.gate_attempts: Dict[str, int] = {}
        self.turn_index = 0
        #: Skills loaded in this session, in order. Attribution is at **session**
        #: grain, not turn grain, and that is a measurement decision rather than
        #: a convenience: `turnId` is present on 162 of 2,069 tool events
        #: (measured 2026-08-26), so a turn-scoped window would mis-attribute
        #: 92% of the work while looking precise.
        self.skills: List[str] = []
        #: The run a session-level event belongs to. `session.start` opens one
        #: and `session.resume` opens another -- a resumed session is a second
        #: invocation, not a continuation, and counting it as one would hide
        #: most of the activity here (38 resumes against 22 starts).
        self.run_id: Optional[str] = None
        self.run_started_at: Optional[str] = None
        #: toolCallId of a `task` call -> the run id of the subagent it started.
        self.subagent_runs: Dict[str, str] = {}
        #: Set when a `session.shutdown` is seen. Its absence is a reported
        #: fact, not a silent zero -- see `coverage`.
        self.has_usage = False
        self.started: Optional[str] = None
        self.last_seen: Optional[str] = None

    def note_context(self, attrs: Dict[str, Any]) -> None:
        self.repo = attrs.get("context.repository") or self.repo
        self.branch = attrs.get("context.branch") or self.branch
        self.model = attrs.get("selectedModel") or self.model
        self.host = attrs.get("context.repositoryHost") or self.host
        # The commit range is the only *exact* join this reader can offer, and
        # it is perishable. `baseCommit` is where the session found the tree;
        # `headCommit` moves as the session commits. Both are captured on every
        # context block, so the first base and the last head bracket the work.
        #
        # Recomputed later from the clone? Measured 2026-08-26: of the seven
        # `gitRoot`s these journals name, **one still exists**. Worktrees are
        # deleted when their branch merges -- exactly the sessions that produced
        # something. Evidence about a repository has to be taken while the
        # repository is still there.
        base = attrs.get("context.baseCommit")
        head = attrs.get("context.headCommit")
        branch = attrs.get("context.branch") or self.branch
        if isinstance(branch, str) and branch:
            slot = self.ranges.setdefault(branch, [None, None])
            if isinstance(base, str) and base and slot[0] is None:
                slot[0] = base
            if isinstance(head, str) and head:
                slot[1] = head
        for key in ("context.gitRoot", "context.cwd"):
            root = attrs.get(key)
            if isinstance(root, str) and root and root not in self.roots:
                self.roots.append(root)

    def agent_for(self, parent_call_id: Optional[str]) -> str:
        """Which agent to bill a call to."""
        if parent_call_id and parent_call_id in self.subagents:
            return self.subagents[parent_call_id]
        return "copilot.cli"

    def run_for(self, parent_call_id: Optional[str]) -> Optional[str]:
        """Which run a call belongs to -- the subagent's, or the session's."""
        if parent_call_id and parent_call_id in self.subagent_runs:
            return self.subagent_runs[parent_call_id]
        return self.run_id

    def skill(self) -> Optional[str]:
        """The skill in force, if exactly one has been loaded.

        Two skills in one session cannot both be credited with the outcome, and
        picking the most recent would make the attribution depend on ordering.
        `sql/08_metrics.sql` compares skill-on against skill-off as arms of a
        configuration test; an arm that is really "some mixture of skills" is
        not an arm.
        """
        return self.skills[0] if len(self.skills) == 1 else None

    def advanced(self) -> List[Tuple[str, str, str]]:
        """The ``(branch, base, head)`` ranges this session actually moved.

        Base equal to head means the session committed nothing on that branch,
        so there is no range and nothing to bind. Empty ranges are left out
        rather than emitted with zero length, which downstream would have to
        special-case into the same answer.
        """
        return [(branch, slot[0], slot[1])
                for branch, slot in sorted(self.ranges.items())
                if slot[0] and slot[1] and slot[0] != slot[1]]

    def context(self) -> Dict[str, Any]:
        return common.make_context(
            # Allow-listed, and the list is usually absent on a laptop -- which
            # is the safe direction here. Measured 2026-08-26, this call has
            # never once produced a key from a real branch name (0 of 37), so
            # what it can realistically contribute is a false one: `fix/AUG-25`
            # yields "AUG-25", which is a date. `run.bound` carries the commit
            # range instead, and the warehouse resolves that to a ticket.
            jira_issue_key=common.extract_jira_key(
                None, self.branch, projects=self.jira_projects),
            repo_full_name=self.repo,
            branch_name=self.branch,
        )


def session_events(session_id: str, journal: str,
                   actor: Optional[Dict[str, Any]] = None,
                   jira_projects: Optional[Tuple[str, ...]] = None
                   ) -> Tuple[List[Dict[str, Any]], Session]:
    """Walk one journal and build its events.

    Every event's natural key starts with the journal record's own ``id``, a
    uuid that is stable for the life of the file. Re-reading a journal therefore
    produces byte-identical `event_id`s and de-duplicates cleanly -- which is
    what makes an hourly unattended run safe over a session that stays open for
    days.
    """
    state = Session(session_id)
    state.jira_projects = jira_projects
    events: List[Dict[str, Any]] = []

    def emit(event_type: str, record: Dict[str, Any], attributes: Dict[str, Any],
             agent_name: str, method: str, confidence: float,
             suffix: Optional[str] = None, run_id: Optional[str] = None,
             parent_run_id: Optional[str] = None) -> None:
        agent = common.make_agent(agent_name, surface=SURFACE)
        # `make_agent` stamps the poller's own version, which is right for a
        # poller identifying itself and wrong here: this is Copilot's agent, and
        # its version is not something the journal records. NULL is unknown;
        # the poller's version number would be a confident wrong answer.
        agent["agent_version"] = None
        agent["skill_name"] = state.skill()
        events.append(common.build_event(
            event_type=event_type,
            event_time=stamp(record.get("timestamp")),
            natural_key=(session_id, record.get("id"), suffix or event_type),
            attributes=attributes,
            actor=actor,
            context=state.context(),
            agent=agent,
            link=common.make_link(method, confidence),
            trace_id=session_id,
            run_id=run_id,
            parent_run_id=parent_run_id,
        ))

    for record in iter_records(journal):
        kind = record.get("type")
        attrs = kept(record)
        when = stamp(record.get("timestamp"))
        if when:
            state.last_seen = when
            state.started = state.started or when
        data = record.get("data") or {}

        if kind in ("session.start", "session.resume"):
            state.note_context(attrs)
            # A session activation IS a run: the journal records the boundary,
            # this does not invent one. CONTRACT §2.4 forbids synthesising a
            # `run_id` "to force a join" -- that is a different act. This id
            # joins nothing external, it names a boundary the source already
            # marks, and the link stays `heuristic`, so these rows remain
            # inadmissible to cost metrics either way. Without it the report's
            # adoption, speed, reliability and human-involvement sections are
            # all structurally empty, whatever the usage numbers say.
            state.run_id = common.deterministic_id(
                "run", session_id, str(record.get("id")))
            state.run_started_at = when
            emit("run.started", record, {
                "invocation_mode": "unknown",
                "model_declared_id": attrs.get("selectedModel"),
                "input_source": "resume" if kind == "session.resume" else "start",
            }, "copilot.cli", "heuristic", 0.9, run_id=state.run_id)

        elif kind == "skill.invoked":
            name = attrs.get("name")
            if name and name not in state.skills:
                state.skills.append(str(name))

        elif kind == "subagent.started":
            call_id = attrs.get("toolCallId")
            name = attrs.get("agentName") or attrs.get("agentDisplayName")
            if call_id and name:
                state.subagents[call_id] = str(name)
                child = common.deterministic_id(
                    "run", session_id, str(record.get("id")))
                state.subagent_runs[call_id] = child
                emit("run.started", record, {
                    "invocation_mode": "unknown",
                    "model_declared_id": None,
                    "input_source": "subagent",
                }, str(name), "heuristic", 0.9, suffix="run:subagent",
                    run_id=child, parent_run_id=state.run_id)

        elif kind in ("subagent.completed", "subagent.failed"):
            call_id = attrs.get("toolCallId")
            child = state.subagent_runs.get(call_id) if call_id else None
            if child:
                emit("run.completed" if kind == "subagent.completed"
                     else "run.failed", record,
                     {"duration_ms": None, "phases_completed": None}
                     if kind == "subagent.completed" else
                     {"duration_ms": None, "failure_class": "subagent_failed",
                      "dependency_failed": "none"},
                     state.agent_for(call_id), "heuristic", 0.9,
                     suffix="run:subagent_end",
                     run_id=child, parent_run_id=state.run_id)

        elif kind == "tool.execution_start":
            call_id = attrs.get("toolCallId")
            if call_id:
                # `arguments` is content and is not kept. The command is read
                # once, here, to decide whether this call is a gate; the gate
                # name is all that survives the line.
                state.calls[call_id] = {
                    "tool": attrs.get("toolName"),
                    "parent": attrs.get("parentToolCallId"),
                    "mcp": bool(attrs.get("mcpServerName")),
                    "gate": classify_gate(dig(data, "arguments.command")),
                    "path": dig(data, "arguments.path"),
                    "at": when,
                }

        elif kind == "tool.execution_complete":
            call_id = attrs.get("toolCallId")
            start = state.calls.pop(call_id, {}) if call_id else {}
            tool = start.get("tool") or "unknown"
            agent_name = state.agent_for(
                start.get("parent") or attrs.get("parentToolCallId"))
            success = attrs.get("success")
            run_id = state.run_for(
                start.get("parent") or attrs.get("parentToolCallId"))
            emit("tool.call", record, {
                "tool_name": tool,
                "tool_kind": "mcp" if start.get("mcp")
                else TOOL_KINDS.get(tool, "other"),
                # Durations are not journalled per call. A subtraction across
                # the two lines would be wall clock including the wait for a
                # human to approve the call, which is not the tool's duration.
                "duration_ms": None,
                "status": None if success is None else ("ok" if success else "error"),
                "error_class": None if success is not False
                else classify_error(dig(data, "error.message")),
            }, agent_name, "heuristic", 0.8, run_id=run_id)

            # A shell command that runs a test, lint or build IS a gate
            # evaluation, whether or not an agent bothered to emit one.
            gate = start.get("gate")
            if gate:
                seen = state.gate_attempts.get(gate, 0)
                state.gate_attempts[gate] = seen + 1
                code = exit_code(dig(data, "result.content"))
                emit("gate.evaluated", record, {
                    "gate_name": gate,
                    # A verdict at last -- but only when the trailer is there.
                    # `success` is about the tool call, not the command: a
                    # failing test suite is a successful bash call, and reading
                    # it as a gate result would report every red build green.
                    "status": None if code is None else ("pass" if code == 0 else "fail"),
                    "quality_score": None,
                    "coverage_pct": None,
                    "attempt_index": seen,
                }, agent_name, "heuristic", 0.6, suffix="gate:" + gate,
                    run_id=run_id)

            if tool in WRITE_TOOLS and success:
                path = relative(
                    start.get("path") or first_file_path(data), state.roots)
                added = attrs.get("toolTelemetry.metrics.linesAdded")
                removed = attrs.get("toolTelemetry.metrics.linesRemoved")
                # A known path is enough. Requiring a non-zero line count -- as
                # this did -- silently dropped every write whose telemetry
                # carried `linesAdded: 0`, and every one that carried no line
                # metrics at all (measured 2026-08-26: ~180 of 417 write calls).
                # The lines were not lost, because those writes added none; the
                # *outputs* were, and the acceptance denominator with them. An
                # absent count is NULL, a counted zero is 0, and they are
                # different facts.
                if path:
                    emit("output.generated", record, {
                        "output_id": common.deterministic_id(
                            "out", session_id, str(record.get("id")), path),
                        "artifact_type": artifact_type(path),
                        "file_path": path,
                        # `acceptance_state` is deliberately absent: whether
                        # an edit survived review is not knowable from the
                        # machine that made it. The downstream acceptance
                        # join owns it.
                        "lines_added": None if added is None else int(added),
                        "lines_removed": None if removed is None else int(removed),
                    }, agent_name, "heuristic", 0.7, suffix="output",
                        run_id=run_id)

        elif kind == "user.message":
            # A `source` means the text was injected by a skill or a hook, not
            # typed. Counting those as human turns would inflate the manual
            # intervention rate with the agent talking to itself.
            if attrs.get("source"):
                continue
            content = data.get("content")
            state.turn_index += 1
            emit("human.turn", record, {
                "turn_index": state.turn_index - 1,
                # Not classifiable from the journal: the text would have to be
                # read to tell a correction from a clarification, and that is
                # content. Approval and rejection arrive as their own events.
                "turn_kind": None,
                "chars": len(content) if isinstance(content, str) else None,
            }, "copilot.cli", "heuristic", 0.8, run_id=state.run_id)

        elif kind == "permission.completed":
            outcome = attrs.get("result.kind")
            if outcome in ("approved", "denied", "rejected"):
                state.turn_index += 1
                emit("human.turn", record, {
                    "turn_index": state.turn_index - 1,
                    "turn_kind": "approval" if outcome == "approved" else "rejection",
                    "chars": None,
                }, "copilot.cli", "heuristic", 0.9, run_id=state.run_id)

        elif kind == "abort":
            state.turn_index += 1
            emit("human.turn", record, {
                "turn_index": state.turn_index - 1,
                # Stopping a run mid-flight is the strongest correction signal
                # the journal carries, and §8 of the weekly report is built to
                # count exactly this.
                "turn_kind": "correction",
                "chars": None,
            }, "copilot.cli", "heuristic", 0.9, run_id=state.run_id)

        elif kind == "session.shutdown":
            state.has_usage = True
            if state.run_id:
                emit("run.completed", record, {
                    "duration_ms": elapsed_ms(state.run_started_at, when),
                    "phases_completed": None,
                }, "copilot.cli", "heuristic", 0.9, suffix="run:end",
                    run_id=state.run_id)
                state.run_id = None
                state.run_started_at = None

            metrics = model_metrics(record)
            duration = attrs.get("totalApiDurationMs")
            for model, usage in sorted(metrics.items()):
                emit("model.call", record, {
                    "model_id": model,
                    "input_tokens": usage.get("usage.inputTokens"),
                    "output_tokens": usage.get("usage.outputTokens"),
                    "cached_input_tokens": usage.get("usage.cacheReadTokens"),
                    "cache_write_tokens": usage.get("usage.cacheWriteTokens"),
                    "reasoning_tokens": usage.get("usage.reasoningTokens"),
                    "request_count": usage.get("requests.count"),
                    # What Copilot actually bills. The weekly report's cost
                    # section has carried a disclaimer that its per-token
                    # figure is notional precisely because this number was not
                    # available; it is now.
                    "premium_requests": usage.get("requests.cost"),
                    "nano_aiu": usage.get("totalNanoAiu"),
                    # One aggregate covering every call to this model in the
                    # session. `totalApiDurationMs` is the session's, not this
                    # model's, and dividing it by the request count would be
                    # modelling. Only carried when one model was used, where
                    # the two are the same number.
                    "latency_ms": duration if len(metrics) == 1 else None,
                    "retry_count": None,
                    "finish_reason": None,
                    # A level, not a total: what the model was carrying at the
                    # end of the session, paid on every request in it. Carried
                    # only for a single-model session for the same reason
                    # `latency_ms` is -- the journal records one context, not
                    # one per model, and splitting it would be invention.
                    "tool_definitions_tokens": (
                        attrs.get("toolDefinitionsTokens")
                        if len(metrics) == 1 else None),
                    "system_tokens": (attrs.get("systemTokens")
                                      if len(metrics) == 1 else None),
                    "conversation_tokens": (attrs.get("conversationTokens")
                                            if len(metrics) == 1 else None),
                # No `run_id`, deliberately, and this is the one place in this
                # module where leaving it off matters. Every other event is a
                # single journalled act and belongs to the run that was open.
                # This one is the whole session's usage, and a session hosts
                # several runs -- 38 resumes against 22 starts here, plus every
                # subagent. Stamping the open run would charge one agent for
                # what the others spent. CONTRACT §3 requires the constituent
                # runs to report `cost_usd = NULL` rather than a share.
                }, "copilot.cli", "heuristic", 0.8, suffix="model:" + model)

    # ------------------------------------------------------------------
    # The bridge. CONTRACT.md §2.4 names `run.bound` as the link-evidence
    # event and nothing was emitting it: the journal reader published a
    # session id on every row and no row that said what that id joins *to*.
    #
    # What goes on it is a commit range, because it is the only exact key
    # available. Measured 2026-08-26 on 37 context blocks from 22 journals:
    # **0** carried a Jira key in the branch name, so
    # `extract_jira_key(None, branch)` -- the link this reader has shipped all
    # along -- resolves to NULL on every real session. A SHA does not have that
    # problem. It is not a guess about a name; it is the commit.
    #
    # `jira_issue_key` stays NULL here rather than being derived from the
    # branch. Publishing a key this reader cannot find is AR-1's exact
    # prohibition, and a NULL that is honest is worth more than a key that is
    # right by luck. Resolving the range to a ticket is the warehouse's job,
    # where the SCM side of the join actually lives.
    for branch, base, head in (state.advanced() if state.repo else []):
        events.append(common.build_event(
            event_type="run.bound",
            event_time=state.last_seen or state.started,
            natural_key=(session_id, "run:bound", branch),
            attributes={
                "copilot_session_id": session_id,
                "base_commit_sha": base,
                "head_commit_sha": head,
                "repository_host": state.host,
                "jira_issue_key": None,
            },
            actor=actor,
            context=common.make_context(
                jira_issue_key=None,
                repo_full_name=state.repo,
                branch_name=branch,
            ),
            agent=dict(common.make_agent("copilot.cli", surface=SURFACE),
                       agent_version=None, skill_name=state.skill()),
            # `explicit`: the session recorded these SHAs itself. Nothing here
            # is inferred from a time window or a name that looked close.
            link=common.make_link("explicit", 1.0),
            trace_id=session_id,
            run_id=state.run_id,
        ))

    return events, state


def first_file_path(data: Dict[str, Any]) -> Optional[str]:
    """The first path a write tool reported touching.

    ``restrictedProperties.filePaths`` is a JSON *string* holding a list, which
    is why it cannot be read through the keep-list: the keep-list refuses
    nested values on purpose, and this arrives flattened into text. It is
    parsed here and immediately reduced to one path, which `relative` then
    strips to a repo-relative form.
    """
    raw = dig(data, "toolTelemetry.restrictedProperties.filePaths")
    if not isinstance(raw, str):
        return None
    try:
        paths = json.loads(raw)
    except ValueError:
        return None
    if isinstance(paths, list) and paths and isinstance(paths[0], str):
        return paths[0]
    return None


#: Reused from the git scanner so a file written by an agent and the same file
#: seen in a commit classify identically. Two rules would drift.
def artifact_type(path: str) -> str:
    import insight  # local: avoids a cycle at import time
    return insight.artifact_type(path)


# --------------------------------------------------------------------------
# all sessions
# --------------------------------------------------------------------------

def to_events(root: str = DEFAULT_ROOT, since: Optional[str] = None,
              actor: Optional[Dict[str, Any]] = None,
              jira_projects: Optional[Tuple[str, ...]] = None) -> Dict[str, Any]:
    """Read every session under ``root``.

    ``since`` (a ``YYYY-MM-DD``) skips journals untouched since then. It is an
    optimisation only: correctness comes from the deterministic `event_id`s, so
    a skipped journal that should not have been skipped costs a re-read and
    never a duplicate.
    """
    events: List[Dict[str, Any]] = []
    sessions: List[Session] = []
    skipped = 0

    cutoff = None
    if since:
        try:
            cutoff = datetime.strptime(since, "%Y-%m-%d").replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            cutoff = None

    for session_id, journal in iter_sessions(root):
        if cutoff is not None:
            try:
                if os.path.getmtime(journal) < cutoff:
                    skipped += 1
                    continue
            except OSError:
                pass
        try:
            found, state = session_events(session_id, journal, actor,
                                          jira_projects=jira_projects)
        except OSError:
            # A session being deleted underneath us is not a reason to lose the
            # other twenty-seven.
            continue
        events += found
        sessions.append(state)

    return {
        "events": events,
        "sessions": sessions,
        "sessions_read": len(sessions),
        "sessions_skipped": skipped,
        "coverage": coverage(sessions),
    }


def discover_repos(root: str = DEFAULT_ROOT) -> List[str]:
    """Every git working tree Copilot has been run in, from the journals.

    This is why nothing has to be registered by hand any more. ``setup`` used to
    take a repeated ``--repo`` and remember the answers, and the failure mode
    was structural: the repository somebody forgot to name was the one that
    silently reported nothing, and a bundle missing it looked exactly like a
    quiet week. Asking was never the point -- knowing which trees to walk was --
    and ``session.start`` has been writing ``context.gitRoot`` all along.

    Worktrees come back alongside their parent, and that is correct rather than
    duplication: they are separate checkouts with separate branches. ``scan``
    keys commits by ``(repo_full_name, sha)``, so a commit reachable from both
    yields one event id and de-duplicates on the way into the buffer.

    Paths that no longer exist are dropped. A clone deleted last month should
    not become an error every hour.
    """
    found: List[str] = []
    state = os.path.join(root, "session-state")
    if not os.path.isdir(state):
        return found
    for _, journal in iter_sessions(root):
        try:
            for record in iter_records(journal):
                if record.get("type") not in ("session.start", "session.resume"):
                    continue
                path = dig(record.get("data") or {}, "context.gitRoot")
                # `exists`, not `isdir`: in a linked worktree `.git` is a
                # *file* holding a gitdir pointer, and worktrees are where a
                # good deal of agent work happens.
                if (isinstance(path, str) and path and path not in found
                        and os.path.exists(os.path.join(path, ".git"))):
                    found.append(path)
        except OSError:
            continue
    return sorted(found)


def coverage(sessions: List[Session]) -> Dict[str, Any]:
    """What this read could and could not see.

    Published alongside the events because two of its numbers change how the
    cost section must be read, and a reader who cannot see them will read a
    small total as small usage rather than as partial measurement.

    * ``sessions_without_usage`` -- a session ends without ``session.shutdown``
      when it crashed, was killed, or is still open. It carries **no**
      ``modelMetrics``, so its tokens are not merely uncounted, they are
      unknowable. Measured 2026-08-26: 2 of 22 on the reference machine.
      Note the denominator: that tree held 28 session *directories*, six of
      which contain no ``events.jsonl`` at all. An empty directory is a session
      that recorded nothing, not a session whose usage went missing, and
      counting it here would report a collection gap that does not exist.
    * ``surfaces_not_covered`` -- named, always, so that a surface nobody
      measured never reads as a surface nobody used.
    """
    total = len(sessions)
    without = [s.id for s in sessions if not s.has_usage]
    return {
        "sessions": total,
        "sessions_with_usage": total - len(without),
        "sessions_without_usage": len(without),
        "usage_coverage": round((total - len(without)) / total, 3) if total else None,
        "surfaces_not_covered": ["vscode-copilot-chat", "inline-completions"],
    }


def verify_no_content(events: List[Dict[str, Any]]) -> List[str]:
    """Last line of defence: the collector's own allow-list, applied here.

    The keep-list should make this impossible. It runs anyway, because the cost
    of being wrong is publishing somebody's prompts -- and unlike the span
    stream, this source has the whole conversation sitting next to the numbers.
    """
    problems = []
    for event in events:
        allowed = collector_main.ATTRIBUTE_ALLOWLIST.get(event["event_type"])
        if allowed is None:
            problems.append(f"{event['event_id']}: unknown event type")
            continue
        for key in event.get("attributes") or {}:
            if key not in allowed:
                problems.append(f"{event['event_id']}: {key} not allowed")
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert Copilot's session journals into contract events.")
    parser.add_argument("--root", default=DEFAULT_ROOT,
                        help="Copilot home (default: ~/.copilot)")
    parser.add_argument("--since", help="skip journals untouched since YYYY-MM-DD")
    parser.add_argument("--out", help="write events here (default: stdout)")
    parser.add_argument("--summary", action="store_true",
                        help="print the coverage rollup instead of events")
    args = parser.parse_args(argv)

    if not os.path.isdir(os.path.join(args.root, "session-state")):
        raise SystemExit(f"no Copilot session journals under {args.root}")

    result = to_events(args.root, args.since)
    problems = verify_no_content(result["events"])
    if problems:
        for problem in problems[:5]:
            print("REJECTED " + problem, file=sys.stderr)
        raise SystemExit("attributes outside the allow-list; nothing written")

    if args.summary:
        print(json.dumps(result["coverage"], indent=2, sort_keys=True))
        return 0

    lines = "".join(json.dumps(e, sort_keys=True) + "\n" for e in result["events"])
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(lines)
    else:
        sys.stdout.write(lines)

    counts: Dict[str, int] = {}
    for event in result["events"]:
        counts[event["event_type"]] = counts.get(event["event_type"], 0) + 1
    print(json.dumps({"msg": "copilot_read_complete",
                      "sessions_read": result["sessions_read"],
                      "sessions_skipped": result["sessions_skipped"],
                      "events": len(result["events"]), "by_type": counts,
                      "coverage": result["coverage"]},
                     sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
