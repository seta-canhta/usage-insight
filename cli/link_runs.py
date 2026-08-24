#!/usr/bin/env python3
"""Join OTel conversations to emitter runs, with the evidence written down.

    python3 cli/link_runs.py

Two streams describe the same work and neither can answer alone. The emitter
knows *what* -- which agent, which ticket, which phases. OTel knows *what it
cost* -- tokens, model, latency. `CONTRACT.md` §2.4 names the bridge
(`run.bound` carrying `otel_conversation_id`) and then warns that it "is NOT a
join key on its own".

**This produces a link table, not a `run_id` on an OTel event.** §2.4 is
explicit: *"Never synthesise a `run_id` to force a join -- that manufactures a
join key and breaches AR-1."* Writing the id onto the span would bury a guess
inside a field that everything downstream reads as fact. A separate table with
a method and a confidence keeps the guess visible and refusable.

## The tiers, strongest first

| Method | Confidence | Evidence |
|---|---|---|
| `explicit` | 1.0 | `run.bound` names the conversation id |
| `heuristic` | 0.8 | time containment **and** the agent name agrees |
| `heuristic` | 0.5 | time containment alone, exactly one candidate |
| none | — | more than one run overlaps, or none does |

Tier 2 is worth more than time alone because the two signals are independent:
`copilot_chat.mode_name` comes from Copilot, `--agent` from the agent's own
instructions. Both saying "Platform Developer 2.0" is corroboration, not one fact
counted twice.

Ambiguity is reported, never resolved by picking the nearest. Two runs
overlapping one conversation is a real state -- someone ran two agents at once
-- and the honest output is "cannot attribute", because the alternative is
charging one agent for another's tokens.

**§2.4 also settles what this can be used for:** only `explicit` rows may feed
cost-per-output. Until an agent emits `run.bound`, the joins here are good
enough to report tokens by agent and not good enough to price an output. That
limit is a property of the evidence, not a gap in the code.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import insight  # noqa: E402

#: A run's window is [started, completed]. Spans land slightly outside it: the
#: exporter batches, and the emitter's run-end fires before the last span is
#: flushed. Widened by this much at each end -- enough to catch the flush, small
#: enough that a second run starting minutes later is not swept in.
SLACK = timedelta(seconds=90)

#: How long an unfinished run is allowed to claim. A run with no completion
#: event is open-ended, and open-ended means it would match every conversation
#: for the rest of the day -- billing an agent that crashed at 10am for the
#: chat somebody had at 4pm. Bounded instead: past this, the run is treated as
#: abandoned and the conversation goes unlinked, which is the truthful answer.
OPEN_RUN_MAX = timedelta(hours=1)

RUN_START = "run.started"
RUN_END = ("run.completed", "run.failed", "run.timeout")
OTEL_TYPES = ("model.call", "tool.call", "gate.evaluated")


def parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def runs_from(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    runs: Dict[str, Dict[str, Any]] = {}
    for event in events:
        run_id = event.get("run_id")
        if not run_id:
            continue
        if event["event_type"] == RUN_START:
            runs.setdefault(run_id, {})
            runs[run_id].update({
                "run_id": run_id,
                "agent": (event.get("agent") or {}).get("agent_name"),
                "jira_issue_key": (event.get("context") or {}).get("jira_issue_key"),
                "started_at": event.get("event_time"),
                "trace_id": event.get("trace_id"),
            })
        elif event["event_type"] in RUN_END:
            runs.setdefault(run_id, {"run_id": run_id})
            runs[run_id]["ended_at"] = event.get("event_time")
        elif event["event_type"] == "run.bound":
            runs.setdefault(run_id, {"run_id": run_id})
            runs[run_id]["otel_conversation_id"] = (
                event.get("attributes") or {}).get("otel_conversation_id")
    return runs


def conversations_from(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    for event in events:
        if event["event_type"] not in OTEL_TYPES:
            continue
        conversation = event.get("trace_id")
        if not conversation:
            continue
        entry = found.setdefault(conversation, {
            "conversation_id": conversation, "agents": set(), "events": 0,
            "first_at": None, "last_at": None, "input_tokens": 0,
            "output_tokens": 0, "cached_tokens": 0, "model_calls": 0,
        })
        entry["events"] += 1
        agent = (event.get("agent") or {}).get("agent_name")
        if agent:
            entry["agents"].add(agent)
        stamp = event.get("event_time")
        if stamp:
            if not entry["first_at"] or stamp < entry["first_at"]:
                entry["first_at"] = stamp
            if not entry["last_at"] or stamp > entry["last_at"]:
                entry["last_at"] = stamp
        if event["event_type"] == "model.call":
            attributes = event.get("attributes") or {}
            entry["model_calls"] += 1
            entry["input_tokens"] += attributes.get("input_tokens") or 0
            entry["output_tokens"] += attributes.get("output_tokens") or 0
            entry["cached_tokens"] += attributes.get("cached_input_tokens") or 0
    return found


def link(runs: Dict[str, Dict[str, Any]],
         conversations: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    links: List[Dict[str, Any]] = []
    unlinked: List[Dict[str, Any]] = []

    bound = {r.get("otel_conversation_id"): r for r in runs.values()
             if r.get("otel_conversation_id")}

    for conversation_id, conversation in sorted(conversations.items()):
        run = bound.get(conversation_id)
        if run:
            links.append({
                "conversation_id": conversation_id, "run_id": run["run_id"],
                "agent": run.get("agent"), "jira_issue_key": run.get("jira_issue_key"),
                "method": "explicit", "confidence": 1.0,
                "evidence": ["run.bound names this conversation"],
                **{k: conversation[k] for k in
                   ("model_calls", "input_tokens", "output_tokens", "cached_tokens")},
            })
            continue

        first, last = parse(conversation["first_at"]), parse(conversation["last_at"])
        candidates = []
        for run in runs.values():
            started, ended = parse(run.get("started_at")), parse(run.get("ended_at"))
            if not started or not first:
                continue
            # An unfinished run gets a bounded window, not an unbounded one.
            finish = (ended + SLACK) if ended else (started + OPEN_RUN_MAX)
            if started - SLACK <= first and (last or first) <= finish:
                candidates.append(run)

        if len(candidates) != 1:
            unlinked.append({
                "conversation_id": conversation_id,
                "agents": sorted(conversation["agents"]),
                "input_tokens": conversation["input_tokens"],
                # Named, so the ambiguity can be looked at rather than inferred
                # from a count that says only "some".
                "reason": ("no run covers this window" if not candidates
                           else "{} runs overlap; attributing to one would charge "
                                "an agent for another's tokens".format(len(candidates))),
                "candidate_run_ids": [c["run_id"] for c in candidates],
            })
            continue

        run = candidates[0]
        agrees = run.get("agent") and run["agent"] in conversation["agents"]
        evidence = ["the run's window contains every span in this conversation"]
        if agrees:
            evidence.append(
                "agent name agrees: Copilot reported {!r} and the emitter "
                "declared the same".format(run["agent"]))
        links.append({
            "conversation_id": conversation_id, "run_id": run["run_id"],
            "agent": run.get("agent"), "jira_issue_key": run.get("jira_issue_key"),
            "method": "heuristic", "confidence": 0.8 if agrees else 0.5,
            "evidence": evidence,
            **{k: conversation[k] for k in
               ("model_calls", "input_tokens", "output_tokens", "cached_tokens")},
        })

    return {
        "links": links,
        "unlinked_conversations": unlinked,
        "runs_seen": len(runs),
        "conversations_seen": len(conversations),
        # §2.4: only explicit rows may feed cost-per-output. Reported so a
        # consumer does not have to re-derive the rule to know what it may do.
        "explicit_links": sum(1 for l in links if l["method"] == "explicit"),
        "usable_for_cost_per_output": sum(
            1 for l in links if l["method"] == "explicit"),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Join OTel conversations to emitter runs, with evidence.")
    parser.add_argument("--out", help="write the link table here")
    args = parser.parse_args(argv)

    events = insight.read_buffer()
    result = link(runs_from(events), conversations_from(events))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
