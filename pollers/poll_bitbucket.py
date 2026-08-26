#!/usr/bin/env python3
"""Bitbucket Cloud backfill poller -- SCM events for AI telemetry.

Emits CONTRACT.md §3 events 16-19::

    scm.pr.reviewed   scm.pr.merged   scm.pr.declined   scm.revert

Usage::

    python poll_bitbucket.py --workspace WS --repo REPO \
        [--since 2026-08-01T00:00:00Z] [--out events.ndjson] \
        [--state OPEN,MERGED,DECLINED]

Auth: HTTP **Basic**, ``BITBUCKET_USERNAME`` + ``BITBUCKET_ACCESS_TOKEN``, exactly
as ``skills/bitbucket-ops/commands.md`` prescribes. Bearer is never used -- it
returns 401 with these credentials.

Endpoints used (REST v2.0)::

    GET /2.0/repositories/{ws}/{repo}/pullrequests?state=...     PR list
    GET /2.0/repositories/{ws}/{repo}/pullrequests/{id}/activity review timeline
    GET /2.0/repositories/{ws}/{repo}/pullrequests/{id}/comments comment counts
    GET /2.0/repositories/{ws}/{repo}/pullrequests/{id}/diffstat churn (paginated)
    GET /2.0/repositories/{ws}/{repo}/pullrequests/{id}/commits  AI markers/trailers
    GET /2.0/repositories/{ws}/{repo}/commits                    revert detection

Never printed, never emitted: credentials, diffs, source, comment text, raw email
addresses. Comment *text* is not even fetched into a field we keep -- only counts
and identifiers leave this process.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

# The shared library sits at the repository root, one level up: it is
# depended on by `cli/`, `importers/` and `report/` too, so it cannot
# live inside one of its consumers.
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import (  # noqa: E402
    Config,
    ConfigError,
    HttpClient,
    HttpError,
    NdjsonWriter,
    REVERTS_COMMIT_RE,
    REVERT_QUOTED_SUBJECT_RE,
    REVERT_SUBJECT_RE,
    WatermarkStore,
    actor_key,
    build_event,
    days_between,
    default_since,
    deterministic_id,
    email_from_raw_author,
    extract_jira_key,
    fail,
    has_ai_commit_marker,
    has_ai_pr_title_marker,
    hash_email,
    log,
    looks_like_bot,
    make_actor,
    make_agent,
    make_context,
    make_link,
    min_ts,
    ms_between,
    paginate,
    parse_ai_trailers,
    parse_ts,
    person_id_of,
    to_rfc3339,
)

AGENT_NAME = "poller.bitbucket"
VALID_STATES = ("OPEN", "MERGED", "DECLINED", "SUPERSEDED")

# Bounded enum for scm.pr.declined.decline_reason_class (CONTRACT.md §1 rule 5).
DECLINE_REASONS = (
    "declined_by_author",
    "changes_requested_unresolved",
    "declined_by_reviewer",
    "unknown",
)


# ---------------------------------------------------------------------------
# Pure derivation helpers -- no I/O, directly unit-tested
# ---------------------------------------------------------------------------


def commit_subject(commit: Dict[str, Any]) -> str:
    """First line of a commit message."""
    raw = commit.get("message") or (commit.get("summary") or {}).get("raw") or ""
    return raw.splitlines()[0].strip() if raw else ""


def commit_message(commit: Dict[str, Any]) -> str:
    return commit.get("message") or (commit.get("summary") or {}).get("raw") or ""


def derive_review_timeline(
    pull_request: Dict[str, Any], activity: Iterable[Dict[str, Any]]
) -> Dict[str, Any]:
    """Derive first review time and per-reviewer actions from the activity feed.

    **The rule that matters**: ``first_review_at`` is the earliest of
    {first comment, first approval, first changes-requested} made by *a person
    other than the PR author*. A PR author commenting on their own PR is not
    review -- counting it collapses PR review lead time towards zero and makes
    the metric meaningless. Bot activity is excluded for the same reason.

    Returns::

        {
          "first_review_at": str|None,
          "first_reviewer_person_id": str|None,
          "reviewers": [ {person_id, action, reviewed_at, comment_count,
                          approved_at, changes_requested_at, is_first_review} ],
          "merged_at": str|None, "declined_at": str|None,
          "self_comment_count": int, "bot_activity_count": int,
        }
    """
    author = actor_key(pull_request.get("author"))
    reviewers: Dict[str, Dict[str, Any]] = {}
    self_comments = 0
    bot_activity = 0
    merged_at: Optional[str] = None
    declined_at: Optional[str] = None
    decline_actor: Optional[str] = None

    for entry in activity or []:
        update = entry.get("update")
        if isinstance(update, dict):
            state = (update.get("state") or "").upper()
            when = to_rfc3339(update.get("date"))
            if state == "MERGED":
                merged_at = min_ts(merged_at, when) if merged_at else when
            elif state == "DECLINED":
                if declined_at is None or (
                    parse_ts(when) and parse_ts(when) < parse_ts(declined_at)
                ):
                    declined_at = when
                    decline_actor = actor_key(update.get("author"))

        for action, payload in (
            ("approved", entry.get("approval")),
            ("changes_requested", entry.get("changes_requested")),
            ("commented", entry.get("comment")),
        ):
            if not isinstance(payload, dict):
                continue
            if action == "commented":
                if payload.get("deleted"):
                    continue
                user = payload.get("user")
                when = to_rfc3339(payload.get("created_on") or payload.get("date"))
            else:
                user = payload.get("user")
                when = to_rfc3339(payload.get("date") or payload.get("created_on"))
            if when is None:
                continue

            key = actor_key(user)
            if key is not None and key == author:
                # Self-review is not review. This is the whole point.
                if action == "commented":
                    self_comments += 1
                continue
            if looks_like_bot(user):
                bot_activity += 1
                continue

            slot = reviewers.setdefault(
                key or f"anon:{when}",
                {
                    "person_id": person_id_of(user),
                    "reviewed_at": when,
                    "action": action,
                    "comment_count": 0,
                    "approved_at": None,
                    "changes_requested_at": None,
                },
            )
            if parse_ts(when) < parse_ts(slot["reviewed_at"]):
                slot["reviewed_at"] = when
            if action == "commented":
                slot["comment_count"] += 1
            elif action == "approved":
                slot["approved_at"] = min_ts(slot["approved_at"], when) if slot[
                    "approved_at"
                ] else when
            elif action == "changes_requested":
                slot["changes_requested_at"] = (
                    min_ts(slot["changes_requested_at"], when)
                    if slot["changes_requested_at"]
                    else when
                )

    # Final action per reviewer: approval outranks changes-requested outranks comment.
    ordered: List[Dict[str, Any]] = []
    for slot in reviewers.values():
        if slot["approved_at"]:
            slot["action"] = "approved"
        elif slot["changes_requested_at"]:
            slot["action"] = "changes_requested"
        else:
            slot["action"] = "commented"
        ordered.append(slot)
    ordered.sort(key=lambda item: (parse_ts(item["reviewed_at"]), item["person_id"] or ""))

    first_review_at = ordered[0]["reviewed_at"] if ordered else None
    for index, slot in enumerate(ordered):
        slot["is_first_review"] = index == 0

    return {
        "first_review_at": first_review_at,
        "first_reviewer_person_id": ordered[0]["person_id"] if ordered else None,
        "reviewers": ordered,
        "merged_at": merged_at,
        "declined_at": declined_at,
        "decline_actor_key": decline_actor,
        "self_comment_count": self_comments,
        "bot_activity_count": bot_activity,
    }


def summarise_comments(
    comments: Iterable[Dict[str, Any]], author: Optional[str]
) -> Dict[str, Any]:
    """Count PR comments, split inline vs top-level, excluding bots and self.

    Only counts and account ids leave this function -- comment text is never
    retained (CONTRACT.md §1 rule 1).
    """
    totals = {
        "comment_count": 0,
        "inline_comment_count": 0,
        "toplevel_comment_count": 0,
        "author_self_comment_count": 0,
        "bot_comment_count": 0,
        "deleted_comment_count": 0,
    }
    per_person: Dict[str, int] = {}
    for comment in comments or []:
        if comment.get("deleted"):
            totals["deleted_comment_count"] += 1
            continue
        user = comment.get("user")
        key = actor_key(user)
        if key is not None and key == author:
            totals["author_self_comment_count"] += 1
            continue
        if looks_like_bot(user):
            totals["bot_comment_count"] += 1
            continue
        totals["comment_count"] += 1
        if comment.get("inline"):
            totals["inline_comment_count"] += 1
        else:
            totals["toplevel_comment_count"] += 1
        person = person_id_of(user)
        if person:
            per_person[person] = per_person.get(person, 0) + 1
    totals["comment_count_by_person"] = per_person
    return totals


#: Path patterns -> automation artefact kind, in priority order (first match wins).
#: Ordered most-specific first: ``login.steps.ts`` must classify as a step
#: definition, not as generic TypeScript, and ``API.feature`` must not be caught
#: by a broad ``features/`` directory rule before its extension is considered.
AUTOMATION_KIND_RULES: Tuple[Tuple[str, "re.Pattern"], ...] = (
    ("feature", re.compile(r"\.feature$", re.I)),
    ("step_definition", re.compile(r"[._-]steps?\.(ts|js|tsx|jsx|py|java)$", re.I)),
    ("spec", re.compile(r"(\.|_)(spec|test)\.(ts|js|tsx|jsx)$|(^|/)test_[^/]+\.py$"
                        r"|[._-]test\.py$|Test\.java$", re.I)),
    ("page_object", re.compile(r"(^|/)(pages?|page[-_]?objects?|locators?)/"
                               r"|[._-]page\.(ts|js|py)$", re.I)),
    # A data file is only a fixture when it *lives* in a fixture directory.
    # Matching every .json/.yaml in the repository would classify package.json,
    # tsconfig.json and every CI config as test data.
    ("fixture", re.compile(r"(^|/)(fixtures?|test[-_]?data|testdata|mocks?|stubs?)/"
                           r"|(^|/)(tests?|e2e|specs?)/.*\.(csv|json|ya?ml|xlsx?)$",
                           re.I)),
)

#: Kinds that count as an executable automation script for metric 1
#: ("Automation Output"). A page object or a fixture is real work and is
#: reported, but it is scaffolding: counting it as a script would inflate the
#: number with files that never execute a scenario on their own.
AUTOMATION_SCRIPT_KINDS = ("feature", "step_definition", "spec")

#: Bitbucket diffstat statuses, normalised. `merge conflict` and `local deleted`
#: appear on merge commits and are folded into `modified` rather than dropped.
_DIFFSTAT_STATUS = {
    "added": "added",
    "removed": "removed",
    "modified": "modified",
    "renamed": "modified",
    "merge conflict": "modified",
    "local deleted": "removed",
    "remote deleted": "removed",
}


def classify_path(path: Optional[str]) -> str:
    """Automation artefact kind for one repository path, or ``'other'``.

    Classification is on the path alone. File *contents* are never fetched --
    CONTRACT.md §1 permits paths and forbids contents, and a classifier that
    needed to read the file would breach that for no extra accuracy.
    """
    if not path:
        return "other"
    for kind, pattern in AUTOMATION_KIND_RULES:
        if pattern.search(path):
            return kind
    return "other"


def diffstat_path(entry: Dict[str, Any]) -> Optional[str]:
    """Path of a diffstat entry, preferring the new path over the old one.

    A deleted file has ``new = null``, and a rename carries both; taking ``new``
    first means a renamed test still classifies by where it landed.
    """
    for side in ("new", "old"):
        node = entry.get(side)
        if isinstance(node, dict) and node.get("path"):
            return str(node["path"])
    return None


def summarise_diffstat(entries: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Sum a (fully paginated) diffstat collection.

    File *paths* are permitted by CONTRACT.md §1; file *contents* are not, and
    are never requested. Paths are used to classify and are then **discarded**:
    the event carries counts per kind, never the path list. A repository tree is
    not a secret, but it is also not needed downstream, and shipping it would
    put customer and endpoint names from test filenames into the warehouse.
    """
    lines_added = 0
    lines_removed = 0
    files_changed = 0
    by_status: Dict[str, int] = {}
    by_kind: Dict[str, Dict[str, int]] = {}
    for entry in entries or []:
        files_changed += 1
        lines_added += int(entry.get("lines_added") or 0)
        lines_removed += int(entry.get("lines_removed") or 0)
        raw_status = str(entry.get("status") or "unknown").lower()
        status = _DIFFSTAT_STATUS.get(raw_status, "modified")
        by_status[status] = by_status.get(status, 0) + 1
        kind = classify_path(diffstat_path(entry))
        bucket = by_kind.setdefault(kind, {"added": 0, "modified": 0, "removed": 0})
        bucket[status] = bucket.get(status, 0) + 1

    scripts_added = sum(by_kind.get(k, {}).get("added", 0)
                        for k in AUTOMATION_SCRIPT_KINDS)
    scripts_modified = sum(by_kind.get(k, {}).get("modified", 0)
                           for k in AUTOMATION_SCRIPT_KINDS)
    scripts_removed = sum(by_kind.get(k, {}).get("removed", 0)
                          for k in AUTOMATION_SCRIPT_KINDS)
    return {
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "files_changed": files_changed,
        "files_by_status": by_status,
        "files_added": by_status.get("added", 0),
        "files_modified": by_status.get("modified", 0),
        "files_removed": by_status.get("removed", 0),
        "automation_scripts_added": scripts_added,
        "automation_scripts_modified": scripts_modified,
        "automation_scripts_removed": scripts_removed,
        "automation_files_by_kind": by_kind,
    }


def summarise_pr_commits(commits: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """AI-marker and trailer summary over a PR's commits.

    Detection is marker-only + trailers. There is deliberately no
    ``^feat|^fix|^chore`` clause here (design §3.5): conventional-commit
    prefixes are mandated repo-wide and would classify every human commit as
    AI-authored.
    """
    run_ids: List[str] = []
    trace_ids: List[str] = []
    models: List[str] = []
    ai_commits = 0
    for commit in commits or []:
        subject = commit_subject(commit)
        trailers = parse_ai_trailers(commit_message(commit))
        marked = has_ai_commit_marker(subject) or "ai-run-id" in trailers
        if marked:
            ai_commits += 1
        if trailers.get("ai-run-id") and trailers["ai-run-id"] not in run_ids:
            run_ids.append(trailers["ai-run-id"])
        if trailers.get("ai-trace-id") and trailers["ai-trace-id"] not in trace_ids:
            trace_ids.append(trailers["ai-trace-id"])
        if trailers.get("ai-model") and trailers["ai-model"] not in models:
            models.append(trailers["ai-model"])
    return {
        "commit_count": len(list(commits or [])),
        "ai_commit_count": ai_commits,
        "ai_run_ids": run_ids[:20],
        "ai_trace_ids": trace_ids[:20],
        "ai_model_ids": models[:20],
        # The commit -> PR edge. `sql/05_transform_output.sql` unnests exactly
        # this field to decide which PR an output was first reviewed in, and
        # this function used to return counts only -- so that UNNEST ran over
        # an absent array and the whole output -> PR -> ticket path resolved to
        # nothing. A structural zero, not a sparse one: no PR has ever had a
        # commit attached to it.
        #
        # Bounded like its neighbours. A PR with more than 200 commits is a
        # branch someone forgot to rebase, and the tail carries no information
        # the head does not; `commit_count` above stays exact either way.
        "commit_shas": [c.get("hash") for c in (commits or [])
                        if c.get("hash")][:200],
    }


def resolve_link(
    pr_title_marker: bool, commit_summary: Dict[str, Any], jira_key: Optional[str]
) -> Tuple[Dict[str, Any], Optional[str], Optional[str]]:
    """Decide ``link``, ``run_id`` and ``trace_id`` for a PR-level event.

    * an ``AI-Run-Id`` trailer is the only thing that earns ``explicit``
      (CONTRACT.md §2.4 -- only explicit rows may feed cost metrics);
    * an explicit marker with no trailer is ``marker_only`` (design §5.3 L3);
    * otherwise the row is a plain SCM fact linked, at best, heuristically by
      Jira key (design §5.3 L2).
    """
    run_ids = commit_summary.get("ai_run_ids") or []
    trace_ids = commit_summary.get("ai_trace_ids") or []
    if run_ids:
        run_id = run_ids[0] if len(run_ids) == 1 else None
        trace_id = trace_ids[0] if len(trace_ids) == 1 else None
        return make_link("explicit", 1.0), run_id, trace_id
    if pr_title_marker or commit_summary.get("ai_commit_count"):
        return make_link("marker_only", 0.3), None, None
    if jira_key:
        return make_link("heuristic", 0.5), None, None
    return make_link("heuristic", 0.0), None, None


def classify_decline(
    timeline: Dict[str, Any], pull_request: Dict[str, Any]
) -> str:
    """Bounded decline reason class."""
    author = actor_key(pull_request.get("author"))
    actor = timeline.get("decline_actor_key")
    if actor and author and actor == author:
        return "declined_by_author"
    if any(r.get("changes_requested_at") for r in timeline.get("reviewers") or []):
        return "changes_requested_unresolved"
    if actor:
        return "declined_by_reviewer"
    return "unknown"


def find_reverts(commits: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Detect ``Revert ...`` commits and resolve what they reverted.

    Resolution order:
      1. the ``This reverts commit <sha>.`` line git itself writes -- exact;
      2. the quoted original subject in ``Revert "<subject>"`` -- matched
         against the most recent earlier commit with that subject;
      3. unresolved -- still emitted, with ``resolution='unresolved'``, because
         a revert we cannot pair is still a revert (AR-9).

    ``days_to_revert`` is the elapsed time from the reverted commit to the
    revert.
    """
    by_hash: Dict[str, Dict[str, Any]] = {}
    by_subject: Dict[str, List[Dict[str, Any]]] = {}
    for commit in commits:
        sha = commit.get("hash")
        if sha:
            by_hash[sha] = commit
        by_subject.setdefault(commit_subject(commit), []).append(commit)

    results: List[Dict[str, Any]] = []
    for commit in commits:
        subject = commit_subject(commit)
        if not REVERT_SUBJECT_RE.match(subject):
            continue
        message = commit_message(commit)
        target: Optional[Dict[str, Any]] = None
        target_sha: Optional[str] = None
        resolution = "unresolved"

        match = REVERTS_COMMIT_RE.search(message)
        if match:
            candidate = match.group(1).lower()
            target_sha = candidate
            resolution = "reverts_commit_trailer"
            if candidate in by_hash:
                target = by_hash[candidate]
            else:
                for sha, other in by_hash.items():
                    if sha.lower().startswith(candidate):
                        target, target_sha = other, sha
                        break
                else:
                    resolution = "reverts_commit_trailer_unresolved_sha"

        if target is None:
            quoted = REVERT_QUOTED_SUBJECT_RE.match(subject)
            if quoted:
                original_subject = quoted.group(1).strip()
                candidates = [
                    other
                    for other in by_subject.get(original_subject, [])
                    if other.get("hash") != commit.get("hash")
                    and parse_ts(other.get("date"))
                    and parse_ts(commit.get("date"))
                    and parse_ts(other.get("date")) < parse_ts(commit.get("date"))
                ]
                if candidates:
                    target = max(candidates, key=lambda c: parse_ts(c.get("date")))
                    target_sha = target.get("hash")
                    resolution = "subject_match"

        results.append(
            {
                "revert_commit": commit,
                "reverted_commit": target,
                "reverted_commit_sha": target_sha,
                "resolution": resolution,
                "days_to_revert": days_between(
                    (target or {}).get("date"), commit.get("date")
                ),
            }
        )
    return results


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------


class BitbucketPoller:
    def __init__(
        self,
        client: HttpClient,
        workspace: str,
        repo: str,
        config: Optional[Config] = None,
        fetch_pr_commits: bool = True,
    ) -> None:
        self.client = client
        self.workspace = workspace
        self.repo = repo
        self.config = config or Config()
        self.repo_full_name = f"{workspace}/{repo}"
        self.base = f"{self.config.bitbucket_api_base}/2.0/repositories/{workspace}/{repo}"
        self.fetch_pr_commits = fetch_pr_commits
        self.stats: Dict[str, int] = {}

    # -- fetching ------------------------------------------------------------

    def iter_pull_requests(
        self, states: Sequence[str], since: Optional[str]
    ) -> Iterator[Dict[str, Any]]:
        params: Dict[str, Any] = {
            "state": list(states),
            "pagelen": 50,
            "sort": "-updated_on",
        }
        if since:
            params["q"] = f'updated_on >= "{since}"'
        for pull_request in paginate(self.client, f"{self.base}/pullrequests", params):
            yield pull_request

    def pr_activity(self, pr_id: Any) -> List[Dict[str, Any]]:
        return list(
            paginate(
                self.client, f"{self.base}/pullrequests/{pr_id}/activity", {"pagelen": 50}
            )
        )

    def pr_comments(self, pr_id: Any) -> List[Dict[str, Any]]:
        return list(
            paginate(
                self.client, f"{self.base}/pullrequests/{pr_id}/comments", {"pagelen": 100}
            )
        )

    def pr_diffstat(self, pr_id: Any) -> List[Dict[str, Any]]:
        # Paginated deliberately: large PRs touch far more files than one page.
        return list(
            paginate(
                self.client, f"{self.base}/pullrequests/{pr_id}/diffstat", {"pagelen": 100}
            )
        )

    def pr_commits(self, pr_id: Any) -> List[Dict[str, Any]]:
        return list(
            paginate(
                self.client, f"{self.base}/pullrequests/{pr_id}/commits", {"pagelen": 100}
            )
        )

    def iter_commits(self, since: Optional[str], max_commits: int = 5000) -> List[Dict[str, Any]]:
        """Newest-first commit stream, stopped once older than ``since``.

        The v2.0 commits endpoint has no reliable server-side date filter, so
        the window is applied client-side and paging stops at the boundary.
        """
        boundary = parse_ts(since) if since else None
        collected: List[Dict[str, Any]] = []
        for commit in paginate(self.client, f"{self.base}/commits", {"pagelen": 100}):
            when = parse_ts(commit.get("date"))
            if boundary and when and when < boundary:
                break
            collected.append(commit)
            if len(collected) >= max_commits:
                break
        return collected

    # -- event construction --------------------------------------------------

    def _actor_for_user(
        self, user: Optional[Dict[str, Any]], raw_author: Optional[str] = None
    ) -> Dict[str, Any]:
        return make_actor(
            person_id=person_id_of(user),
            person_email_hash=hash_email(
                email_from_raw_author(raw_author), self.config.email_salt
            ),
        )

    def _context_for_pr(self, pull_request: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
        branch = (
            ((pull_request.get("source") or {}).get("branch") or {}).get("name") or None
        )
        jira_key = extract_jira_key(branch, pull_request.get("title"),
                                    projects=self.config.jira_project_keys)
        return (
            make_context(
                jira_issue_key=jira_key,
                repo_full_name=self.repo_full_name,
                branch_name=branch,
            ),
            jira_key,
        )

    def _synthetic_trace(self, *parts: Any) -> str:
        """Deterministic trace id for events with no agent trace of their own.

        Groups every event about one entity (a PR, a revert) without inventing
        an agent run. See CONTRACT.md 2.4.
        """
        return deterministic_id("trc", "bitbucket", self.repo_full_name, *parts)

    def build_pr_events(self, pull_request: Dict[str, Any]) -> List[Dict[str, Any]]:
        pr_id = pull_request.get("id")
        state = (pull_request.get("state") or "").upper()
        context, jira_key = self._context_for_pr(pull_request)
        agent = make_agent(AGENT_NAME)
        author = actor_key(pull_request.get("author"))

        activity = self.pr_activity(pr_id)
        timeline = derive_review_timeline(pull_request, activity)
        comments = summarise_comments(self.pr_comments(pr_id), author)
        diffstat = summarise_diffstat(self.pr_diffstat(pr_id))
        commits = self.pr_commits(pr_id) if self.fetch_pr_commits else []
        commit_summary = summarise_pr_commits(commits)

        title_marker = has_ai_pr_title_marker(pull_request.get("title"))
        link, run_id, trailer_trace = resolve_link(title_marker, commit_summary, jira_key)
        trace_id = trailer_trace or self._synthetic_trace("pr", pr_id)
        created_on = to_rfc3339(pull_request.get("created_on"))

        commits_after_first_review = 0
        first_review_at = timeline["first_review_at"]
        if first_review_at:
            boundary = parse_ts(first_review_at)
            commits_after_first_review = sum(
                1
                for commit in commits
                if parse_ts(commit.get("date")) and parse_ts(commit.get("date")) > boundary
            )

        shared = {
            "pr_id": pr_id,
            "pr_state": state,
            "pr_title_has_ai_marker": title_marker,
            "has_ai_marker": bool(title_marker or commit_summary["ai_commit_count"]),
            "created_on": created_on,
            "first_review_at": first_review_at,
            "review_lead_time_ms": _ms(created_on, first_review_at),
            "reviewer_count": len(timeline["reviewers"]),
            "approval_count": sum(
                1 for r in timeline["reviewers"] if r["action"] == "approved"
            ),
            "changes_requested_count": sum(
                1 for r in timeline["reviewers"] if r["action"] == "changes_requested"
            ),
            "commits_after_first_review": commits_after_first_review,
            "self_comment_count": timeline["self_comment_count"],
            "bot_comment_count": comments["bot_comment_count"],
            "comment_count": comments["comment_count"],
            "inline_comment_count": comments["inline_comment_count"],
            "toplevel_comment_count": comments["toplevel_comment_count"],
            "lines_added": diffstat["lines_added"],
            "lines_removed": diffstat["lines_removed"],
            "files_changed": diffstat["files_changed"],
            "files_added": diffstat["files_added"],
            "files_modified": diffstat["files_modified"],
            "files_removed": diffstat["files_removed"],
            "automation_scripts_added": diffstat["automation_scripts_added"],
            "automation_scripts_modified": diffstat["automation_scripts_modified"],
            "automation_scripts_removed": diffstat["automation_scripts_removed"],
            "automation_files_by_kind": diffstat["automation_files_by_kind"],
            "commit_count": commit_summary["commit_count"],
            "ai_commit_count": commit_summary["ai_commit_count"],
            "ai_run_ids": commit_summary["ai_run_ids"],
            "ai_model_ids": commit_summary["ai_model_ids"],
        }

        events: List[Dict[str, Any]] = []

        # -- 15. scm.pr.created ---------------------------------------------
        # The commit -> PR edge, and the reason this event exists at all.
        # `sql/05_transform_output.sql` decides which PR an output was first
        # reviewed in by unnesting `scm.pr.created.commit_shas`; the event type
        # was in the collector's allow-list and in the SQL, and **no poller had
        # ever emitted it**. The join has been resolving to nothing since the
        # warehouse was written, silently -- an empty result and a
        # never-populated one look identical downstream.
        #
        # Emitted for every PR, not only AI-marked ones: this is the edge the
        # rest of the join stands on, and a PR that turns out to contain an
        # AI-authored commit cannot be recognised as such after the fact if its
        # commit list was never recorded.
        if created_on:
            events.append(
                build_event(
                    "scm.pr.created",
                    created_on,
                    (self.repo_full_name, pr_id, "created", created_on),
                    {
                        "pr_id": pr_id,
                        "commit_shas": commit_summary["commit_shas"],
                        "pr_title_has_ai_marker": title_marker,
                    },
                    actor=make_actor(person_id=author, role="dev"),
                    context=context,
                    agent=agent,
                    link=link,
                    trace_id=trace_id,
                    run_id=run_id,
                )
            )

        # -- 16. scm.pr.reviewed, one per non-author reviewer ----------------
        for reviewer in timeline["reviewers"]:
            events.append(
                build_event(
                    "scm.pr.reviewed",
                    reviewer["reviewed_at"],
                    (
                        self.repo_full_name,
                        pr_id,
                        reviewer["person_id"] or "anonymous",
                        reviewer["action"],
                        reviewer["reviewed_at"],
                    ),
                    {
                        "pr_id": pr_id,
                        "reviewer_person_id": reviewer["person_id"],
                        "action": reviewer["action"],
                        "comment_count": reviewer["comment_count"],
                        "reviewed_at": reviewer["reviewed_at"],
                        "is_first_review": reviewer["is_first_review"],
                        "first_review_at": first_review_at,
                        "pr_created_on": created_on,
                        "review_lead_time_ms": _ms(created_on, reviewer["reviewed_at"]),
                        "pr_title_has_ai_marker": title_marker,
                        "has_ai_marker": shared["has_ai_marker"],
                    },
                    actor=make_actor(person_id=reviewer["person_id"], role="dev"),
                    context=context,
                    agent=agent,
                    link=link,
                    trace_id=trace_id,
                    run_id=run_id,
                )
            )

        # -- 17/18. terminal state -------------------------------------------
        if state == "MERGED":
            merged_at = timeline["merged_at"] or to_rfc3339(pull_request.get("updated_on"))
            attributes = dict(shared)
            attributes.update(
                {
                    "merged_at": merged_at,
                    "merge_commit_sha": (pull_request.get("merge_commit") or {}).get("hash"),
                    "merge_lead_time_ms": _ms(created_on, merged_at),
                    "review_to_merge_ms": _ms(first_review_at, merged_at),
                }
            )
            events.append(
                build_event(
                    "scm.pr.merged",
                    merged_at,
                    (self.repo_full_name, pr_id, "merged", merged_at),
                    attributes,
                    actor=self._actor_for_user(pull_request.get("author")),
                    context=context,
                    agent=agent,
                    link=link,
                    trace_id=trace_id,
                    run_id=run_id,
                )
            )
        elif state == "DECLINED":
            declined_at = timeline["declined_at"] or to_rfc3339(
                pull_request.get("updated_on")
            )
            attributes = dict(shared)
            attributes.update(
                {
                    "declined_at": declined_at,
                    "decline_reason_class": classify_decline(timeline, pull_request),
                    "decline_lead_time_ms": _ms(created_on, declined_at),
                }
            )
            events.append(
                build_event(
                    "scm.pr.declined",
                    declined_at,
                    (self.repo_full_name, pr_id, "declined", declined_at),
                    attributes,
                    actor=self._actor_for_user(pull_request.get("author")),
                    context=context,
                    agent=agent,
                    link=link,
                    trace_id=trace_id,
                    run_id=run_id,
                )
            )
        return events

    def build_revert_events(self, commits: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        for found in find_reverts(commits):
            revert = found["revert_commit"]
            reverted = found["reverted_commit"] or {}
            revert_sha = revert.get("hash")
            reverted_sha = found["reverted_commit_sha"]
            trailers = parse_ai_trailers(commit_message(reverted)) if reverted else {}
            reverted_marker = (
                has_ai_commit_marker(commit_subject(reverted)) if reverted else None
            )
            run_id = trailers.get("ai-run-id")
            if run_id:
                link = make_link("explicit", 1.0)
            elif reverted_marker:
                link = make_link("marker_only", 0.3)
            else:
                link = make_link("heuristic", 0.0)

            jira_key = extract_jira_key(
                commit_subject(reverted) if reverted else None,
                commit_subject(revert),
                projects=self.config.jira_project_keys,
            )
            author_user = (revert.get("author") or {}).get("user")
            events.append(
                build_event(
                    "scm.revert",
                    revert.get("date"),
                    (self.repo_full_name, "revert", revert_sha),
                    {
                        "revert_commit_sha": revert_sha,
                        "reverted_commit_sha": reverted_sha,
                        "days_to_revert": found["days_to_revert"],
                        "resolution": found["resolution"],
                        "reverted_commit_has_ai_marker": reverted_marker,
                        "reverted_at": to_rfc3339(revert.get("date")),
                        "reverted_commit_at": to_rfc3339(reverted.get("date"))
                        if reverted
                        else None,
                    },
                    actor=self._actor_for_user(
                        author_user, (revert.get("author") or {}).get("raw")
                    ),
                    context=make_context(
                        jira_issue_key=jira_key, repo_full_name=self.repo_full_name
                    ),
                    agent=make_agent(AGENT_NAME),
                    link=link,
                    trace_id=trailers.get("ai-trace-id")
                    or self._synthetic_trace("revert", revert_sha),
                    run_id=run_id,
                )
            )
        return events


def _ms(start: Optional[str], end: Optional[str]) -> Optional[int]:
    return ms_between(start, end)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="poll_bitbucket.py",
        description="Emit scm.pr.* and scm.revert telemetry events from Bitbucket Cloud.",
    )
    parser.add_argument("--workspace", required=True, help="Bitbucket workspace id")
    parser.add_argument("--repo", required=True, help="Repository slug")
    parser.add_argument("--since", help="ISO8601 window start (overrides the watermark)")
    parser.add_argument("--out", help="NDJSON output file (default: stdout)")
    parser.add_argument(
        "--state",
        default="OPEN,MERGED,DECLINED",
        help="Comma-separated PR states to poll (default: OPEN,MERGED,DECLINED)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=30,
        help="Window when no watermark exists yet (default: 30)",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=10,
        metavar="N",
        help=(
            "Print a progress line to stderr every N pull requests (0 disables). "
            "A full-repo pass makes ~4 API calls per PR, so a large window can run "
            "for several minutes; silence is hard to distinguish from a hang."
        ),
    )
    parser.add_argument("--state-file", help="Watermark JSON path")
    parser.add_argument(
        "--no-watermark",
        action="store_true",
        help="Do not read or advance the watermark (ad-hoc backfill)",
    )
    parser.add_argument(
        "--no-reverts", action="store_true", help="Skip the commit scan for reverts"
    )
    parser.add_argument(
        "--no-pr-commits",
        action="store_true",
        help="Skip per-PR commit fetch (loses AI trailers and explicit links)",
    )
    parser.add_argument(
        "--max-prs", type=int, default=0, help="Stop after N pull requests (0 = no limit)"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None, client: Optional[HttpClient] = None) -> int:
    """``client`` is injectable so the test-suite never touches the network."""
    args = build_arg_parser().parse_args(argv)
    config = Config.from_env()
    if args.state_file:
        config.state_path = args.state_file

    states = [s.strip().upper() for s in args.state.split(",") if s.strip()]
    unknown = [s for s in states if s not in VALID_STATES]
    if unknown:
        fail(f"unknown --state values: {', '.join(unknown)}", exit_code=2)

    if client is None:
        try:
            username, token = config.require_bitbucket()
        except ConfigError as exc:
            fail(str(exc), exit_code=2)
        client = HttpClient(
            auth=(username, token),
            max_retries=config.max_retries,
            timeout=config.timeout_seconds,
        )
    poller = BitbucketPoller(
        client, args.workspace, args.repo, config, fetch_pr_commits=not args.no_pr_commits
    )
    store = WatermarkStore(config.state_path)
    pr_key = f"bitbucket:pullrequests:{poller.repo_full_name}"
    commit_key = f"bitbucket:commits:{poller.repo_full_name}"

    pr_since = args.since or (
        None if args.no_watermark else default_since(store.get(pr_key), args.lookback_days)
    )
    if pr_since is None:
        pr_since = default_since(None, args.lookback_days)
    commit_since = args.since or (
        None
        if args.no_watermark
        else default_since(store.get(commit_key), args.lookback_days)
    )
    if commit_since is None:
        commit_since = default_since(None, args.lookback_days)

    counts = {"pull_requests": 0, "events": 0, "reverts": 0}
    try:
        with NdjsonWriter(args.out) as writer:
            if args.no_watermark:
                _run(poller, writer, states, pr_since, commit_since, args, counts, None, None)
            else:
                with store.checkpoint(pr_key) as pr_mark, store.checkpoint(
                    commit_key
                ) as commit_mark:
                    truncated = _run(
                        poller,
                        writer,
                        states,
                        pr_since,
                        commit_since,
                        args,
                        counts,
                        pr_mark,
                        commit_mark,
                    )
                    if truncated:
                        # Discard every proposal made this run. A truncated pass
                        # covered only the newest slice of the window, so advancing
                        # the mark would permanently skip everything older.
                        pr_mark.discard()
                        commit_mark.discard()
            counts["events"] = writer.count
    except HttpError as exc:
        log(
            "bitbucket_api_error",
            status=exc.status,
            url=exc.url,
            hint="401/403 => check BITBUCKET_USERNAME/BITBUCKET_ACCESS_TOKEN and that "
            "Basic auth is used (never Bearer). Watermark not advanced.",
        )
        return 3
    except KeyboardInterrupt:
        log("interrupted", hint="watermark not advanced")
        return 130

    log(
        "bitbucket_poll_complete",
        repo=poller.repo_full_name,
        since=pr_since,
        requests=client.request_count,
        retries=client.retry_count,
        **counts,
    )
    return 0


def _run(poller, writer, states, pr_since, commit_since, args, counts, pr_mark, commit_mark):
    # NOTE ON --max-prs AND THE WATERMARK.
    # PRs arrive newest-first (sort=-updated_on), so the FIRST PR seen carries the
    # NEWEST updated_on. Proposing per-PR is therefore not enough to make a
    # truncated run safe: PR #1 already proposes the newest timestamp, and if that
    # is committed the next run starts after it and the older, never-processed PRs
    # are skipped forever.
    # The gate must be at COMMIT time — see _run's return value and its use in
    # main(). Verified against a live repo: the per-PR-only fix still advanced the
    # mark to the newest timestamp.
    # This mirrors the "no silent caps" rule: a bounded run must report what it
    # dropped rather than look complete.
    truncated = False
    progress_every = max(1, args.progress_every) if getattr(args, "progress_every", 0) else 0

    for pull_request in poller.iter_pull_requests(states, pr_since):
        for event in poller.build_pr_events(pull_request):
            writer.write(event)
        counts["pull_requests"] += 1

        if progress_every and counts["pull_requests"] % progress_every == 0:
            print(
                f"  ... {counts['pull_requests']} pull requests processed",
                file=sys.stderr,
                flush=True,
            )

        if pr_mark is not None:
            pr_mark.propose(pull_request.get("updated_on"))

        if args.max_prs and counts["pull_requests"] >= args.max_prs:
            truncated = True
            break

    if truncated:
        counts["truncated_by_max_prs"] = 1
        print(
            f"WARNING: stopped after --max-prs={args.max_prs} pull requests. "
            f"Older pull requests in the window were NOT processed and the "
            f"watermark was NOT advanced, so the next run will re-cover this "
            f"window. Raise --max-prs or narrow --since for full coverage.",
            file=sys.stderr,
            flush=True,
        )

    if not args.no_reverts:
        commits = poller.iter_commits(commit_since)
        for event in poller.build_revert_events(commits):
            writer.write(event)
            counts["reverts"] += 1
        if commit_mark is not None and commits:
            newest = max(
                (parse_ts(c.get("date")) for c in commits if parse_ts(c.get("date"))),
                default=None,
            )
            if newest:
                commit_mark.propose(newest)

    return truncated


if __name__ == "__main__":
    raise SystemExit(main())
