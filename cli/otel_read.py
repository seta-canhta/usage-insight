#!/usr/bin/env python3
"""Turn captured Copilot OTel spans into contract events.

    python3 cli/otel_read.py --raw ~/.seta-insight/otel-raw.ndjson

Reads what `otel_capture.py` recorded and emits `model.call` and `tool.call`
events per `CONTRACT.md` §3, so the weekly pipeline can read them like any
other source.

**Measured shape, 2026-08-24** -- exporter `OTel-OTLP-Exporter-JavaScript/0.219.0`,
`Transfer-Encoding: chunked`, JSON bodies to `/v1/traces`, `/v1/logs` and
`/v1/metrics`. Spans are named by operation and subject:

    invoke_agent GitHub Copilot Chat
    chat gpt-5.3-codex
    execute_tool run_in_terminal

## Content is dropped here, deliberately and by allow-list

The stream carries the prompts, **and setting `captureContent: false` does not
stop it**. That is a known, open VS Code defect -- microsoft/vscode#326254,
"the log and metric paths honor the setting; the span path does not" -- filed
against copilot-chat 0.57.0 and still reproducing here on 0.62.0.

Measured on a real run: `gen_ai.input.messages` (11,203 chars),
`copilot_chat.user_request` (8,440), `gen_ai.system_instructions` (6,009),
`gen_ai.output.messages` (3,724) and `gen_ai.tool.call.result` (824 -- real
terminal output) all arrived in full.

So this module is the mitigation, not a second line of defence behind a setting
that works. The upstream issue recommends disabling span capture entirely in
sensitive environments; that also discards the cache-token detail which only
spans carry, so instead the content is dropped here, before anything is stored.

So this reader **names the fields it keeps** rather than excluding the ones it
knows about. An exclusion list is only as good as today's knowledge of the
exporter, and the exporter ships new attributes without asking. Anything not
named below never reaches an event, whatever it is called.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (os.path.join(_ROOT, "pollers"), os.path.join(_ROOT, "collector")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import common  # noqa: E402
import main as collector_main  # noqa: E402

#: The only attributes that ever leave this module. Everything else is dropped
#: without inspection -- see the module docstring for why this is a keep-list.
KEEP = {
    "gen_ai.request.model", "gen_ai.response.model", "gen_ai.operation.name",
    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
    "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.reasoning.output_tokens", "gen_ai.usage.reasoning_tokens",
    "gen_ai.response.finish_reasons", "gen_ai.response.time_to_first_chunk",
    "gen_ai.conversation.id", "gen_ai.agent.name",
    "gen_ai.tool.name", "gen_ai.tool.type",
    "copilot_chat.mode_name", "copilot_chat.session_id",
    "copilot_chat.chat_session_id", "copilot_chat.time_to_first_token",
    "copilot_chat.copilot_usage_nano_aiu",
    "copilot_chat.repo.head_branch_name",
    "github.copilot.agent.type", "github.copilot.git.branch",
}

#: Read to classify, then discarded -- the same rule the commit-subject parser
#: follows. The command itself is content and never leaves this module; only
#: which gate it represents does.
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

#: A gate needs a verdict, and the span carries none: the tool result is content
#: and stays out. So status is NULL -- "it ran" is what we know, and claiming
#: pass would turn an unknown into a green tick.
def classify_gate(command):
    if not command:
        return None
    for name, pattern in GATE_PATTERNS:
        if pattern.search(command):
            return name
    return None


TOOL_KINDS = {
    "run_in_terminal": "terminal", "runCommands": "terminal",
    "read_file": "file", "create_file": "file", "replace_string_in_file": "file",
    "apply_patch": "file", "insert_edit_into_file": "file",
    "grep_search": "file", "file_search": "file", "list_dir": "file",
    "fetch_webpage": "http", "github_repo": "http",
}


def scalar(value: Dict[str, Any]) -> Any:
    """Unwrap one OTLP AnyValue. Nested values are refused, not flattened.

    An arrayValue or kvlistValue is where free text hides -- the message lists
    arrive in exactly that shape. Returning None keeps them out even if such a
    key is ever added to KEEP by mistake.
    """
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        try:
            return int(value["intValue"])
        except (TypeError, ValueError):
            return None
    if "doubleValue" in value:
        return value["doubleValue"]
    if "boolValue" in value:
        return value["boolValue"]
    return None


def raw_attribute(span: Dict[str, Any], key: str) -> Optional[str]:
    """Read one attribute for classification. The caller must not store it.

    Used only for the terminal command, which is content: it decides which gate
    a span represents and is then dropped, exactly as commit subjects are.
    """
    for attribute in span.get("attributes") or []:
        if attribute.get("key") == key:
            value = attribute.get("value") or {}
            return value.get("stringValue")
    return None


def attributes_of(span: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for attribute in span.get("attributes") or []:
        key = attribute.get("key")
        if key in KEEP:
            got = scalar(attribute.get("value") or {})
            if got is not None:
                out[key] = got
    return out


def iter_spans(raw_path: str) -> Iterable[Dict[str, Any]]:
    """Yield spans from either shape this file can arrive in.

    The file exporter (`otel.exporterType: file`, `otel.outfile`) writes each
    signal as one JSON line -- no HTTP, nothing listening, no daemon. The
    capture tool wraps the same payload in a record carrying the request
    headers. Both are read here so switching between them is a settings change
    rather than a code change.
    """
    with open(raw_path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if "encoding" in record:            # from otel_capture.py
                if record.get("encoding") != "json":
                    continue
                payload = record.get("payload") or {}
            else:                                # straight from the exporter
                payload = record
            for resource in payload.get("resourceSpans", []):
                for scope in resource.get("scopeSpans", []):
                    for span in scope.get("spans", []):
                        yield span


def nanos_to_ms(start: Any, end: Any) -> Optional[int]:
    try:
        return int((int(end) - int(start)) / 1_000_000)
    except (TypeError, ValueError):
        return None


def to_events(raw_path: str, salt: Optional[str] = None) -> Dict[str, Any]:
    events: List[Dict[str, Any]] = []
    agents: Dict[str, str] = {}
    conversations: Dict[str, Dict[str, Any]] = {}

    spans = list(iter_spans(raw_path))

    # Which platform agent is running comes from `copilot_chat.mode_name` on the
    # invoke_agent span, and applies to every call in that conversation. This
    # is the answer to "which agent is being used" -- and it arrives without
    # emit.py, which the design did not anticipate.
    for span in spans:
        attrs = attributes_of(span)
        conversation = attrs.get("gen_ai.conversation.id")
        mode = attrs.get("copilot_chat.mode_name")
        if conversation and mode:
            agents[conversation] = mode

    for span in spans:
        attrs = attributes_of(span)
        operation = attrs.get("gen_ai.operation.name")
        conversation = attrs.get("gen_ai.conversation.id")
        duration = nanos_to_ms(span.get("startTimeUnixNano"),
                               span.get("endTimeUnixNano"))
        # OTLP stamps nanoseconds since the epoch. Passing that to a helper
        # that expects seconds yields a date around the year 33000, which sorts
        # and formats perfectly well and is wrong.
        started = span.get("startTimeUnixNano")
        event_time = None
        if started:
            try:
                event_time = datetime.fromtimestamp(
                    int(started) / 1_000_000_000, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            except (TypeError, ValueError, OSError):
                event_time = None

        agent_name = agents.get(conversation) or attrs.get("gen_ai.agent.name")
        agent = common.make_agent(agent_name or "copilot.chat", surface="ide")

        if operation == "chat":
            model = attrs.get("gen_ai.response.model") or attrs.get("gen_ai.request.model")
            events.append(common.build_event(
                event_type="model.call",
                event_time=event_time,
                natural_key=(span.get("spanId"), model),
                attributes={
                    "model_id": model,
                    "input_tokens": attrs.get("gen_ai.usage.input_tokens"),
                    "output_tokens": attrs.get("gen_ai.usage.output_tokens"),
                    "cached_input_tokens": attrs.get("gen_ai.usage.cache_read.input_tokens"),
                    "reasoning_tokens": attrs.get("gen_ai.usage.reasoning.output_tokens")
                    or attrs.get("gen_ai.usage.reasoning_tokens"),
                    "latency_ms": duration,
                    "finish_reason": attrs.get("gen_ai.response.finish_reasons"),
                },
                agent=agent,
                link=common.make_link("heuristic", 0.8),
                trace_id=conversation,
            ))
            bucket = conversations.setdefault(conversation or "unknown", {
                "agent": agent_name, "models": {}, "input_tokens": 0,
                "output_tokens": 0, "cached_tokens": 0, "calls": 0, "tools": {},
                "nano_aiu": 0,
            })
            bucket["calls"] += 1
            bucket["input_tokens"] += attrs.get("gen_ai.usage.input_tokens") or 0
            bucket["output_tokens"] += attrs.get("gen_ai.usage.output_tokens") or 0
            bucket["cached_tokens"] += attrs.get("gen_ai.usage.cache_read.input_tokens") or 0
            bucket["nano_aiu"] += attrs.get("copilot_chat.copilot_usage_nano_aiu") or 0
            if model:
                bucket["models"][model] = bucket["models"].get(model, 0) + 1

        elif operation == "execute_tool":
            tool = attrs.get("gen_ai.tool.name")
            events.append(common.build_event(
                event_type="tool.call",
                event_time=event_time,
                natural_key=(span.get("spanId"), tool),
                attributes={
                    "tool_name": tool,
                    "tool_kind": TOOL_KINDS.get(tool, "other"),
                    "duration_ms": duration,
                    # The span carries no status field; claiming "ok" would be
                    # inventing one. NULL means unknown, which is the truth.
                    "status": None,
                    "error_class": None,
                },
                agent=agent,
                link=common.make_link("heuristic", 0.8),
                trace_id=conversation,
            ))
            if conversation in conversations and tool:
                tools = conversations[conversation]["tools"]
                tools[tool] = tools.get(tool, 0) + 1

            # A terminal command that runs a test, lint or build IS a gate
            # evaluation, whether or not the agent bothered to emit one. The
            # command is read here and discarded; only the gate name survives.
            if tool in ("run_in_terminal", "runCommands"):
                gate = classify_gate(
                    raw_attribute(span, "gen_ai.tool.call.arguments"))
                if gate:
                    gates = conversations.setdefault(
                        conversation or "unknown", {}).setdefault("gates", {})
                    gates[gate] = gates.get(gate, 0) + 1
                    events.append(common.build_event(
                        event_type="gate.evaluated",
                        event_time=event_time,
                        natural_key=(span.get("spanId"), gate),
                        attributes={
                            "gate_name": gate,
                            # NULL, not "pass". The verdict lives in the tool
                            # result, which is content and stays out.
                            "status": None,
                            "quality_score": None,
                            "coverage_pct": None,
                            "attempt_index": gates[gate] - 1,
                        },
                        agent=agent,
                        link=common.make_link("heuristic", 0.6),
                        trace_id=conversation,
                    ))

    return {"events": events, "conversations": conversations,
            "spans_read": len(spans)}


def verify_no_content(events: List[Dict[str, Any]]) -> List[str]:
    """Last line of defence: the collector's own allow-list, applied here.

    The keep-list above should make this impossible. It runs anyway, because
    the cost of being wrong is publishing somebody's prompts.
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
        description="Convert captured Copilot OTel spans into contract events.")
    parser.add_argument("--raw", default=os.path.join(
        os.path.expanduser("~"), ".seta-insight", "otel-raw.ndjson"))
    parser.add_argument("--out", help="write events here (default: stdout)")
    parser.add_argument("--summary", action="store_true",
                        help="print the per-conversation rollup instead of events")
    args = parser.parse_args(argv)

    if not os.path.exists(args.raw):
        raise SystemExit(f"no capture at {args.raw}")

    result = to_events(args.raw)
    problems = verify_no_content(result["events"])
    if problems:
        for problem in problems[:5]:
            print("REJECTED " + problem, file=sys.stderr)
        raise SystemExit("attributes outside the allow-list; nothing written")

    if args.summary:
        print(json.dumps(result["conversations"], indent=2, sort_keys=True))
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
    print(json.dumps({"msg": "otel_read_complete", "spans_read": result["spans_read"],
                      "events": len(result["events"]), "by_type": counts,
                      "conversations": len(result["conversations"])},
                     sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
