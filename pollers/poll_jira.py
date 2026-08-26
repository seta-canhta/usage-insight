#!/usr/bin/env python3
"""Jira Cloud backfill poller -- workflow events for AI telemetry.

Emits CONTRACT.md §3 event 21 (``jira.transition``), each carrying an issue
snapshot (status, assignee accountId, issue type, labels, parent, estimates) in
its attributes.

Usage::

    python poll_jira.py --project PRJ [--since 2026-08-01T00:00:00Z] [--out events.ndjson]

Auth: HTTP **Basic** with ``JIRA_URL`` / ``JIRA_USERNAME`` / ``JIRA_API_TOKEN``,
the pattern already used by ``skills/qualdev/jira-attach/SKILL.md`` (REST v3).

Two things this poller is careful about
---------------------------------------
1. **Identity.** ``person_id`` is the Atlassian ``accountId`` and nothing else.
   Display names and email addresses are never emitted -- design §9.4 measured
   real collisions on this repository ("Ann Lee" vs "Lee, Ann",
   "Bob Smith" vs "Bob Smtih", a committer with no address at all), so
   a name-keyed rollup splits one engineer across three rows.
2. **The qd_jira_key attribution hazard (design §4.1, AR-3).**
   ``supervisor-test-spec.agent.md:1518`` sends *all* delivery comments and
   label updates (``PLANNED_BY_COPILOT``, ``AUTH_BY_COPILOT``) to a separate
   QualDev delivery ticket instead of the feature ticket. The ticket wearing the
   AI label is therefore often **not** the ticket describing the work. This
   poller never resolves that itself; it emits the issue links so the transform
   can apply AR-3 (attribute to the feature ticket, keep
   ``delivery_ticket_key`` separately) instead of double-counting one run across
   two tickets.

Snapshot note: CONTRACT.md §3 is a **closed enum** and the collector rejects
unknown ``event_type`` values, so there is no ``jira.issue.snapshot`` event. The
snapshot rides on the ``jira.transition`` events as ``attributes.issue``, and
every issue yields at least one transition because issue creation is
synthesised as the transition into its first status (Jira's changelog does not
record it).
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

# The shared library sits at the repository root, one level up: it is
# depended on by `cli/`, `importers/` and `report/` too, so it cannot
# live inside one of its consumers.
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import (  # noqa: E402
    AI_LABELS,
    AI_LABEL_AUTHORED,
    AI_LABEL_GENERATED,
    AI_LABEL_PLANNED,
    AI_LABEL_REVIEWED,
    Config,
    ConfigError,
    HttpClient,
    HttpError,
    NdjsonWriter,
    WatermarkStore,
    build_event,
    default_since,
    deterministic_id,
    fail,
    log,
    make_actor,
    make_agent,
    make_context,
    make_link,
    ms_between,
    parse_ts,
    to_rfc3339,
    unrecognised_ai_labels,
)

AGENT_NAME = "poller.jira"

DEFAULT_FIELDS = [
    "status",
    "assignee",
    "reporter",
    "creator",
    "issuetype",
    "labels",
    "parent",
    "project",
    "created",
    "updated",
    "resolutiondate",
    "timeoriginalestimate",
    "timeestimate",
    "timespent",
    "issuelinks",
    "subtasks",
    "priority",
]

#: Link types that plausibly connect a QualDev delivery ticket to the feature
#: ticket it delivers. Ordered by how strongly they imply it.
FEATURE_LINK_TYPES = (
    "tests",
    "is tested by",
    "implements",
    "is implemented by",
    "blocks",
    "is blocked by",
    "causes",
    "is caused by",
    "relates",
    "relates to",
    "duplicates",
)

#: Issue-type names that read as a QualDev delivery/tracking ticket rather than
#: the feature itself. Extendable via --delivery-issue-type.
DEFAULT_DELIVERY_ISSUE_TYPES = ("qualdev", "test", "test execution", "delivery", "task")

_STATUS_CATEGORY_FALLBACK = {
    "to do": "new",
    "open": "new",
    "backlog": "new",
    "new": "new",
    "in progress": "indeterminate",
    "in review": "indeterminate",
    "in development": "indeterminate",
    "blocked": "indeterminate",
    "done": "done",
    "closed": "done",
    "resolved": "done",
    "cancelled": "done",
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def account_id_of(user: Optional[Dict[str, Any]]) -> Optional[str]:
    """Atlassian accountId, or None. Never a displayName, never an address."""
    if not isinstance(user, dict):
        return None
    account_id = user.get("accountId")
    return account_id if isinstance(account_id, str) and account_id else None


def status_category(name: Optional[str], lookup: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Map a status name to a bounded category (``new``/``indeterminate``/``done``)."""
    if not name:
        return None
    if lookup:
        found = lookup.get(name.strip().lower())
        if found:
            return found
    return _STATUS_CATEGORY_FALLBACK.get(name.strip().lower())


def ai_labels_on(labels: Sequence[str]) -> List[str]:
    """The closed-set provenance labels present, in ``AI_LABELS`` order.

    Anything outside the closed set is NOT returned here -- see
    ``unrecognised_ai_labels`` for the drift signal.
    """
    upper = {str(label).strip().upper() for label in labels or []}
    return [label for label in AI_LABELS if label in upper]


def linked_issues_of(fields: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten ``fields.issuelinks`` into ``{issue_key, link_type, direction}``."""
    links: List[Dict[str, Any]] = []
    for link in fields.get("issuelinks") or []:
        if not isinstance(link, dict):
            continue
        link_type = (link.get("type") or {}).get("name")
        for direction, node in (("outward", link.get("outwardIssue")), ("inward", link.get("inwardIssue"))):
            if isinstance(node, dict) and node.get("key"):
                links.append(
                    {
                        "issue_key": node.get("key"),
                        "link_type": link_type,
                        "link_label": (link.get("type") or {}).get(direction),
                        "direction": direction,
                    }
                )
    return links


def resolve_attribution(
    issue_key: str,
    fields: Dict[str, Any],
    delivery_projects: Sequence[str] = (),
    delivery_issue_types: Sequence[str] = DEFAULT_DELIVERY_ISSUE_TYPES,
) -> Dict[str, Any]:
    """Emit everything AR-3 needs, without deciding AR-3 here.

    The transform owns the resolution (CONTRACT.md §6 says attribution rules are
    *enforced in the transform layer, not by convention*). This function's job
    is to make the delivery-ticket→feature-ticket edge visible, and to be honest
    about how confident the pairing is.
    """
    labels = fields.get("labels") or []
    present = ai_labels_on(labels)
    drifted = unrecognised_ai_labels(labels)
    project_key = (fields.get("project") or {}).get("key") or (
        issue_key.split("-", 1)[0] if "-" in issue_key else None
    )
    issue_type = ((fields.get("issuetype") or {}).get("name") or "").strip()
    parent_key = (fields.get("parent") or {}).get("key")
    links = linked_issues_of(fields)

    looks_delivery = bool(
        (project_key and project_key.upper() in {p.upper() for p in delivery_projects})
        or (issue_type and issue_type.lower() in {t.lower() for t in delivery_issue_types})
    )

    feature_key: Optional[str] = None
    source: Optional[str] = None
    confidence = 0.0

    # Cross-project links first -- a QualDev ticket delivering an PRJ feature is
    # the shape the hazard actually takes.
    def _other_project(key: Optional[str]) -> bool:
        return bool(key and project_key and not key.startswith(f"{project_key}-"))

    for wanted in FEATURE_LINK_TYPES:
        for link in links:
            if (link.get("link_type") or "").strip().lower() != wanted:
                continue
            if _other_project(link.get("issue_key")):
                feature_key, source = link["issue_key"], f"issue_link:{link['link_type']}"
                confidence = 0.7
                break
            if feature_key is None:
                feature_key, source = link["issue_key"], f"issue_link:{link['link_type']}"
                confidence = 0.4
        if confidence >= 0.7:
            break

    if parent_key and (feature_key is None or _other_project(parent_key)):
        feature_key, source = parent_key, "parent"
        confidence = max(confidence, 0.6 if _other_project(parent_key) else 0.5)

    if feature_key is None and links:
        feature_key = links[0]["issue_key"]
        source = f"issue_link:{links[0].get('link_type')}"
        confidence = 0.2

    is_delivery_candidate = bool(present and feature_key and (looks_delivery or _other_project(feature_key)))

    return {
        "rule": "AR-3",
        "ai_labels": present,
        "has_ai_labels": bool(present),
        "label_authored_by_ai": AI_LABEL_AUTHORED in present,
        "label_planned_by_ai": AI_LABEL_PLANNED in present,
        "label_generated_by_ai": AI_LABEL_GENERATED in present,
        # Applied by an external AI code-review system, not by anything in this
        # repository -- so its absence says nothing about whether review happened.
        "label_reviewed_by_ai": AI_LABEL_REVIEWED in present,
        # DQ signal, not a classification. Labels shaped like a provenance marker
        # but outside the closed set (e.g. PLANNER_BY_COPILOT, DEV_BY_COPILOT,
        # COPILOT_TESTING). Each one is AI work that is NOT being counted, so the
        # names must reach a report rather than be dropped silently.
        "unrecognised_ai_labels": drifted,
        "has_ai_label_drift": bool(drifted),
        "is_delivery_ticket_candidate": is_delivery_candidate,
        "delivery_ticket_key": issue_key if is_delivery_candidate else None,
        "feature_ticket_key": feature_key if is_delivery_candidate else None,
        "feature_ticket_source": source if is_delivery_candidate else None,
        "resolution_confidence": round(confidence, 2) if is_delivery_candidate else 0.0,
        "linked_issues": links,
        "parent_key": parent_key,
        "note": (
            "Labels may sit on the delivery ticket rather than the feature ticket "
            "(design §4.1). The transform applies AR-3; this is evidence, not a decision."
        )
        if is_delivery_candidate
        else None,
    }


def build_snapshot(
    issue: Dict[str, Any], category_lookup: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """Issue snapshot -- identifiers, enums and counts only.

    Summary and description are content and are neither requested nor emitted
    (CONTRACT.md §1 rule 1).
    """
    fields = issue.get("fields") or {}
    status_name = (fields.get("status") or {}).get("name")
    category = ((fields.get("status") or {}).get("statusCategory") or {}).get("key")
    labels = [str(label) for label in (fields.get("labels") or [])]
    return {
        "issue_key": issue.get("key"),
        "issue_id": issue.get("id"),
        "jira_project_key": (fields.get("project") or {}).get("key"),
        "issue_type": (fields.get("issuetype") or {}).get("name"),
        "is_subtask": bool((fields.get("issuetype") or {}).get("subtask")),
        "status": status_name,
        "status_category": category or status_category(status_name, category_lookup),
        "priority": (fields.get("priority") or {}).get("name"),
        "assignee_person_id": account_id_of(fields.get("assignee")),
        "reporter_person_id": account_id_of(fields.get("reporter")),
        "creator_person_id": account_id_of(fields.get("creator")),
        "labels": labels,
        "label_count": len(labels),
        "parent_key": (fields.get("parent") or {}).get("key"),
        "subtask_keys": [s.get("key") for s in (fields.get("subtasks") or []) if s.get("key")],
        "estimate_original_seconds": fields.get("timeoriginalestimate"),
        "estimate_remaining_seconds": fields.get("timeestimate"),
        "time_spent_seconds": fields.get("timespent"),
        "created_at": to_rfc3339(fields.get("created")),
        "updated_at": to_rfc3339(fields.get("updated")),
        "resolved_at": to_rfc3339(fields.get("resolutiondate")),
    }


def extract_status_transitions(histories: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pull status changes out of a changelog, oldest first."""
    transitions: List[Dict[str, Any]] = []
    for history in histories or []:
        when = to_rfc3339(history.get("created"))
        author = account_id_of(history.get("author"))
        for item in history.get("items") or []:
            if str(item.get("field") or "").lower() != "status":
                continue
            transitions.append(
                {
                    "history_id": history.get("id"),
                    "transitioned_at": when,
                    "author_person_id": author,
                    "from_status": item.get("fromString"),
                    "to_status": item.get("toString"),
                    "from_status_id": item.get("from"),
                    "to_status_id": item.get("to"),
                }
            )
    transitions.sort(key=lambda t: (parse_ts(t["transitioned_at"]) or parse_ts("1970-01-01T00:00:00Z")))
    return transitions


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------


class JiraPoller:
    def __init__(
        self,
        client: HttpClient,
        base_url: str,
        config: Optional[Config] = None,
        page_size: int = 50,
        search_api: str = "auto",
        delivery_projects: Sequence[str] = (),
        delivery_issue_types: Sequence[str] = DEFAULT_DELIVERY_ISSUE_TYPES,
    ) -> None:
        self.client = client
        self.base = base_url.rstrip("/")
        self.config = config or Config()
        self.page_size = page_size
        self.search_api = search_api
        self.delivery_projects = list(delivery_projects)
        self.delivery_issue_types = list(delivery_issue_types)
        self._category_lookup: Optional[Dict[str, str]] = None

    # -- fetching ------------------------------------------------------------

    def status_category_lookup(self) -> Dict[str, str]:
        """One call to /rest/api/3/status, cached, mapping name -> category key."""
        if self._category_lookup is not None:
            return self._category_lookup
        lookup: Dict[str, str] = {}
        try:
            payload = self.client.get_json(f"{self.base}/rest/api/3/status")
            for status in payload or []:
                name = (status.get("name") or "").strip().lower()
                key = (status.get("statusCategory") or {}).get("key")
                if name and key:
                    lookup[name] = key
        except HttpError as exc:
            log("jira_status_lookup_unavailable", status=exc.status, url=exc.url)
        self._category_lookup = lookup
        return lookup

    def build_jql(self, project: str, since: Optional[str]) -> str:
        clauses = [f'project = "{project}"']
        if since:
            moment = parse_ts(since)
            if moment:
                clauses.append(f'updated >= "{moment.strftime("%Y-%m-%d %H:%M")}"')
        return " AND ".join(clauses) + " ORDER BY updated ASC"

    def iter_issues(self, jql: str, max_issues: int = 0) -> Iterator[Dict[str, Any]]:
        """Search issues with the changelog expanded, following pagination.

        Jira Cloud is mid-migration from ``GET /rest/api/3/search`` to
        ``POST /rest/api/3/search/jql``. Neither has been exercised against this
        organisation's instance, so both are implemented and the newer one is
        tried first (``--search-api`` forces either).
        """
        emitted = 0
        if self.search_api in ("auto", "jql"):
            try:
                for issue in self._iter_issues_jql(jql, max_issues):
                    emitted += 1
                    yield issue
                return
            except HttpError as exc:
                if self.search_api == "jql" or exc.status not in (404, 405, 410):
                    raise
                if emitted:
                    raise
                log(
                    "jira_search_jql_unavailable",
                    status=exc.status,
                    hint="falling back to the legacy GET /rest/api/3/search endpoint",
                )
        for issue in self._iter_issues_legacy(jql, max_issues):
            yield issue

    def _iter_issues_jql(self, jql: str, max_issues: int) -> Iterator[Dict[str, Any]]:
        token: Optional[str] = None
        seen = 0
        while True:
            payload = {
                "jql": jql,
                "maxResults": self.page_size,
                "fields": DEFAULT_FIELDS,
                "expand": "changelog",
            }
            if token:
                payload["nextPageToken"] = token
            data = self.client.request(
                "POST", f"{self.base}/rest/api/3/search/jql", json_body=payload
            ).json()
            issues = data.get("issues") or []
            for issue in issues:
                yield issue
                seen += 1
                if max_issues and seen >= max_issues:
                    return
            token = data.get("nextPageToken")
            if not token or data.get("isLast") or not issues:
                return

    def _iter_issues_legacy(self, jql: str, max_issues: int) -> Iterator[Dict[str, Any]]:
        start_at = 0
        while True:
            data = self.client.get_json(
                f"{self.base}/rest/api/3/search",
                {
                    "jql": jql,
                    "startAt": start_at,
                    "maxResults": self.page_size,
                    "expand": "changelog",
                    "fields": ",".join(DEFAULT_FIELDS),
                },
            )
            issues = data.get("issues") or []
            for issue in issues:
                yield issue
                start_at += 1
                if max_issues and start_at >= max_issues:
                    return
            if not issues:
                return
            total = data.get("total")
            if total is not None and start_at >= int(total):
                return

    def changelog_histories(self, issue: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Full changelog, refetching when ``expand=changelog`` truncated it.

        ``expand=changelog`` caps at the instance's page size; a long-lived
        ticket loses its earliest transitions without this.
        """
        changelog = issue.get("changelog") or {}
        histories = list(changelog.get("histories") or [])
        total = changelog.get("total")
        if total is None or len(histories) >= int(total):
            return histories

        key = issue.get("key")
        collected: List[Dict[str, Any]] = []
        start_at = 0
        while True:
            data = self.client.get_json(
                f"{self.base}/rest/api/3/issue/{key}/changelog",
                {"startAt": start_at, "maxResults": 100},
            )
            values = data.get("values") or data.get("histories") or []
            collected.extend(values)
            start_at += len(values)
            if not values or data.get("isLast") or (
                data.get("total") is not None and start_at >= int(data["total"])
            ):
                break
        return collected or histories

    # -- event construction --------------------------------------------------

    def build_issue_events(self, issue: Dict[str, Any]) -> List[Dict[str, Any]]:
        fields = issue.get("fields") or {}
        key = issue.get("key")
        lookup = self._category_lookup or {}
        snapshot = build_snapshot(issue, lookup)
        attribution = resolve_attribution(
            key, fields, self.delivery_projects, self.delivery_issue_types
        )
        histories = self.changelog_histories(issue)
        transitions = extract_status_transitions(histories)

        created_at = to_rfc3339(fields.get("created"))
        agent = make_agent(AGENT_NAME)
        context = make_context(jira_issue_key=key)
        # Labels are markers, not run ids: this is L3 marker-only linkage
        # (design §5.3) and must never be treated as an explicit link.
        link = make_link("marker_only", 0.3) if attribution["has_ai_labels"] else make_link(
            "heuristic", 0.5
        )
        trace_id = deterministic_id("trc", "jira", key)

        events: List[Dict[str, Any]] = []

        # Synthesised creation transition: Jira's changelog never records the
        # move into the initial status, so cycle time would silently lose its
        # start point.
        initial_status = transitions[0]["from_status"] if transitions else snapshot["status"]
        events.append(
            self._transition_event(
                key,
                {
                    "history_id": None,
                    "transitioned_at": created_at,
                    "author_person_id": snapshot["reporter_person_id"]
                    or snapshot["creator_person_id"],
                    "from_status": None,
                    "to_status": initial_status,
                    "from_status_id": None,
                    "to_status_id": None,
                },
                snapshot,
                attribution,
                context,
                agent,
                link,
                trace_id,
                lookup,
                is_synthesised=True,
                created_at=created_at,
            )
        )

        for transition in transitions:
            events.append(
                self._transition_event(
                    key,
                    transition,
                    snapshot,
                    attribution,
                    context,
                    agent,
                    link,
                    trace_id,
                    lookup,
                    is_synthesised=False,
                    created_at=created_at,
                )
            )
        return events

    def _transition_event(
        self,
        key: str,
        transition: Dict[str, Any],
        snapshot: Dict[str, Any],
        attribution: Dict[str, Any],
        context: Dict[str, Any],
        agent: Dict[str, Any],
        link: Dict[str, Any],
        trace_id: str,
        lookup: Dict[str, str],
        is_synthesised: bool,
        created_at: Optional[str],
    ) -> Dict[str, Any]:
        to_status = transition.get("to_status")
        return build_event(
            "jira.transition",
            transition.get("transitioned_at"),
            (
                key,
                transition.get("history_id") or "created",
                transition.get("from_status"),
                to_status,
                transition.get("transitioned_at"),
            ),
            {
                "jira_issue_key": key,
                "from_status": transition.get("from_status"),
                "to_status": to_status,
                "transitioned_at": transition.get("transitioned_at"),
                "status_category": status_category(to_status, lookup),
                "from_status_category": status_category(
                    transition.get("from_status"), lookup
                ),
                "is_synthesised_creation": is_synthesised,
                "age_at_transition_ms": ms_between(
                    created_at, transition.get("transitioned_at")
                ),
                "issue": snapshot,
                "attribution": attribution,
            },
            actor=make_actor(person_id=transition.get("author_person_id")),
            context=context,
            agent=agent,
            link=link,
            trace_id=trace_id,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="poll_jira.py",
        description="Emit jira.transition telemetry events (with issue snapshots) from Jira Cloud.",
    )
    parser.add_argument("--project", required=True, help="Jira project key, e.g. PRJ")
    parser.add_argument("--since", help="ISO8601 window start (overrides the watermark)")
    parser.add_argument("--out", help="NDJSON output file (default: stdout)")
    parser.add_argument("--jql", help="Full JQL override (--project still names the watermark)")
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--state-file", help="Watermark JSON path")
    parser.add_argument("--no-watermark", action="store_true")
    parser.add_argument("--max-issues", type=int, default=0)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument(
        "--search-api",
        choices=("auto", "jql", "legacy"),
        default="auto",
        help="auto: try POST /search/jql then fall back to GET /search",
    )
    parser.add_argument(
        "--delivery-project",
        action="append",
        default=[],
        help="Project key whose issues are QualDev delivery tickets (repeatable, AR-3)",
    )
    parser.add_argument(
        "--delivery-issue-type",
        action="append",
        default=[],
        help="Issue type name that indicates a delivery ticket (repeatable, AR-3)",
    )
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    client: Optional[HttpClient] = None,
    base_url: Optional[str] = None,
) -> int:
    """``client``/``base_url`` are injectable so tests never touch the network."""
    args = build_arg_parser().parse_args(argv)
    config = Config.from_env()
    if args.state_file:
        config.state_path = args.state_file

    if client is None:
        try:
            jira_url, username, token = config.require_jira()
        except ConfigError as exc:
            fail(str(exc), exit_code=2)
        client = HttpClient(
            auth=(username, token),
            max_retries=config.max_retries,
            timeout=config.timeout_seconds,
        )
    else:
        jira_url = base_url or config.jira_url or "https://jira.invalid"
    poller = JiraPoller(
        client,
        jira_url,
        config,
        page_size=args.page_size,
        search_api=args.search_api,
        delivery_projects=args.delivery_project,
        delivery_issue_types=list(DEFAULT_DELIVERY_ISSUE_TYPES) + list(args.delivery_issue_type),
    )
    store = WatermarkStore(config.state_path)
    watermark_key = f"jira:issues:{args.project}"

    since = args.since or (
        None if args.no_watermark else default_since(store.get(watermark_key), args.lookback_days)
    )
    if since is None:
        since = default_since(None, args.lookback_days)
    jql = args.jql or poller.build_jql(args.project, since)

    counts = {"issues": 0, "events": 0, "ai_labelled": 0, "delivery_candidates": 0}
    try:
        poller.status_category_lookup()
        with NdjsonWriter(args.out) as writer:
            if args.no_watermark:
                _run(poller, writer, jql, args, counts, None)
            else:
                with store.checkpoint(watermark_key) as mark:
                    _run(poller, writer, jql, args, counts, mark)
            counts["events"] = writer.count
    except HttpError as exc:
        log(
            "jira_api_error",
            status=exc.status,
            url=exc.url,
            hint="401/403 => check JIRA_URL/JIRA_USERNAME/JIRA_API_TOKEN (Basic auth). "
            "400 usually means the JQL was rejected. Watermark not advanced.",
        )
        return 3
    except KeyboardInterrupt:
        log("interrupted", hint="watermark not advanced")
        return 130

    log(
        "jira_poll_complete",
        project=args.project,
        since=since,
        requests=client.request_count,
        retries=client.retry_count,
        **counts,
    )
    return 0


def _run(poller: JiraPoller, writer: NdjsonWriter, jql: str, args, counts, mark) -> None:
    for issue in poller.iter_issues(jql, args.max_issues):
        for event in poller.build_issue_events(issue):
            writer.write(event)
            attribution = event["attributes"]["attribution"]
            if event["attributes"].get("is_synthesised_creation"):
                if attribution["has_ai_labels"]:
                    counts["ai_labelled"] += 1
                if attribution["is_delivery_ticket_candidate"]:
                    counts["delivery_candidates"] += 1
        counts["issues"] += 1
        if mark is not None:
            mark.propose((issue.get("fields") or {}).get("updated"))


if __name__ == "__main__":
    raise SystemExit(main())
