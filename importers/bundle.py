#!/usr/bin/env python3
"""Read the weekly bundles handed over from engineers' machines.

    python3 importers/bundle.py --inbox inbox/ --out events.ndjson

A bundle is NDJSON: a manifest on the first line, contract events after it.
``cli/insight.py pack`` writes them; this reads them.

Two jobs, and the second is the one people forget:

1. Merge the events, deduplicating on ``event_id``.
2. **Record which machine covered which week.** With hand-collected data a gap
   is the normal case -- someone forgets, someone is on leave, someone joins
   mid-quarter. A report that renders those weeks as ``0`` shows a team getting
   worse when it is only getting quieter. Coverage is what lets a report tell a
   measured zero from an absent one, so it is a first-class output here rather
   than a diagnostic.

On integrity: the checksum catches truncation and accidental corruption. It is
**not** tamper-evidence -- an engineer can read and edit their own bundle before
handing it over, which is what makes the collection consensual. These figures
are a voluntary record, never an audit trail. See ``docs/ARCHITECTURE.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "pollers"), os.path.join(_ROOT, "collector")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import common  # noqa: E402
import main as collector_main  # noqa: E402

BUNDLE_FORMAT = "seta-insight-bundle/1"


class BundleError(Exception):
    """A bundle that cannot be trusted. Never imported partially."""


def parse_bundle(path: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Read one bundle, or raise.

    A bundle either imports whole or not at all. Half a bundle is worse than
    none: the events land, the window looks covered, and nobody can tell that
    the rest is missing.
    """
    with open(path, "r", encoding="utf-8") as handle:
        first = handle.readline()
        body = handle.read()

    if not first.strip():
        raise BundleError("empty file")
    try:
        manifest = json.loads(first).get("_manifest")
    except json.JSONDecodeError as exc:
        raise BundleError("first line is not a manifest: {}".format(exc))
    if not isinstance(manifest, dict):
        raise BundleError("first line carries no _manifest object")

    if manifest.get("format") != BUNDLE_FORMAT:
        raise BundleError(
            "unknown bundle format {!r}".format(manifest.get("format")))
    if manifest.get("schema_version") != common.SCHEMA_VERSION:
        raise BundleError(
            "schema {} does not match this pipeline's {}".format(
                manifest.get("schema_version"), common.SCHEMA_VERSION))

    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if digest != manifest.get("sha256"):
        raise BundleError(
            "checksum mismatch -- file is truncated or altered "
            "(manifest {}, computed {})".format(
                str(manifest.get("sha256"))[:12], digest[:12]))

    events: List[Dict[str, Any]] = []
    for number, line in enumerate(body.splitlines(), start=2):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise BundleError("line {} is not JSON: {}".format(number, exc))

    declared = manifest.get("event_count")
    if declared is not None and declared != len(events):
        raise BundleError(
            "manifest declares {} events, file holds {}".format(
                declared, len(events)))

    return manifest, events


def validate_events(events: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Re-check the allow-list on the way in.

    The client already checks on write. This checks again because a bundle is a
    file a person can edit, and the allow-list is the boundary that keeps
    content out of the warehouse. Checking twice costs nothing; checking once
    puts the whole guarantee on a process running somewhere else.
    """
    kept: List[Dict[str, Any]] = []
    problems: List[str] = []
    for event in events:
        event_type = event.get("event_type")
        allowed = collector_main.ATTRIBUTE_ALLOWLIST.get(event_type)
        if allowed is None:
            problems.append("{}: event_type not in the contract enum".format(
                event.get("event_id")))
            continue
        extra = sorted(
            key for key in (event.get("attributes") or {}) if key not in allowed)
        if extra:
            problems.append("{}: {} not in the allow-list for {}".format(
                event.get("event_id"), ", ".join(extra), event_type))
            continue
        kept.append(event)
    return kept, problems


def iso_weeks(start: Optional[str], end: Optional[str]) -> List[str]:
    """Every ISO week the declared window touches, as ``YYYY-Www``."""
    if not start or not end:
        return []
    first = common.parse_ts(start)
    last = common.parse_ts(end)
    if first is None or last is None or last < first:
        return []
    weeks, cursor = [], first
    # A day at a time, carrying tzinfo forward. datetime.fromordinal() would
    # drop it and the next comparison then raises on naive-vs-aware.
    while cursor <= last:
        year, week, _ = cursor.isocalendar()
        label = "{}-W{:02d}".format(year, week)
        if label not in weeks:
            weeks.append(label)
        cursor = cursor + timedelta(days=1)
    return weeks


def import_inbox(inbox: str, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    state = state or {}
    seen = set(state.get("event_ids") or [])
    coverage: Dict[str, Dict[str, Any]] = dict(state.get("coverage") or {})

    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, str]] = []
    duplicates = 0
    imported_files: List[str] = []

    for name in sorted(os.listdir(inbox)):
        if not name.endswith(".ndjson"):
            continue
        path = os.path.join(inbox, name)
        try:
            manifest, events = parse_bundle(path)
        except BundleError as exc:
            # Loudly, and the whole file. A rejected bundle is not counted as
            # coverage either -- otherwise its week looks measured when it is not.
            rejected.append({"file": name, "reason": str(exc)})
            continue

        kept, problems = validate_events(events)
        for problem in problems:
            rejected.append({"file": name, "reason": problem})

        machine = manifest.get("machine_id") or "unknown"
        entry = coverage.setdefault(machine, {"weeks": [], "bundles": 0, "events": 0})
        for week in iso_weeks(manifest.get("window_start"), manifest.get("window_end")):
            if week not in entry["weeks"]:
                entry["weeks"].append(week)
        entry["bundles"] += 1
        entry["events"] += len(kept)

        for event in kept:
            event_id = event.get("event_id")
            if event_id in seen:
                duplicates += 1
                continue
            seen.add(event_id)
            event["ingested_at"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            accepted.append(event)
        imported_files.append(name)

    for machine in coverage:
        coverage[machine]["weeks"] = sorted(coverage[machine]["weeks"])

    return {
        "events": accepted,
        "files_imported": imported_files,
        "files_rejected": sorted({r["file"] for r in rejected}),
        "rejected": rejected,
        "duplicates": duplicates,
        "coverage": coverage,
        "event_ids": sorted(seen),
    }


def coverage_report(coverage: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Machine-weeks covered, and which weeks are missing from which machine.

    A machine that reported in week 30 and week 34 but not 31-33 has three
    absent weeks, and they are absent, not zero. Naming them is the only way a
    reader can weigh an aggregate honestly.
    """
    all_weeks = sorted({w for entry in coverage.values() for w in entry["weeks"]})
    machines = {}
    for machine, entry in sorted(coverage.items()):
        weeks = set(entry["weeks"])
        span = [w for w in all_weeks
                if entry["weeks"] and entry["weeks"][0] <= w <= entry["weeks"][-1]]
        machines[machine] = {
            "weeks_covered": sorted(weeks),
            "weeks_missing_within_span": [w for w in span if w not in weeks],
            "bundles": entry["bundles"],
            "events": entry["events"],
        }
    return {
        "machines": len(coverage),
        "weeks_seen": all_weeks,
        "machine_weeks_covered": sum(len(e["weeks"]) for e in coverage.values()),
        "by_machine": machines,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import weekly bundles handed over from engineers' machines.")
    parser.add_argument("--inbox", required=True, help="directory holding *.ndjson bundles")
    parser.add_argument("--out", help="merged NDJSON output (default: stdout)")
    parser.add_argument("--state", help="JSON state file carrying seen ids and coverage")
    parser.add_argument("--coverage-out", help="write the coverage report here")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.inbox):
        raise SystemExit("inbox {} does not exist".format(args.inbox))

    state = {}
    if args.state and os.path.exists(args.state):
        with open(args.state, "r", encoding="utf-8") as handle:
            state = json.load(handle)

    result = import_inbox(args.inbox, state)
    report = coverage_report(result["coverage"])

    lines = "".join(json.dumps(e, sort_keys=True) + "\n" for e in result["events"])
    if args.out:
        with open(args.out, "a", encoding="utf-8") as handle:
            handle.write(lines)
    else:
        sys.stdout.write(lines)

    if args.state:
        os.makedirs(os.path.dirname(os.path.abspath(args.state)), exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as handle:
            json.dump({"event_ids": result["event_ids"],
                       "coverage": result["coverage"]}, handle, indent=2, sort_keys=True)

    if args.coverage_out:
        with open(args.coverage_out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)

    summary = {
        "msg": "bundle_import_complete",
        "files_imported": len(result["files_imported"]),
        "files_rejected": len(result["files_rejected"]),
        "events_written": len(result["events"]),
        "duplicates_skipped": result["duplicates"],
        "machine_weeks_covered": report["machine_weeks_covered"],
    }
    print(json.dumps(summary, sort_keys=True), file=sys.stderr)
    for problem in result["rejected"]:
        print("REJECTED {}: {}".format(problem["file"], problem["reason"]),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
