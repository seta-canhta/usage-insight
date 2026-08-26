"""Shared plumbing for the AI-telemetry backfill pollers.

Conforms to ``schema/CONTRACT.md`` v1.0.0. That file is the
single source of truth; nothing here may redefine an envelope, an event type or
an attribute name locally.

What lives here
---------------
* :class:`Config`          -- environment/`.env` configuration loading.
* :class:`HttpClient`      -- GET with retry, exponential backoff + jitter and
                              429 ``Retry-After`` handling. The network
                              transport is injectable so tests never touch the
                              network.
* :func:`paginate`         -- follows Bitbucket's ``next`` links to completion.
* :func:`build_event`      -- envelope construction per CONTRACT.md §2.
* :func:`validate_event`   -- two-layer redaction guard per §3: exact-match on
                              lowercased attribute key names (never substring),
                              plus a separate regex screen of string values for
                              secrets and raw email addresses.
* :class:`NdjsonWriter`    -- newline-delimited JSON output, written atomically.
* :class:`WatermarkStore`  -- per-source last-successful-poll timestamps, so
                              runs are incremental. The watermark advances
                              **only** on a clean, complete run.
* AI marker detection      -- the *narrow*, marker-only detectors (see below).

Hard rules honoured by this module
----------------------------------
* Never log, print or emit credentials, tokens, prompts, source code, diffs or
  raw email addresses. Errors carry an HTTP status and a sanitised URL, never a
  response body.
* ``event_id`` is deterministic (a hash of the event's natural key), so a
  re-poll of the same fact produces the same id and de-duplicates cleanly
  (CONTRACT.md §1 rule 3).
* ``ingested_at`` is always ``None`` here -- the collector sets it, never a
  client (CONTRACT.md §2).

Dependencies: standard library only. ``requests`` is used when importable and
``urllib`` otherwise; the detection is lazy, following the pattern in
``skills/bigquery/bq_tool.py``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import (Any, Callable, Collection, Dict, Iterable, Iterator, List,
                    Optional, Sequence, Tuple)

SCHEMA_VERSION = "1.1.0"
POLLER_VERSION = "0.1.0"
USER_AGENT = f"aiep-ai-telemetry-poller/{POLLER_VERSION}"

# ---------------------------------------------------------------------------
# CONTRACT.md §3 -- closed enum of event types. Unknown values are rejected by
# the collector, so we reject them here too rather than shipping garbage.
# ---------------------------------------------------------------------------

EVENT_TYPES = frozenset(
    {
        "run.started",
        "run.bound",
        "run.phase.started",
        "run.phase.completed",
        "model.call",
        "tool.call",
        "human.turn",
        "output.generated",
        "gate.evaluated",
        "run.completed",
        "run.failed",
        "run.timeout",
        "run.abandoned",
        "scm.commit",
        "scm.pr.created",
        "scm.pr.reviewed",
        "scm.pr.merged",
        "scm.pr.declined",
        "scm.revert",
        "ci.pipeline.completed",
        "jira.transition",
        "test.run.completed",
        "test.case.snapshot",
    }
)

# CONTRACT.md §3 -- an event carrying any of these attribute names is rejected
# wholesale by the collector.
FORBIDDEN_ATTRIBUTE_NAMES = frozenset(
    {
        "prompt",
        "response",
        "content",
        "message",
        "code",
        "diff",
        "body",
        "text",
        "stack_trace",
        "error_message",
        "token",
        "password",
        "secret",
        "api_key",
        "email",
    }
)

# CONTRACT.md §3, second layer: string *values* are screened against the secret
# patterns from skills/dev-quality-gates/SKILL.md plus Atlassian/GitHub token
# prefixes. Value screening is regex/substring by nature -- that is correct here
# and must NOT be confused with the key-name guard, which is exact-match only.
SECRET_VALUE_CHECKS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("password_assignment", re.compile(r"password\s*=", re.IGNORECASE)),
    ("api_key_literal", re.compile(r"api[_-]?key", re.IGNORECASE)),
    ("private_key_block", re.compile(r"BEGIN[ A-Z]*PRIVATE KEY")),
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("atlassian_api_token", re.compile(r"ATATT[A-Za-z0-9_\-=]{8,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}")),
)

ENVELOPE_KEYS = (
    "schema_version",
    "event_id",
    "event_type",
    "event_time",
    "ingested_at",
    "trace_id",
    "run_id",
    "parent_run_id",
    "span_id",
    "actor",
    "context",
    "agent",
    "attributes",
    "link",
)

LINK_METHODS = frozenset({"explicit", "heuristic", "marker_only"})

_EMAIL_RE = re.compile(r"[^\s@<>,;:\"']+@[^\s@<>,;:\"']+\.[A-Za-z]{2,}")

# ---------------------------------------------------------------------------
# AI marker detection
#
# DELIBERATELY NARROW. Widen only by NAMING another marker in the closed set
# below -- never by matching a *shape*, a prefix, or a keyword.
#
# `skills/aiep-impact-report-generator` on origin/feature/ATSP-22288 ends its
# keyword pattern with `|^feat[\(:]|^feat\b|^fix[\(:]|^fix\b|^chore[\(:]|^chore\b`.
# Because `.github/copilot-instructions.md` §5 mandates Conventional Commits
# repo-wide, that clause classifies every conventionally-formatted *human*
# commit as AI-authored (design §3.5: 43 genuinely marked commits out of 354).
# The boundary guards below are what keep that failure mode out, and they stay.
# We match ONLY:
#   1. the explicit commit-subject markers in `AI_COMMIT_MARKERS`, in any of the
#      four placements that occur in real history (prefix bare / prefix
#      bracketed / infix / suffix),
#   2. the explicit `[Authored By Copilot]` PR-title marker,
#   3. the `AI-Run-Id:` git trailer (CONTRACT.md §9) -- the only *explicit* link.
#
# Why there are two closed sets, not one
# --------------------------------------
# The AIEP flow applies four provenance markers, and they do NOT all live in the
# same place:
#
#   AUTH_BY_COPILOT     Jira label AND commit-subject prefix
#   GEN_BY_COPILOT      commit-subject ONLY -- no Jira label exists today
#                       (supervisor-test-spec.agent.md:958,1181;
#                        test-executor-committer.agent.md:267,270)
#   PLANNED_BY_COPILOT  Jira label ONLY (architect.planner, test-spec-generator)
#   REVIEW_BY_COPILOT   Jira label ONLY, applied by an EXTERNAL AI code review
#                       system that tags the ticket back when it is done
#
# So `AI_COMMIT_MARKERS` (2) is a strict subset of `AI_LABELS` (4). Leaving
# GEN_BY_COPILOT out of the commit regex is why a 60-day pull of
# acme/qa-automation reported `ai_commit_count = 0` across all 102 PRs and
# we wrongly concluded that no AI attribution exists in SCM.
#
# Conversely, PLANNED_/REVIEW_BY_COPILOT must NOT enter the commit regex: no
# agent writes them onto a subject, so a commit saying "PLANNED_BY_COPILOT label
# handling" is a human commit *about* the feature. There is a test for that.
# ---------------------------------------------------------------------------

#: Closed set of markers an agent writes onto a **commit subject**.
AI_COMMIT_MARKER_AUTHORED = "AUTH_BY_COPILOT"
AI_COMMIT_MARKER_GENERATED = "GEN_BY_COPILOT"
AI_COMMIT_MARKERS = (AI_COMMIT_MARKER_AUTHORED, AI_COMMIT_MARKER_GENERATED)

#: Matches a commit marker wherever it sits on the commit subject:
#:   "AUTH_BY_COPILOT: add route"        (prefix, bare)
#:   "[GEN_BY_COPILOT] [PRJ-6383] add"   (prefix, bracketed)
#:   "[PRJ-6383] [AUTH_BY_COPILOT] : add rate limit"  (infix)
#:   "add rate limit GEN_BY_COPILOT"     (suffix)
#: The boundaries stop it matching a longer identifier that merely contains one
#: of them -- `XAUTH_BY_COPILOTX`, `OXYGEN_BY_COPILOT`, `MY_GEN_BY_COPILOTS`.
AI_COMMIT_MARKER_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    + "|".join(re.escape(marker) for marker in AI_COMMIT_MARKERS)
    + r")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

#: PR title marker, mandated by skills/bitbucket-ops/commands.md §9.
AI_PR_TITLE_MARKER_RE = re.compile(r"\[\s*Authored\s+By\s+Copilot\s*\]", re.IGNORECASE)

#: Git trailers appended by prepare-commit-msg (CONTRACT.md §9).
AI_TRAILER_RE = re.compile(
    r"^\s*(AI-Run-Id|AI-Trace-Id|AI-Agent|AI-Model)\s*:\s*(\S.*?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

#: Jira labels in the provenance closed set (design §4.1). All four markers the
#: AIEP flow applies appear here; only the first two are ALSO commit markers, and
#: GEN_BY_COPILOT currently has no Jira-label writer at all (it is kept in the set
#: so that the day one appears it is counted rather than silently dropped).
#: REVIEW_BY_COPILOT is applied by an external AI code-review system, not by
#: anything in this repository.
AI_LABEL_AUTHORED = "AUTH_BY_COPILOT"
AI_LABEL_PLANNED = "PLANNED_BY_COPILOT"
AI_LABEL_GENERATED = "GEN_BY_COPILOT"
AI_LABEL_REVIEWED = "REVIEW_BY_COPILOT"
AI_LABELS = (
    AI_LABEL_AUTHORED,
    AI_LABEL_PLANNED,
    AI_LABEL_GENERATED,
    AI_LABEL_REVIEWED,
)

#: A label that *looks* like it was meant to be a provenance marker.
#:
#: This shape is used ONLY to raise a data-quality signal. It must never be used
#: to classify a label as AI: doing so would make the AI figure depend on
#: whatever anyone happened to type, which is exactly the "widen to a shape"
#: mistake the block above forbids. Anything matching this that is not in
#: `AI_LABELS` is drift -- it was intended to mark AI work and is instead
#: silently subtracting from the AI figure.
#:
#: Live examples found on this org's Jira: PLANNER_BY_COPILOT (typo of
#: PLANNED_), DEV_BY_COPILOT, COPILOT_TESTING. The guards allow `_`/`-` on either
#: side (so `COPILOT_TESTING` and `PLANNER_BY_COPILOT` both match) but not
#: letters or digits (so `COPILOTS` does not).
AI_LABEL_DRIFT_SHAPE_RE = re.compile(r"(?<![A-Za-z0-9])COPILOT(?![A-Za-z0-9])", re.IGNORECASE)

#: The spine of the whole model (design §5.2).
JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")

#: "Revert "subject"" / "Revert: subject" -- git's own revert subject form.
REVERT_SUBJECT_RE = re.compile(r"^\s*Revert[\s:\"]", re.IGNORECASE)

#: git revert bodies carry: This reverts commit <sha>.
REVERTS_COMMIT_RE = re.compile(
    r"This\s+reverts\s+commit\s+([0-9a-f]{7,40})", re.IGNORECASE
)

#: Quoted original subject inside a revert subject: Revert "feat(api): add x"
REVERT_QUOTED_SUBJECT_RE = re.compile(r'^\s*Revert[:\s]+"(.+)"\s*$', re.IGNORECASE)

_BOT_HINTS = (
    "bot",
    "jenkins",
    "sonarqube",
    "sonarcloud",
    "snyk",
    "dependabot",
    "renovate",
    "codecov",
    "coveralls",
    "pipelines",
    "automation",
    "service-account",
    "svc-",
    "noreply",
)


def has_ai_commit_marker(subject: Optional[str]) -> bool:
    """True when a commit subject carries an explicit commit marker.

    The closed set is ``AI_COMMIT_MARKERS`` (``AUTH_BY_COPILOT``,
    ``GEN_BY_COPILOT``). Matches prefix (bare and bracketed), infix and suffix
    placements. Does NOT match conventional commit prefixes -- ``feat(api): add
    endpoint`` is a human commit -- and does NOT match the label-only markers
    ``PLANNED_BY_COPILOT`` / ``REVIEW_BY_COPILOT``.
    """
    if not subject:
        return False
    first_line = subject.splitlines()[0] if "\n" in subject else subject
    return bool(AI_COMMIT_MARKER_RE.search(first_line))


def unrecognised_ai_labels(labels: Optional[Sequence[Any]]) -> List[str]:
    """Labels shaped like a provenance marker but outside the closed ``AI_LABELS``.

    A data-quality signal, never a classification: these are deliberately NOT
    counted as AI. Each one is a marker somebody meant to apply, so each one
    silently subtracts from the AI figure until it is reconciled -- which is
    precisely why the names have to reach a report instead of being dropped.

    Returns upper-cased, de-duplicated names in sorted order. Label names are
    bounded operator-chosen vocabulary, not content (CONTRACT.md §1.1).
    """
    known = set(AI_LABELS)
    found = set()
    for label in labels or []:
        text = str(label).strip().upper()
        if not text or text in known:
            continue
        if AI_LABEL_DRIFT_SHAPE_RE.search(text):
            found.add(text)
    return sorted(found)


def has_ai_pr_title_marker(title: Optional[str]) -> bool:
    """True when a PR title carries the explicit ``[Authored By Copilot]`` marker."""
    if not title:
        return False
    return bool(AI_PR_TITLE_MARKER_RE.search(title))


def parse_ai_trailers(commit_message: Optional[str]) -> Dict[str, str]:
    """Extract the AI-* git trailers (CONTRACT.md §9) from a commit message.

    Returns a dict with lower-cased trailer keys, e.g. ``{"ai-run-id": "run_..."}``.
    Only identifiers are returned; the message itself is never retained.
    """
    if not commit_message:
        return {}
    out: Dict[str, str] = {}
    for key, value in AI_TRAILER_RE.findall(commit_message):
        out[key.lower()] = value
    return out


def extract_jira_key(*candidates: Optional[str],
                     projects: Optional[Collection[str]] = None) -> Optional[str]:
    """First Jira issue key found across the candidate strings, else None.

    ``projects`` is an allow-list of real project keys. **Pass it wherever one
    can be had.** Without it this function will return anything shaped like a
    key, and "shaped like a key" is not the same as "is a key".

    Measured 2026-08-26 against a live repository, 12 pull requests: of the
    seven distinct keys extracted from branch names and PR titles, **three did
    not exist** in the 218 projects on the Jira site --

        fix/AUG-25   -> "AUG-25"    a date. August 25.
        fix/AUG-24   -> "AUG-24"    a date.
        CY-199       -> "CY-199"    not a project.
        TC-12018     -> "TC-12018"  not a project.

    -- and they outnumbered the real ones (`IML-*`, `APR-*`) in the events
    emitted. A branch called `fix/Aug-21` escaped only because of its
    lowercase letters, which is luck, not a rule.

    CONTRACT.md §2.4: *"Never synthesise a `run_id` to force a join -- that
    manufactures a join key and breaches AR-1."* A fabricated `jira_issue_key`
    is the same offence in a different column, and worse for being plausible:
    `AUG-25` looks exactly like a ticket, so nothing downstream has any reason
    to doubt it. It attributes real engineering work to a ticket that does not
    exist.

    An allow-list, not a list of things to reject. A deny-list here would have
    to anticipate every month abbreviation, every `TC-`, every `CY-`, and every
    convention a team invents next quarter; the allow-list only has to know
    what Jira says exists, which Jira will tell you.
    """
    allowed = {p.upper() for p in projects} if projects is not None else None
    for candidate in candidates:
        if not candidate:
            continue
        for match in JIRA_KEY_RE.finditer(candidate):
            key = match.group(1)
            if allowed is None or key.split("-", 1)[0].upper() in allowed:
                return key
    return None


def jira_project_key(issue_key: Optional[str]) -> Optional[str]:
    if not issue_key or "-" not in issue_key:
        return None
    return issue_key.split("-", 1)[0]


def looks_like_bot(user: Optional[Dict[str, Any]]) -> bool:
    """Heuristic bot detection for comment authors.

    Display names/nicknames are inspected locally to make this decision but are
    never emitted (design §9.4 -- names are not identity keys).
    """
    if not user:
        return True  # an author-less comment is machinery, not a reviewer
    if user.get("type") in {"app", "addon"}:
        return True
    names = " ".join(
        str(user.get(k) or "") for k in ("nickname", "display_name", "username")
    ).lower()
    if any(hint in names for hint in _BOT_HINTS):
        return True
    # No name at all and no Atlassian account -> machinery.
    return not names.strip() and not user.get("account_id")


def actor_key(user: Optional[Dict[str, Any]]) -> Optional[str]:
    """Stable local comparison key for a Bitbucket user.

    Prefers the Atlassian ``account_id``; falls back to ``uuid``. Never a name.
    Used only for "is this the PR author?" comparisons, never emitted as an
    identity unless it is an account_id.
    """
    if not user:
        return None
    return user.get("account_id") or user.get("uuid") or None


def person_id_of(user: Optional[Dict[str, Any]]) -> Optional[str]:
    """The canonical person key: the Atlassian accountId, or None.

    Never returns a display name, nickname or email (CONTRACT.md §2.1).
    """
    if not user:
        return None
    account_id = user.get("account_id")
    return account_id if isinstance(account_id, str) and account_id else None


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?"
    r"(Z|[+-]\d{2}:?\d{2})?$"
)


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Tolerant RFC3339/ISO8601 parser.

    Handles Bitbucket (``2026-08-19T10:00:00.123456+00:00``) and Jira
    (``2026-08-19T10:00:00.000+0700``) shapes on every supported Python.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    match = _TS_RE.match(str(value).strip())
    if not match:
        return None
    year, month, day, hour, minute, second, frac, offset = match.groups()
    micro = 0
    if frac:
        micro = int((frac + "000000")[:6])
    if offset in (None, "Z", "z"):
        tz = timezone.utc
    else:
        sign = 1 if offset[0] == "+" else -1
        digits = offset[1:].replace(":", "")
        tz = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4])))
    return datetime(
        int(year), int(month), int(day), int(hour), int(minute), int(second), micro, tz
    )


def to_rfc3339(value: Optional[Any]) -> Optional[str]:
    """Normalise a timestamp (string or datetime) to RFC3339 UTC, or None."""
    dt = value if isinstance(value, datetime) else parse_ts(value)
    if dt is None:
        return None
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}"[:3] + "Z"


def now_rfc3339() -> str:
    return to_rfc3339(datetime.now(timezone.utc))  # type: ignore[return-value]


def days_between(earlier: Optional[str], later: Optional[str]) -> Optional[float]:
    """Whole-and-fractional days between two timestamps, rounded to 4 dp."""
    start, end = parse_ts(earlier), parse_ts(later)
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() / 86400.0, 4)


def ms_between(earlier: Optional[str], later: Optional[str]) -> Optional[int]:
    start, end = parse_ts(earlier), parse_ts(later)
    if start is None or end is None:
        return None
    return int(round((end - start).total_seconds() * 1000.0))


def min_ts(*values: Optional[str]) -> Optional[str]:
    """Earliest of the given timestamps (Nones ignored), normalised."""
    parsed = [(parse_ts(v), v) for v in values]
    live = [(dt, raw) for dt, raw in parsed if dt is not None]
    if not live:
        return None
    return to_rfc3339(min(live, key=lambda pair: pair[0])[0])


def max_ts(*values: Optional[str]) -> Optional[str]:
    parsed = [(parse_ts(v), v) for v in values]
    live = [(dt, raw) for dt, raw in parsed if dt is not None]
    if not live:
        return None
    return to_rfc3339(max(live, key=lambda pair: pair[0])[0])


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_dotenv_values(path: str) -> Dict[str, str]:
    """Minimal ``.env`` reader (no third-party dependency).

    Values are never logged. Missing/unreadable files yield ``{}``.
    """
    values: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key.lower().startswith("export "):
                    key = key[7:].strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                if key:
                    values[key] = value
    except OSError:
        return {}
    return values


def _find_dotenv(start: Optional[str] = None) -> Optional[str]:
    """Walk up from this module looking for a ``.env``. Bounded, deliberately.

    Two bounds, and both became load-bearing when this module started shipping
    inside ``insight.pyz`` on engineers' laptops.

    *Not a directory* -- inside a zipapp ``os.path.dirname(__file__)`` is a path
    running *through* an archive file, so there is nothing here to search and
    everything above belongs to whoever installed it, not to us.

    *Never ``$HOME`` or above* -- eight levels from an installed archive reaches
    ``~``, ``/Users`` and ``/``. The home directory is shared with every other
    tool that has ever dropped a ``.env`` there, and reading one of those as
    poller configuration is a bug that reads like a credential leak.
    """
    here = os.path.abspath(start or os.path.dirname(__file__))
    stop = {os.path.abspath(os.path.expanduser("~")), os.path.abspath(os.sep)}
    for _ in range(8):
        if here in stop or not os.path.isdir(here):
            return None
        candidate = os.path.join(here, ".env")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return None


DEFAULT_STATE_PATH = os.path.join(
    os.path.expanduser("~"), ".aiep", "telemetry", "poller-state.json"
)


@dataclass
class Config:
    """Poller configuration, loaded from the environment (and optionally .env).

    Environment variable names deliberately reuse the ones already established
    in this repo (``skills/bitbucket-ops/commands.md``,
    ``skills/qualdev/jira-attach/SKILL.md``).
    """

    bitbucket_username: Optional[str] = None
    bitbucket_token: Optional[str] = None
    jira_url: Optional[str] = None
    jira_username: Optional[str] = None
    jira_token: Optional[str] = None
    # AIO TCMS issues its OWN key and does not accept the Jira API token -- the
    # Jira credential returns 401 "Invalid or missing API Token". Hence a
    # separate variable rather than a reuse of jira_token.
    aio_base_url: str = "https://tcms.aiojiraapps.com"
    aio_token: Optional[str] = None
    email_salt: Optional[str] = None
    state_path: str = DEFAULT_STATE_PATH
    bitbucket_api_base: str = "https://api.bitbucket.org"
    #: Real Jira project keys, for `extract_jira_key`. Without it, anything
    #: shaped like a key is accepted -- and measured 2026-08-26 against a live
    #: repository, three of seven extracted keys did not exist (`AUG-25` and
    #: `AUG-24` are dates; `CY-199` and `TC-12018` are not projects). See that
    #: function. `JIRA_PROJECT_KEYS=IML,APR`, or let `poll_jira.py --dump-projects`
    #: fetch the list from Jira, which knows the answer.
    jira_project_keys: Optional[Tuple[str, ...]] = None
    timeout_seconds: float = 30.0
    max_retries: int = 5

    @classmethod
    def from_env(
        cls,
        env: Optional[Dict[str, str]] = None,
        use_dotenv: bool = True,
        dotenv_path: Optional[str] = None,
    ) -> "Config":
        env = dict(os.environ if env is None else env)
        if use_dotenv:
            path = dotenv_path or _find_dotenv()
            if path:
                for key, value in load_dotenv_values(path).items():
                    env.setdefault(key, value)  # real env always wins

        def _num(name: str, default: float) -> float:
            try:
                return float(env.get(name) or default)
            except (TypeError, ValueError):
                return default

        def _keys(name: str) -> Optional[Tuple[str, ...]]:
            raw = (env.get(name) or "").strip()
            if not raw:
                return None
            return tuple(sorted({k.strip().upper()
                                 for k in raw.replace(";", ",").split(",")
                                 if k.strip()})) or None

        return cls(
            bitbucket_username=env.get("BITBUCKET_USERNAME") or None,
            bitbucket_token=env.get("BITBUCKET_ACCESS_TOKEN") or None,
            jira_url=(env.get("JIRA_URL") or "").rstrip("/") or None,
            jira_username=env.get("JIRA_USERNAME") or None,
            jira_token=env.get("JIRA_API_TOKEN") or None,
            aio_base_url=(
                env.get("AIO_BASE_URL") or "https://tcms.aiojiraapps.com"
            ).rstrip("/"),
            aio_token=env.get("AIO_API_TOKEN") or None,
            email_salt=env.get("AIEP_TELEMETRY_SALT") or None,
            state_path=env.get("AIEP_POLLER_STATE") or DEFAULT_STATE_PATH,
            bitbucket_api_base=(
                env.get("BITBUCKET_API_BASE") or "https://api.bitbucket.org"
            ).rstrip("/"),
            jira_project_keys=_keys("JIRA_PROJECT_KEYS") or _keys("JIRA_PROJECT_KEY"),
            timeout_seconds=_num("AIEP_HTTP_TIMEOUT", 30.0),
            max_retries=int(_num("AIEP_HTTP_MAX_RETRIES", 5)),
        )

    # -- credential accessors ------------------------------------------------

    def require_bitbucket(self) -> Tuple[str, str]:
        missing = [
            name
            for name, value in (
                ("BITBUCKET_USERNAME", self.bitbucket_username),
                ("BITBUCKET_ACCESS_TOKEN", self.bitbucket_token),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"missing environment variables: {', '.join(missing)}")
        return self.bitbucket_username, self.bitbucket_token  # type: ignore[return-value]

    def require_jira(self) -> Tuple[str, str, str]:
        missing = [
            name
            for name, value in (
                ("JIRA_URL", self.jira_url),
                ("JIRA_USERNAME", self.jira_username),
                ("JIRA_API_TOKEN", self.jira_token),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"missing environment variables: {', '.join(missing)}")
        return self.jira_url, self.jira_username, self.jira_token  # type: ignore[return-value]

    def require_aio(self) -> Tuple[str, str]:
        if not self.aio_token:
            raise ConfigError(
                "missing environment variables: AIO_API_TOKEN. AIO TCMS issues its "
                "own key and rejects the Jira API token with 401; get one from "
                "AIO Tests > API Keys."
            )
        return self.aio_base_url, self.aio_token


class ConfigError(Exception):
    """Configuration is incomplete. Never carries a credential value."""


def hash_email(email: Optional[str], salt: Optional[str]) -> Optional[str]:
    """``sha256(salt + lower(email))`` hex, per CONTRACT.md §2.1.

    Returns None when either input is missing -- never the raw address, and
    never an unsalted hash (an unsalted hash of a corporate address is
    trivially reversible).
    """
    if not email or not salt:
        return None
    return hashlib.sha256((salt + email.strip().lower()).encode("utf-8")).hexdigest()


_RAW_AUTHOR_RE = re.compile(r"<([^<>]+@[^<>]+)>")


def email_from_raw_author(raw: Optional[str]) -> Optional[str]:
    """Pull the address out of a git ``Name <email>`` string, for hashing only."""
    if not raw:
        return None
    match = _RAW_AUTHOR_RE.search(raw)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# HTTP transport (lazy dependency detection, cf. skills/bigquery/bq_tool.py)
# ---------------------------------------------------------------------------


class TransportError(Exception):
    """Network-level failure. Retryable. Carries no response body."""


class HttpError(Exception):
    """Non-retryable (or retry-exhausted) HTTP failure.

    Deliberately carries only the status code and a sanitised URL. Response
    bodies are never captured: they can contain repository content.
    """

    def __init__(self, status: int, url: str, attempts: int = 1) -> None:
        self.status = status
        self.url = sanitise_url(url)
        self.attempts = attempts
        super().__init__(f"HTTP {status} for {self.url} after {attempts} attempt(s)")


_SENSITIVE_QUERY_KEYS = {"access_token", "token", "api_key", "key", "password", "jwt"}


def sanitise_url(url: str) -> str:
    """Strip any credential-bearing query parameter before the URL is logged."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<unparseable-url>"
    if parts.query:
        kept = [
            (k, v)
            for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _SENSITIVE_QUERY_KEYS
        ]
        query = urllib.parse.urlencode(kept)
    else:
        query = ""
    netloc = parts.netloc
    if "@" in netloc:  # user:pass@host
        netloc = netloc.rsplit("@", 1)[-1]
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, query, ""))


Transport = Callable[[str, str, Dict[str, str], Optional[bytes], float], Tuple[int, Dict[str, str], bytes]]


def _requests_transport() -> Optional[Transport]:
    try:
        import requests  # type: ignore
    except ImportError:
        return None

    session = requests.Session()

    def _send(method, url, headers, body, timeout):
        try:
            resp = session.request(
                method, url, headers=headers, data=body, timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001 - class name only, never the text
            raise TransportError(type(exc).__name__) from None
        return resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, resp.content

    return _send


def _urllib_transport() -> Transport:
    import urllib.error
    import urllib.request

    def _send(method, url, headers, body, timeout):
        request = urllib.request.Request(url, data=body, method=method)
        for key, value in headers.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return (
                    resp.getcode(),
                    {k.lower(): v for k, v in resp.headers.items()},
                    resp.read(),
                )
        except urllib.error.HTTPError as exc:  # a response, not a failure
            return (
                exc.code,
                {k.lower(): v for k, v in (exc.headers or {}).items()},
                exc.read() if hasattr(exc, "read") else b"",
            )
        except Exception as exc:  # noqa: BLE001
            raise TransportError(type(exc).__name__) from None

    return _send


def default_transport() -> Transport:
    """Prefer ``requests`` when installed; fall back to ``urllib``.

    Detected lazily and never at import time, so the module imports cleanly on a
    machine with neither dependency installed beyond the stdlib.
    """
    return _requests_transport() or _urllib_transport()


RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass
class Response:
    status: int
    headers: Dict[str, str]
    data: bytes

    def json(self) -> Any:
        if not self.data:
            return {}
        return json.loads(self.data.decode("utf-8"))


class HttpClient:
    """Authenticated JSON GET client with retry, backoff and 429 handling.

    Auth is HTTP **Basic** (``Authorization: Basic base64(user:token)``), which
    is what this organisation's Bitbucket and Jira access uses. Bearer is NOT
    used -- ``skills/bitbucket-ops/commands.md`` is explicit that Bitbucket
    returns 401 for Bearer with these credentials.
    """

    def __init__(
        self,
        auth: Optional[Tuple[str, str]] = None,
        auth_header: Optional[str] = None,
        transport: Optional[Transport] = None,
        max_retries: int = 5,
        backoff_base: float = 0.5,
        backoff_cap: float = 60.0,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        rng: Optional[random.Random] = None,
        user_agent: str = USER_AGENT,
    ) -> None:
        self._transport = transport or default_transport()
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.timeout = timeout
        self._sleep = sleep
        self._rng = rng or random.Random(1701)
        self._headers = {
            "Accept": "application/json",
            "User-Agent": user_agent,
        }
        if auth and auth_header:
            raise ValueError("pass either auth or auth_header, not both")
        if auth:
            pair = f"{auth[0]}:{auth[1]}".encode("utf-8")
            # Basic auth -- never Bearer (see class docstring).
            self._headers["Authorization"] = "Basic " + base64.b64encode(pair).decode(
                "ascii"
            )
        elif auth_header:
            # Verbatim Authorization value, for a service that uses neither Basic
            # nor Bearer. AIO TCMS is the one such source here: it wants
            # ``AioAuth <key>`` and 401s on anything else.
            self._headers["Authorization"] = auth_header
        self.request_count = 0
        self.retry_count = 0

    # -- low level -----------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        allow_statuses: Sequence[int] = (),
    ) -> Response:
        """Perform a request with retry. Returns the final :class:`Response`.

        Raises :class:`HttpError` for a non-2xx status unless it is listed in
        ``allow_statuses`` (used by probe mode, which wants to *report* a 403
        rather than blow up).
        """
        full_url = with_params(url, params)
        headers = dict(self._headers)
        body: Optional[bytes] = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last_status = 0
        for attempt in range(1, self.max_retries + 1):
            self.request_count += 1
            try:
                status, resp_headers, data = self._transport(
                    method, full_url, headers, body, self.timeout
                )
            except TransportError:
                last_status = 0
                if attempt >= self.max_retries:
                    raise HttpError(0, full_url, attempt) from None
                self.retry_count += 1
                self._sleep(self._backoff(attempt, None))
                continue

            last_status = status
            if 200 <= status < 300 or status in allow_statuses:
                return Response(status, resp_headers, data)

            if status in RETRYABLE_STATUSES and attempt < self.max_retries:
                self.retry_count += 1
                self._sleep(self._backoff(attempt, resp_headers))
                continue

            raise HttpError(status, full_url, attempt)

        raise HttpError(last_status, full_url, self.max_retries)

    def get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("GET", url, params=params).json()

    def probe(
        self, url: str, params: Optional[Dict[str, Any]] = None, method: str = "GET"
    ) -> Response:
        """One call, no retry, no raise -- for --probe mode reporting."""
        full_url = with_params(url, params)
        self.request_count += 1
        try:
            status, headers, data = self._transport(
                method, full_url, dict(self._headers), None, self.timeout
            )
        except TransportError as exc:
            return Response(0, {"x-transport-error": str(exc)}, b"")
        return Response(status, headers, data)

    def _backoff(self, attempt: int, headers: Optional[Dict[str, str]]) -> float:
        """Exponential backoff with jitter; honours ``Retry-After`` on 429."""
        if headers:
            retry_after = headers.get("retry-after")
            if retry_after:
                try:
                    return min(float(retry_after), self.backoff_cap)
                except ValueError:
                    parsed = parse_ts(retry_after)
                    if parsed:
                        delta = (
                            parsed - datetime.now(timezone.utc)
                        ).total_seconds()
                        return max(0.0, min(delta, self.backoff_cap))
        delay = min(self.backoff_base * (2 ** (attempt - 1)), self.backoff_cap)
        return delay * (0.5 + self._rng.random() / 2.0)  # full-ish jitter


def with_params(url: str, params: Optional[Dict[str, Any]]) -> str:
    """Append query parameters, supporting repeated keys via list values."""
    if not params:
        return url
    pairs: List[Tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            pairs.extend((key, str(item)) for item in value)
        else:
            pairs.append((key, str(value)))
    if not pairs:
        return url
    separator = "&" if "?" in url else "?"
    return url + separator + urllib.parse.urlencode(pairs)


def paginate(
    client: HttpClient,
    url: str,
    params: Optional[Dict[str, Any]] = None,
    max_pages: int = 10000,
    values_key: str = "values",
) -> Iterator[Dict[str, Any]]:
    """Yield every item of a Bitbucket paginated collection.

    Bitbucket returns ``{"values": [...], "next": "<absolute url>"}``. The
    ``next`` link already carries its own paging state, so parameters are only
    applied to the first request. Iteration continues until ``next`` is absent
    -- large PRs touch many files and a single diffstat page is not the whole
    story.
    """
    next_url: Optional[str] = with_params(url, params)
    pages = 0
    while next_url and pages < max_pages:
        payload = client.get_json(next_url)
        pages += 1
        if not isinstance(payload, dict):
            return
        for item in payload.get(values_key) or []:
            yield item
        next_url = payload.get("next") or None


# ---------------------------------------------------------------------------
# Envelope construction (CONTRACT.md §2)
# ---------------------------------------------------------------------------


def deterministic_id(prefix: str, *parts: Any) -> str:
    """``<prefix>_<32 hex>`` derived from a natural key.

    CONTRACT.md §1 rule 3 makes ``event_id`` the dedup key and demands that
    re-delivery is safe. Random uuid4s would make every re-poll of the same
    fact a *new* row, so the poller derives ids from the fact's natural key
    instead. The rendered shape (prefix + 32 hex chars) matches the contract.
    """
    material = "\x1f".join("" if part is None else str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]}"


def make_actor(
    person_id: Optional[str] = None,
    person_email_hash: Optional[str] = None,
    team_id: Optional[str] = None,
    role: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "person_id": person_id,
        "person_email_hash": person_email_hash,
        "team_id": team_id,  # null until OQ-6 (no team directory) resolves
        "role": role,
    }


def make_context(
    jira_issue_key: Optional[str] = None,
    repo_full_name: Optional[str] = None,
    branch_name: Optional[str] = None,
    product_profile: Optional[str] = None,
    environment: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "jira_issue_key": jira_issue_key,
        "jira_project_key": jira_project_key(jira_issue_key),
        "repo_full_name": repo_full_name,
        "branch_name": branch_name,
        "product_profile": product_profile,
        "environment": environment,
    }


def make_agent(agent_name: str, surface: str = "headless") -> Dict[str, Any]:
    """Agent block for a poller-sourced event.

    Poller events are observations of systems of record, not the product of an
    agent run, but CONTRACT.md §2.3 requires the block. The poller identifies
    itself so the source of every backfilled row stays visible, and
    ``surface='headless'`` is truthful and within the enum.
    """
    return {
        "agent_name": agent_name,
        "agent_version": POLLER_VERSION,
        "skill_name": None,
        "skill_version": None,
        "surface": surface,
    }


def make_link(method: str, confidence: float) -> Dict[str, Any]:
    if method not in LINK_METHODS:
        raise ValueError(f"link.method must be one of {sorted(LINK_METHODS)}")
    if method == "explicit":
        confidence = 1.0  # CONTRACT.md §2.4
    return {"method": method, "confidence": round(float(confidence), 3)}


def build_event(
    event_type: str,
    event_time: Optional[str],
    natural_key: Sequence[Any],
    attributes: Dict[str, Any],
    actor: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
    agent: Optional[Dict[str, Any]] = None,
    link: Optional[Dict[str, Any]] = None,
    trace_id: Optional[str] = None,
    run_id: Optional[str] = None,
    parent_run_id: Optional[str] = None,
    span_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble one contract-conformant event envelope.

    ``run_id`` deviates from the emitter's usage and the deviation is
    deliberate: a poller observes Bitbucket/Jira/CI facts that exist whether or
    not an agent was involved, so there is usually no run to point at.
    Fabricating one would manufacture a join key and breach AR-1. It is
    populated *only* when the underlying commit carries an ``AI-Run-Id`` trailer
    (CONTRACT.md §9), which is also the only case that earns
    ``link.method = 'explicit'``. See CONTRACT.md 2.4.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"event_type {event_type!r} is not in the CONTRACT.md §3 enum")

    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": deterministic_id("evt", event_type, *natural_key),
        "event_type": event_type,
        "event_time": to_rfc3339(event_time),
        "ingested_at": None,  # set by the collector, never by a client
        "trace_id": trace_id,
        "run_id": run_id,
        "parent_run_id": parent_run_id,
        "span_id": span_id,
        "actor": actor or make_actor(),
        "context": context or make_context(),
        "agent": agent or make_agent("poller"),
        "attributes": attributes,
        "link": link or make_link("heuristic", 0.0),
    }
    validate_event(event)
    return event


class ContractViolation(ValueError):
    """The event would be rejected by the collector. Fix the poller, not this."""


def _walk(node: Any, path: str = "") -> Iterator[Tuple[str, str, Any]]:
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            yield child, str(key), value
            yield from _walk(value, child)
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            child = f"{path}[{index}]"
            yield from _walk(value, child)


def validate_event(event: Dict[str, Any]) -> None:
    """Enforce the parts of the contract a poller can breach by accident."""
    keys = set(event)
    if keys != set(ENVELOPE_KEYS):
        missing = sorted(set(ENVELOPE_KEYS) - keys)
        extra = sorted(keys - set(ENVELOPE_KEYS))
        raise ContractViolation(
            f"envelope key mismatch (missing={missing}, unexpected={extra})"
        )
    if event["event_type"] not in EVENT_TYPES:
        raise ContractViolation(f"unknown event_type {event['event_type']!r}")
    if event["schema_version"] != SCHEMA_VERSION:
        raise ContractViolation("schema_version must be " + SCHEMA_VERSION)
    if event["ingested_at"] is not None:
        raise ContractViolation("ingested_at is set by the collector, never a client")
    link = event.get("link") or {}
    if link.get("method") not in LINK_METHODS:
        raise ContractViolation("link.method outside the enum")
    if link.get("method") == "explicit" and link.get("confidence") != 1.0:
        raise ContractViolation("link.method='explicit' implies confidence 1.0")

    # Layer 1 -- key names. EXACT match on the lowercased key, never substring.
    # Substring matching would reject mandated §3 fields such as
    # output_content_hash ("content"), input_tokens / output_tokens /
    # cached_input_tokens ("token") and error_class ("error"), silently dropping
    # the two most important event types in the system.
    for path, key, _value in _walk(event.get("attributes") or {}):
        if str(key).lower() in FORBIDDEN_ATTRIBUTE_NAMES:
            raise ContractViolation(
                f"forbidden attribute name {key!r} at attributes.{path}"
            )

    # Layer 2 -- string values. Regex/substring by nature, and separate from the
    # key guard on purpose. Rejections name the field and the check that fired,
    # never the value.
    for path, _key, value in _walk(event):
        if not isinstance(value, str):
            continue
        if _EMAIL_RE.search(value):
            raise ContractViolation(
                f"value at {path} looks like a raw email address; emit a hash instead"
            )
        for check_name, pattern in SECRET_VALUE_CHECKS:
            if pattern.search(value):
                raise ContractViolation(
                    f"value at {path} tripped the {check_name} secret check"
                )


# ---------------------------------------------------------------------------
# NDJSON output
# ---------------------------------------------------------------------------


class NdjsonWriter:
    """Newline-delimited JSON sink.

    A file target is written to a sibling temp file and renamed on clean close,
    so a crashed run never leaves a half-written batch that looks complete.
    ``path=None`` writes to stdout.
    """

    def __init__(self, path: Optional[str] = None, stream=None) -> None:
        self.path = path
        self.count = 0
        self._stream = stream
        self._tmp_path: Optional[str] = None
        self._owns_stream = False
        self._committed = False

    def __enter__(self) -> "NdjsonWriter":
        if self._stream is None:
            if self.path:
                directory = os.path.dirname(os.path.abspath(self.path))
                if directory:
                    os.makedirs(directory, exist_ok=True)
                self._tmp_path = f"{self.path}.{os.getpid()}.tmp"
                self._stream = open(self._tmp_path, "w", encoding="utf-8")
                self._owns_stream = True
            else:
                self._stream = sys.stdout
        return self

    def write(self, event: Dict[str, Any]) -> None:
        assert self._stream is not None, "NdjsonWriter used outside its context"
        self._stream.write(json.dumps(event, separators=(",", ":"), sort_keys=True))
        self._stream.write("\n")
        self.count += 1

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._owns_stream and self._stream is not None:
            self._stream.close()
            if exc_type is None and self._tmp_path and self.path:
                os.replace(self._tmp_path, self.path)
                self._committed = True
            elif self._tmp_path and os.path.exists(self._tmp_path):
                os.unlink(self._tmp_path)  # partial batch is discarded
        elif self._stream is not None and not self._owns_stream:
            try:
                self._stream.flush()
            except (ValueError, OSError):
                pass
        self._stream = None


# ---------------------------------------------------------------------------
# Watermarks
# ---------------------------------------------------------------------------


class WatermarkStore:
    """Last-successful-poll timestamp per source, persisted as JSON.

    Usage::

        store = WatermarkStore(path)
        since = store.get("bitbucket:pullrequests:ws/repo")
        with store.checkpoint("bitbucket:pullrequests:ws/repo") as mark:
            for pr in poll(since):
                mark.propose(pr["updated_on"])

    The proposed high-water mark is persisted **only** when the ``with`` block
    exits without an exception, i.e. only on a fully successful run. A failed
    or partial run leaves the previous watermark in place, so the next run
    re-reads the same window. Re-reads are safe because ``event_id`` is
    deterministic (CONTRACT.md §1 rule 3).
    """

    VERSION = 1

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or DEFAULT_STATE_PATH
        self._data = self._load()

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("watermarks"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"version": self.VERSION, "watermarks": {}}

    def _save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, self.path)

    def get(self, key: str) -> Optional[str]:
        entry = self._data["watermarks"].get(key)
        return entry.get("last_success_at") if isinstance(entry, dict) else None

    def all(self) -> Dict[str, Any]:
        return dict(self._data["watermarks"])

    def advance(self, key: str, timestamp: Optional[str]) -> Optional[str]:
        """Move the watermark forward. Never backwards, never to None."""
        normalised = to_rfc3339(timestamp)
        if not normalised:
            return self.get(key)
        current = self.get(key)
        if current and parse_ts(current) and parse_ts(normalised) <= parse_ts(current):
            return current
        entry = self._data["watermarks"].setdefault(key, {})
        entry["last_success_at"] = normalised
        entry["updated_at"] = now_rfc3339()
        entry["runs"] = int(entry.get("runs") or 0) + 1
        self._save()
        return normalised

    @contextmanager
    def checkpoint(self, key: str) -> Iterator["_Checkpoint"]:
        mark = _Checkpoint(key, self.get(key))
        yield mark
        # Reached only when the body completed without raising.
        if mark.proposed:
            self.advance(key, mark.proposed)


class _Checkpoint:
    __slots__ = ("key", "previous", "proposed", "discarded")

    def __init__(self, key: str, previous: Optional[str]) -> None:
        self.key = key
        self.previous = previous
        self.proposed: Optional[str] = None
        self.discarded = False

    def propose(self, timestamp: Optional[Any]) -> None:
        candidate = to_rfc3339(timestamp)
        if not candidate:
            return
        if self.discarded:
            return
        if self.proposed is None or parse_ts(candidate) > parse_ts(self.proposed):
            self.proposed = candidate

    def discard(self) -> None:
        """Throw away every proposal made during this run.

        For a pass that covered only part of its window -- a truncated run, a
        sampled run, a partial failure. Sources are commonly ordered newest-first,
        so the very first record already proposes the newest timestamp; committing
        it would move the watermark past records that were never processed and skip
        them permanently. Discarding makes the next run re-cover the same window,
        which is safe because event ids are deterministic (CONTRACT.md section 2).
        """
        self.discarded = True
        self.proposed = None


def default_since(watermark: Optional[str], lookback_days: int = 30) -> str:
    """Resolve the window start: the watermark, else a bounded lookback."""
    if watermark:
        return watermark
    return to_rfc3339(datetime.now(timezone.utc) - timedelta(days=lookback_days))  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# TLS -- one trust store, shared, because every part of this talks HTTPS
# ---------------------------------------------------------------------------

#: Where to look for a CA bundle when Python's own trust store is empty, in the
#: order they are tried. The first is macOS's system bundle -- the same one
#: `curl` reads, which is why `curl` works on a machine where Python does not.
CA_BUNDLES = (
    "/etc/ssl/cert.pem",                        # macOS, FreeBSD
    "/etc/ssl/certs/ca-certificates.crt",       # Debian, Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",         # Fedora, RHEL
)


def ssl_context() -> "ssl.SSLContext":
    """A context that can actually verify a certificate.

    A stock python.org build on macOS ships with an **empty** trust store until
    somebody runs ``Install Certificates.command``, which nobody does. Every
    HTTPS call then fails with ``unable to get local issuer certificate`` on a
    machine where ``curl`` to the same URL works.

    Lives here rather than in one caller because every part of this system
    talks HTTPS eventually -- the uploader, the puller, the watchdog posting to
    ntfy -- and the bug is per-machine, not per-tool. Fixing it in one place and
    leaving the others is how you get a client that uploads fine and a weekly
    pull that cannot reach the same host.

    Verification is never turned off: an unverified transfer of a sealed bundle
    is worse than a failed one, because it looks like it worked.
    """
    import ssl                                                   # noqa: PLC0415

    context = ssl.create_default_context()
    if context.get_ca_certs():
        return context
    for candidate in CA_BUNDLES:
        if not os.path.exists(candidate):
            continue
        try:
            context.load_verify_locations(cafile=candidate)
        except (OSError, ssl.SSLError):
            continue
        if context.get_ca_certs():
            return context
    return context          # still empty: let it fail, and explain why


def is_certificate_error(exc: BaseException) -> bool:
    """``urlopen`` wraps the SSL error in a ``URLError``; unwrap one level."""
    import ssl                                                   # noqa: PLC0415

    for candidate in (exc, getattr(exc, "reason", None)):
        if isinstance(candidate, ssl.SSLCertVerificationError):
            return True
    return False


CERT_ADVICE = (
    "this machine cannot verify the certificate -- Python's trust store is "
    "empty, which is the default on a python.org install for macOS. Run "
    "\"/Applications/Python 3.x/Install Certificates.command\", or point "
    "SSL_CERT_FILE at a CA bundle"
)


# ---------------------------------------------------------------------------
# Diagnostics -- stderr only, never event content, never credentials
# ---------------------------------------------------------------------------


def log(message: str, **fields: Any) -> None:
    payload = {"msg": message}
    payload.update({k: v for k, v in fields.items() if v is not None})
    sys.stderr.write(json.dumps(payload, default=str, sort_keys=True) + "\n")


def fail(message: str, exit_code: int = 2) -> "NoReturn":  # type: ignore[valid-type]
    log("fatal", error=message)
    raise SystemExit(exit_code)
