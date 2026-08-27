#!/usr/bin/env python3
"""
AI telemetry ingestion collector.

Contract: schema/CONTRACT.md  (single source of truth)
Design:   docs/spikes/ai-effectiveness-observability.md §4.4a, §6.5, §9.4, §11.1, §11.3, §11.4

------------------------------------------------------------------------------
WHY stdlib http.server AND NOT FastAPI
------------------------------------------------------------------------------
FastAPI is installed on the dev machine and would give free request models, but it
is deliberately NOT used here:

  1. The public surface is two endpoints (POST /v1/events, GET /healthz). Pydantic
     models buy nothing: the envelope is validated against CONTRACT.md by an
     explicit allow-list anyway, and an allow-list is exactly what must NOT be
     delegated to a framework's coercion rules.
  2. Contract rule §1.2 is "fail open" — telemetry must never be the thing that
     breaks. A collector whose only runtime dependency is CPython cannot break on
     a transitive dependency bump, and there is no uvicorn/starlette/pydantic
     surface to patch on a CVE.
  3. Cloud Run cold start and image size: the container is python:slim plus one
     optional wheel (google-cloud-pubsub). uvicorn+fastapi+pydantic-core roughly
     triples it.
  4. Testability: tests/test_collector.py is stdlib `unittest` with no TestClient
     and no ASGI plumbing — it drives a real socket, so what is tested is what
     ships.

If this service ever grows beyond ingest (query endpoints, OpenAPI for external
consumers), revisit — that is the point at which FastAPI starts paying for itself.
------------------------------------------------------------------------------

Behaviour summary (all mandatory, see the task contract):
  * Bearer auth, compared with hmac.compare_digest (constant time).
  * schema_version major must be supported, else 400.
  * event_type validated against the CLOSED enum of CONTRACT.md §3, else rejected.
  * Redaction enforcement: forbidden attribute key (CONTRACT.md §3) or a string
    value matching the dev-quality-gates secret patterns rejects the WHOLE event.
    Rejection logs the field NAME and the check id ONLY — never the value.
  * Allow-list stripping: any field not named in CONTRACT.md §2/§3 is dropped
    before publish. Deny-lists are not used for stripping (§11.3 defence in depth).
  * ingested_at is set server-side, always; a client-supplied value is discarded.
  * DQ-10 clock skew: event_time > now + 5min is clamped; an end timestamp before
    its start is clamped. Both raise a dq_flag; the event is still published.
  * DQ-11 idempotency: in-process TTL cache on event_id.
  * Publishes to Pub/Sub topic `ai-run-events`; on any failure appends to a local
    NDJSON file so nothing is lost, and still returns 200.
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Tuple

LOG = logging.getLogger("aiep.collector")

# --------------------------------------------------------------------------- #
# Configuration (env only — no project id, endpoint or secret is ever literal)
# --------------------------------------------------------------------------- #

ENV_TOKEN = "AIEP_COLLECTOR_TOKEN"          # shared bearer token
ENV_PROJECT = "AIEP_PUBSUB_PROJECT"         # ${PROJECT_ID} at deploy time
ENV_TOPIC = "AIEP_PUBSUB_TOPIC"             # default: ai-run-events
ENV_FALLBACK = "AIEP_FALLBACK_PATH"         # local NDJSON spill file
ENV_FINDINGS = "AIEP_FINDINGS_PATH"         # local NDJSON dq findings file
ENV_DEDUP_TTL = "AIEP_DEDUP_TTL_SECONDS"
ENV_MAX_BODY = "AIEP_MAX_BODY_BYTES"
ENV_PORT = "PORT"

DEFAULT_TOPIC = "ai-run-events"
DEFAULT_FALLBACK = "/var/tmp/aiep/ai-run-events.ndjson"
DEFAULT_FINDINGS = "/var/tmp/aiep/dq_findings.ndjson"
DEFAULT_DEDUP_TTL = 900          # 15 minutes; DQ-11 is re-applied in the warehouse
DEFAULT_MAX_BODY = 8 * 1024 * 1024

# CONTRACT.md §2 — schema_version. Only this major is understood.
SUPPORTED_MAJOR = 1

# DQ-10 — clock skew tolerance (design §9.4).
MAX_FUTURE_SKEW = timedelta(minutes=5)

# --------------------------------------------------------------------------- #
# CONTRACT.md §3 — CLOSED event_type enum. Unknown values are rejected.
# --------------------------------------------------------------------------- #

#: The block `pollers/poll_bitbucket.py` puts on every terminal PR event.
#:
#: Named once here because it is named once there. Measured 2026-08-27, the
#: three-name entries below it had fallen 34 names behind what the poller
#: writes -- and nothing failed, because poller output goes straight to the
#: warehouse and never passes `importers/bundle.py`, where this list is
#: enforced. So the list was not protecting these events; it was describing
#: them, wrongly, while `sql/06_marts.sql` read the names it did not have.
#:
#: Every name here is a count, a timestamp, a duration, a classification or an
#: id. No path, no title, no message body, and no valuation -- `cost_usd` is
#: still absent from this file and still derived in the warehouse (§4).
#: `automation_files_by_kind` is a map of classified kind to counts; the paths
#: that produced it are classified and dropped in the poller.
_PR_SHARED = frozenset({
    "pr_id", "pr_state", "pr_title_has_ai_marker", "has_ai_marker",
    "created_on", "first_review_at", "review_lead_time_ms",
    "reviewer_count", "approval_count", "changes_requested_count",
    # Rework, as the review timeline records it.
    "commits_after_first_review", "reopened_at", "reopen_count",
    "revision_count", "revisions_after_first_review",
    # CONTRACT.md §5's numerator, and the figure §8.9 requires beside it
    # rather than folded into it.
    "lines_changed_pre_review", "lines_changed_after_first_review",
    "self_comment_count", "bot_comment_count", "comment_count",
    "inline_comment_count", "toplevel_comment_count",
    "lines_added", "lines_removed",
    "files_changed", "files_added", "files_modified", "files_removed",
    "automation_scripts_added", "automation_scripts_modified",
    "automation_scripts_removed", "automation_files_by_kind",
    "commit_count", "ai_commit_count", "ai_run_ids", "ai_model_ids",
})

ATTRIBUTE_ALLOWLIST: Dict[str, frozenset] = {
    "run.started": frozenset({"invocation_mode", "model_declared_id", "input_source"}),
    # `base_commit_sha`/`head_commit_sha` are the 1.1.0 addition, and they are
    # what finally makes this event carry evidence rather than an assertion.
    # A session id joins to a ticket only through something; measured
    # 2026-08-26, the branch name is not that something (0 of 37 branches held
    # a Jira key). A commit range is exact, and the client captures it while
    # the clone still exists -- 6 of 7 of them were gone by the time anything
    # went looking.
    "run.bound": frozenset({
        "copilot_session_id", "jira_issue_key",
        "base_commit_sha", "head_commit_sha", "repository_host",
    }),
    "run.phase.started": frozenset({"phase_name"}),
    "run.phase.completed": frozenset({"phase_name", "duration_ms", "status"}),
    # Schema 1.1.0 widened this row with what Copilot's own session journal
    # records and the OTel span stream never did — above all `premium_requests`,
    # the unit Copilot actually bills. The weekly report's cost section carried
    # a standing disclaimer that its per-token figure was "notional" for exactly
    # as long as that number was unavailable.
    #
    # `cost_usd` and `cost_basis` are deliberately NOT here, though
    # weekly_report.py reads both: CONTRACT.md §4 is explicit that cost is never
    # emitted by a client and is derived in the warehouse against dated pricing.
    # Permitting them would let a laptop assert a price. `premium_requests` is
    # different in kind — a count of billed events, measured, not a valuation.
    "model.call": frozenset({
        "model_id", "input_tokens", "output_tokens", "cached_input_tokens",
        "cache_write_tokens", "reasoning_tokens", "latency_ms", "retry_count",
        "finish_reason", "request_count", "premium_requests", "nano_aiu",
        # Context composition, from `session.shutdown`. Not usage — a *level*:
        # what the model was carrying, paid on every request in the session.
        # `tool_definitions_tokens` is the one with a decision attached: it ran
        # 14,318–34,219 across only 7 distinct values in 57 sessions, which is
        # what a step function looks like, and it steps when an MCP server is
        # connected. Nothing else in this system can price that decision.
        #
        # The other two are here so the composition adds up rather than being
        # one number without a whole — and it does add up: the three sum to
        # `currentTokens` within 4 tokens on all 57, so they are the whole
        # context and not a subset of it. Measured 2026-08-26, and measured
        # because the first figure written here (12–30% of context) was carried
        # over from a plan rather than computed; the real share is 17.1–73.8%,
        # median 34.2%. Multiplying any of them by `request_count` would be
        # modelling, not measurement.
        "tool_definitions_tokens", "system_tokens", "conversation_tokens",
    }),
    "tool.call": frozenset({
        "tool_name", "tool_kind", "duration_ms", "status", "error_class",
    }),
    "human.turn": frozenset({"turn_index", "turn_kind", "chars"}),
    # `acceptance_state` added in 1.1.0 for the same reason as `cost_usd`
    # above: weekly_report.py has grouped outputs by it since it was written,
    # and every event carrying it was stripped at ingest.
    "output.generated": frozenset({
        "output_id", "artifact_type", "file_path", "lines_added", "lines_removed",
        "output_content_hash", "reuse_source", "acceptance_state",
    }),
    "gate.evaluated": frozenset({
        "gate_name", "status", "quality_score", "coverage_pct", "attempt_index",
    }),
    "run.completed": frozenset({"duration_ms", "phases_completed"}),
    "run.failed": frozenset({"duration_ms", "failure_class", "dependency_failed"}),
    "run.timeout": frozenset({"duration_ms", "timeout_policy"}),
    "run.abandoned": frozenset({"last_seen_at"}),
    "scm.commit": frozenset({
        "commit_sha", "output_ids", "lines_added", "lines_removed", "has_ai_marker",
    }),
    "scm.pr.created": frozenset({"pr_id", "commit_shas", "pr_title_has_ai_marker"}),
    "scm.pr.reviewed": frozenset({
        "pr_id", "reviewer_person_id", "action", "comment_count", "reviewed_at",
        # `is_first_review` and `review_lead_time_ms` are metric 3's inputs:
        # first-pass acceptance is a question about the first review, and it
        # cannot be asked of a row that does not say which one that was.
        "is_first_review", "first_review_at", "pr_created_on",
        "review_lead_time_ms", "pr_title_has_ai_marker", "has_ai_marker",
    }),
    "scm.pr.merged": _PR_SHARED | frozenset({
        "merged_at", "merge_commit_sha", "merge_lead_time_ms",
        "review_to_merge_ms",
    }),
    "scm.pr.declined": _PR_SHARED | frozenset({
        "declined_at", "decline_reason_class", "decline_lead_time_ms",
    }),
    "scm.revert": frozenset({
        "reverted_commit_sha", "revert_commit_sha", "days_to_revert",
        # `resolution` says whether the reverted commit was found at all. A
        # revert of something outside the window is not evidence about that
        # something, and without this the two are one number.
        "resolution", "reverted_commit_has_ai_marker",
        "reverted_at", "reverted_commit_at",
    }),
    "ci.pipeline.completed": frozenset({
        "pipeline_id", "commit_sha", "status", "duration_ms", "tests_total",
        "tests_passed", "tests_failed", "coverage_pct",
        # `tests_skipped` belongs beside the other three or the four do not sum
        # to `tests_total`, and metric 8 is a rate over that total.
        "tests_skipped",
        # Provenance for the numbers above. `coverage_source` says where a
        # coverage figure was read from and `ci_system_verified` whether the
        # system was identified or assumed -- an unverified guess and a read
        # value must not arrive looking alike (§1).
        "ci_system", "ci_system_verified", "coverage_source",
        "pipeline_build_number", "trigger_kind", "ref_name",
        "started_at", "completed_at", "step_count", "failed_step_name",
        # `poll_ci.py` has two sources and they do not share a vocabulary.
        # These five are the Jenkins-via-commit-statuses path, which is the one
        # production uses -- CONTRACT.md §3 row 20 records that the CI is
        # self-hosted Jenkins, not Bitbucket Pipelines. The names above are the
        # Pipelines path. Both are listed because both are emitted; the
        # alternative is a list that is right about the source nobody has.
        "ci_provider", "ci_kind", "job_name", "job_branch", "build_number",
    }),
    # 21 — `issue` is the snapshot sub-object CONTRACT.md §3 row 21 mandates: the
    # enum is closed, so there is no separate snapshot event and the snapshot has
    # to ride here. `attribution` carries the AR-3 evidence and the provenance
    # markers — the four `label_*_by_ai` booleans and `unrecognised_ai_labels`,
    # the marker-drift DQ signal. Both were absent from this list while being
    # emitted, so every one of those fields was stripped at ingest; adding a
    # field to poll_jira.py alone is exactly the failure this comment records.
    "jira.transition": frozenset({
        "from_status", "to_status", "transitioned_at", "status_category",
        "issue", "attribution",
        # `from_status_category` completes the pair -- a transition reported
        # with only the destination's category cannot say whether it moved
        # forward. `age_at_transition_ms` is metric 5's input at the grain it
        # is measured. `is_synthesised_creation` marks the one row the poller
        # manufactures, because an issue's creation predates its changelog;
        # unlabelled it would count as a measured transition (§1).
        "from_status_category", "age_at_transition_ms",
        "is_synthesised_creation", "jira_issue_key",
    }),
    # 22 — AIO TCMS test execution. Added 2026-08-20 once an AioAuth key made the
    # source reachable; the enum previously stopped at 21 because it could not be.
    # `test_case_title` is deliberately absent: titles are free text authored by
    # engineers and are exactly the kind of field §11.3 keeps out of the stream.
    "test.run.completed": frozenset({
        "test_case_key", "test_cycle_key", "test_run_id", "status",
        "status_category", "is_automated", "executed_by_person_id",
        "assigned_to_person_id", "executed_at", "effort_seconds",
        "defect_count", "folder_name", "priority",
    }),
    # 23 -- AIO TCMS test case inventory. Separate from event 22 because the
    # Automation Coverage denominator includes cases that have never been run,
    # and those emit no run event at all.
    "test.case.snapshot": frozenset({
        "test_case_key", "automation_status", "automation_owner_person_id",
        "has_automation_key", "test_case_status", "script_type", "folder_name",
        "priority", "is_archived", "created_at", "updated_at",
    }),
}
EVENT_TYPES = frozenset(ATTRIBUTE_ALLOWLIST)

# CONTRACT.md §3 — "Forbidden attribute names (collector rejects the whole event)".
# Matched EXACTLY (case-insensitive) on the key, never as a substring: `input_tokens`
# must not trip on `token`, and `output_content_hash` must not trip on `content`.
FORBIDDEN_ATTRIBUTE_KEYS = frozenset({
    "prompt", "response", "content", "message", "code", "diff", "body", "text",
    "stack_trace", "error_message", "token", "password", "secret", "api_key",
    "email",
})

# CONTRACT.md §2 — envelope allow-lists. Anything else is stripped before publish.
ENVELOPE_SCALARS = (
    "schema_version", "event_id", "event_type", "event_time",
    "trace_id", "run_id", "parent_run_id", "span_id",
)
ACTOR_FIELDS = ("person_id", "person_email_hash", "team_id", "role")
CONTEXT_FIELDS = (
    "jira_issue_key", "jira_project_key", "repo_full_name", "branch_name",
    "product_profile", "environment",
)
AGENT_FIELDS = (
    "agent_name", "agent_version", "skill_name", "skill_version", "surface",
)
LINK_FIELDS = ("method", "confidence")

# Server-set only. A client-supplied value for either is discarded (§2 "ingested_at
# ... set by the collector, never by the client"); dq_flags carries the DQ-10 marks.
SERVER_SET_FIELDS = ("ingested_at", "dq_flags")

# Bounded enums (§1.5 / DQ-15). A value outside the set is flagged, not rejected —
# rejecting would lose an otherwise clean event, and the warehouse re-checks.
BOUNDED_ENUMS = {
    "actor.role": frozenset({"dev", "qa", "devops", "po", "lead"}),
    "context.environment": frozenset({"dev", "sit", "pre", "prd", "local"}),
    "agent.surface": frozenset({
        "vscode-copilot-chat", "copilot-cli", "headless", "unknown",
    }),
    "link.method": frozenset({"explicit", "heuristic", "marker_only"}),
}

# --------------------------------------------------------------------------- #
# Secret screening — patterns reused verbatim in spirit from
# skills/dev-quality-gates/SKILL.md `execute_secret_detection_gate.patterns_to_flag`,
# extended with the Atlassian and GitHub token shapes this org actually issues.
# The skill's own rule applies: "Report file path and line number (never print the
# secret value)" -> here, report the field path and check id, never the value.
# --------------------------------------------------------------------------- #

SECRET_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    # dev-quality-gates: password\s*=\s*['"][^'"]{4,}  (quotes made optional)
    ("secret.password_assignment",
     re.compile(r"password\s*[=:]\s*['\"]?[^'\"\s]{4,}", re.IGNORECASE)),
    # dev-quality-gates: api[_-]?key\s*=\s*['"][^'"]{8,}
    ("secret.api_key_assignment",
     re.compile(r"api[_-]?key\s*[=:]\s*['\"]?[^'\"\s]{8,}", re.IGNORECASE)),
    # dev-quality-gates: secret\s*=\s*['"][^'"]{4,}
    ("secret.secret_assignment",
     re.compile(r"\bsecret\s*[=:]\s*['\"]?[^'\"\s]{4,}", re.IGNORECASE)),
    # dev-quality-gates: BEGIN (RSA|EC|OPENSSH) PRIVATE KEY  (widened to BEGIN * KEY)
    ("secret.private_key_block",
     re.compile(r"-{0,5}BEGIN[ A-Z0-9]*PRIVATE KEY")),
    # dev-quality-gates: AKIA[0-9A-Z]{16}
    ("secret.aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # Atlassian API token (JIRA_API_TOKEN / CONFLUENCE_API_TOKEN in .github/.env)
    ("secret.atlassian_api_token",
     re.compile(r"\bATATT[A-Za-z0-9_\-=]{16,}")),
    # GitHub PAT / OAuth / user / server / refresh token
    ("secret.github_token",
     re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b")),
)


class RejectionError(Exception):
    """Raised when an event must be rejected. Carries NO value, by construction."""

    def __init__(self, check_id: str, reason: str, field: Optional[str] = None):
        super().__init__(f"{check_id}: {reason}" + (f" (field={field})" if field else ""))
        self.check_id = check_id
        self.reason = reason
        self.field = field

    def as_dict(self) -> Dict[str, Any]:
        return {"check_id": self.check_id, "reason": self.reason, "field": self.field}


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_rfc3339(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# Redaction enforcement
# --------------------------------------------------------------------------- #

def _walk(node: Any, path: str) -> Iterable[Tuple[str, Optional[str], Any]]:
    """Yield (path, key_or_None, value) for every node in a JSON document."""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            yield child, str(key), value
            yield from _walk(value, child)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            child = f"{path}[{index}]"
            yield child, None, value
            yield from _walk(value, child)


def screen_forbidden_keys(event: Dict[str, Any]) -> None:
    """CONTRACT.md §3 forbidden attribute names -> reject the whole event.

    Exact, case-insensitive match on the key name. Applied recursively so a nested
    object cannot smuggle `prompt` one level down.
    """
    for path, key, _value in _walk(event, ""):
        if key is None:
            continue
        if key.strip().lower() in FORBIDDEN_ATTRIBUTE_KEYS:
            raise RejectionError(
                "redaction.forbidden_attribute_key",
                "attribute key is on the CONTRACT.md §3 forbidden list",
                field=path,
            )


def screen_secret_values(event: Dict[str, Any]) -> None:
    """Regex screening of every string value (design §11.3, dev-quality-gates)."""
    for path, _key, value in _walk(event, ""):
        if not isinstance(value, str):
            continue
        for check_id, pattern in SECRET_PATTERNS:
            if pattern.search(value):
                # The value is NEVER interpolated into the exception or the log.
                raise RejectionError(
                    check_id,
                    "string value matched a secret pattern",
                    field=path,
                )


# --------------------------------------------------------------------------- #
# Validation + normalisation (allow-list, never deny-list)
# --------------------------------------------------------------------------- #

def _require(event: Dict[str, Any], key: str) -> Any:
    if key not in event or event[key] in (None, ""):
        raise RejectionError("envelope.missing_field", "required field is missing", field=key)
    return event[key]


def validate_schema_version(value: Any) -> str:
    if not isinstance(value, str):
        raise RejectionError("envelope.schema_version_invalid",
                             "schema_version must be a semver string",
                             field="schema_version")
    parts = value.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise RejectionError("envelope.schema_version_invalid",
                             "schema_version must be MAJOR.MINOR.PATCH",
                             field="schema_version")
    if int(parts[0]) != SUPPORTED_MAJOR:
        raise RejectionError(
            "envelope.schema_version_unsupported",
            f"unsupported major version (collector understands {SUPPORTED_MAJOR}.x.y)",
            field="schema_version",
        )
    return value


def validate_event_type(value: Any) -> str:
    if not isinstance(value, str) or value not in EVENT_TYPES:
        raise RejectionError(
            "envelope.event_type_unknown",
            "event_type is not in the CONTRACT.md §3 closed enum",
            field="event_type",
        )
    return value


def _subset(source: Any, fields: Iterable[str]) -> Dict[str, Any]:
    obj = source if isinstance(source, dict) else {}
    return {name: obj.get(name) for name in fields}


def _clamp_timestamps(
    event_time: Optional[datetime],
    attributes: Dict[str, Any],
    now: datetime,
) -> Tuple[datetime, Dict[str, Any], List[str]]:
    """DQ-10 — clamp clock skew and out-of-order timestamps, flag both."""
    flags: List[str] = []

    if event_time is None:
        flags.append("DQ-10:event_time_unparseable")
        event_time = now
    elif event_time > now + MAX_FUTURE_SKEW:
        flags.append("DQ-10:event_time_future_clamped")
        event_time = now

    # An end timestamp that precedes its start is clamped up to the start.
    pairs = (
        ("started_at", "ended_at"),
        ("start_time", "end_time"),
        ("created_at", "merged_at"),
        ("created_at", "declined_at"),
        ("created_at", "reviewed_at"),
    )
    for start_key, end_key in pairs:
        start = parse_rfc3339(attributes.get(start_key))
        end = parse_rfc3339(attributes.get(end_key))
        if start and end and end < start:
            attributes[end_key] = rfc3339(start)
            flags.append(f"DQ-10:{end_key}_before_{start_key}_clamped")

    # Any lone *_at attribute that is itself in the future gets clamped too.
    for key, value in list(attributes.items()):
        if not key.endswith(("_at", "_time")):
            continue
        stamp = parse_rfc3339(value)
        if stamp and stamp > now + MAX_FUTURE_SKEW:
            attributes[key] = rfc3339(now)
            flags.append(f"DQ-10:{key}_future_clamped")

    # A negative duration is the same class of defect.
    for key in ("duration_ms", "latency_ms"):
        value = attributes.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0:
            attributes[key] = 0
            flags.append(f"DQ-10:{key}_negative_clamped")

    return event_time, attributes, flags


def normalise_event(raw: Any, now: Optional[datetime] = None) -> Tuple[Dict[str, Any], List[str]]:
    """Validate, screen, strip to the contract allow-list and stamp server fields.

    Returns (published_envelope, dq_flags). Raises RejectionError on any rejection.
    """
    now = now or utcnow()

    if not isinstance(raw, dict):
        raise RejectionError("envelope.not_an_object", "event must be a JSON object")

    # 1. Redaction enforcement FIRST — before anything is copied or logged, and
    #    before stripping, so a forbidden key cannot be silently dropped instead
    #    of rejected (§11.3: reject, do not sanitise).
    screen_forbidden_keys(raw)
    screen_secret_values(raw)

    # 2. Envelope validation.
    validate_schema_version(_require(raw, "schema_version"))
    event_type = validate_event_type(_require(raw, "event_type"))
    for required in ("event_id", "event_time", "trace_id"):
        _require(raw, required)
    # `run_id` must be PRESENT and may be NULL, which is not the same rule.
    #
    # CONTRACT.md §2.4 is explicit -- "Poller events carry `run_id = null`
    # unless the commit carries an `AI-Run-Id` trailer" -- and this loop used
    # to demand a non-null value for every event type, so the collector
    # rejected the exact shape the contract mandates. Measured 2026-08-26
    # against real data: **all 57 `model.call` events** built from Copilot's
    # session journal were refused with `envelope.missing_field: run_id`, and
    # those are the rows carrying every token count and every premium request
    # -- the whole Cost section of the weekly report.
    #
    # It survived 798 tests because the collector's own `make_event` fixture
    # always stamped a run id, so the contract's mandated case was the one
    # case never exercised.
    #
    # Present-but-null still has to be distinguished from absent: an event
    # that simply forgot the field is malformed, and silently reading it as
    # "no run" would turn a client bug into an unattributed row.
    if "run_id" not in raw:
        raise RejectionError("envelope.missing_field",
                             "required field is missing", field="run_id")

    # 3. Allow-list strip. Anything not named in CONTRACT.md §2/§3 disappears.
    out: Dict[str, Any] = {name: raw.get(name) for name in ENVELOPE_SCALARS}
    out["actor"] = _subset(raw.get("actor"), ACTOR_FIELDS)
    out["context"] = _subset(raw.get("context"), CONTEXT_FIELDS)
    out["agent"] = _subset(raw.get("agent"), AGENT_FIELDS)
    out["link"] = _subset(raw.get("link"), LINK_FIELDS)

    allowed_attributes = ATTRIBUTE_ALLOWLIST[event_type]
    raw_attributes = raw.get("attributes")
    raw_attributes = raw_attributes if isinstance(raw_attributes, dict) else {}
    attributes = {k: v for k, v in raw_attributes.items() if k in allowed_attributes}
    dropped = sorted(set(raw_attributes) - allowed_attributes)

    flags: List[str] = []
    if dropped:
        # Field NAMES only, never values.
        flags.append("strip:attributes:" + ",".join(dropped))

    # 4. Bounded-enum soft check (DQ-15 spirit).
    for path, allowed in BOUNDED_ENUMS.items():
        section, field = path.split(".")
        value = out[section].get(field)
        if value is not None and value not in allowed:
            out[section][field] = None
            flags.append(f"DQ-15:{path}_out_of_enum")

    # 5. DQ-10 clock skew.
    event_time, attributes, skew_flags = _clamp_timestamps(
        parse_rfc3339(raw.get("event_time")), attributes, now
    )
    flags.extend(skew_flags)

    out["event_time"] = rfc3339(event_time)
    out["attributes"] = attributes

    # 6. Server-set fields. Client values for these were dropped by the strip above.
    out["ingested_at"] = rfc3339(now)
    out["dq_flags"] = flags

    return out, flags


# --------------------------------------------------------------------------- #
# Idempotency — DQ-11, in-process TTL cache
# --------------------------------------------------------------------------- #

class TTLCache:
    def __init__(self, ttl_seconds: int, max_entries: int = 200_000):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._entries: Dict[str, float] = {}
        self._lock = threading.Lock()

    def add_if_absent(self, key: str, now: Optional[float] = None) -> bool:
        """True when the key was new; False when it is a duplicate inside the TTL."""
        now = now if now is not None else time.monotonic()
        with self._lock:
            self._evict(now)
            expiry = self._entries.get(key)
            if expiry is not None and expiry > now:
                return False
            self._entries[key] = now + self.ttl
            return True

    def _evict(self, now: float) -> None:
        if len(self._entries) < self.max_entries:
            expired = [k for k, exp in self._entries.items() if exp <= now]
        else:
            expired = [k for k, exp in self._entries.items() if exp <= now] or \
                      sorted(self._entries, key=self._entries.get)[: self.max_entries // 10]
        for key in expired:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# --------------------------------------------------------------------------- #
# Publishing — Pub/Sub with a local NDJSON fallback so nothing is lost
# --------------------------------------------------------------------------- #

class Publisher:
    """Publishes to Pub/Sub `ai-run-events`; spills to a local NDJSON file on failure.

    The fallback is not best-effort decoration: contract rule §1.2 (fail open) means
    an ingest that cannot reach Pub/Sub must still return 200 and keep the payload.
    The spill file is drained by the deploy/README procedure.
    """

    def __init__(
        self,
        project: Optional[str] = None,
        topic: Optional[str] = None,
        fallback_path: Optional[str] = None,
    ):
        self.project = project or os.environ.get(ENV_PROJECT) or ""
        self.topic = topic or os.environ.get(ENV_TOPIC) or DEFAULT_TOPIC
        self.fallback_path = fallback_path or os.environ.get(ENV_FALLBACK) or DEFAULT_FALLBACK
        self._lock = threading.Lock()
        self._client = None
        self._topic_path = None
        self._init_client()

    def _init_client(self) -> None:
        if not self.project:
            LOG.info("pubsub disabled (no %s); using local spill file", ENV_PROJECT)
            return
        try:
            from google.cloud import pubsub_v1  # type: ignore

            self._client = pubsub_v1.PublisherClient()
            self._topic_path = self._client.topic_path(self.project, self.topic)
            LOG.info("pubsub publisher ready for topic %s", self.topic)
        except Exception as exc:  # noqa: BLE001 - fail open, never raise on startup
            self._client = None
            LOG.warning("pubsub unavailable (%s); using local spill file", type(exc).__name__)

    # -- sinks ------------------------------------------------------------- #

    def _publish_pubsub(self, envelope: Dict[str, Any]) -> None:
        payload = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        future = self._client.publish(  # type: ignore[union-attr]
            self._topic_path,
            payload,
            event_id=str(envelope.get("event_id", "")),
            event_type=str(envelope.get("event_type", "")),
            schema_version=str(envelope.get("schema_version", "")),
        )
        future.result(timeout=10)

    def _publish_file(self, envelope: Dict[str, Any]) -> None:
        directory = os.path.dirname(self.fallback_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        line = json.dumps(envelope, separators=(",", ":"))
        with self._lock, open(self.fallback_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def publish(self, envelope: Dict[str, Any]) -> str:
        """Returns the sink actually used: 'pubsub' or 'file'."""
        if self._client is not None:
            try:
                self._publish_pubsub(envelope)
                return "pubsub"
            except Exception as exc:  # noqa: BLE001 - fall back, never lose the event
                LOG.warning(
                    "pubsub publish failed (%s) for event_id=%s; spilling to file",
                    type(exc).__name__, envelope.get("event_id"),
                )
        self._publish_file(envelope)
        return "file"


class FindingsSink:
    """Writes dq_findings rows (design §9.4). Field names and check ids only."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.environ.get(ENV_FINDINGS) or DEFAULT_FINDINGS
        self._lock = threading.Lock()

    def write(self, finding: Dict[str, Any]) -> None:
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with self._lock, open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(finding, separators=(",", ":")) + "\n")
        except Exception as exc:  # noqa: BLE001 - fail open
            LOG.warning("could not persist dq finding (%s)", type(exc).__name__)


# --------------------------------------------------------------------------- #
# Collector
# --------------------------------------------------------------------------- #

class Collector:
    def __init__(
        self,
        token: Optional[str] = None,
        publisher: Optional[Publisher] = None,
        findings: Optional[FindingsSink] = None,
        dedup_ttl: Optional[int] = None,
    ):
        self.token = token if token is not None else os.environ.get(ENV_TOKEN, "")
        self.publisher = publisher or Publisher()
        self.findings = findings or FindingsSink()
        ttl = dedup_ttl if dedup_ttl is not None else int(
            os.environ.get(ENV_DEDUP_TTL, DEFAULT_DEDUP_TTL)
        )
        self.dedup = TTLCache(ttl)
        self.max_body = int(os.environ.get(ENV_MAX_BODY, DEFAULT_MAX_BODY))

    # -- auth --------------------------------------------------------------- #

    def authenticate(self, header_value: Optional[str]) -> bool:
        """Constant-time bearer comparison. No configured token => deny everything."""
        expected = self.token or ""
        if not expected:
            return False
        presented = ""
        if isinstance(header_value, str):
            prefix, _, rest = header_value.partition(" ")
            if prefix.lower() == "bearer":
                presented = rest.strip()
        # compare_digest on bytes; still called on the empty case to keep the
        # code path uniform (the branch above already decided nothing).
        return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))

    # -- body parsing -------------------------------------------------------- #

    @staticmethod
    def parse_body(body: bytes) -> List[Any]:
        """Accept a JSON array, a single JSON object, or NDJSON."""
        text = body.decode("utf-8", errors="strict").strip()
        if not text:
            return []
        if text[0] == "[":
            parsed = json.loads(text)
            if not isinstance(parsed, list):
                raise ValueError("top-level JSON must be an array or an object")
            return parsed
        if text[0] == "{" and "\n" not in text.strip():
            return [json.loads(text)]
        events: List[Any] = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
        return events

    # -- ingest -------------------------------------------------------------- #

    def _record_rejection(self, rejection: RejectionError, event_id: Optional[str]) -> None:
        # LOGS THE FIELD NAME AND THE CHECK ID ONLY. Never the value. (§11.3)
        LOG.warning(
            "dq_payload_rejected check_id=%s field=%s reason=%s event_id=%s",
            rejection.check_id, rejection.field, rejection.reason, event_id or "unknown",
        )
        self.findings.write({
            "finding_id": "dqf_" + uuid.uuid4().hex,
            "check_id": "dq_payload_rejected",
            "sub_check_id": rejection.check_id,
            "detected_at": rfc3339(utcnow()),
            "severity": "blocker",
            "event_id": event_id,
            "field": rejection.field,       # name only
            "reason": rejection.reason,     # no value, by construction
            "action": "rejected",
        })

    def ingest(self, events: List[Any]) -> Dict[str, Any]:
        accepted = 0
        duplicates = 0
        sinks: Dict[str, int] = {}
        rejections: List[Dict[str, Any]] = []

        for raw in events:
            event_id = raw.get("event_id") if isinstance(raw, dict) else None
            try:
                envelope, _flags = normalise_event(raw)
            except RejectionError as rejection:
                self._record_rejection(rejection, event_id if isinstance(event_id, str) else None)
                rejections.append({**rejection.as_dict(), "event_id": event_id})
                continue

            if not self.dedup.add_if_absent(envelope["event_id"]):
                duplicates += 1
                LOG.info("DQ-11 duplicate dropped event_id=%s", envelope["event_id"])
                continue

            sink = self.publisher.publish(envelope)
            sinks[sink] = sinks.get(sink, 0) + 1
            accepted += 1

        return {
            "accepted": accepted,
            "duplicates": duplicates,
            "rejected": len(rejections),
            "rejections": rejections,
            "sinks": sinks,
        }


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

def make_handler(collector: Collector):
    class Handler(BaseHTTPRequestHandler):
        server_version = "aiep-collector/1.0"
        protocol_version = "HTTP/1.1"

        # -- helpers -------------------------------------------------------- #

        def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            LOG.debug("http %s", fmt % args)

        # -- routes --------------------------------------------------------- #

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?")[0] == "/healthz":
                self._send_json(HTTPStatus.OK, {"status": "ok"})
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            if path != "/v1/events":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return

            if not collector.authenticate(self.headers.get("Authorization")):
                LOG.warning("unauthorised request to /v1/events")
                self.send_response(HTTPStatus.UNAUTHORIZED)
                self.send_header("WWW-Authenticate", 'Bearer realm="aiep-collector"')
                body = json.dumps({"error": "unauthorized"}).encode("utf-8")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > collector.max_body:
                self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                                {"error": "payload_too_large"})
                return
            body = self.rfile.read(length) if length else b""

            try:
                events = collector.parse_body(body)
            except (ValueError, UnicodeDecodeError) as exc:
                LOG.warning("malformed request body (%s)", type(exc).__name__)
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "malformed_body"})
                return

            if not events:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "empty_batch"})
                return

            result = collector.ingest(events)
            status = HTTPStatus.BAD_REQUEST if result["rejected"] else HTTPStatus.OK
            self._send_json(status, result)

    return Handler


def create_server(collector: Collector, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(collector))
    server.daemon_threads = True
    return server


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AI telemetry ingestion collector")
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - Cloud Run requires it
    parser.add_argument("--port", type=int, default=int(os.environ.get(ENV_PORT, 8080)))
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    collector = Collector()
    if not collector.token:
        LOG.error("%s is not set — every request will be rejected with 401", ENV_TOKEN)

    server = create_server(collector, args.host, args.port)
    LOG.info("collector listening on %s:%s", args.host, server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
