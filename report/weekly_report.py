"""Weekly AI-engineering report.

Reads the NDJSON event stream produced by the emitter and the pollers, and renders
a weekly report in Markdown (default), HTML, or JSON.

Usage:
    python weekly_report.py --input events.ndjson [more.ndjson ...] [options]

Options:
    --input PATH...     NDJSON files or directories. Directories are scanned for
                        *.ndjson. Repeatable. Reads stdin when omitted.
    --week YYYY-Www     ISO week to report (default: the most recent complete week
                        present in the data). Also accepts YYYY-MM-DD, which is
                        resolved to the ISO week containing that date.
    --weeks N           Include N trailing weeks of context in trend rows (default 4).
    --format md|html|json
    --out PATH          Output file (default: stdout).
    --min-group N       Suppress percentages for groups smaller than N (default 5).
    --scope-note TEXT   Name what this report covers when the input is a subset.

Cost metrics are restricted to link_method == "explicit" rows by construction; the
report shows the link-method distribution so the reader can see how much of the
window that covers. There is no per-person table: design section 11.5 requires
governance sign-off before any individual-level view is built.

What this report deliberately does NOT contain
----------------------------------------------
No ROI, no monetary "value delivered", no counterfactual "time saved" headline and
no AI-vs-human comparison. AI is applied to essentially all work here, so there is
no non-AI control group and no attribution is possible; any such number would be a
scenario model presented as a measurement.

The economic metric is premium requests per accepted output -- both terms measured,
no pricing table, no counterfactual. Premium requests are the unit Copilot bills
(CONTRACT.md section 4.1); tokens are an economic weight and are reported as such.
The comparisons that ARE valid are between AI configurations: agent vs agent, model
vs model, skill on vs off.

Refused metrics, and the measurement that refused them
------------------------------------------------------
The practice here is to publish the refusal rather than quietly omit the number,
because a metric that is merely absent gets re-proposed every quarter. Each of
these was computable from the session journal and was rejected on the evidence.

* **Time to approve a permission.** All 151 request-to-complete deltas measured
  2026-08-26 are exactly 0.00s: the journal writes the request record
  retroactively, at completion. The field would measure file-writing order.
* **Permission denial rate.** ``result.kind`` is ``approved`` on 151 of 151
  records. A denial produces no such event at all -- it surfaces as a tool error --
  so the denominator structurally excludes the numerator and the rate is 0% by
  construction, for every week, forever.
* **Per-run cost from ``assistant.message.outputTokens``.** This is the seductive
  one, because it works: per-message output tokens sum exactly to the session
  total and each one is timestamped, so they could be attributed to a run. But
  output tokens are 0.3-1.1% of input tokens. It would attribute roughly 1% of
  the cost mass and present the result as the cost.
* **Tokens per agent or sub-agent.** CONTRACT.md section 3's ``model.call``
  warning is categorical: the journal totals usage per session, and 38 resumes
  across 22 sessions makes the multi-run session the norm, not the exception.
* **Repeated ``edit`` on one file read as rework.** Copilot's ``edit`` applies one
  ``old_str`` -> ``new_str`` per call, so five unrelated changes to one file are
  five calls and zero rework. It measures the tool's edit granularity. The
  defended rework metric already exists: ``post_review_change_ratio`` (CONTRACT
  section 5), which is computed against review, not against tool call count.

See docs/spikes/ai-effectiveness-observability.md sections 8.16, 9.1 and 11.5.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _fail(message: str) -> None:
    print(json.dumps({"error": message}, indent=2), file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso_week(moment: datetime) -> str:
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def week_bounds(label: str) -> Tuple[datetime, datetime]:
    """Monday 00:00 UTC to the following Monday 00:00 UTC for an ISO week label."""
    year, week = int(label[:4]), int(label[6:])
    monday = datetime.fromisocalendar(year, week, 1).replace(tzinfo=timezone.utc)
    return monday, monday + timedelta(days=7)


def previous_weeks(label: str, count: int) -> List[str]:
    start, _ = week_bounds(label)
    return [iso_week(start - timedelta(weeks=offset)) for offset in range(count, 0, -1)]


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    """Nearest-rank percentile. Returns None on an empty input rather than 0."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def median(values: Sequence[float]) -> Optional[float]:
    return statistics.median(values) if values else None


#: Automation artefact kinds worth naming in the report. 'other' is not one:
#: it is everything that is not an automation artefact, and listing it under
#: "created by kind" reads as though it were.
AUTOMATION_KINDS = ("feature", "step_definition", "spec", "page_object", "fixture")

#: AIO runs its own priority scale and has no P1/P2/P3 field. On this project the
#: mapping below is the agreed reading; it is written down here rather than
#: assumed at each call site, because a wrong mapping quietly reports the wrong
#: population under the right label.
PRIORITY_LABELS = {"high": "P1", "medium": "P2", "low": "P3", "lowest": "P4"}


def priority_label(name: Optional[str]) -> str:
    if not name:
        return "unset"
    key = str(name).strip().lower()
    return f"{PRIORITY_LABELS[key]} ({name})" if key in PRIORITY_LABELS else str(name)

#: Test-run status categories that represent an actual execution. 'not_run' is
#: excluded on purpose: AIO seeds every new cycle with one row per test case at
#: "Not Run", so counting them turns cycle planning into apparent test activity
#: and puts a denominator under a pass rate nobody earned.
TEST_EXECUTED = ("passed", "failed", "blocked", "skipped")

#: Event types produced by whatever client is installed on the engineer's machine,
#: as opposed to the pollers. A change of client — the 1.1.0 move from Copilot
#: Chat's OTel span exporter to Copilot CLI's own session journal — changes what
#: these rows can contain, so the week either side of it is not comparable. §9
#: marks the affected cells rather than drawing a line through the whole table.
CLIENT_EVENT_TYPES = frozenset({
    "run.started", "run.bound", "run.phase.started", "run.phase.completed",
    "run.completed", "run.failed", "run.timeout", "run.abandoned",
    "model.call", "tool.call", "gate.evaluated", "output.generated", "human.turn",
})

#: Trend rows whose meaning depends on which client produced the week (see above).
#: PR, CI and Jira rows come from pollers and are unaffected.
SOURCE_SENSITIVE_ROWS = frozenset({
    "Runs", "Tool error rate", "Gate pass rate", "Total tokens",
    "Premium requests", "Cache read %",
})

#: Fields the current source does not carry. Each renders as an em dash and each
#: is named in §10, because a reader who sees a dash deserves to know whether it
#: means "nothing happened" or "nothing was recorded".
NULL_BY_SOURCE = (
    ("`model.call.retry_count`", "not recorded by the session journal (CONTRACT §3 row 5)"),
    ("`model.call.finish_reason`", "not recorded by the session journal (CONTRACT §3 row 5)"),
    ("`tool.call.duration_ms`", "the journal's two timestamps span the wait for a "
                               "human approval, which is not the tool's duration"),
    ("`cost_usd` / `cost_basis`", "derived in the warehouse, never emitted by a "
                                  "client (CONTRACT §4)"),
    ("`model.call.tool_definitions_tokens` and the other two context levels",
     "NULL for a session that used more than one model — the journal records "
     "one context, not one per model, so there is nothing to split"),
)


def ratio(numerator: int, denominator: int, min_group: int) -> Optional[float]:
    """Percentage, suppressed when the denominator is too small to be meaningful.

    Returning None rather than a number is deliberate. A "100% acceptance rate" over
    two outputs is noise that reads as a finding, and suppressing it doubles as the
    k-anonymity control required for any figure that crosses a person boundary
    (design section 11.5).
    """
    if denominator < min_group or denominator == 0:
        return None
    return 100.0 * numerator / denominator


def fmt_pct(value: Optional[float], denominator: Optional[int] = None) -> str:
    if value is None:
        return f"n={denominator}" if denominator is not None else "—"
    return f"{value:.1f}%"


def fmt_num(value: Optional[float], suffix: str = "", digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}{suffix}"


def fmt_tokens(value: Optional[float]) -> str:
    """A token count, or an em dash. Never 0 standing in for "not recorded"."""
    if value is None:
        return "—"
    return f"{int(round(value)):,}"


def fmt_range(values: Sequence[float]) -> str:
    """Observed min-max. A level is worth a range: the spread IS the finding.

    A single median hides that `tool_definitions_tokens` moves in steps when the
    tool set changes rather than drifting with the work.
    """
    if not values:
        return "—"
    low, high = min(values), max(values)
    if low == high:
        return fmt_tokens(low)
    return f"{fmt_tokens(low)} – {fmt_tokens(high)}"


def fmt_duration(ms: Optional[float]) -> str:
    """Human duration, scaled to the magnitude.

    Fixed hours would render a 12-second merge as "0.0h" and hide the most
    interesting thing in the data: a pull request that was merged before anyone
    could have read it. Sub-minute and sub-hour values keep their own units.
    """
    if ms is None:
        return "—"
    seconds = ms / 1000
    if seconds < 90:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


# Backwards-compatible alias; the name no longer describes what it does.
fmt_hours = fmt_duration


def arrow(current: Optional[float], previous: Optional[float], higher_is_better: bool = True) -> str:
    if current is None or previous is None:
        return ""
    delta = current - previous
    if abs(delta) < 1e-9:
        return " ="
    good = (delta > 0) == higher_is_better
    return f" {'▲' if delta > 0 else '▼'}{abs(delta):.1f}{'' if good else ' !'}"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def iter_paths(inputs: Sequence[str]) -> Iterable[str]:
    for entry in inputs:
        if os.path.isdir(entry):
            for root, _dirs, files in os.walk(entry):
                for name in sorted(files):
                    if name.endswith(".ndjson"):
                        yield os.path.join(root, name)
        else:
            yield entry


def load_events(inputs: Sequence[str]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Load NDJSON events. Malformed lines are counted, never silently dropped."""
    events: List[Dict[str, Any]] = []
    stats = {"files": 0, "lines": 0, "malformed": 0, "no_timestamp": 0}
    seen_ids = set()

    def consume(handle: Iterable[str]) -> None:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            stats["lines"] += 1
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                stats["malformed"] += 1
                continue
            if not isinstance(event, dict) or "event_type" not in event:
                stats["malformed"] += 1
                continue
            event_id = event.get("event_id")
            if event_id and event_id in seen_ids:
                continue  # idempotent: poller event ids are deterministic
            if event_id:
                seen_ids.add(event_id)
            if not parse_ts(event.get("event_time")):
                stats["no_timestamp"] += 1
                continue
            events.append(event)

    if not inputs:
        consume(sys.stdin)
    else:
        for path in iter_paths(inputs):
            stats["files"] += 1
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    consume(handle)
            except OSError as exc:
                _fail(f"cannot read {path}: {type(exc).__name__}")
    return events, stats


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

class WeekAggregate:
    """Everything the report needs for one ISO week."""

    def __init__(self, label: str, min_group: int) -> None:
        self.label = label
        self.min_group = min_group

        self.runs = 0
        self.runs_by_status: Counter = Counter()
        self.runs_by_agent: Counter = Counter()
        self.runs_by_model: Counter = Counter()
        self.runs_by_input_source: Counter = Counter()
        self.people: set = set()
        self.projects: set = set()
        self.repos: set = set()
        self.skills: set = set()
        # A Copilot CLI session is one journal file and one `trace_id`; it hosts
        # several runs (start, then a resume per continuation, then a run per
        # sub-agent). Sessions and runs are different denominators and the
        # adoption table must not silently use one for the other.
        self.sessions: set = set()
        self.sessions_with_usage: set = set()

        self.run_duration_ms: List[float] = []
        self.input_tokens = 0
        self.output_tokens = 0
        # NULL, not 0, for the same reason as `reasoning_tokens` below: a
        # source that never reports the field must not render as a source that
        # measured zero. VS Code Copilot Chat carries none of these three --
        # measured 2026-W34, `premium_requests` NULL on 75 of 75 model calls --
        # and seeding them at 0 printed "Premium requests 0 | measured" and
        # "cache reads 0 (0.0%)" for a week nobody had measured at all.
        self.cached_tokens: Optional[int] = None
        self.cache_write_tokens: Optional[int] = None
        self.reasoning_tokens: Optional[int] = None
        self.premium_requests: Optional[int] = None
        self.api_requests = 0
        self.nano_aiu: Optional[int] = None
        self.model_latency_ms: List[float] = []
        # Context composition (CONTRACT §3 row 5). These are LEVELS, not usage:
        # what the model was carrying at the end of a session and paid for on
        # every request in it. They are kept as one value per session and never
        # summed across the week — adding a level counts the same fixed overhead
        # once per session, and multiplying by `request_count` would be
        # modelling. Hence lists, and a median at render time.
        self.context_tool_defs: List[int] = []
        self.context_system: List[int] = []
        self.context_conversation: List[int] = []
        self.context_total: List[int] = []
        #: Sessions whose context is NULL because they used more than one model.
        self.context_unknown = 0
        # CONTRACT §4: cost is derived in the warehouse and is never emitted by a
        # client, so `cost_usd` is absent from the collector allow-list and never
        # arrives here. None means "no source supplied one" and renders as a dash.
        # It was 0.0 for as long as this file has existed, which rendered "$0.00"
        # every week — a measurement of zero for something nobody measured.
        self.cost_usd: Optional[float] = None
        self.cost_basis: Counter = Counter()
        self.tool_calls = 0
        # Three buckets, not two. The old single `tool_failures` counter pooled a
        # NULL status with a successful one, so a source that carried no verdict
        # reported a perfect week. Unknown is its own column.
        self.tool_ok = 0
        self.tool_error = 0
        self.tool_status_unknown = 0
        self.tool_kinds: Counter = Counter()
        self.tool_error_classes: Counter = Counter()
        # None, not 0: `retry_count` is permanently NULL on the journal source
        # (CONTRACT §3 row 5). It becomes an int only if some source supplies one.
        self.retries: Optional[int] = None
        self.dependency_failures: Counter = Counter()

        self.human_turns: Counter = Counter()
        self.runs_with_correction: set = set()
        self.runs_seen: set = set()
        self.runs_terminal: set = set()

        self.outputs_by_state: Counter = Counter()
        self.output_lines_added = 0
        self.output_lines_removed = 0

        #: gate_name -> Counter of pass / fail / unknown. A NULL verdict is a
        #: missing verdict, not a third outcome of the gate, so it gets its own
        #: column and stays out of the pass-rate denominator.
        self.gates: Dict[str, Counter] = {}
        #: gate_name -> attempt_index at the first pass within a session. Reads
        #: `attempt_index`, which the emitter has always sent and nothing read.
        self.gate_first_pass: Dict[str, List[int]] = {}
        self._gate_passed: set = set()

        #: Which client produced this week's agent-side events. Two weeks either
        #: side of a source change are not comparable, and §9 says so per cell.
        self.client_surfaces: Counter = Counter()
        self.client_schemas: Counter = Counter()

        self.prs_merged = 0
        self.prs_declined = 0
        self.prs_reverted = 0
        self.review_lead_ms: List[float] = []
        self.merge_lead_ms: List[float] = []
        self.pr_lines: Dict[Any, int] = {}
        self.pr_comments: Dict[Any, int] = {}
        self.pr_ai_marker: Dict[Any, bool] = {}

        # AIO TCMS test execution (CONTRACT §3 event 22).
        self.tests_by_category: Counter = Counter()
        self.tests_automated = 0
        self.test_defects = 0
        self.test_cycles: set = set()
        self.test_executors: set = set()
        self.automation_status: Counter = Counter()
        self.scripts_added = 0
        self.scripts_modified = 0
        self.script_kinds: Counter = Counter()

        self.ci_by_status: Counter = Counter()
        self.ci_duration_ms: List[float] = []
        self.ci_by_kind: Counter = Counter()

        self.link_methods: Counter = Counter()
        self.event_types: Counter = Counter()

    # -- ingestion ---------------------------------------------------------

    def add(self, event: Dict[str, Any]) -> None:
        kind = event.get("event_type", "")
        attrs = event.get("attributes") or {}
        agent = event.get("agent") or {}
        actor = event.get("actor") or {}
        context = event.get("context") or {}

        self.event_types[kind] += 1
        self.link_methods[((event.get("link") or {}).get("method") or "unknown")] += 1

        if actor.get("person_id") or actor.get("person_email_hash"):
            self.people.add(actor.get("person_id") or actor.get("person_email_hash"))
        if context.get("jira_project_key"):
            self.projects.add(context["jira_project_key"])
        if context.get("repo_full_name"):
            self.repos.add(context["repo_full_name"])

        run_id = event.get("run_id")
        if run_id:
            self.runs_seen.add(run_id)

        if kind in CLIENT_EVENT_TYPES:
            self.client_surfaces[agent.get("surface") or "unknown"] += 1
            self.client_schemas[str(event.get("schema_version") or "unknown")] += 1
            if event.get("trace_id"):
                self.sessions.add(event["trace_id"])
            if agent.get("skill_name"):
                self.skills.add(agent["skill_name"])

        if kind == "run.started":
            self.runs += 1
            if agent.get("agent_name"):
                self.runs_by_agent[agent["agent_name"]] += 1
            # `input_source` distinguishes a fresh session from a resume of one
            # and from a sub-agent the supervisor spawned. All three are runs;
            # only the first is a person sitting down to start something.
            source = attrs.get("input_source")
            if source is None and event.get("parent_run_id"):
                source = "subagent"
            self.runs_by_input_source[source or "unknown"] += 1

        elif kind in ("run.completed", "run.failed", "run.timeout", "run.abandoned"):
            self.runs_by_status[kind.split(".", 1)[1]] += 1
            if run_id:
                self.runs_terminal.add(run_id)
            if attrs.get("duration_ms"):
                self.run_duration_ms.append(float(attrs["duration_ms"]))
            dep = attrs.get("dependency_failed")
            if dep and dep != "none":
                self.dependency_failures[dep] += 1

        elif kind == "model.call":
            self.input_tokens += int(attrs.get("input_tokens") or 0)
            self.output_tokens += int(attrs.get("output_tokens") or 0)
            # CONTRACT §3 row 5: input_tokens = cacheRead + cacheWrite + fresh,
            # exactly. The cached share is therefore a share OF input_tokens and
            # `total_tokens = in + out` does not double-count.
            self.api_requests += int(attrs.get("request_count") or 0)
            for field, name in (("cached_input_tokens", "cached_tokens"),
                                ("cache_write_tokens", "cache_write_tokens"),
                                ("premium_requests", "premium_requests"),
                                ("reasoning_tokens", "reasoning_tokens"),
                                ("nano_aiu", "nano_aiu")):
                value = attrs.get(field)
                # NULL where the model reports none. Seeding these at 0 would
                # print a measured zero for a model that never told us.
                if value is not None:
                    setattr(self, name, (getattr(self, name) or 0) + int(value))
            if attrs.get("latency_ms") is not None:
                self.model_latency_ms.append(float(attrs["latency_ms"]))
            # All three or none. A partial composition does not add up, and the
            # reason the other two are carried at all is so the whole is visible
            # behind `tool_definitions_tokens` rather than one number alone.
            composition = [attrs.get("tool_definitions_tokens"),
                           attrs.get("system_tokens"),
                           attrs.get("conversation_tokens")]
            if all(value is not None for value in composition):
                defs, system, conversation = (int(v) for v in composition)
                self.context_tool_defs.append(defs)
                self.context_system.append(system)
                self.context_conversation.append(conversation)
                self.context_total.append(defs + system + conversation)
            else:
                # The journal records one context per session, not one per
                # model, so a multi-model session has no context to report and
                # apportioning it between the models would be invention.
                self.context_unknown += 1
            if attrs.get("retry_count") is not None:
                self.retries = (self.retries or 0) + int(attrs["retry_count"])
            model = attrs.get("model_id")
            if model:
                self.runs_by_model[model] += 1
            if event.get("trace_id"):
                self.sessions_with_usage.add(event["trace_id"])
            if attrs.get("cost_usd") is not None:
                self.cost_usd = (self.cost_usd or 0.0) + float(attrs["cost_usd"])
                self.cost_basis[attrs.get("cost_basis") or "unknown"] += 1

        elif kind == "tool.call":
            self.tool_calls += 1
            status = (attrs.get("status") or "").lower()
            if not status:
                self.tool_status_unknown += 1
            elif status in ("ok", "success", "passed"):
                self.tool_ok += 1
            else:
                self.tool_error += 1
                self.tool_error_classes[attrs.get("error_class") or "unclassified"] += 1
            # `tool_kind` is a closed five-value enum (CONTRACT §3 row 6).
            # `tool_name` deliberately is not broken out: 23 distinct values here
            # and unbounded across MCP servers, which breaches §1 rule 5.
            self.tool_kinds[attrs.get("tool_kind") or "unknown"] += 1

        elif kind == "human.turn":
            turn_kind = attrs.get("turn_kind") or "unknown"
            self.human_turns[turn_kind] += 1
            # Approval and clarification are DESIGNED gates, not interventions
            # (design section 8.11). Counting them would punish correct behaviour.
            if turn_kind in ("correction", "rejection") and run_id:
                self.runs_with_correction.add(run_id)

        elif kind == "output.generated":
            self.outputs_by_state[attrs.get("acceptance_state") or "in_flight"] += 1
            self.output_lines_added += int(attrs.get("lines_added") or 0)
            self.output_lines_removed += int(attrs.get("lines_removed") or 0)

        elif kind == "gate.evaluated":
            gate = str(attrs.get("gate_name") or "unknown")
            raw = (attrs.get("status") or "").lower()
            # A missing exit-code trailer means nobody saw the verdict (~12% of
            # bash calls). It is not a third gate outcome and it never enters a
            # pass rate; the old key rendered it as a literal `None` row.
            verdict = raw if raw in ("pass", "fail", "skipped") else "unknown"
            self.gates.setdefault(gate, Counter())[verdict] += 1
            attempt = attrs.get("attempt_index")
            if verdict == "pass" and attempt is not None:
                # First pass per session, per gate: re-running a green suite is
                # not a retry. `trace_id` is the session; `attempt_index` is
                # only meaningful within one.
                key = (event.get("trace_id"), gate)
                if key not in self._gate_passed:
                    self._gate_passed.add(key)
                    self.gate_first_pass.setdefault(gate, []).append(int(attempt))

        elif kind == "scm.pr.merged":
            self.prs_merged += 1
            self._pr_common(attrs)
            self._automation_files(attrs)
            if attrs.get("merge_lead_time_ms"):
                self.merge_lead_ms.append(float(attrs["merge_lead_time_ms"]))

        elif kind == "scm.pr.declined":
            self.prs_declined += 1
            self._pr_common(attrs)

        elif kind == "scm.pr.reviewed":
            self._pr_common(attrs)
            if attrs.get("review_lead_time_ms"):
                self.review_lead_ms.append(float(attrs["review_lead_time_ms"]))

        elif kind == "scm.revert":
            self.prs_reverted += 1

        elif kind == "test.case.snapshot":
            if attrs.get("is_archived"):
                self.automation_status["archived"] += 1
            else:
                status = (attrs.get("automation_status") or "").strip().lower()
                self.automation_status[status or "unset"] += 1

        elif kind == "test.run.completed":
            category = attrs.get("status_category") or "other"
            self.tests_by_category[category] += 1
            # 'not_run' rows are AIO's seeding of a new cycle, not test activity.
            # They are counted so the section can say how many were skipped, but
            # never enter the executed denominator.
            if category in TEST_EXECUTED:
                if attrs.get("is_automated"):
                    self.tests_automated += 1
                if attrs.get("executed_by_person_id"):
                    self.test_executors.add(attrs["executed_by_person_id"])
            self.test_defects += int(attrs.get("defect_count") or 0)
            if attrs.get("test_cycle_key"):
                self.test_cycles.add(attrs["test_cycle_key"])

        elif kind == "ci.pipeline.completed":
            self.ci_by_status[attrs.get("status") or "unknown"] += 1
            self.ci_by_kind[attrs.get("ci_kind") or "unknown"] += 1
            if attrs.get("duration_ms"):
                self.ci_duration_ms.append(float(attrs["duration_ms"]))

    def _automation_files(self, attrs: Dict[str, Any]) -> None:
        self.scripts_added += int(attrs.get("automation_scripts_added") or 0)
        self.scripts_modified += int(attrs.get("automation_scripts_modified") or 0)
        for kind, counts in (attrs.get("automation_files_by_kind") or {}).items():
            if isinstance(counts, dict):
                self.script_kinds[kind] += int(counts.get("added") or 0)

    def _pr_common(self, attrs: Dict[str, Any]) -> None:
        pr_id = attrs.get("pr_id")
        if pr_id is None:
            return
        lines = (attrs.get("lines_added") or 0) + (attrs.get("lines_removed") or 0)
        if lines:
            self.pr_lines[pr_id] = lines
        if attrs.get("comment_count") is not None:
            self.pr_comments[pr_id] = max(
                self.pr_comments.get(pr_id, 0), int(attrs["comment_count"])
            )
        marker = attrs.get("pr_title_has_ai_marker")
        if marker is not None:
            self.pr_ai_marker[pr_id] = bool(marker)

    # -- derived -----------------------------------------------------------

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def accepted(self) -> int:
        return self.outputs_by_state.get("accepted", 0)

    @property
    def judged_outputs(self) -> int:
        return sum(v for k, v in self.outputs_by_state.items() if k != "in_flight")

    @property
    def automation_known(self) -> int:
        """Cases whose automation status somebody has actually set."""
        return (self.automation_status["automated"]
                + self.automation_status["to be automated"])

    def automation_coverage(self, min_group: int = 5) -> Optional[float]:
        return ratio(self.automation_status["automated"], self.automation_known,
                     min_group)

    @property
    def tests_executed(self) -> int:
        return sum(self.tests_by_category[c] for c in TEST_EXECUTED)

    def test_pass_rate(self, min_group: int = 5) -> Optional[float]:
        """passed / executed.

        Blocked and skipped stay in the denominator: a blocked test is a test
        that could not be run, and dropping it flatters the number.
        """
        return ratio(self.tests_by_category["passed"], self.tests_executed,
                     min_group)

    def acceptance_rate(self) -> Optional[float]:
        return ratio(self.accepted, self.judged_outputs, self.min_group)

    def cost_per_accepted(self) -> Optional[float]:
        if self.accepted < self.min_group or not self.cost_usd:
            return None
        return self.cost_usd / self.accepted

    def premium_per_accepted(self) -> Optional[float]:
        """Premium requests per accepted output — both terms measured.

        This replaces cost-per-accepted-output as the headline. Copilot bills per
        seat plus premium requests (CONTRACT §4.1), so this is a ratio of two
        counted things with no pricing table between them and nothing modelled.
        """
        if self.accepted < self.min_group or not self.premium_requests:
            return None
        return self.premium_requests / self.accepted

    @property
    def tool_status_known(self) -> int:
        return self.tool_ok + self.tool_error

    def tool_error_rate(self) -> Optional[float]:
        """error / (ok + error). Calls with no verdict are excluded, not passed.

        Pooling an unknown status with a success is what made the old counter
        report zero tool failures for the whole life of the span source.
        """
        return ratio(self.tool_error, self.tool_status_known, self.min_group)

    def gate_counts(self, verdict: str) -> int:
        return sum(counter.get(verdict, 0) for counter in self.gates.values())

    def gate_pass_rate(self, gate: Optional[str] = None) -> Optional[float]:
        """pass / (pass + fail). Unknown verdicts are excluded from both terms."""
        if gate is None:
            passed, failed = self.gate_counts("pass"), self.gate_counts("fail")
        else:
            counter = self.gates.get(gate) or Counter()
            passed, failed = counter.get("pass", 0), counter.get("fail", 0)
        return ratio(passed, passed + failed, self.min_group)

    @property
    def runs_without_terminal(self) -> int:
        return len(self.runs_seen - self.runs_terminal)

    @property
    def cached_share(self) -> Optional[float]:
        if not self.input_tokens or self.cached_tokens is None:
            return None
        return 100.0 * self.cached_tokens / self.input_tokens

    def tool_definition_share(self) -> Optional[float]:
        """Median share of a session's context taken by tool definitions.

        The median of the per-session shares, not a share of the medians: the
        three levels come from the same session and dividing one week-median by
        another would pair numbers that never coexisted.
        """
        shares = [100.0 * defs / total
                  for defs, total in zip(self.context_tool_defs, self.context_total)
                  if total]
        if len(shares) < self.min_group:
            return None
        return median(shares)

    @property
    def source_fingerprint(self) -> frozenset:
        """Which client produced this week's agent events.

        Both halves matter: `surface` names the product and `schema_version`
        names the contract it was emitted against, and the 1.1.0 source change
        moved both.
        """
        return frozenset(
            [f"surface:{s}" for s in self.client_surfaces]
            + [f"schema:{v}" for v in self.client_schemas]
        )

    def run_success_rate(self) -> Optional[float]:
        total = sum(self.runs_by_status.values())
        return ratio(self.runs_by_status.get("completed", 0), total, self.min_group)

    def manual_intervention_rate(self) -> Optional[float]:
        return ratio(len(self.runs_with_correction), len(self.runs_seen), self.min_group)

    def pr_decline_rate(self) -> Optional[float]:
        closed = self.prs_merged + self.prs_declined
        return ratio(self.prs_declined, closed, self.min_group)

    def comments_per_100_lines(self) -> Optional[float]:
        shared = set(self.pr_comments) & set(self.pr_lines)
        if len(shared) < self.min_group:
            return None
        lines = sum(self.pr_lines[p] for p in shared)
        if lines <= 0:
            return None
        return 100.0 * sum(self.pr_comments[p] for p in shared) / lines

    def completeness(self) -> Optional[float]:
        total = sum(self.link_methods.values())
        return ratio(self.link_methods.get("explicit", 0), total, 1)


def inventory_coverage(events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Automation coverage across the whole estate, not per week.

    Coverage is a **stock**, not a flow. A ``test.case.snapshot`` is stamped with
    the case's ``updated_at``, so bucketing it by week answers "of the cases
    somebody edited last week, how many are automated" -- a number nobody asked
    for and everybody would misread as estate coverage.

    So the inventory is folded across every event, keeping the latest snapshot
    per test case, and reported as a position "as at" the newest one.
    """
    latest: Dict[str, Dict[str, Any]] = {}
    as_at: Optional[str] = None
    for event in events:
        if event.get("event_type") != "test.case.snapshot":
            continue
        attrs = event.get("attributes") or {}
        key = attrs.get("test_case_key")
        if not key:
            continue
        stamp = event.get("event_time") or ""
        previous = latest.get(key)
        if previous is None or stamp >= (previous.get("_stamp") or ""):
            latest[key] = dict(attrs, _stamp=stamp)
        if as_at is None or stamp > as_at:
            as_at = stamp

    counts: Counter = Counter()
    by_priority: Dict[str, Counter] = {}
    for attrs in latest.values():
        if attrs.get("is_archived"):
            counts["archived"] += 1
            continue
        status = (attrs.get("automation_status") or "").strip().lower() or "unset"
        counts[status] += 1
        counts["live"] += 1
        bucket = by_priority.setdefault(priority_label(attrs.get("priority")),
                                        Counter())
        bucket[status] += 1
        bucket["live"] += 1
    known = counts["automated"] + counts["to be automated"]

    priorities = []
    for label in sorted(by_priority, key=lambda x: (x == "unset", x)):
        bucket = by_priority[label]
        priorities.append({
            "label": label, "counts": bucket, "live": bucket["live"],
            "known": bucket["automated"] + bucket["to be automated"],
            "coverage_pct": (round(100 * bucket["automated"] / bucket["live"], 1)
                             if bucket["live"] else None),
        })
    return {
        "counts": counts, "known": known, "as_at": as_at,
        "cases": len(latest), "by_priority": priorities,
        "statuses": [k for k in counts
                     if k not in ("live", "archived")],
        # PRIMARY -- matches the AIO "Regression Test Automation Coverage"
        # dashboard: Automated / Total, where Total is every live case in scope.
        # An earlier version divided by Automated + To Be Automated only, on the
        # reasoning that an unset field is not a claim of "not automated". AIO
        # does not agree, and AIO owns the metric: its own tile labels
        # Total - Automated as "Non Automated", so Manual, In Progress and Not
        # Assigned all sit in the denominator. Reporting a different definition
        # under the same name is how two dashboards end up disagreeing by three
        # points with nobody able to say which is right.
        "coverage_pct": (round(100 * counts["automated"] / counts["live"], 1)
                         if counts["live"] else None),
        # SECONDARY -- the stricter reading, kept because it answers a different
        # and still useful question: of the cases somebody has triaged, how many
        # are done.
        "coverage_pct_classified": (round(100 * counts["automated"] / known, 1)
                                    if known else None),
    }


def aggregate(events: Sequence[Dict[str, Any]], min_group: int) -> Dict[str, WeekAggregate]:
    weeks: Dict[str, WeekAggregate] = {}
    for event in events:
        moment = parse_ts(event.get("event_time"))
        if not moment:
            continue
        label = iso_week(moment)
        weeks.setdefault(label, WeekAggregate(label, min_group)).add(event)
    return weeks


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_markdown(
    label: str,
    weeks: Dict[str, WeekAggregate],
    trail: Sequence[str],
    stats: Dict[str, int],
    min_group: int,
    inventory: Optional[Dict[str, Any]] = None,
    scope_note: Optional[str] = None,
) -> str:
    current = weeks.get(label)
    if current is None:
        return f"# AI Engineering — Weekly Report\n\nNo events found for **{label}**.\n"
    prior = weeks.get(trail[-1]) if trail else None
    start, end = week_bounds(label)
    out: List[str] = []
    add = out.append

    add(f"# AI Engineering — Weekly Report · {label}")
    add("")
    if scope_note:
        # A scoped report and a whole-team report look identical once the numbers
        # are pasted into a deck, so the scope is stated before anything else.
        add(f"> **Scope: {scope_note}**  ")
        add("> This is a subset, not the whole team's activity. Do not compare "
            "its totals against an unscoped report.")
        add("")
    add(f"**Window** {start:%Y-%m-%d} → {(end - timedelta(days=1)):%Y-%m-%d} (UTC)  ")
    add(f"**Events** {sum(current.event_types.values()):,}  ·  "
        f"**Repos** {len(current.repos) or '—'}  ·  "
        f"**Projects** {len(current.projects) or '—'}")
    completeness = current.completeness()
    add(f"**Explicit-link completeness** {fmt_pct(completeness)}  "
        f"— only explicitly-linked rows are admissible to cost metrics")
    add("")
    add(f"> Percentages are suppressed for groups smaller than **{min_group}**; the raw "
        f"count is shown instead. This keeps small-sample noise from reading as a "
        f"finding, and doubles as the k-anonymity control.")
    add("")

    # -- 1. Adoption ------------------------------------------------------
    add("## 1. Adoption")
    add("")
    add("| Metric | This week | Prior |")
    add("|---|---:|---:|")
    sources = current.runs_by_input_source
    prior_sources = prior.runs_by_input_source if prior else Counter()
    add(f"| AI runs | {current.runs} | {prior.runs if prior else '—'} |")
    add(f"| ...started fresh | {sources.get('start', 0) or '—'} | "
        f"{prior_sources.get('start', 0) if prior else '—'} |")
    add(f"| ...resumed | {sources.get('resume', 0) or '—'} | "
        f"{prior_sources.get('resume', 0) if prior else '—'} |")
    add(f"| ...sub-agent invocations | {sources.get('subagent', 0) or '—'} | "
        f"{prior_sources.get('subagent', 0) if prior else '—'} |")
    add(f"| Sessions | {len(current.sessions) or '—'} | "
        f"{len(prior.sessions) if prior else '—'} |")
    add(f"| Active people | {len(current.people) or '—'} | {len(prior.people) if prior else '—'} |")
    add(f"| Distinct agents used | {len(current.runs_by_agent) or '—'} | "
        f"{len(prior.runs_by_agent) if prior else '—'} |")
    add(f"| Distinct models seen | {len(current.runs_by_model) or '—'} | "
        f"{len(prior.runs_by_model) if prior else '—'} |")
    add(f"| Distinct skills used | {len(current.skills) or '—'} | "
        f"{len(prior.skills) if prior else '—'} |")
    add("")
    add("> A **session** is one journal file; it hosts several **runs** — the "
        "first activation, one per resume, and one per sub-agent. Runs and "
        "sessions are different denominators; nothing here divides one by the "
        "other.")
    add("")
    if current.runs_by_agent:
        # NOT a league table. The measured `agentName` values on this surface are
        # Copilot's own generic sub-agent labels, so the split says which slot a
        # run record came out of — it does not compare one team's agent against
        # another's, and there is no configuration behind these names to prefer.
        add("**Where run records came from** — a breakdown, not a comparison")
        add("")
        add("| Agent name | Runs |")
        add("|---|---:|")
        for name, count in current.runs_by_agent.most_common(10):
            add(f"| `{name}` | {count} |")
        add("")
        add("> These are the `agent_name` values the source reports, not "
            "configurations anyone chose between. On the Copilot CLI surface "
            "they are the CLI itself plus its generic sub-agent labels, so this "
            "table must not be read as agent-vs-agent performance. The valid "
            "configuration comparisons are model vs model and skill on vs off.")
        add("")

    # -- 2. Acceptance ----------------------------------------------------
    add("## 2. Output acceptance")
    add("")
    if current.judged_outputs or current.outputs_by_state:
        add("| State | Count |")
        add("|---|---:|")
        for state in ("accepted", "reworked", "rejected", "reverted", "in_flight"):
            add(f"| {state} | {current.outputs_by_state.get(state, 0)} |")
        add("")
        rate = current.acceptance_rate()
        prior_rate = prior.acceptance_rate() if prior else None
        add(f"**AI acceptance rate** {fmt_pct(rate, current.judged_outputs)}"
            f"{arrow(rate, prior_rate)}")
        add("")
        add("> Accepted = merged, ≤25% of its lines rewritten after first review, and not "
            "reverted within 30 days. Merged is not the same as accepted.")
    else:
        add("_No `output.generated` events in this window._  ")
        add("This section stays empty until the emitter is deployed — poller data alone "
            "cannot attribute an artifact to a run.")
    add("")

    # -- 3. Speed ---------------------------------------------------------
    add("## 3. Speed")
    add("")
    add("| Metric | Median | p85 | n |")
    add("|---|---:|---:|---:|")
    add(f"| PR review lead time | {fmt_hours(median(current.review_lead_ms))} | "
        f"{fmt_hours(percentile(current.review_lead_ms, 0.85))} | {len(current.review_lead_ms)} |")
    add(f"| PR merge lead time | {fmt_hours(median(current.merge_lead_ms))} | "
        f"{fmt_hours(percentile(current.merge_lead_ms, 0.85))} | {len(current.merge_lead_ms)} |")
    add(f"| AI run duration | {fmt_hours(median(current.run_duration_ms))} | "
        f"{fmt_hours(percentile(current.run_duration_ms, 0.85))} | {len(current.run_duration_ms)} |")
    add(f"| CI duration | {fmt_hours(median(current.ci_duration_ms))} | "
        f"{fmt_hours(percentile(current.ci_duration_ms, 0.85))} | {len(current.ci_duration_ms)} |")
    add("")
    add("> Median and p85, never the mean — these distributions are long-tailed by "
        "construction and a mean hides the tail that actually hurts.")
    add("")

    # A separate table, deliberately. Model API time is not on the same clock as
    # a merge lead time: one is seconds spent inside HTTP calls, the other is
    # hours of elapsed calendar including everyone's sleep. Putting them in
    # adjacent rows invites "2.0h to merge but only 76s of AI", which compares a
    # duration against a subset of a different duration.
    if current.model_latency_ms:
        add("**Model API time** — a different clock; do not compare against the rows above")
        add("")
        add("| Metric | Median | p85 | n |")
        add("|---|---:|---:|---:|")
        add(f"| AI session API time | {fmt_hours(median(current.model_latency_ms))} | "
            f"{fmt_hours(percentile(current.model_latency_ms, 0.85))} | "
            f"{len(current.model_latency_ms)} |")
        add("")
        add("> This is `totalApiDurationMs` per session — **time inside model API "
            "calls, not wall clock**. It excludes everything between calls: tool "
            "execution, the wait for a human to approve a command, and the human "
            "reading the answer. A session lasting an afternoon can show a minute "
            "here, and that is not a contradiction.")
        add("")
        add("> Carried only for sessions that used a **single model** (CONTRACT §3 "
            "row 5): `totalApiDurationMs` is the session's total, and splitting it "
            "across two models by request count would be modelling, not measuring.")
        add("")

    # -- 4. Quality -------------------------------------------------------
    add("## 4. Quality")
    add("")
    add("| Metric | Value | Basis |")
    add("|---|---:|---|")
    closed = current.prs_merged + current.prs_declined
    add(f"| PRs merged | {current.prs_merged} | |")
    add(f"| PRs declined | {current.prs_declined} | |")
    add(f"| PR decline rate | {fmt_pct(current.pr_decline_rate(), closed)} | of {closed} closed |")
    add(f"| Reverts detected | {current.prs_reverted} | |")
    add(f"| Review comments / 100 lines | {fmt_num(current.comments_per_100_lines())} | "
        f"{len(set(current.pr_comments) & set(current.pr_lines))} PRs |")
    add("")
    if current.gates:
        add("**Quality gates**")
        add("")
        add("| Gate | Pass | Fail | Unknown | Pass rate |")
        add("|---|---:|---:|---:|---:|")
        for gate in sorted(current.gates):
            counter = current.gates[gate]
            passed = counter.get("pass", 0)
            failed = counter.get("fail", 0)
            unknown = counter.get("unknown", 0) + counter.get("skipped", 0)
            add(f"| {gate} | {passed} | {failed} | {unknown or '—'} | "
                f"{fmt_pct(current.gate_pass_rate(gate), passed + failed)} |")
        add("")
        add("> **Pass rate is pass / (pass + fail).** A gate whose verdict was "
            "never seen sits in its own column and in neither term. Roughly 12% "
            "of shell calls return no `<exited with exit code N>` trailer — the "
            "command was still running, or the output was truncated — and a "
            "missing verdict is a different fact from a passing one.")
        add("")

        # `attempt_index` has been emitted since the gate event existed and
        # nothing has ever read it. It answers the question a pass rate cannot:
        # not "did it go green" but "how many goes did that take".
        add("**Gate retry depth** — retries before the first pass, per gate, per session")
        add("")
        add("| Gate | Median retries to first pass | Max | Sessions |")
        add("|---|---:|---:|---:|")
        for gate in sorted(current.gates):
            attempts = current.gate_first_pass.get(gate) or []
            depth = median(attempts)
            add(f"| {gate} | "
                f"{fmt_num(depth, digits=0) if len(attempts) >= min_group else f'n={len(attempts)}'} | "
                f"{max(attempts) if attempts else '—'} | {len(attempts)} |")
        add("")
        add("> `attempt_index` counts prior runs of the same gate in the same "
            "session, so **0 means it passed first time**. A gate that never "
            "passed in a session contributes nothing here — the failures are in "
            "the table above, and inventing a retry count for a gate that is "
            "still red would be a guess.")
        add("")
        add("> Most weeks will show `n=` on most rows and that is correct, not a "
            "gap: only a shell command recognisable as a test, lint or build "
            "counts as a gate at all (33 of 887 measured), so the sample is small "
            "by construction and a percentage over it would be noise.")
        add("")

    # -- 5. Test execution -------------------------------------------------
    add("## 5. Test execution and automation")
    add("")
    if current.scripts_added or current.scripts_modified:
        add("**Automation output** — this week")
        add("")
        add("| Metric | Value | Basis |")
        add("|---|---:|---|")
        add(f"| Automation scripts created | {current.scripts_added:,} "
            f"| .feature / step-definition / spec files added in merged PRs |")
        add(f"| Automation scripts modified | {current.scripts_modified:,} | |")
        kinds = [(k, v) for k, v in current.script_kinds.most_common()
                 if k in AUTOMATION_KINDS and v]
        if kinds:
            add("| ...created by kind | "
                + ", ".join(f"{k} {v}" for k, v in kinds) + " | |")
        add("")

    if inventory and inventory["cases"]:
        counts, known = inventory["counts"], inventory["known"]
        unset = counts["unset"]
        add(f"**Automation coverage** — position as at "
            f"{(inventory['as_at'] or '')[:10]}, not a weekly figure")
        add("")
        add("| Metric | Value | Basis |")
        add("|---|---:|---|")
        add(f"| **Coverage** | {fmt_pct(inventory['coverage_pct'], counts['live'])} "
            f"| automated / {counts['live']:,} live cases — the AIO dashboard "
            f"definition |")
        add(f"| ...automated | {counts['automated']:,} | |")
        add(f"| ...to be automated | {counts['to be automated']:,} | |")
        add(f"| ...manual | {counts['manual']:,} | |")
        add(f"| ...in progress | {counts['in progress']:,} | |")
        add(f"| ...not assigned | {unset:,} | |")
        add(f"| Of triaged cases only | "
            f"{fmt_pct(inventory['coverage_pct_classified'], known)} "
            f"| automated / {known:,} that are Automated or To Be Automated |")
        add("")
        priorities = inventory.get("by_priority") or []
        if len(priorities) > 1:
            add("| Priority | Cases | Automated | To be automated | Not assigned "
                "| Coverage |")
            add("|---|---:|---:|---:|---:|---:|")
            for row in priorities:
                bucket = row["counts"]
                add(f"| {row['label']} | {bucket['live']:,} "
                    f"| {bucket['automated']:,} | {bucket['to be automated']:,} "
                    f"| {bucket['unset']:,} "
                    f"| {fmt_pct(row['coverage_pct'], row['live'])} |")
            add("")
            add("> AIO has no P1/P2/P3 field — it runs High / Medium / Low. The "
                "mapping shown is this project's agreed reading, not something "
                "AIO asserts.")
            add("")
        add("> Coverage is a **stock, not a flow**: it is the state of the test "
            "estate at a moment, so it is reported as a position rather than "
            "bucketed into this week. Bucketing it would answer \"of the cases "
            "somebody edited last week, how many are automated\" — a number that "
            "reads exactly like estate coverage and is not.")
        if unset > known:
            add("")
            add(f"> ⚠️ **Treat the percentage as provisional.** {unset:,} cases "
                f"have no automation status at all, against {known:,} that do. "
                f"An unset field is not \"not automated\". Fix it by setting the "
                f"field, not by changing the metric.")
        add("")

    if current.tests_by_category:
        executed = current.tests_executed
        add("| Metric | Value | Basis |")
        add("|---|---:|---|")
        add(f"| Test runs executed | {executed:,} | passed + failed + blocked + skipped |")
        for category in TEST_EXECUTED:
            count = current.tests_by_category[category]
            add(f"| ...{category} | {count:,} | |")
        add(f"| **Pass rate** | {fmt_pct(current.test_pass_rate(min_group), executed)} "
            f"| of {executed:,} executed |")
        automated = ratio(current.tests_automated, executed, min_group)
        add(f"| Automated share | {fmt_pct(automated, executed)} | AIO's own flag |")
        add(f"| Defects raised from runs | {current.test_defects:,} | |")
        add(f"| Cycles touched | {len(current.test_cycles):,} | |")
        add(f"| People executing tests | {len(current.test_executors):,} | |")
        not_run = current.tests_by_category.get("not_run", 0)
        if not_run:
            add(f"| Rows never executed | {not_run:,} | excluded from every rate above |")
        add("")
        add("> Blocked and skipped stay **in** the pass-rate denominator — a blocked "
            "test is one that could not be run, and dropping it flatters the number. "
            "Rows AIO seeded at \"Not Run\" stay **out** of it: they are cycle "
            "planning, not test activity.")
    else:
        add("_No `test.run.completed` events in this window._  ")
        add("This section needs `poll_aio.py` and an `AIO_API_TOKEN`. AIO issues "
            "its own key and rejects the Jira token, and the app is enabled per "
            "Jira project — run `poll_aio.py --project <KEY> --probe` to tell a "
            "bad token from a project the app is not enabled for.")
    add("")

    # -- 6. Cost ----------------------------------------------------------
    add("## 6. Cost")
    add("")
    if current.total_tokens or current.premium_requests:
        add("| Metric | Value | Basis |")
        add("|---|---:|---|")
        add(f"| **Premium requests** | "
            f"{f'{current.premium_requests:,}' if current.premium_requests is not None else '—'} "
            f"| measured — the unit Copilot bills; `—` = this source does not carry it |")
        add(f"| Sessions with usage | {len(current.sessions_with_usage):,} of "
            f"{len(current.sessions):,} | the rest ended without a `session.shutdown` |")
        add(f"| API requests | {current.api_requests:,} "
            f"| `requests.count` — **not** the billed unit |")
        add(f"| Input tokens | {current.input_tokens:,} "
            f"| economic weight, not spend |")
        add(f"| ...of which cache reads | "
            f"{f'{current.cached_tokens:,}' if current.cached_tokens is not None else '—'} "
            f"({fmt_num(current.cached_share, '%', 1)}) | higher is better |")
        add(f"| ...of which cache writes | "
            f"{f'{current.cache_write_tokens:,}' if current.cache_write_tokens is not None else '—'} "
            f"| |")
        add(f"| Output tokens | {current.output_tokens:,} | |")
        add(f"| Reasoning tokens | "
            f"{f'{current.reasoning_tokens:,}' if current.reasoning_tokens is not None else '—'} "
            f"| NULL where the model reports none |")
        add(f"| Total tokens | {current.total_tokens:,} | input + output |")
        add(f"| Token cost (modelled) | "
            f"{('$%.2f' % current.cost_usd) if current.cost_usd else '—'} "
            f"| derived in the warehouse; absent from this stream |")
        ppa = current.premium_per_accepted()
        add(f"| **Premium requests per accepted output** | "
            f"{fmt_num(ppa, digits=2) if ppa is not None else 'n=%d' % current.accepted} "
            f"| both terms measured |")
        add("| Seat cost | excluded | a contract term, not telemetry |")
        add("")
        if current.cost_basis:
            basis = ", ".join(f"{k} ({v})" for k, v in current.cost_basis.most_common())
            add(f"**Cost basis:** {basis}")
            if "modelled" in current.cost_basis:
                add("")
                add("> ⚠️ Some cost is **modelled**, not measured (±40%). Usable for "
                    "relative comparison and outlier spotting; not for chargeback.")
            add("")
        # The old note here ended "a per-token figure is notional", which read as
        # an apology for the best available number. It is no longer the best
        # available number: `premium_requests` is the billed unit, measured.
        add("> **Premium requests are not API requests.** Measured 2026-08-26: a "
            "107-call session cost 2, an 11-call session cost 2, a 12-call session "
            "cost 1. Both counts are shown and neither is divided by the other — "
            "the ratio between them is Copilot's pricing policy, not a fact about "
            "how anyone worked.")
        add("")
        add("> **Seat cost is excluded and cannot be included.** It is a contract "
            "term and is not visible from any client (CONTRACT §4.1), so no total "
            "on this page is spend. The token line is an economic weight — right "
            "for comparing models and configurations against each other, wrong to "
            "hand a finance team.")
        add("")
        add("> Cache reads are the live cost lever here: measured 92–98% of input "
            "tokens. `input_tokens = cache reads + cache writes + fresh`, exactly, "
            "so the share is a share of the row above it and `total tokens` does "
            "not double-count.")
        add("")
        # Context composition. Deliberately its own table, below the usage rows
        # and visually separated from them: a token count in a cost section
        # reads as spend, and these are not spend and not even a weekly total.
        measured = len(current.context_total)
        if measured or current.context_unknown:
            add("**Context composition** — a **level per session**, not a total "
                "for the week")
            add("")
            add("| Level | Median per session | Range | Sessions |")
            add("|---|---:|---:|---:|")
            for name, values, note in (
                ("Tool definitions", current.context_tool_defs, ""),
                ("System prompt", current.context_system, ""),
                ("Conversation", current.context_conversation, ""),
                ("**Context carried**", current.context_total, ""),
            ):
                add(f"| {name}{note} | {fmt_tokens(median(values))} "
                    f"| {fmt_range(values)} | {len(values) or '—'} |")
            add(f"| ...tool definitions' share | "
                f"{fmt_pct(current.tool_definition_share(), measured)} | | "
                f"{measured or '—'} |")
            if current.context_unknown:
                add(f"| Sessions with no context recorded | — | | "
                    f"{current.context_unknown} |")
            add("")
            add("> **A level, not a total.** This is what the model was carrying "
                "at the end of a session and paid for on **every request in it** "
                "— not tokens consumed this week. Summing it across sessions "
                "would count the same fixed overhead once per session, and "
                "multiplying it by `request_count` would be modelling, not "
                "measurement (CONTRACT §3 row 5). So the figure is a median per "
                "session with the observed range beside it, and there is no "
                "weekly total anywhere on this page.")
            add("")
            add("> **Tool definitions are what connecting an MCP server costs.** "
                "They are the fixed price of the tool set — paid on every "
                "request, before anyone has typed anything — and they step when "
                "a server is added or removed, not when the work changes. "
                "Measured 14,318–34,219 across only **7 distinct values** in 22 "
                "sessions, which is what a step function looks like. This is the "
                "only measure in the contract that prices that decision; the "
                "other two rows are here so the composition adds up rather than "
                "being one number without a whole.")
            add("")
            if current.context_unknown:
                add("> A session that used **more than one model** reports `—` "
                    "here, not a share. The journal records one context, not one "
                    "per model, and splitting it between them would be invention "
                    "— the same rule that keeps `latency_ms` off multi-model "
                    "sessions.")
                add("")
    else:
        add("_No `model.call` events in this window._  ")
        add("Usage now comes from Copilot CLI's own session journal at "
            "`~/.copilot/session-state/<id>/events.jsonl`, read by "
            "`cli/copilot_read.py`. An empty section means no session in this "
            "window reached a `session.shutdown` record — the journal writes "
            "usage totals once, at shutdown — or that this surface is not the one "
            "people used. The VS Code Copilot Chat surface is not covered by this "
            "reader at all.")
    add("")

    # -- 6. Reliability ---------------------------------------------------
    add("## 7. Reliability")
    add("")
    total_terminal = sum(current.runs_by_status.values())
    if total_terminal:
        add("**Runs**")
        add("")
        add("| Metric | Value | Basis |")
        add("|---|---:|---|")
        add(f"| Run success rate | "
            f"{fmt_pct(current.run_success_rate(), total_terminal)} "
            f"| of {total_terminal} runs that reached a terminal event |")
        for status in ("completed", "failed", "timeout", "abandoned"):
            add(f"| ...{status} | {current.runs_by_status.get(status, 0)} | |")
        add(f"| Runs with no terminal event | "
            f"{current.runs_without_terminal or '—'} "
            f"| still open, or the session was killed |")
        add(f"| Aborts | {current.human_turns.get('correction', 0) or '—'} "
            f"| a person stopped a run mid-flight |")
        add("")
        add("> A run with **no terminal event** is not a failed run and is not in "
            "the success-rate denominator. The session may still be open — the "
            "journal is read while it is being written — or the process was killed "
            "before it could record an ending.")
        add("")
        if current.dependency_failures:
            add("**Dependency failures**")
            add("")
            add("| Dependency | Count |")
            add("|---|---:|")
            for dep, count in current.dependency_failures.most_common():
                add(f"| {dep} | {count} |")
            add("")
    else:
        add("_No terminal run events in this window._")
        add("")

    # Deliberately outside the terminal-run guard above. Tool activity is
    # recorded per call and exists whether or not any run recorded an ending;
    # nesting it under `if total_terminal` hid a measured 2,062 tool calls
    # behind the absence of a different event type.
    if current.tool_calls:
        add("**Tool calls**")
        add("")
        add("| Metric | Value | Basis |")
        add("|---|---:|---|")
        add(f"| Tool calls | {current.tool_calls:,} | |")
        add(f"| ...ok | {current.tool_ok:,} | |")
        add(f"| ...error | {current.tool_error:,} | |")
        add(f"| ...no status recorded | {current.tool_status_unknown or '—'} "
            f"| excluded from the rate below |")
        add(f"| **Tool error rate** | "
            f"{fmt_pct(current.tool_error_rate(), current.tool_status_known)} "
            f"| error / (ok + error) |")
        add(f"| Model retries | "
            f"{current.retries if current.retries is not None else '—'} "
            f"| `retry_count` is not recorded by this source |")
        add(f"| Tool call duration | — "
            f"| `duration_ms` is not recorded by this source |")
        add("")
        add("> **`—` is not `0`.** `retry_count` and per-call durations are "
            "permanently NULL on the session journal (CONTRACT §3 rows 5 and 6): "
            "the journal does not record retries at all, and the gap between its "
            "two timestamps for a call includes waiting for a human to approve it, "
            "which is not the tool's duration.")
        add("")
        if current.tool_error_classes:
            add("**Tool failures by class**")
            add("")
            add("| Error class | Count | Share of errors |")
            add("|---|---:|---:|")
            for name, count in current.tool_error_classes.most_common():
                add(f"| {name} | {count} | "
                    f"{fmt_pct(ratio(count, current.tool_error, min_group), current.tool_error)} |")
            add("")
            add("> `error_class` is a bounded set derived from the error message, "
                "which is then discarded. The message itself is content and never "
                "leaves the machine (CONTRACT §1 rule 1).")
            add("")
        if current.tool_kinds:
            add("**Tool mix**")
            add("")
            add("| Kind | Calls | Share |")
            add("|---|---:|---:|")
            for name, count in current.tool_kinds.most_common():
                add(f"| {name} | {count:,} | "
                    f"{fmt_pct(ratio(count, current.tool_calls, min_group), current.tool_calls)} |")
            add("")
            add("> Broken out by `tool_kind`, a closed five-value enum — never by "
                "`tool_name`, which is unbounded across MCP servers and would "
                "breach the <100-distinct-values rule for a dimension (CONTRACT §1 "
                "rule 5).")
            add("")

    if current.ci_by_status:
        add("**CI outcomes**")
        add("")
        add("| Status | Count |")
        add("|---|---:|")
        for status, count in current.ci_by_status.most_common():
            add(f"| {status} | {count} |")
        if current.ci_by_kind:
            kinds = ", ".join(f"{k} ({v})" for k, v in current.ci_by_kind.most_common())
            add("")
            add(f"**By kind:** {kinds}")
        add("")

    # -- 7. Human involvement ---------------------------------------------
    add("## 8. Human involvement")
    add("")
    if current.human_turns:
        add("| Turn kind | Count | Basis |")
        add("|---|---:|---|")
        for kind, count in current.human_turns.most_common():
            note = ""
            if kind == "unknown":
                note = ("a typed message; the journal cannot classify it without "
                        "reading the text")
            elif kind == "approval":
                note = "a permission the agent asked for and a person granted"
            elif kind == "correction":
                note = "a run stopped mid-flight"
            add(f"| {kind} | {count} | {note} |")
        add("")
        # A count, deliberately, and never a rate — see the note below.
        add(f"**Approvals granted** {current.human_turns.get('approval', 0)}")
        add("")
        add(f"**Manual intervention rate** "
            f"{fmt_pct(current.manual_intervention_rate(), len(current.runs_seen))}")
        add("")
        add("> Counts `correction` and `rejection` only. `approval` and `clarification` "
            "are designed gates — the agents are built to ask — and counting them would "
            "penalise correct behaviour.")
        add("")
        add("> **Approvals are a count, not a rate.** There is no denominator: a "
            "denial produces no `permission.completed` record at all — it surfaces "
            "later as a tool error — so an approval rate computed from this stream "
            "would be 100% every week by construction. The time-to-approve is "
            "refused for the same kind of reason: the request record is written "
            "retroactively at completion, so every measured delta is 0.00s.")
        add("")
        add("> **This section cannot see the largest intervention.** A person who "
            "reads the agent's output and silently rewrites it leaves no turn in "
            "the journal. What is measured here is interruption, not correction; "
            "the metric that sees quiet rewriting is `post_review_change_ratio` in "
            "§2, computed against review rather than against the session.")
        add("")
        add("> Read a high rate as a signal about **agent quality or task fit**, not "
            "about the person. The same number means opposite things for a new joiner "
            "and a six-month user.")
    else:
        add("_No `human.turn` events in this window._")
    add("")

    # -- 8. Trend ---------------------------------------------------------
    if trail:
        add("## 9. Trend")
        add("")
        header = "| Metric | " + " | ".join(trail + [label]) + " |"
        add(header)
        add("|---" * (len(trail) + 2) + "|")

        # A step change in these rows may be a change of telemetry source rather
        # than a change of behaviour — the 1.1.0 move from Copilot Chat's OTel
        # span exporter to Copilot CLI's session journal changed what `Runs`
        # counts, made tool status a real verdict where it had been NULL, and
        # made gate verdicts exist at all. The mirror of "a dash is not a zero"
        # is "a jump is not necessarily behaviour".
        here = current.source_fingerprint
        marked = False

        def row(name: str, getter) -> None:
            nonlocal marked
            sensitive = name in SOURCE_SENSITIVE_ROWS
            cells = []
            for week_label in list(trail) + [label]:
                week = weeks.get(week_label)
                if not week:
                    cells.append("—")
                    continue
                cell = getter(week)
                other = week.source_fingerprint
                if sensitive and here and other and other != here:
                    cell += " ‡"
                    marked = True
                cells.append(cell)
            add(f"| {name} | " + " | ".join(cells) + " |")

        row("Runs", lambda w: str(w.runs))
        row("Accepted outputs", lambda w: str(w.accepted))
        row("Acceptance rate", lambda w: fmt_pct(w.acceptance_rate(), w.judged_outputs))
        row("PRs merged", lambda w: str(w.prs_merged))
        row("Automation scripts created", lambda w: str(w.scripts_added)
            if w.scripts_added else "—")
        row("PR decline rate", lambda w: fmt_pct(w.pr_decline_rate(),
                                                 w.prs_merged + w.prs_declined))
        row("Merge lead (median)", lambda w: fmt_hours(median(w.merge_lead_ms)))
        row("Tests executed", lambda w: f"{w.tests_executed:,}"
            if w.tests_executed else "—")
        row("Test pass rate", lambda w: fmt_pct(w.test_pass_rate(min_group),
                                                w.tests_executed))
        row("Defects from test runs", lambda w: str(w.test_defects)
            if w.test_defects else "—")
        row("Total tokens", lambda w: f"{w.total_tokens:,}" if w.total_tokens else "—")
        row("Premium requests", lambda w: f"{w.premium_requests:,}"
            if w.premium_requests else "—")
        row("Cache read %", lambda w: fmt_num(w.cached_share, "%", 0))
        row("Tool error rate", lambda w: fmt_pct(w.tool_error_rate(),
                                                 w.tool_status_known))
        row("Gate pass rate", lambda w: fmt_pct(
            w.gate_pass_rate(), w.gate_counts("pass") + w.gate_counts("fail")))
        add("")
        add("> Four weeks is not a trend. Read the slope over 8+ weeks; treat "
            "week-on-week movement as noise until then. A dash means the source "
            "produced nothing that week — it is not a zero.")
        add("")
        if marked:
            add("> ‡ **A different telemetry source produced this cell.** The "
                "mirror of the rule above: a step change may be a source change, "
                "not a behaviour change. Runs, tool errors, gates and tokens all "
                "mean different things either side of the 1.1.0 move from Copilot "
                "Chat's OTel span exporter to Copilot CLI's own session journal — "
                "tool `status` was structurally NULL before it, so the old tool "
                "error rate was 0% by construction, and gate verdicts did not "
                "exist. Do not read a slope across a ‡.")
            add("")
        add("**Sources present**")
        add("")
        add("| Week | Surface | Schema |")
        add("|---|---|---|")
        for week_label in list(trail) + [label]:
            week = weeks.get(week_label)
            if not week or not week.client_surfaces:
                add(f"| {week_label} | — | — |")
                continue
            add(f"| {week_label} "
                f"| {', '.join(sorted(week.client_surfaces))} "
                f"| {', '.join(sorted(week.client_schemas))} |")
        add("")

    # -- 10. Data quality -------------------------------------------------
    add("## 10. Data quality")
    add("")
    add("| Check | Value |")
    add("|---|---:|")
    add(f"| Files read | {stats['files']} |")
    add(f"| Lines read | {stats['lines']:,} |")
    add(f"| Malformed lines | {stats['malformed']} |")
    add(f"| Events without a timestamp | {stats['no_timestamp']} |")
    add("")
    add("**Link method distribution**")
    add("")
    add("| Method | Count | Admissible to cost metrics |")
    add("|---|---:|---|")
    for method, count in current.link_methods.most_common():
        add(f"| {method} | {count} | {'yes' if method == 'explicit' else 'no'} |")
    add("")
    add("**Event types present**")
    add("")
    add("| Event type | Count |")
    add("|---|---:|")
    for kind, count in current.event_types.most_common():
        add(f"| `{kind}` | {count} |")
    add("")

    # The audit is unconditional. A reader who sees an em dash needs to know
    # whether it means "nothing happened" or "nothing was recorded", and the
    # second case is a property of the source that does not vary by week.
    add("**NULL vs zero — fields rendered `—` because the source does not carry them**")
    add("")
    add("| Field | Why it is NULL, not 0 |")
    add("|---|---|")
    for field, reason in NULL_BY_SOURCE:
        add(f"| {field} | {reason} |")
    add("")
    add("> None of these is a measurement of zero. Every one of them was rendered "
        "as a zero at some point in this report's history, and each of those "
        "zeros travelled into a dashboard where the caveat did not.")
    add("")
    if (current.tool_status_unknown or current.gate_counts("unknown")
            or current.context_unknown):
        add("| Partial coverage | Count | Effect |")
        add("|---|---:|---|")
        add(f"| Tool calls with no status | {current.tool_status_unknown} "
            f"| excluded from the tool error rate |")
        add(f"| Gate evaluations with no verdict | {current.gate_counts('unknown')} "
            f"| excluded from the gate pass rate |")
        add(f"| Sessions with no context recorded | {current.context_unknown} "
            f"| multi-model; excluded from the §6 context levels |")
        add("")

    missing = [
        name for name, present in (
            ("`model.call` (tokens, premium requests)", bool(current.total_tokens)),
            ("`output.generated` (acceptance)", bool(current.outputs_by_state)),
            ("`human.turn` (intervention)", bool(current.human_turns)),
            ("`ci.pipeline.completed` (CI)", bool(current.ci_by_status)),
            ("`gate.evaluated` (quality gates)", bool(current.gates)),
        ) if not present
    ]
    if missing:
        add("> ⚠️ **Absent from this window:** " + ", ".join(missing) + ".  ")
        add("> The corresponding sections are empty rather than zero. An absent "
            "measurement is not a measurement of zero.")
        add("")

    add("---")
    add("")
    add("*No ROI, no monetary value-delivered, and no AI-vs-human comparison appear in "
        "this report. AI is applied to essentially all work, so no control group exists "
        "and no attribution is possible.*")
    add("")
    return "\n".join(out)


def render_html(markdown_body: str, label: str) -> str:
    """Minimal self-contained HTML. No external assets, light and dark aware."""
    import html as html_mod

    rows: List[str] = []
    in_table = False
    for line in markdown_body.splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells) and cells:
                continue
            tag = "td" if in_table else "th"
            if not in_table:
                rows.append("<table>")
                in_table = True
            rows.append(
                "<tr>" + "".join(f"<{tag}>{html_mod.escape(c)}</{tag}>" for c in cells) + "</tr>"
            )
            continue
        if in_table:
            rows.append("</table>")
            in_table = False
        if stripped.startswith("# "):
            rows.append(f"<h1>{html_mod.escape(stripped[2:])}</h1>")
        elif stripped.startswith("## "):
            rows.append(f"<h2>{html_mod.escape(stripped[3:])}</h2>")
        elif stripped.startswith("> "):
            rows.append(f"<blockquote>{html_mod.escape(stripped[2:])}</blockquote>")
        elif stripped in ("---", ""):
            rows.append("<hr>" if stripped == "---" else "")
        else:
            rows.append(f"<p>{html_mod.escape(stripped)}</p>")
    if in_table:
        rows.append("</table>")

    css = """
:root{--bg:#ffffff;--fg:#1a1a1a;--muted:#5b6470;--line:#e2e6ea;--accent:#2563eb;--quote:#f6f8fa}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#14171a;--fg:#e8eaed;--muted:#9aa4b2;--line:#2a2f36;--accent:#7aa2f7;--quote:#1c2027}}
*{box-sizing:border-box}
body{margin:0;padding:2rem 1.25rem;background:var(--bg);color:var(--fg);
 font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:900px;margin:0 auto}
h1{font-size:1.6rem;margin:0 0 .5rem;border-bottom:2px solid var(--accent);padding-bottom:.4rem}
h2{font-size:1.15rem;margin:2rem 0 .6rem;color:var(--accent)}
table{border-collapse:collapse;width:100%;margin:.6rem 0;font-size:.9rem;display:block;overflow-x:auto}
th,td{border:1px solid var(--line);padding:.4rem .6rem;text-align:left}
th{background:var(--quote);font-weight:600}
td:not(:first-child),th:not(:first-child){text-align:right}
blockquote{margin:.6rem 0;padding:.5rem .9rem;background:var(--quote);
 border-left:3px solid var(--accent);color:var(--muted);font-size:.88rem}
hr{border:0;border-top:1px solid var(--line);margin:1.5rem 0}
p{margin:.4rem 0}
code{background:var(--quote);padding:.1rem .3rem;border-radius:3px;font-size:.85em}
"""
    return (
        f"<!doctype html><html><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>AI Engineering — {html_mod.escape(label)}</title>"
        f"<style>{css}</style></head><body><main>{''.join(rows)}</main></body></html>"
    )


def render_json(label: str, week: Optional[WeekAggregate], stats: Dict[str, int]) -> str:
    if week is None:
        return json.dumps({"week": label, "error": "no events"}, indent=2)
    payload = {
        "week": label,
        "adoption": {
            "runs": week.runs,
            "runs_by_input_source": dict(week.runs_by_input_source),
            "sessions": len(week.sessions),
            "people": len(week.people),
            # Named `agents_by_name`, not `agents`: these are the `agent_name`
            # values the source reports, not configurations anyone chose
            # between, and a dashboard that ranks them is reading a breakdown as
            # a comparison.
            "agents_by_name": dict(week.runs_by_agent),
            "models": dict(week.runs_by_model),
            "skills": sorted(week.skills),
        },
        "acceptance": {
            "by_state": dict(week.outputs_by_state),
            "rate_pct": week.acceptance_rate(),
        },
        "speed": {
            "review_lead_ms_median": median(week.review_lead_ms),
            "review_lead_ms_p85": percentile(week.review_lead_ms, 0.85),
            "merge_lead_ms_median": median(week.merge_lead_ms),
            "merge_lead_ms_p85": percentile(week.merge_lead_ms, 0.85),
            "ci_duration_ms_median": median(week.ci_duration_ms),
            # Named for what it is. It is time inside model API calls, not
            # elapsed wall clock, and a key called `duration` next to the lead
            # times above would be read as the same clock.
            "model_api_ms_median": median(week.model_latency_ms),
            "model_api_ms_p85": percentile(week.model_latency_ms, 0.85),
            "model_api_n": len(week.model_latency_ms),
        },
        "quality": {
            "prs_merged": week.prs_merged,
            "prs_declined": week.prs_declined,
            "decline_rate_pct": week.pr_decline_rate(),
            "reverts": week.prs_reverted,
            "comments_per_100_lines": week.comments_per_100_lines(),
            "gates": {
                gate: {
                    "pass": counter.get("pass", 0),
                    "fail": counter.get("fail", 0),
                    # Unknown is its own key and is in neither term of the rate.
                    "unknown": counter.get("unknown", 0) + counter.get("skipped", 0),
                    "pass_rate_pct": week.gate_pass_rate(gate),
                    "first_pass_attempt_median": median(
                        week.gate_first_pass.get(gate) or []),
                    "first_pass_n": len(week.gate_first_pass.get(gate) or []),
                }
                for gate, counter in sorted(week.gates.items())
            },
            "gate_pass_rate_pct": week.gate_pass_rate(),
        },
        "cost": {
            # The billed unit first, because it is the measured one.
            "premium_requests": week.premium_requests,
            "api_request_count": week.api_requests,
            "sessions_with_usage": len(week.sessions_with_usage),
            "sessions": len(week.sessions),
            "input_tokens": week.input_tokens,
            "output_tokens": week.output_tokens,
            "cached_input_tokens": week.cached_tokens,
            "cache_write_tokens": week.cache_write_tokens,
            "cached_share_pct": week.cached_share,
            "reasoning_tokens": week.reasoning_tokens,
            "nano_aiu": week.nano_aiu,
            # null, never 0. CONTRACT §4 keeps cost out of the client stream, so
            # nothing here can supply it; a 0.0 serialised into a dashboard is a
            # false measurement travelling without its disclaimer.
            "cost_usd": week.cost_usd,
            "cost_basis": dict(week.cost_basis),
            "cost_per_accepted_output": week.cost_per_accepted(),
            "premium_requests_per_accepted_output": week.premium_per_accepted(),
            "seat_cost_usd": None,
            "seat_cost_note": "excluded — a contract term, not telemetry (CONTRACT §4.1)",
            # A level per session, never a weekly total. Every key here is
            # named `_median` / `_min` / `_max` for that reason: there is no
            # `_total`, so a dashboard cannot pick one up by accident and no
            # summing is possible without deliberately writing the loop.
            "context": {
                "tool_definitions_tokens_median": median(week.context_tool_defs),
                "tool_definitions_tokens_min": (min(week.context_tool_defs)
                                                if week.context_tool_defs else None),
                "tool_definitions_tokens_max": (max(week.context_tool_defs)
                                                if week.context_tool_defs else None),
                "system_tokens_median": median(week.context_system),
                "conversation_tokens_median": median(week.context_conversation),
                "context_tokens_median": median(week.context_total),
                "tool_definitions_share_pct": week.tool_definition_share(),
                "sessions_measured": len(week.context_total),
                # null, never 0: a multi-model session records one context and
                # it belongs to no single model.
                "sessions_context_null_multi_model": week.context_unknown,
                "basis": "a level per session, not a total for the week — what "
                         "the model carried and paid for on every request in "
                         "the session; never multiply by request_count",
            },
        },
        "reliability": {
            "runs_by_status": dict(week.runs_by_status),
            "success_rate_pct": week.run_success_rate(),
            "runs_without_terminal_event": week.runs_without_terminal,
            "aborts": week.human_turns.get("correction", 0),
            "tool_calls": week.tool_calls,
            "tool_ok": week.tool_ok,
            "tool_error": week.tool_error,
            "tool_status_unknown": week.tool_status_unknown,
            "tool_error_rate_pct": week.tool_error_rate(),
            "tool_error_classes": dict(week.tool_error_classes),
            "tool_kinds": dict(week.tool_kinds),
            # null, never 0: `retry_count` is not recorded by this source.
            "retries": week.retries,
            "tool_duration_ms_median": None,
            "dependency_failures": dict(week.dependency_failures),
            "ci_by_status": dict(week.ci_by_status),
        },
        "human": {
            "turns": dict(week.human_turns),
            "approvals_granted": week.human_turns.get("approval", 0),
            "manual_intervention_rate_pct": week.manual_intervention_rate(),
            "not_visible": "a person silently rewriting the agent's output "
                           "afterwards leaves no turn in the journal",
        },
        "data_quality": {
            **stats,
            "link_methods": dict(week.link_methods),
            "event_types": dict(week.event_types),
            "explicit_completeness_pct": week.completeness(),
            "client_surfaces": dict(week.client_surfaces),
            "client_schema_versions": dict(week.client_schemas),
            # The disclaimer travels with the payload, because the markdown one
            # does not travel into a dashboard.
            # Backticks are markdown; they have no business in a JSON key.
            "null_not_zero": {field.replace("`", ""): reason
                              for field, reason in NULL_BY_SOURCE},
        },
        "excluded_by_design": [
            "roi",
            "monetary_value_delivered",
            "counterfactual_time_saved",
            "ai_vs_human_comparison",
            "time_to_approve_a_permission",
            "permission_denial_rate",
            "per_run_cost_from_output_tokens",
            "tokens_per_agent_or_subagent",
            "repeated_edit_as_rework",
        ],
    }
    return json.dumps(payload, indent=2, default=str)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def resolve_week(requested: Optional[str], weeks: Dict[str, WeekAggregate]) -> Optional[str]:
    if requested:
        if len(requested) == 10 and requested[4] == "-":
            moment = parse_ts(requested + "T00:00:00Z")
            if not moment:
                _fail(f"cannot parse --week {requested!r}")
            return iso_week(moment)
        return requested
    if not weeks:
        return None
    # Most recent COMPLETE week: exclude the one currently in progress, because a
    # partial week always looks like a decline.
    this_week = iso_week(datetime.now(timezone.utc))
    complete = sorted(w for w in weeks if w != this_week)
    return complete[-1] if complete else sorted(weeks)[-1]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a weekly AI-engineering report from the NDJSON event stream.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--input", nargs="*", default=[], metavar="PATH",
                        help="NDJSON files or directories (default: stdin)")
    parser.add_argument("--week", help="ISO week (YYYY-Www) or a date (YYYY-MM-DD)")
    parser.add_argument("--weeks", type=int, default=4, metavar="N",
                        help="trailing weeks of trend context (default 4)")
    parser.add_argument("--format", choices=("md", "html", "json"), default="md")
    parser.add_argument("--out", help="output file (default: stdout)")
    parser.add_argument("--scope-note", metavar="TEXT",
                        help="One line naming what this report covers, e.g. "
                             "'Release 26.8 - 8 cycles'. Printed under the "
                             "title. Use it whenever the input is a subset, so "
                             "a scoped report is never mistaken for the whole "
                             "team's.")
    parser.add_argument("--min-group", type=int, default=5, metavar="N",
                        help="suppress percentages below this denominator (default 5)")
    args = parser.parse_args(argv)

    events, stats = load_events(args.input)
    weeks = aggregate(events, args.min_group)
    label = resolve_week(args.week, weeks)
    if label is None:
        _fail("no events with a usable event_time were found")

    trail = [w for w in previous_weeks(label, args.weeks) if w in weeks]

    if args.format == "json":
        body = render_json(label, weeks.get(label), stats)
    else:
        markdown = render_markdown(label, weeks, trail, stats, args.min_group,
                                   inventory_coverage(events), args.scope_note)
        body = markdown if args.format == "md" else render_html(markdown, label)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(body)
        print(json.dumps({"ok": True, "week": label, "out": args.out,
                          "events": len(events)}, indent=2))
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
