#!/usr/bin/env python3
"""Rank the Atlassian accountIds in an export, so `identities.txt` can be filled.

    python3 tools/diagnostics/who_is_who.py reports/2026-W34/exports/*.ndjson

`importers/pull.py --identities` needs one `email accountId` line per person,
and without it every laptop event keeps `person_id: null` and joins to nothing
(measured 2026-08-26: 935 events, no join to AIO or Bitbucket in any
direction). The addresses are known -- they are on the roster. The accountIds
are the half nobody has written down.

**This cannot tell you who is who, and does not pretend to.** The pipeline
stores no names by design: `test_case_title` is excluded, git authors are
hashed, and the Jira poller keeps an accountId and nothing else. What this does
is narrow the question from "what are the accountIds" to "which of these five
is Ngoc", by showing what each account actually did -- how many test runs they
executed, which cycles, how many pull requests they opened and reviewed.

Match that against what you know of the team, confirm it in Jira, and write the
file. A guess here attributes one person's AI use to another, which is worse
than the null it replaces.

Belongs to no metric, so it lives here rather than in `importers/`.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from typing import Any, Dict, Iterable, List


def load(paths: Iterable[str]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError as exc:
            print("skipped {}: {}".format(path, exc), file=sys.stderr)
    return events


def profile(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    for event in events:
        who = (event.get("actor") or {}).get("person_id")
        if not who:
            continue
        entry = found.setdefault(who, {
            "events": 0,
            "by_type": collections.Counter(),
            "cycles": set(),
            "surfaces": collections.Counter(),
            "first_seen": None,
            "last_seen": None,
        })
        entry["events"] += 1
        entry["by_type"][event.get("event_type") or "?"] += 1
        entry["surfaces"][(event.get("agent") or {}).get("surface") or "?"] += 1
        cycle = (event.get("context") or {}).get("test_cycle_key")
        if cycle:
            entry["cycles"].add(cycle)
        when = event.get("event_time")
        if isinstance(when, str) and when:
            if entry["first_seen"] is None or when < entry["first_seen"]:
                entry["first_seen"] = when
            if entry["last_seen"] is None or when > entry["last_seen"]:
                entry["last_seen"] = when
    return found


def render(found: Dict[str, Dict[str, Any]]) -> None:
    if not found:
        print("No accountIds in these files. Nothing to match.")
        return
    ranked = sorted(found.items(), key=lambda kv: -kv[1]["events"])
    print("{} distinct accountIds, busiest first.\n".format(len(ranked)))
    for who, entry in ranked:
        print(who)
        print("   events      {:,}".format(entry["events"]))
        print("   active      {} .. {}".format(
            (entry["first_seen"] or "?")[:10], (entry["last_seen"] or "?")[:10]))
        kinds = ", ".join("{} {}".format(n, k)
                          for k, n in entry["by_type"].most_common(4))
        print("   does        {}".format(kinds))
        if entry["cycles"]:
            shown = sorted(entry["cycles"])[:4]
            print("   cycles      {}{}".format(
                ", ".join(shown),
                " (+{} more)".format(len(entry["cycles"]) - len(shown))
                if len(entry["cycles"]) > len(shown) else ""))
        print()

    print("Write the file once you have confirmed each in Jira:\n")
    print("    # identities.txt -- email accountId")
    for who, _entry in ranked[:5]:
        print("    someone@aeris.net   {}".format(who))
    print()
    print("Then: importers/pull.py --week ... --identities identities.txt")
    print()
    print("An unconfirmed guess attributes one person's AI use to another, "
          "which is worse than the null it replaces. Leave a line out rather "
          "than guess it -- pull.py names whoever it could not map.")


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rank accountIds by what they did, to fill identities.txt.")
    parser.add_argument("files", nargs="+", help="NDJSON exports to read")
    args = parser.parse_args(argv)
    render(profile(load(args.files)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
