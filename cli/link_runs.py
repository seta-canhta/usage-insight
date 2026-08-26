#!/usr/bin/env python3
"""Join Copilot sessions to emitter runs, with the evidence written down.

    python3 cli/link_runs.py

Two streams describe the same work and neither can answer alone. The emitter
knows *what* -- which agent, which ticket, which phases. Copilot's session
journal knows *what it cost* -- tokens, premium requests, model. `CONTRACT.md`
§2.4 names the bridge (`run.bound` carrying `copilot_session_id`) and then warns
that it "is NOT a join key on its own".

**This produces a link table, not a `run_id` on a usage event.** §2.4 is
explicit: *"Never synthesise a `run_id` to force a join -- that manufactures a
join key and breaches AR-1."* Writing the id onto the usage row would bury a
guess inside a field that everything downstream reads as fact. A separate table
with a method and a confidence keeps the guess visible and refusable.

**Usage is session grain now, and every row says so.** The span source emitted
one `model.call` per API call, each individually timestamped, so a time window
placed tokens on the right run. The journal totals usage per *session* and
stamps it at shutdown, so one `model.call` covers every run the session hosted
-- measured 2026-08-26: 38 resumes against 22 starts, so multi-run sessions are
the norm rather than the exception.

The tokens are still reported, because a table that dropped them would report
zero and zero is a claim. What travels with them is `usage_grain`: a link whose
session held one run can carry its usage to that run, and one whose session
held several cannot. Dividing a session total by time or by call count is,
in CONTRACT.md §3's phrase, "the same offence wearing arithmetic" -- so the
row says `per_session`, and CONTRACT.md §3 requires those runs to report
`cost_usd = NULL` rather than a share.

## The tiers, strongest first

| Method | Confidence | Evidence |
|---|---|---|
| `explicit` | 1.0 | `run.bound` names the session id |
| `heuristic` | 0.8 | time containment **and** the agent name agrees |
| `heuristic` | 0.5 | time containment alone, exactly one candidate |
| none | — | more than one run overlaps, or none does |

Tier 2 is worth more than time alone because the two signals are independent:
the agent name comes from Copilot's own `subagent.started` record, `--agent`
from the agent's instructions. Both saying "Platform Developer 2.0" is
corroboration, not one fact counted twice.

Ambiguity is reported, never resolved by picking the nearest. Two runs
overlapping one session is a real state -- someone ran two agents at once --
and the honest output is "cannot attribute", because the alternative is
charging one agent for another's work.

**§2.4 also settles what this can be used for:** only `explicit` rows may feed
cost-per-output. Until an agent emits `run.bound`, the joins here are good
enough to report activity by agent and not good enough to price an output. That
limit is a property of the evidence, not a gap in the code -- and since the
journal reader emits everything at `heuristic`, `run.bound` from the emitter is
now the *only* thing that can turn the cost-per-output metric on.
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
#: event is open-ended, and open-ended means it would match every session for
#: the rest of the day -- billing an agent that crashed at 10am for the work
#: somebody did at 4pm. Bounded instead: past this, the run is treated as
#: abandoned and the session goes unlinked, which is the truthful answer.
OPEN_RUN_MAX = timedelta(hours=1)

RUN_START = "run.started"
RUN_END = ("run.completed", "run.failed", "run.timeout")
#: What a session contributes to the link table.
#:
#: `model.call` is included because the table has to report what a session
#: spent -- dropping it would leave the token columns permanently zero, which
#: is worse than reporting them with their grain attached. But it is *session*
#: grain: one event covers every run the session hosted. So a link row carries
#: `usage_grain`, and a caller that divides a session total across runs is
#: doing something this table has told it not to.
SESSION_TYPES = ("model.call", "tool.call", "gate.evaluated",
                 "output.generated")


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
            attributes = event.get("attributes") or {}
            runs[run_id]["copilot_session_id"] = attributes.get(
                "copilot_session_id")
    return runs


def conversations_from(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    for event in events:
        if event["event_type"] not in SESSION_TYPES:
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


#: The grain vocabulary, shared with the warehouse on purpose.
#:
#: ``sql/03_core_fct.sql`` carries a ``usage_grain`` column over the same axis,
#: with the values ``per_call`` (the retired span source, one row per API call),
#: ``per_session_model`` (one ``model.call`` per session *and model*, which is
#: what the reader emits) and ``none``. This table sits one step further out
#: and sums a session's ``model.call`` events across every model in it, so its
#: usage is ``per_session`` -- coarser again than the warehouse's coarsest.
#:
#: The words are shared rather than invented so the two can be read together.
#: The distinction that matters is the same at both layers and is the only one
#: a consumer needs: anything other than ``per_call`` cannot be attributed to a
#: single run, and ``none`` is not zero.
GRAIN_PER_SESSION = "per_session"
GRAIN_NONE = "none"


def grain_of(conversation: Dict[str, Any]) -> str:
    """What the token columns on a link row actually describe.

    ``per_session`` whenever usage is present: the journal totals at session
    grain and this sums across models on top of that. ``none`` when the session
    carried no ``model.call`` at all -- a real state, since a session that
    crashed before shutdown records none, and its tokens are then unknowable
    rather than zero.
    """
    return GRAIN_PER_SESSION if conversation.get("model_calls") else GRAIN_NONE


def link(runs: Dict[str, Dict[str, Any]],
         conversations: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    links: List[Dict[str, Any]] = []
    unlinked: List[Dict[str, Any]] = []

    bound = {r.get("copilot_session_id"): r for r in runs.values()
             if r.get("copilot_session_id")}

    for conversation_id, conversation in sorted(conversations.items()):
        run = bound.get(conversation_id)
        if run:
            links.append({
                "conversation_id": conversation_id, "run_id": run["run_id"],
                "agent": run.get("agent"), "jira_issue_key": run.get("jira_issue_key"),
                "method": "explicit", "confidence": 1.0,
                "evidence": ["run.bound names this session"],
                # Even an explicit link cannot make session-grain usage into
                # run-grain usage. `explicit` says *which* run the session
                # belongs to; `usage_grain` says what the tokens beside it
                # describe, which is the whole session.
                "usage_grain": grain_of(conversation),
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
                                "an agent for another's work".format(len(candidates))),
                "candidate_run_ids": [c["run_id"] for c in candidates],
            })
            continue

        run = candidates[0]
        agrees = run.get("agent") and run["agent"] in conversation["agents"]
        evidence = ["the run's window contains every event in this session"]
        if agrees:
            evidence.append(
                "agent name agrees: Copilot reported {!r} and the emitter "
                "declared the same".format(run["agent"]))
        links.append({
            "conversation_id": conversation_id, "run_id": run["run_id"],
            "agent": run.get("agent"), "jira_issue_key": run.get("jira_issue_key"),
            "method": "heuristic", "confidence": 0.8 if agrees else 0.5,
            "evidence": evidence,
            "usage_grain": grain_of(conversation),
            **{k: conversation[k] for k in
               ("model_calls", "input_tokens", "output_tokens", "cached_tokens")},
        })

    return {
        "links": links,
        "unlinked_sessions": unlinked,
        "runs_seen": len(runs),
        "sessions_seen": len(conversations),
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
