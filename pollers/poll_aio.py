#!/usr/bin/env python3
"""AIO TCMS poller -- test execution events for AI telemetry.

Emits CONTRACT.md §3 event 22 (``test.run.completed``): one event per test case
per cycle, carrying the latest run's status, who executed it, whether it was
automated, and how many Jira defects it raised.

Usage::

    python poll_aio.py --project PRJ [--since 2026-06-21T00:00:00Z] [--out events.ndjson]
    python poll_aio.py --project PRJ --probe          # one call, report reachability

Auth: ``Authorization: AioAuth $AIO_API_TOKEN``.

**AIO does not accept the Jira API token.** It issues its own key and returns
``401 Invalid or missing API Token`` for the Jira credential, which is why
``AIO_API_TOKEN`` is a separate variable rather than a reuse of
``JIRA_API_TOKEN``. Get one from AIO Tests > API Keys. The app must also be
enabled per Jira project -- a project without it returns 401 with the message
"The app is not enabled for this project", which this poller reports as a
configuration problem rather than an auth failure, because the two need very
different fixes.

Why this source exists at all
-----------------------------
Design §4 originally listed AIO as unreachable and the §3 enum stopped at 21.
That was a credential gap, not a judgement that test execution did not matter.
For a QA engineer the **test cycle is the delivery record** -- pull requests are
not. A workbook built only from Bitbucket makes a QA engineer look inactive
while they are running hundreds of test cases.

Three things this poller is careful about
-----------------------------------------
1. **Not-run is not failed.** AIO seeds a cycle with every test case at status
   "Not Run" the moment the cycle is created. Counting those as failures, or
   even as executions, would turn cycle *planning* into apparent test *activity*
   and would make every large cycle look like a disaster. They are emitted with
   ``status_category = 'not_run'`` and a NULL ``executed_at``, and the
   aggregators must exclude them from any pass-rate denominator.
2. **Titles never leave AIO.** Test case titles are free text written by
   engineers and often quote customer names, endpoints and device ids. §11.3
   keeps that class of field out of the stream entirely, so the event carries
   ``test_case_key`` and ``folder_name`` and nothing else identifying. The
   folder is a controlled taxonomy ("E2E Awareness"), not free text.
3. **Time is milliseconds here.** AIO mixes epoch seconds (cycles) and epoch
   milliseconds (runs, cases) across endpoints in the same API. Feeding seconds
   to a millisecond parser silently yields the year 57490, which sorts fine and
   is wrong, so every timestamp goes through :func:`epoch_to_rfc3339`.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (  # noqa: E402
    Config,
    ConfigError,
    HttpClient,
    HttpError,
    NdjsonWriter,
    WatermarkStore,
    build_event,
    default_since,
    fail,
    log,
    make_actor,
    make_agent,
    make_context,
    make_link,
    parse_ts,
    to_rfc3339,
)

AGENT_NAME = "poller.aio"

#: AIO run status name -> the bounded category the warehouse groups on.
#: AIO lets an administrator add custom statuses, so anything unrecognised maps
#: to 'other' and is counted separately rather than being forced into pass/fail.
STATUS_CATEGORY = {
    "passed": "passed",
    "pass": "passed",
    "failed": "failed",
    "fail": "failed",
    "blocked": "blocked",
    "skipped": "skipped",
    "not applicable": "skipped",
    "n/a": "skipped",
    "in progress": "in_progress",
    "wip": "in_progress",
    "not run": "not_run",
    "notrun": "not_run",
    "no run": "not_run",
}

#: Categories that represent an actual execution. Everything else -- above all
#: 'not_run' -- must stay out of a pass-rate denominator.
EXECUTED_CATEGORIES = frozenset({"passed", "failed", "blocked", "skipped"})


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def epoch_to_rfc3339(value: Optional[Any]) -> Optional[str]:
    """Epoch seconds *or* milliseconds -> RFC3339, or None.

    AIO is inconsistent between endpoints: ``testcycle.createdDate`` is seconds
    while ``testrun.updatedDate`` is milliseconds. Guessing wrong is not a
    visible crash -- it produces a timestamp in the year 57490 that sorts and
    formats perfectly well -- so the unit is inferred from magnitude. The cutoff
    of 1e11 seconds is the year 5138, comfortably past any real date and
    comfortably below any millisecond value from this century.
    """
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number > 1e11:
        number /= 1000.0
    from datetime import datetime, timezone

    try:
        return to_rfc3339(datetime.fromtimestamp(number, timezone.utc))
    except (OverflowError, OSError, ValueError):
        return None


def status_category(status_name: Optional[str]) -> str:
    if not status_name:
        return "not_run"
    return STATUS_CATEGORY.get(str(status_name).strip().lower(), "other")


def cycle_in_window(cycle: Dict[str, Any], since: Optional[str]) -> bool:
    """Keep a cycle whose *activity* could fall in the window.

    Filtering on ``createdDate`` alone would drop a cycle opened in March and
    still being executed today -- exactly the long-running regression cycles
    that matter most. So a cycle survives if any of created / updated / start /
    end is at or after the window start, and a cycle with no usable timestamp
    at all is kept rather than silently dropped.
    """
    if not since:
        return True
    floor = parse_ts(since)
    if floor is None:
        return True
    stamps = [
        epoch_to_rfc3339(cycle.get(field))
        for field in ("updatedDate", "createdDate", "startDate", "endDate")
    ]
    parsed = [parse_ts(s) for s in stamps if s]
    if not parsed:
        return True
    return any(p >= floor for p in parsed if p)


def cycle_release_match(cycle: Dict[str, Any], release_ids: Sequence[int],
                        release_names: Sequence[str]) -> Optional[str]:
    """How this cycle matches the requested release, or None.

    Returns ``'release_id'`` or ``'title'`` so the caller can report the split.
    That distinction is not cosmetic: the id is a structured field an
    administrator set, while the title is free text an engineer typed. On this
    project two "26.8 Dev Integration Testing" cycles carry no release id at
    all, so a title fallback is needed to see them -- but a run whose numbers
    depend on somebody's spelling has to say so out loud.
    """
    if not release_ids and not release_names:
        return None
    if release_ids:
        ids = {cycle.get("jiraReleaseID")}
        ids.update(cycle.get("jiraReleaseIDs") or [])
        if any(rid in ids for rid in release_ids):
            return "release_id"
    title = str(cycle.get("title") or "")
    for name in release_names:
        if name and name.lower() in title.lower():
            return "title"
    return None


def defect_count(run: Dict[str, Any]) -> int:
    ids = run.get("jiraDefectIDs")
    return len(ids) if isinstance(ids, list) else 0


def folder_name(test_case: Dict[str, Any]) -> Optional[str]:
    folder = test_case.get("folder")
    return folder.get("name") if isinstance(folder, dict) else None


def priority_name(test_case: Dict[str, Any]) -> Optional[str]:
    priority = test_case.get("priority")
    return priority.get("name") if isinstance(priority, dict) else None


def effort_seconds(run: Dict[str, Any]) -> Optional[int]:
    """AIO 'effort' is a duration in seconds, and is almost always NULL.

    It stays NULL rather than becoming 0 -- an unrecorded effort is not an
    effort of zero, and averaging zeros would report that tests take no time.
    """
    value = run.get("effort")
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------


class AioPoller:
    def __init__(self, client: HttpClient, base_url: str, project: str,
                 page_size: int = 100) -> None:
        self.client = client
        self.base = f"{base_url.rstrip('/')}/aio-tcms/api/v1"
        self.project = project
        self.page_size = page_size

    # -- HTTP ---------------------------------------------------------------

    def _paginate(self, path: str, max_items: int = 0) -> Iterator[Dict[str, Any]]:
        """AIO paginates with startAt/maxResults/isLast, like Jira but not quite.

        ``isLast`` is authoritative when present; an empty page is treated as
        the end regardless, because a truthy ``isLast`` has been observed
        missing on some endpoints.
        """
        start = 0
        yielded = 0
        while True:
            payload = self.client.get_json(
                f"{self.base}{path}",
                params={"startAt": start, "maxResults": self.page_size},
            )
            items = (payload or {}).get("items") or []
            if not items:
                return
            for item in items:
                yield item
                yielded += 1
                if max_items and yielded >= max_items:
                    return
            if (payload or {}).get("isLast"):
                return
            start += len(items)

    def iter_cycles(self, max_cycles: int = 0) -> Iterator[Dict[str, Any]]:
        yield from self._paginate(f"/project/{self.project}/testcycle", max_cycles)

    def iter_test_cases(self, max_cases: int = 0) -> Iterator[Dict[str, Any]]:
        """Every test case in the project, for the coverage denominator.

        The project-level list carries ``automationStatus``; the *cycle*-level
        list does not, which is why coverage needs its own pass rather than
        riding along on the run poll.
        """
        yield from self._paginate(f"/project/{self.project}/testcase", max_cases)

    def cycle_case_keys(self, since: Optional[str], max_cycles: int = 0,
                        release_ids: Sequence[int] = (),
                        release_names: Sequence[str] = (),
                        ) -> Tuple[Set[str], Dict[str, str]]:
        """Test case keys that appear in the cycles inside the window.

        This is the scope that makes coverage answerable. The project inventory
        is 10,515 cases and includes everything ever written, most of it retired
        or belonging to another release; a coverage figure over that answers a
        question about the archive, not about the work being reported on.
        Scoping to the cycles actually in flight answers "of what we are
        currently testing, how much is automated".

        When a release is given, the window is ignored: a release is a scope in
        its own right, and intersecting it with "the last 60 days" would silently
        drop the cycles opened before the reporting window that are still the
        release's own test evidence.

        Returns ``(case_keys, {cycle_key: how_it_matched})``.
        """
        case_keys: Set[str] = set()
        matched: Dict[str, str] = {}
        scoped_to_release = bool(release_ids or release_names)
        for cycle in self.iter_cycles(max_cycles):
            key = cycle.get("key")
            if not key:
                continue
            if scoped_to_release:
                how = cycle_release_match(cycle, release_ids, release_names)
                if how is None:
                    continue
            else:
                if not cycle_in_window(cycle, since):
                    continue
                how = "window"
            matched[key] = how
            for entry in self.iter_cycle_cases(key):
                case_key = (entry.get("testCase") or {}).get("key")
                if case_key:
                    case_keys.add(case_key)
        return case_keys, matched

    def build_case_event(self, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        test_case = entry.get("testCase") or entry
        case_key = test_case.get("key")
        if not case_key:
            return None

        status = test_case.get("automationStatus")
        if isinstance(status, dict):
            status = status.get("name")
        # Passed through verbatim, NULL included. An unset field is not "not
        # automated" -- roughly half this estate has never had it set -- so the
        # coverage metric must divide by the known-status population and say so.
        automation_status = str(status) if status not in (None, "") else None

        case_status = test_case.get("status")
        if isinstance(case_status, dict):
            case_status = case_status.get("name")
        script_type = test_case.get("scriptType")
        if isinstance(script_type, dict):
            script_type = script_type.get("name")

        updated = epoch_to_rfc3339(test_case.get("updatedDate"))
        created = epoch_to_rfc3339(test_case.get("createdDate"))

        attributes = {
            "test_case_key": case_key,
            "automation_status": automation_status,
            "automation_owner_person_id": test_case.get("automationOwnerID"),
            "has_automation_key": bool(test_case.get("automationKey")),
            "test_case_status": case_status,
            "script_type": script_type,
            "folder_name": folder_name(test_case),
            "priority": priority_name(test_case),
            "is_archived": bool(test_case.get("isArchived")),
            "created_at": created,
            "updated_at": updated,
        }
        return build_event(
            event_type="test.case.snapshot",
            event_time=updated or created,
            # updated_at is in the key so an inventory re-poll after an edit
            # produces a new row rather than silently overwriting the old state.
            natural_key=(case_key, updated),
            attributes=attributes,
            actor=make_actor(person_id=test_case.get("ownedByID")),
            context=make_context(),
            agent=make_agent(AGENT_NAME),
            link=make_link("heuristic", 0.0),
        )

    def iter_cycle_cases(self, cycle_key: str) -> Iterator[Dict[str, Any]]:
        """Test cases in a cycle, each with its ``latestRun`` inlined.

        The list endpoint already embeds the latest run, so this is one paged
        call per cycle rather than one call per test case. On a cycle of 200
        cases that is 2 requests instead of 200.
        """
        yield from self._paginate(
            f"/project/{self.project}/testcycle/{cycle_key}/testcase"
        )

    # -- event building -----------------------------------------------------

    def build_event(self, cycle: Dict[str, Any],
                    entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        test_case = entry.get("testCase") or {}
        case_key = test_case.get("key")
        if not case_key:
            return None
        run = entry.get("latestRun") or {}
        status_name = ((run.get("testRunStatus") or {}).get("name")) or None
        category = status_category(status_name)

        executed_at = epoch_to_rfc3339(run.get("updatedDate"))
        if category == "not_run":
            # A seeded, never-executed row has an updatedDate from when the
            # cycle was built. Reporting that as an execution time would invent
            # activity that did not happen.
            executed_at = None

        # event_time falls back to the cycle timestamp so an unexecuted row
        # still lands in a defensible week rather than at the epoch.
        event_time = (
            executed_at
            or epoch_to_rfc3339(cycle.get("updatedDate"))
            or epoch_to_rfc3339(cycle.get("createdDate"))
        )

        executed_by = run.get("executedByID") if category != "not_run" else None
        assigned_to = entry.get("assignedToID") or run.get("assigneeToID")

        attributes = {
            "test_case_key": case_key,
            "test_cycle_key": cycle.get("key"),
            "test_run_id": run.get("ID"),
            "status": status_name or "Not Run",
            "status_category": category,
            "is_automated": bool(run.get("isAutomated")),
            "executed_by_person_id": executed_by,
            "assigned_to_person_id": assigned_to,
            "executed_at": executed_at,
            "effort_seconds": effort_seconds(run),
            "defect_count": defect_count(run),
            "folder_name": folder_name(test_case),
            "priority": priority_name(test_case),
        }

        # The actor is whoever ran it; for an unexecuted row it is whoever it is
        # assigned to, and if it is neither, it stays null rather than being
        # attributed to the cycle owner (AR-1: never manufacture attribution).
        actor_id = executed_by or assigned_to

        return build_event(
            event_type="test.run.completed",
            event_time=event_time,
            natural_key=(cycle.get("key"), case_key, run.get("ID"), executed_at),
            attributes=attributes,
            actor=make_actor(person_id=actor_id),
            context=make_context(
                jira_issue_key=None,
                repo_full_name=None,
            ),
            agent=make_agent(AGENT_NAME),
            # A test run is observed, never explicitly bound to an agent run.
            link=make_link("heuristic", 0.0),
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def probe(client: HttpClient, base_url: str, project: str) -> int:
    """One call per candidate endpoint, reporting rather than raising."""
    base = f"{base_url.rstrip('/')}/aio-tcms/api/v1"
    checks = [
        ("list test cycles", f"{base}/project/{project}/testcycle"),
    ]
    print(f"# AIO TCMS reachability -- project {project}\n")
    ok = True
    for name, url in checks:
        response = client.probe(url, params={"startAt": 0, "maxResults": 1})
        status = response.status
        verdict = "OK" if 200 <= status < 300 else "FAIL"
        if verdict == "FAIL":
            ok = False
        print(f"- **{name}** — HTTP {status} — {verdict}")
        if status == 401:
            body = (response.data or b"")[:200].decode("utf-8", "replace")
            if "not enabled for this project" in body:
                print("  - AIO Tests is not enabled for this Jira project. This is a "
                      "project configuration problem, not a bad token — the same "
                      "token may work for another project.")
            else:
                print("  - The token was rejected. AIO issues its own key; the Jira "
                      "API token does not work here.")
    print()
    print("Result: " + ("reachable" if ok else "NOT reachable"))
    return 0 if ok else 4


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Emit test.run.completed telemetry events from AIO TCMS.")
    ap.add_argument("--project", required=True, help="Jira project key, e.g. PRJ")
    ap.add_argument("--since", help="ISO8601 window start (overrides the watermark)")
    ap.add_argument("--out", help="NDJSON output file (default: stdout)")
    ap.add_argument("--probe", action="store_true",
                    help="One call per endpoint; report reachability and exit")
    ap.add_argument("--lookback-days", type=int, default=30)
    ap.add_argument("--state-file", help="Watermark JSON path")
    ap.add_argument("--no-watermark", action="store_true")
    ap.add_argument("--max-cycles", type=int, default=0)
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--include-not-run", action="store_true",
                    help="Emit rows for test cases that have never been executed. "
                         "Off by default: AIO seeds every new cycle with them, so "
                         "including them inflates volume with cycle planning.")
    ap.add_argument("--progress-every", type=int, default=0, metavar="N",
                    help="Print a progress line to stderr every N cycles")
    ap.add_argument("--coverage", action="store_true",
                    help="Emit test.case.snapshot instead of test runs. This is "
                         "the Automation Coverage denominator: a case nobody has "
                         "executed emits no run, and those are exactly the "
                         "un-automated ones.")
    ap.add_argument("--priority", action="append", default=[], metavar="NAME",
                    help="Repeatable. Restrict coverage to these AIO test-case "
                         "priorities. On this project P1=High, P2=Medium, "
                         "P3=Low. Omit for every priority.")
    ap.add_argument("--release", action="append", default=[], metavar="TEXT",
                    help="Repeatable. Scope to cycles whose title contains this "
                         "(e.g. 26.8). Combined with --release-id as OR.")
    ap.add_argument("--release-id", action="append", default=[], type=int,
                    metavar="ID",
                    help="Repeatable. Scope to cycles carrying this Jira release "
                         "id. Structured and exact; prefer it over --release.")
    ap.add_argument("--coverage-scope", choices=("cycles", "project"),
                    default="cycles",
                    help="cycles (default): only cases that appear in a cycle "
                         "inside the window — 'of what we are testing now, how "
                         "much is automated'. project: the entire inventory, "
                         "including retired cases from past releases.")
    ap.add_argument("--max-cases", type=int, default=0)
    ap.add_argument("--pace", type=float, default=0.0, metavar="SECONDS",
                    help="Sleep this long between requests. AIO rate-limits far "
                         "more aggressively than Bitbucket or Jira; a full "
                         "inventory pass needs roughly 0.5.")
    ap.add_argument("--max-retries", type=int, default=8,
                    help="AIO returns 429 readily, so this defaults higher than "
                         "the shared client's 5.")
    args = ap.parse_args(argv)

    try:
        config = Config.from_env()
        if args.state_file:
            config.state_path = args.state_file
        base_url, token = config.require_aio()
    except ConfigError as exc:
        return fail(str(exc))

    client = HttpClient(
        auth_header=f"AioAuth {token}",
        max_retries=max(args.max_retries, config.max_retries),
        timeout=config.timeout_seconds,
    )
    if args.pace > 0:
        poller_sleep = args.pace

        original_request = client.request

        def paced(*a, **kw):
            result = original_request(*a, **kw)
            time.sleep(poller_sleep)
            return result

        client.request = paced  # type: ignore[method-assign]

    if args.probe:
        return probe(client, base_url, args.project)

    poller = AioPoller(client, base_url, args.project, page_size=args.page_size)

    if args.coverage:
        coverage_since = args.since or (
            None if args.no_watermark
            else default_since(WatermarkStore(config.state_path)
                               .get(f"aio:testruns:{args.project}"),
                               args.lookback_days))
        return _run_coverage(poller, client, args, coverage_since)

    store = WatermarkStore(config.state_path)
    watermark_key = f"aio:testruns:{args.project}"
    since = args.since or (
        None if args.no_watermark
        else default_since(store.get(watermark_key), args.lookback_days)
    )
    if since is None:
        since = default_since(None, args.lookback_days)

    counts = {"cycles_seen": 0, "cycles_in_window": 0, "test_cases": 0,
              "executed": 0, "not_run_skipped": 0, "events": 0}
    try:
        with NdjsonWriter(args.out) as writer:
            if args.no_watermark:
                _run(poller, writer, since, args, counts, None)
            else:
                with store.checkpoint(watermark_key) as mark:
                    _run(poller, writer, since, args, counts, mark)
            counts["events"] = writer.count
    except HttpError as exc:
        log("aio_api_error", status=exc.status, url=exc.url, hint=error_hint(exc.status))
        return 3
    except KeyboardInterrupt:
        log("interrupted", hint="watermark not advanced")
        return 130

    log("aio_poll_complete", project=args.project, since=since,
        requests=client.request_count, retries=client.retry_count, **counts)
    return 0


def error_hint(status: int) -> str:
    """What to actually do about an AIO error, keyed on the status.

    A single catch-all hint is worse than none: the first version told a reader
    hitting 429 to check their token.
    """
    if status == 429:
        return ("AIO rate-limited the poller and the retries were exhausted. "
                "Re-run with a larger --pace (seconds between requests) or a "
                "smaller --page-size; the watermark was not advanced, so a "
                "re-run resumes the same window safely.")
    if status in (401, 403):
        return ("AIO_API_TOKEN is wrong, or AIO Tests is not enabled for this "
                "Jira project. Run --probe: it distinguishes the two, and they "
                "need different people to fix them.")
    if status == 404:
        return "Endpoint or project key not found. Check --project."
    return "Watermark not advanced; a re-run resumes the same window."


def _run_coverage(poller: AioPoller, client: HttpClient, args,
                  since: Optional[str] = None) -> int:
    """Emit one test.case.snapshot per in-scope test case.

    No watermark: this is an inventory position, and an incremental inventory
    would drop exactly the stale, never-touched cases that drag coverage down.
    """
    counts = {"test_cases": 0, "events": 0, "automated": 0,
              "to_be_automated": 0, "status_unset": 0, "archived": 0,
              "out_of_scope": 0}
    wanted_priorities = {p.strip().lower() for p in args.priority if p.strip()}
    counts["priority_filtered"] = 0
    scope_keys: Optional[Set[str]] = None
    matched: Dict[str, str] = {}
    scoped_to_release = bool(args.release or args.release_id)
    if args.coverage_scope == "cycles" or scoped_to_release:
        scope_keys, matched = poller.cycle_case_keys(
            since, args.max_cycles, args.release_id, args.release)
        by_route: Dict[str, int] = {}
        for how in matched.values():
            by_route[how] = by_route.get(how, 0) + 1
        log("aio_coverage_scope",
            scope="release" if scoped_to_release else "cycles",
            since=None if scoped_to_release else since,
            release=args.release or None, release_id=args.release_id or None,
            cycles=len(matched), matched_by=by_route,
            cycles_matched_by_title_only=sorted(
                k for k, v in matched.items() if v == "title"),
            test_cases_in_scope=len(scope_keys),
            priorities=sorted(wanted_priorities) or "all",
            hint=("Cycles listed under cycles_matched_by_title_only carry no Jira "
                  "release id; they were included on a free-text title match. "
                  "Set the release on them in AIO so the scope stops depending "
                  "on spelling." if by_route.get("title") else None))
        if not scope_keys:
            log("aio_coverage_empty_scope",
                hint="No cycle matched. Check --release / --release-id, widen "
                     "--since, or use --coverage-scope project.")
            return 0
    try:
        with NdjsonWriter(args.out) as writer:
            for entry in poller.iter_test_cases(args.max_cases):
                test_case = entry.get("testCase") or entry
                if scope_keys is not None and test_case.get("key") not in scope_keys:
                    counts["out_of_scope"] += 1
                    continue
                if wanted_priorities:
                    name = (priority_name(test_case) or "").strip().lower()
                    if name not in wanted_priorities:
                        counts["priority_filtered"] += 1
                        continue
                counts["test_cases"] += 1
                event = poller.build_case_event(entry)
                if event is None:
                    continue
                attrs = event["attributes"]
                status = (attrs["automation_status"] or "").strip().lower()
                if not status:
                    counts["status_unset"] += 1
                elif status == "automated":
                    counts["automated"] += 1
                elif status.startswith("to be"):
                    counts["to_be_automated"] += 1
                if attrs["is_archived"]:
                    counts["archived"] += 1
                writer.write(event)
                if (args.progress_every
                        and counts["test_cases"] % args.progress_every == 0):
                    print(f"  ... {counts['test_cases']} test cases",
                          file=sys.stderr)
            counts["events"] = writer.count
    except HttpError as exc:
        log("aio_api_error", status=exc.status, url=exc.url,
            test_cases_read=counts["test_cases"], hint=error_hint(exc.status))
        return 3
    except KeyboardInterrupt:
        log("interrupted")
        return 130

    known = counts["automated"] + counts["to_be_automated"]
    log("aio_coverage_complete", project=args.project,
        scope="release" if scoped_to_release else args.coverage_scope,
        cycles_in_scope=len(matched),
        priorities=sorted(wanted_priorities) or "all",
        requests=client.request_count, retries=client.retry_count,
        coverage_pct=(round(100 * counts["automated"] / known, 1)
                      if known else None),
        coverage_denominator=known,
        hint="coverage_pct divides by cases with a KNOWN automation status. "
             f"{counts['status_unset']} cases have none set — an unset field is "
             "not 'not automated', and including them would measure how "
             "diligently the field is filled in.",
        **counts)
    return 0


def _run(poller: AioPoller, writer: NdjsonWriter, since: Optional[str],
         args, counts: Dict[str, int], mark) -> None:
    for cycle in poller.iter_cycles(args.max_cycles):
        counts["cycles_seen"] += 1
        if not cycle_in_window(cycle, since):
            continue
        counts["cycles_in_window"] += 1
        cycle_key = cycle.get("key")
        if not cycle_key:
            continue
        for entry in poller.iter_cycle_cases(cycle_key):
            counts["test_cases"] += 1
            event = poller.build_event(cycle, entry)
            if event is None:
                continue
            category = event["attributes"]["status_category"]
            if category == "not_run" and not args.include_not_run:
                counts["not_run_skipped"] += 1
                continue
            if category in EXECUTED_CATEGORIES:
                counts["executed"] += 1
            writer.write(event)
        if args.progress_every and counts["cycles_in_window"] % args.progress_every == 0:
            print(f"  ... {counts['cycles_in_window']} cycles processed",
                  file=sys.stderr)
        if mark is not None:
            mark.propose(epoch_to_rfc3339(cycle.get("updatedDate")))


if __name__ == "__main__":
    raise SystemExit(main())
