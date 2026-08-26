#!/usr/bin/env python3
"""Read `rtk`, the second token source -- if this machine has one.

    python3 cli/rtk_read.py [--probe]

What rtk is, as far as this machine can tell
--------------------------------------------
Found on both pilot QA machines (2026-08-26), installed by something automated:
an identical `~/.copilot/copilot-instructions.md` carrying an
``<!-- rtk-instructions v2 -->`` marker, plus an identical
``~/.copilot/hooks/rtk-rewrite.json`` registering it as a **PreToolUse hook**
(``rtk hook copilot``, 5s timeout). It describes itself as

    a CLI proxy that filters and compresses command outputs, saving 60-90%
    tokens

and advertises ``rtk gain`` (a savings dashboard), ``rtk gain --history``
(per-command history), ``rtk discover`` and ``rtk proxy``.

**Nothing here has been verified against a running rtk.** It is not installed on
the machine this was written on, and the pilot samples shipped the hook
configuration without any of rtk's own data. So this module is written to find
out rather than to assume: it locates rtk, captures what it reports, and emits
only what it can recognise. Everything else is reported as an unparsed shape so
the format can be added once somebody has actually seen it.

Why it is worth reading at all
------------------------------
A PreToolUse hook sits in a better position than anything else this system has.
It sees **every tool call before it runs** -- finer than the Copilot CLI journal
(which totals at session shutdown) and finer than VS Code's chat store (which
records a request, not the commands inside it).

What it must NOT be used for
----------------------------
**The savings figure is the tool's own claim about itself.** `rtk gain` reports
how much rtk believes it saved. Two separate cautions apply and they are not
the same:

1. *Self-report.* `docs/WHAT-WE-MEASURE.md` forbids counting from self-reported
   data -- the same rule that keeps the daily-sync spreadsheet's "AI Usage"
   column out of the adoption figure. A number a tool publishes about its own
   value is exactly that.
2. *Basis unknown.* "Saved" is a difference against a baseline, and nothing on
   these machines says what the baseline is. If it is `tokens(raw output) -
   tokens(filtered output)`, both terms are measured and the difference is
   sound. If it is a modelled estimate of what a session "would have" cost, it
   is a counterfactual and belongs with Productivity Gain and ROI in the
   metrics this system refuses.

So this module **does not emit a savings number** into the contract. It emits
the tool calls, which are observations, and it records the savings claim in the
probe output where a human can see it and decide. That decision needs an answer
from whoever deployed rtk, not a guess from here.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import common  # noqa: E402

SURFACE = "rtk-proxy"

#: Where a tool of this shape usually keeps state. Checked in order; the first
#: that exists wins. None of these is confirmed -- see the module docstring.
DATA_DIRS: Tuple[str, ...] = (
    "~/.rtk",
    "~/.config/rtk",
    "~/.local/share/rtk",
    "~/Library/Application Support/rtk",
    "~/.cache/rtk",
)

#: How long rtk gets before it is abandoned. It is a hook that must not slow a
#: tool call down, so it should answer instantly; anything slower is a hang and
#: an hourly collector may not wait for it.
TIMEOUT_S = 20

#: Keys that could plausibly carry a per-command record. Names are matched
#: exactly, and a record that matches none is kept as an unparsed shape rather
#: than being coerced into the closest guess.
COMMAND_KEYS = ("command", "cmd", "argv", "tool", "name")
TIME_KEYS = ("timestamp", "time", "at", "ts", "started_at", "created_at")
SAVED_KEYS = ("saved", "saved_tokens", "tokens_saved", "savings", "gain")
RAW_KEYS = ("raw_tokens", "before", "input_tokens", "original_tokens")
KEPT_KEYS = ("filtered_tokens", "after", "output_tokens", "final_tokens")

#: `tool_kind` for the wrapped binary. Exact-match against a closed set, as in
#: the other readers: substring matching would put `gitk` in `vcs` for starting
#: with `git`. Anything unlisted is `other` and stays visible as such.
TOOL_KINDS: Dict[str, str] = {
    "git": "vcs", "gh": "vcs", "hg": "vcs",
    "cargo": "build", "make": "build", "mvn": "build", "gradle": "build",
    "npm": "build", "yarn": "build", "pnpm": "build", "tsc": "build",
    "pytest": "test", "jest": "test", "vitest": "test", "go": "build",
    "docker": "container", "kubectl": "container", "helm": "container",
    "ls": "read", "cat": "read", "find": "search", "grep": "search",
    "rg": "search", "curl": "network",
}


def which() -> Optional[str]:
    return shutil.which("rtk")


def data_dir() -> Optional[str]:
    for candidate in DATA_DIRS:
        path = os.path.expanduser(candidate)
        if os.path.isdir(path):
            return path
    return None


def run(args: List[str], timeout: int = TIMEOUT_S) -> Tuple[int, str]:
    """Run rtk, returning ``(returncode, stdout)``.

    Never raises. This is called from an hourly collector, and a tool that is
    missing, hung or broken is a fact to report, not a reason to lose the run.
    stderr is discarded rather than captured: it is the most likely place for a
    path or a command line to appear, and neither may be stored.
    """
    binary = which()
    if not binary:
        return 127, ""
    try:
        done = subprocess.run(
            [binary] + args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return 126, ""
    return done.returncode, done.stdout or ""


def history() -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """rtk's per-command history as records, plus how it was obtained.

    JSON is asked for first because a machine-readable answer is the only one
    worth parsing. A tool that has no ``--json`` will usually reject the flag,
    and its human table is deliberately **not** scraped: column positions in a
    dashboard change without warning, and a mis-parsed column is a wrong number
    that looks right.
    """
    for flags, label in ((["gain", "--history", "--json"], "gain --history --json"),
                         (["gain", "--json"], "gain --json")):
        code, out = run(flags)
        if code == 0 and out.strip():
            try:
                data = json.loads(out)
            except ValueError:
                continue
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict)], label
            if isinstance(data, dict):
                for key in ("history", "entries", "records", "commands", "items"):
                    if isinstance(data.get(key), list):
                        return [r for r in data[key] if isinstance(r, dict)], label
                return [data], label
    return None, "unavailable"


def first_word(value: Any) -> Optional[str]:
    """The binary rtk wrapped, from a command record.

    Only the first token is taken, and only if it looks like a bare program
    name. The rest of a command line is arguments, and arguments carry paths,
    hostnames and occasionally secrets -- CONTRACT.md §11.3. A name with a slash
    in it is a path, so it is dropped rather than trimmed to its basename: the
    basename of `/Users/someone/bin/deploy` is safe, but deciding that per case
    is how a path eventually ships.
    """
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if not isinstance(value, str) or not value.strip():
        return None
    word = value.strip().split()[0]
    if "/" in word or "\\" in word or len(word) > 40:
        return None
    return word


def pick(record: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    for key in keys:
        if key in record:
            return record[key]
    return None


def stamp(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, (int, float)) and value > 0:
        seconds = value / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(
                seconds, tz=timezone.utc).isoformat(
                    timespec="milliseconds").replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return None
    return None


def to_events(actor: Optional[Dict[str, Any]] = None,
              repo: Optional[str] = None) -> Dict[str, Any]:
    """Contract events from rtk's history, and an honest account of the rest."""
    binary, store = which(), data_dir()
    if not binary and not store:
        return {"present": False, "binary": None, "data_dir": None,
                "events": [], "records": 0, "unparsed": 0,
                "source": "unavailable", "coverage": coverage(0, 0, False)}

    records, source = history()
    events: List[Dict[str, Any]] = []
    unparsed = 0

    context = common.make_context(repo_full_name=repo)
    agent = common.make_agent("rtk", surface=SURFACE)
    agent["agent_version"] = None

    for index, record in enumerate(records or []):
        name = first_word(pick(record, COMMAND_KEYS))
        if not name:
            # Recognised as a record, not recognised as a command. Counted so
            # the gap is visible rather than rounded away.
            unparsed += 1
            continue
        events.append(common.build_event(
            event_type="tool.call",
            event_time=stamp(pick(record, TIME_KEYS)),
            natural_key=("rtk", name, index, pick(record, TIME_KEYS)),
            attributes={
                "tool_name": name,
                "tool_kind": TOOL_KINDS.get(name, "other"),
                # rtk reports what it filtered, not whether the command worked.
                # "ok" would be an assumption about every command it wrapped.
                "status": None,
                "duration_ms": None,
                "error_class": None,
            },
            actor=actor,
            context=context,
            agent=agent,
            # `heuristic`: rtk records the command, and nothing ties it to a
            # run or a session. Manufacturing that tie is AR-1.
            link=common.make_link("heuristic", 0.7),
            trace_id=common.deterministic_id("trc", "rtk", str(name), str(index)),
            run_id=None,
        ))

    return {
        "present": True,
        "binary": bool(binary),
        "data_dir": bool(store),
        "events": events,
        "records": len(records or []),
        "unparsed": unparsed,
        "source": source,
        "coverage": coverage(len(records or []), unparsed, bool(records)),
    }


def coverage(records: int, unparsed: int, parsed_any: bool) -> Dict[str, Any]:
    return {
        "surface": SURFACE,
        "records_seen": records,
        "records_unparsed": unparsed,
        "history_readable": parsed_any,
        "savings_emitted": False,
        "reason": (
            "rtk's savings figure is the tool's own claim about itself and its "
            "baseline is undocumented on these machines. It is shown by "
            "`--probe` for a human to judge and is never emitted as a "
            "measurement. See the module docstring."),
    }


def probe() -> Dict[str, Any]:
    """Find out what rtk is, without emitting anything.

    This exists because the format is unknown. Run it on a machine that has
    rtk, read the shape, then teach the reader -- rather than guessing a schema
    from a marketing line and shipping a parser for a format nobody has seen.
    """
    binary, store = which(), data_dir()
    report: Dict[str, Any] = {
        "binary_on_path": bool(binary),
        "data_dir": store,
        "data_dir_entries": sorted(os.listdir(store))[:40] if store else [],
        "hook_installed": os.path.exists(
            os.path.expanduser("~/.copilot/hooks/rtk-rewrite.json")),
    }
    for label, args in (("version", ["--version"]), ("gain", ["gain"]),
                        ("gain_json", ["gain", "--json"]),
                        ("history_json", ["gain", "--history", "--json"])):
        code, out = run(args, timeout=10)
        # Truncated hard. This is a shape report, not a transcript, and rtk's
        # output is command lines.
        report[label] = {"rc": code, "head": out[:400]}
    records, source = history()
    report["history_source"] = source
    report["history_records"] = len(records or [])
    report["record_keys"] = sorted(
        {k for r in (records or [])[:50] for k in r})[:40]
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Emit tool.call events from rtk's history.")
    parser.add_argument("--probe", action="store_true",
                        help="report what rtk is on this machine and exit")
    parser.add_argument("--out", help="NDJSON output file")
    args = parser.parse_args(argv)

    if args.probe:
        print(json.dumps(probe(), indent=2, sort_keys=True))
        return 0

    result = to_events()
    if args.out and result["events"]:
        with open(args.out, "w", encoding="utf-8") as handle:
            for event in result["events"]:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "events"} |
                     {"events": len(result["events"])},
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
