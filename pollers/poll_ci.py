#!/usr/bin/env python3
"""CI backfill poller -- ``ci.pipeline.completed`` (CONTRACT.md §3 event 20).

    !!  THE CI SYSTEM FOR THIS ORGANISATION IS NOT CONFIRMED.  !!

Design §4.3 is explicit: *there is no CI/CD integration in this repository* -- no
pipeline file, no skill or agent that calls a CI API. **Bitbucket Pipelines is an
inference** drawn solely from Bitbucket Cloud being the SCM, and it is recorded
as open question **OQ-3**, "the largest single unknown in the spike".

So this file does two things:

1. **Poll mode** implements the Bitbucket Pipelines path end to end. Every
   endpoint it touches is marked UNVERIFIED against this organisation. If
   Pipelines is not enabled, it says so plainly and exits without pretending.

2. **``--probe`` mode** makes exactly one call per candidate endpoint and prints
   a copy-pasteable report of what is and is not retrievable, with HTTP status
   codes. **That report is the deliverable that answers OQ-3.** Paste it into
   the spike doc, or hand it to a DevOps engineer.

The probe also queries ``/commit/{sha}/statuses``, which is the one endpoint
that reveals a *non*-Bitbucket CI: Jenkins, GitHub Actions and TeamCity all post
build statuses there, so a populated response names the real CI system even when
Pipelines is switched off.

Usage::

    python poll_ci.py --workspace WS --repo REPO --probe
    python poll_ci.py --workspace WS --repo REPO [--since ...] [--out events.ndjson]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

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
    Response,
    WatermarkStore,
    build_event,
    default_since,
    deterministic_id,
    extract_jira_key,
    fail,
    log,
    make_actor,
    make_agent,
    make_context,
    make_link,
    ms_between,
    paginate,
    parse_ts,
    person_id_of,
    to_rfc3339,
)

AGENT_NAME = "poller.ci"

#: Bounded status enum for ci.pipeline.completed.status.
PIPELINE_STATUS = {
    "SUCCESSFUL": "passed",
    "PASSED": "passed",
    "FAILED": "failed",
    "ERROR": "error",
    "STOPPED": "stopped",
    "EXPIRED": "expired",
    "HALTED": "stopped",
    "SKIPPED": "skipped",
}

STATUS_MEANING = {
    0: "no response (network/DNS/TLS failure, or VPN required)",
    200: "retrievable",
    201: "retrievable",
    400: "bad request -- endpoint exists, parameters rejected",
    401: "authentication failed -- check BITBUCKET_USERNAME/BITBUCKET_ACCESS_TOKEN "
    "(Basic auth; Bearer returns 401 with these credentials)",
    403: "authenticated but forbidden -- the token most likely lacks the required "
    "scope (pipeline:read / repository:read), or the plan does not include it",
    404: "not found -- feature never enabled, no data yet, or endpoint absent on this plan",
    429: "rate limited -- retrievable, but the poller must back off",
    500: "server error -- retry",
    503: "service unavailable -- retry",
}


def status_meaning(status: int) -> str:
    return STATUS_MEANING.get(status, f"HTTP {status}")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def map_pipeline_status(pipeline: Dict[str, Any]) -> Tuple[Optional[str], bool]:
    """Return ``(bounded_status, is_complete)`` for a pipeline object."""
    state = pipeline.get("state") or {}
    state_name = str(state.get("name") or "").upper()
    result_name = str(((state.get("result") or {}).get("name")) or "").upper()
    if state_name in {"IN_PROGRESS", "PENDING", "PAUSED"}:
        return None, False
    if result_name:
        return PIPELINE_STATUS.get(result_name, "unknown"), True
    if state_name == "COMPLETED":
        return "unknown", True
    return PIPELINE_STATUS.get(state_name, "unknown"), bool(state_name)


def summarise_test_report(report: Any) -> Dict[str, Optional[int]]:
    """Normalise a Bitbucket test-report payload.

    UNVERIFIED shape. The Pipelines test-report endpoint is thinly documented and
    has been observed returning both a counts object and a paginated collection
    of cases, so both are handled and anything unrecognised yields Nones rather
    than fabricated zeros (CONTRACT.md §4's "never 0" principle applied to tests).
    """
    empty = {"tests_total": None, "tests_passed": None, "tests_failed": None, "tests_skipped": None}
    if not isinstance(report, dict):
        return empty

    def _int(*names: str) -> Optional[int]:
        for name in names:
            value = report.get(name)
            if isinstance(value, (int, float)):
                return int(value)
        return None

    total = _int("total", "test_count", "number_of_tests")
    passed = _int("successful", "passed", "success_count")
    failed = _int("failed", "failure_count")
    skipped = _int("skipped", "skipped_count")
    errored = _int("error", "error_count")
    if failed is not None and errored:
        failed += errored

    if total is None and isinstance(report.get("values"), list):
        cases = report["values"]
        total = len(cases)
        passed = sum(1 for c in cases if str(c.get("status", "")).upper() in {"SUCCESSFUL", "PASSED"})
        failed = sum(1 for c in cases if str(c.get("status", "")).upper() in {"FAILED", "ERROR"})
        skipped = sum(1 for c in cases if str(c.get("status", "")).upper() == "SKIPPED")

    if total is None and passed is None and failed is None:
        return empty
    if total is None:
        total = (passed or 0) + (failed or 0) + (skipped or 0)
    return {
        "tests_total": total,
        "tests_passed": passed,
        "tests_failed": failed,
        "tests_skipped": skipped,
    }


def pipeline_duration_ms(pipeline: Dict[str, Any]) -> Optional[int]:
    seconds = pipeline.get("build_seconds_used")
    if isinstance(seconds, (int, float)) and seconds > 0:
        return int(seconds * 1000)
    return ms_between(pipeline.get("created_on"), pipeline.get("completed_on"))


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------


class CiPoller:
    def __init__(
        self,
        client: HttpClient,
        workspace: str,
        repo: str,
        config: Optional[Config] = None,
        fetch_test_reports: bool = True,
    ) -> None:
        self.client = client
        self.workspace = workspace
        self.repo = repo
        self.config = config or Config()
        self.repo_full_name = f"{workspace}/{repo}"
        self.base = f"{self.config.bitbucket_api_base}/2.0/repositories/{workspace}/{repo}"
        self.fetch_test_reports = fetch_test_reports
        self.test_reports_available: Optional[bool] = None

    # -- fetching ------------------------------------------------------------

    def iter_pipelines(self, since: Optional[str], max_pipelines: int = 0) -> Iterator[Dict[str, Any]]:
        """Newest-first pipeline stream, stopped at the ``since`` boundary."""
        boundary = parse_ts(since) if since else None
        seen = 0
        for pipeline in paginate(
            self.client, f"{self.base}/pipelines/", {"pagelen": 50, "sort": "-created_on"}
        ):
            created = parse_ts(pipeline.get("created_on"))
            if boundary and created and created < boundary:
                return
            yield pipeline
            seen += 1
            if max_pipelines and seen >= max_pipelines:
                return

    def pipeline_steps(self, pipeline_uuid: str) -> List[Dict[str, Any]]:
        return list(
            paginate(self.client, f"{self.base}/pipelines/{quote(pipeline_uuid)}/steps/", {"pagelen": 100})
        )

    def step_test_report(self, pipeline_uuid: str, step_uuid: str) -> Optional[Dict[str, Any]]:
        """Fetch one step's test report; None when unavailable.

        A 403/404 here is recorded once and then short-circuits: if JUnit XML is
        not published, no step in this repository will have a report and there is
        no point asking again for every step of every pipeline.
        """
        if self.test_reports_available is False:
            return None
        url = f"{self.base}/pipelines/{quote(pipeline_uuid)}/steps/{quote(step_uuid)}/test_reports"
        try:
            report = self.client.get_json(url)
        except HttpError as exc:
            if exc.status in (403, 404):
                if self.test_reports_available is None:
                    log(
                        "ci_test_reports_unavailable",
                        status=exc.status,
                        hint="pipeline does not publish JUnit XML, or the token lacks scope; "
                        "tests_* will be null (never 0). This is OQ-3 evidence.",
                    )
                self.test_reports_available = False
                return None
            raise
        self.test_reports_available = True
        return report

    # -- event construction --------------------------------------------------

    def build_pipeline_event(self, pipeline: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        status, complete = map_pipeline_status(pipeline)
        if not complete:
            return None

        uuid = pipeline.get("uuid") or ""
        target = pipeline.get("target") or {}
        commit_sha = (target.get("commit") or {}).get("hash")
        ref_name = target.get("ref_name") or (target.get("selector") or {}).get("pattern")
        completed_on = to_rfc3339(pipeline.get("completed_on")) or to_rfc3339(
            pipeline.get("created_on")
        )

        tests = {"tests_total": None, "tests_passed": None, "tests_failed": None, "tests_skipped": None}
        step_count = 0
        failed_step_name = None
        if self.fetch_test_reports:
            aggregate = {"tests_total": None, "tests_passed": None, "tests_failed": None, "tests_skipped": None}
            for step in self.pipeline_steps(uuid):
                step_count += 1
                step_state = ((step.get("state") or {}).get("result") or {}).get("name")
                if failed_step_name is None and str(step_state or "").upper() in {"FAILED", "ERROR"}:
                    failed_step_name = step.get("name")
                report = self.step_test_report(uuid, step.get("uuid") or "")
                summary = summarise_test_report(report)
                for key, value in summary.items():
                    if value is None:
                        continue
                    aggregate[key] = (aggregate[key] or 0) + value
            tests = aggregate

        jira_key = extract_jira_key(ref_name,
                                    projects=self.config.jira_project_keys)
        return build_event(
            "ci.pipeline.completed",
            completed_on,
            (self.repo_full_name, "pipeline", uuid),
            {
                "pipeline_id": uuid,
                "pipeline_build_number": pipeline.get("build_number"),
                "commit_sha": commit_sha,
                "status": status,
                "duration_ms": pipeline_duration_ms(pipeline),
                "tests_total": tests["tests_total"],
                "tests_passed": tests["tests_passed"],
                "tests_failed": tests["tests_failed"],
                "tests_skipped": tests["tests_skipped"],
                # design §4.3: no coverage publishing exists anywhere in this org.
                # NULL, never 0 -- a zero here would read as "0% covered".
                "coverage_pct": None,
                "coverage_source": None,
                "step_count": step_count,
                "failed_step_name": failed_step_name,
                "trigger_kind": (pipeline.get("trigger") or {}).get("name"),
                "ref_name": ref_name,
                "started_at": to_rfc3339(pipeline.get("created_on")),
                "completed_at": completed_on,
                "ci_system": "bitbucket-pipelines",
                "ci_system_verified": False,  # OQ-3 unresolved
            },
            actor=make_actor(person_id=person_id_of(pipeline.get("creator"))),
            context=make_context(
                jira_issue_key=jira_key,
                repo_full_name=self.repo_full_name,
                branch_name=ref_name,
            ),
            agent=make_agent(AGENT_NAME),
            link=make_link("heuristic", 0.5 if jira_key else 0.0),
            trace_id=deterministic_id("trc", "ci", self.repo_full_name, uuid),
        )


# ---------------------------------------------------------------------------
# Jenkins-via-build-statuses  (the ACTUAL CI path for this organisation)
# ---------------------------------------------------------------------------
# MEASURED 2026-08-19 against acme. Bitbucket Pipelines is not used anywhere:
# pipelines_config 404, no bitbucket-pipelines.yml, 0 pipeline runs and 0
# deployments across every repository probed. CI is self-hosted Jenkins, which
# posts build statuses back to Bitbucket:
#
#   build-connectivity.aerjupiter.com   build jobs
#   deploy-acp.aerjupiter.com           deploy jobs
#
# A build status carries enough to satisfy the CI metrics WITHOUT any Jenkins
# credential:
#   state       SUCCESSFUL | FAILED | INPROGRESS | STOPPED
#   name        "<job> » <branch> #<build number>"   (Jenkins multibranch format)
#   url         the Jenkins job URL
#   created_on  build start
#   updated_on  build end        -> duration = updated_on - created_on
#   commit      the sha, which joins straight to fct_pull_request
#
# What this path CANNOT give, and which still needs Jenkins API access:
# per-test results (tests_total/passed/failed) and coverage. Those stay NULL,
# never 0, per CONTRACT section 4's "never fabricate a zero" rule.

def _ms(start: Optional[str], end: Optional[str]) -> Optional[int]:
    """Milliseconds between two RFC3339 timestamps, or None if either is missing.

    None rather than 0 on missing input: a build whose end time never arrived is
    unknown-duration, not instantaneous.
    """
    a, b = parse_ts(start), parse_ts(end)
    if not a or not b:
        return None
    delta = int((b - a).total_seconds() * 1000)
    return delta if delta >= 0 else None


JENKINS_BUILD_HOSTS = ("build-connectivity.aerjupiter.com",)
JENKINS_DEPLOY_HOSTS = ("deploy-acp.aerjupiter.com",)

# "BuildSecurityPolicyService » release/26.8.0 #12"
_JENKINS_NAME_RE = re.compile(r"^(?P<job>.+?)\s*»\s*(?P<branch>.+?)\s*#(?P<build>\d+)\s*$")

_BUILD_STATE_MAP = {
    "SUCCESSFUL": "passed",
    "FAILED": "failed",
    "INPROGRESS": "running",
    "STOPPED": "cancelled",
}


def parse_jenkins_name(name: Optional[str]) -> Dict[str, Optional[str]]:
    """Split a Jenkins multibranch status name into job / branch / build number.

    Returns all-None on anything that does not match rather than guessing, so an
    unrecognised CI provider is visible as missing dimensions instead of silently
    mis-parsed ones.
    """
    if not name:
        return {"job_name": None, "branch": None, "build_number": None}
    m = _JENKINS_NAME_RE.match(name.strip())
    if not m:
        return {"job_name": name.strip()[:200], "branch": None, "build_number": None}
    return {
        "job_name": m.group("job").strip(),
        "branch": m.group("branch").strip(),
        "build_number": m.group("build"),
    }


def classify_status_host(url: Optional[str]) -> str:
    """build | deploy | unknown, from the Jenkins host that posted the status."""
    if not url:
        return "unknown"
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return "unknown"
    if any(host.endswith(h) for h in JENKINS_DEPLOY_HOSTS):
        return "deploy"
    if any(host.endswith(h) for h in JENKINS_BUILD_HOSTS):
        return "build"
    return "unknown"


def build_status_event_attributes(status: Dict[str, Any]) -> Dict[str, Any]:
    """CONTRACT section 3 #20 attributes derived from one Bitbucket build status."""
    parsed = parse_jenkins_name(status.get("name"))
    started = to_rfc3339(status.get("created_on"))
    ended = to_rfc3339(status.get("updated_on"))
    duration_ms = _ms(started, ended)
    state = (status.get("state") or "").upper()
    return {
        # pipeline_id: the status key is Bitbucket's stable per-job identifier.
        "pipeline_id": status.get("key"),
        "commit_sha": ((status.get("commit") or {}).get("hash")),
        "status": _BUILD_STATE_MAP.get(state, "unknown"),
        "duration_ms": duration_ms,
        "ci_provider": "jenkins",
        "ci_kind": classify_status_host(status.get("url")),
        "job_name": parsed["job_name"],
        "job_branch": parsed["branch"],
        "build_number": parsed["build_number"],
        # NULL, never 0 -- Jenkins does not publish these through Bitbucket.
        # Populating them requires Jenkins API access (see the module docstring).
        "tests_total": None,
        "tests_passed": None,
        "tests_failed": None,
        "coverage_pct": None,
    }


def quote(value: str) -> str:
    """Percent-encode a path segment. Pipeline uuids arrive as ``{...}``."""
    return urllib.parse.quote(str(value), safe="")


# ---------------------------------------------------------------------------
# --probe : the OQ-3 deliverable
# ---------------------------------------------------------------------------


class ProbeStep:
    def __init__(
        self,
        step_id: str,
        description: str,
        path: Optional[Callable[[Dict[str, Any]], Optional[str]]],
        params: Optional[Dict[str, Any]] = None,
        feeds: str = "",
        capture: Optional[Callable[[Any, Dict[str, Any]], None]] = None,
        method: str = "GET",
    ) -> None:
        self.step_id = step_id
        self.description = description
        self.path = path
        self.params = params or {}
        self.feeds = feeds
        self.capture = capture
        self.method = method


def _json_or_none(response: Response) -> Any:
    try:
        return response.json()
    except (ValueError, UnicodeDecodeError):
        return None


def run_probe(client: HttpClient, workspace: str, repo: str, api_base: str) -> Dict[str, Any]:
    """One call per candidate endpoint. Never raises; reports what happened."""
    base = f"{api_base}/2.0/repositories/{workspace}/{repo}"
    captured: Dict[str, Any] = {"main_branch": "main"}

    def cap_repo(payload, store):
        if isinstance(payload, dict):
            store["main_branch"] = (payload.get("mainbranch") or {}).get("name") or "main"
            store["repo_uuid"] = payload.get("uuid")

    def cap_pipelines(payload, store):
        values = (payload or {}).get("values") or []
        store["pipeline_count_page"] = len(values)
        store["pipeline_size"] = (payload or {}).get("size")
        if values:
            store["pipeline_uuid"] = values[0].get("uuid")
            state = values[0].get("state") or {}
            store["pipeline_state"] = state.get("name")
            store["pipeline_result"] = (state.get("result") or {}).get("name")
            store["pipeline_has_build_seconds"] = values[0].get("build_seconds_used") is not None

    def cap_steps(payload, store):
        values = (payload or {}).get("values") or []
        store["step_count"] = len(values)
        if values:
            store["step_uuid"] = values[0].get("uuid")

    def cap_commits(payload, store):
        values = (payload or {}).get("values") or []
        if values:
            store["commit_sha"] = values[0].get("hash")

    def cap_statuses(payload, store):
        values = (payload or {}).get("values") or []
        vendors = []
        for status in values:
            url = status.get("url") or ""
            host = urllib.parse.urlsplit(url).netloc if url else None
            vendors.append(
                {
                    "key": status.get("key"),
                    "name": status.get("name"),
                    "state": status.get("state"),
                    "host": host,  # host only -- never the full internal URL
                }
            )
        store["build_status_providers"] = vendors

    steps = [
        ProbeStep(
            "repo",
            "GET /2.0/repositories/{ws}/{repo}",
            lambda c: base,
            feeds="control: does the token see this repository at all",
            capture=cap_repo,
        ),
        ProbeStep(
            "pipelines_config",
            "GET /2.0/repositories/{ws}/{repo}/pipelines_config",
            lambda c: f"{base}/pipelines_config",
            feeds="is Bitbucket Pipelines ENABLED on this repository",
        ),
        ProbeStep(
            "pipelines_yml",
            "GET /2.0/repositories/{ws}/{repo}/src/{main}/bitbucket-pipelines.yml",
            lambda c: f"{base}/src/{quote(c.get('main_branch') or 'main')}/bitbucket-pipelines.yml",
            feeds="is a pipeline actually CONFIGURED in the repo (file presence only; "
            "contents are never read into the report)",
        ),
        ProbeStep(
            "pipelines_list",
            "GET /2.0/repositories/{ws}/{repo}/pipelines/?sort=-created_on",
            lambda c: f"{base}/pipelines/",
            params={"pagelen": 1, "sort": "-created_on"},
            feeds="CI duration, build success rate (metric §7.8)",
            capture=cap_pipelines,
        ),
        ProbeStep(
            "pipeline_steps",
            "GET /2.0/repositories/{ws}/{repo}/pipelines/{uuid}/steps/",
            lambda c: (
                f"{base}/pipelines/{quote(c['pipeline_uuid'])}/steps/"
                if c.get("pipeline_uuid")
                else None
            ),
            params={"pagelen": 5},
            feeds="stage-level duration, failure stage",
            capture=cap_steps,
        ),
        ProbeStep(
            "test_reports",
            "GET .../pipelines/{uuid}/steps/{step}/test_reports",
            lambda c: (
                f"{base}/pipelines/{quote(c['pipeline_uuid'])}/steps/{quote(c['step_uuid'])}/test_reports"
                if c.get("pipeline_uuid") and c.get("step_uuid")
                else None
            ),
            feeds="tests_total / tests_passed / tests_failed -- REQUIRES the pipeline to publish JUnit XML",
        ),
        ProbeStep(
            "test_cases",
            "GET .../pipelines/{uuid}/steps/{step}/test_reports/test_cases/",
            lambda c: (
                f"{base}/pipelines/{quote(c['pipeline_uuid'])}/steps/{quote(c['step_uuid'])}/test_reports/test_cases/"
                if c.get("pipeline_uuid") and c.get("step_uuid")
                else None
            ),
            params={"pagelen": 1},
            feeds="flaky-test detection (§7.8)",
        ),
        ProbeStep(
            "deployments",
            "GET /2.0/repositories/{ws}/{repo}/deployments/",
            lambda c: f"{base}/deployments/",
            params={"pagelen": 1},
            feeds="deployment frequency, delivery outcome",
        ),
        ProbeStep(
            "environments",
            "GET /2.0/repositories/{ws}/{repo}/environments/",
            lambda c: f"{base}/environments/",
            params={"pagelen": 5},
            feeds="environment dimension (dev/sit/pre/prd)",
        ),
        ProbeStep(
            "commits",
            "GET /2.0/repositories/{ws}/{repo}/commits",
            lambda c: f"{base}/commits",
            params={"pagelen": 1},
            feeds="a commit sha to test build statuses against",
            capture=cap_commits,
        ),
        ProbeStep(
            "commit_statuses",
            "GET /2.0/repositories/{ws}/{repo}/commit/{sha}/statuses",
            lambda c: (
                f"{base}/commit/{quote(c['commit_sha'])}/statuses" if c.get("commit_sha") else None
            ),
            params={"pagelen": 10},
            feeds="** identifies a NON-Bitbucket CI ** -- Jenkins/GitHub Actions/TeamCity all "
            "post build statuses here",
            capture=cap_statuses,
        ),
    ]

    results: List[Dict[str, Any]] = []
    for step in steps:
        url = step.path(captured) if step.path else None
        if not url:
            results.append(
                {
                    "id": step.step_id,
                    "endpoint": step.description,
                    "status": None,
                    "retrievable": False,
                    "note": "skipped -- a prerequisite probe returned no id to test with",
                    "feeds": step.feeds,
                }
            )
            continue
        response = client.probe(url, step.params, method=step.method)
        payload = _json_or_none(response) if 200 <= response.status < 300 else None
        if step.capture and payload is not None:
            try:
                step.capture(payload, captured)
            except Exception as exc:  # noqa: BLE001 - class name only
                log("probe_capture_failed", step=step.step_id, error=type(exc).__name__)
        results.append(
            {
                "id": step.step_id,
                "endpoint": step.description,
                "status": response.status,
                "retrievable": 200 <= response.status < 300,
                "note": status_meaning(response.status),
                "feeds": step.feeds,
                "size_bytes": len(response.data or b""),
            }
        )

    return {"workspace": workspace, "repo": repo, "results": results, "captured": captured}


def render_probe_report(probe: Dict[str, Any]) -> str:
    """Markdown, deliberately paste-ready for the spike doc / a Jira comment."""
    results = probe["results"]
    by_id = {r["id"]: r for r in results}
    captured = probe["captured"]
    lines: List[str] = []
    add = lines.append

    add(f"# OQ-3 probe — CI retrievability for `{probe['workspace']}/{probe['repo']}`")
    add("")
    add(
        "One call per endpoint, no retries. Generated by "
        "`pollers/poll_ci.py --probe`."
    )
    add("")
    add("| # | Endpoint | HTTP | Retrievable | What it feeds | Meaning |")
    add("|---|---|---|---|---|---|")
    for index, result in enumerate(results, start=1):
        status = result["status"]
        add(
            "| {n} | `{endpoint}` | {status} | {ok} | {feeds} | {note} |".format(
                n=index,
                endpoint=result["endpoint"],
                status="skipped" if status is None else status,
                ok="YES" if result["retrievable"] else "no",
                feeds=result["feeds"] or "—",
                note=result["note"],
            )
        )
    add("")
    add("## Verdict")
    add("")

    repo_ok = by_id.get("repo", {}).get("retrievable")
    config_ok = by_id.get("pipelines_config", {}).get("retrievable")
    yml_ok = by_id.get("pipelines_yml", {}).get("retrievable")
    list_ok = by_id.get("pipelines_list", {}).get("retrievable")
    steps_ok = by_id.get("pipeline_steps", {}).get("retrievable")
    reports_ok = by_id.get("test_reports", {}).get("retrievable")
    statuses_ok = by_id.get("commit_statuses", {}).get("retrievable")
    providers = captured.get("build_status_providers") or []

    if not repo_ok:
        add(
            "- **Inconclusive.** The repository itself is not readable "
            f"(HTTP {by_id.get('repo', {}).get('status')}). Fix credentials/scopes and re-run; "
            "nothing below this line can be trusted."
        )
    else:
        add("- Repository access: **OK** (credentials and scopes work for `repository:read`).")

    if list_ok and captured.get("pipeline_size"):
        add(
            f"- **Bitbucket Pipelines IS in use.** `{captured.get('pipeline_size')}` pipeline run(s) "
            f"visible; most recent state `{captured.get('pipeline_state')}` / "
            f"result `{captured.get('pipeline_result')}`."
        )
        add(
            "  - `ci.pipeline.completed` can be emitted with `pipeline_id`, `commit_sha`, "
            "`status` and `duration_ms`."
        )
    elif list_ok:
        add(
            "- **Pipelines endpoint is readable but EMPTY.** Either Pipelines is enabled and never "
            "used, or runs have aged out. CI metrics have no data today."
        )
    else:
        add(
            f"- **Bitbucket Pipelines is NOT retrievable** (pipelines list HTTP "
            f"{by_id.get('pipelines_list', {}).get('status')}; pipelines_config HTTP "
            f"{by_id.get('pipelines_config', {}).get('status')}"
            f"{'; no bitbucket-pipelines.yml in the repo' if not yml_ok else ''})."
        )

    if steps_ok:
        add(f"- Step-level data: **OK** ({captured.get('step_count')} step(s) on the sampled run).")
    if reports_ok:
        add(
            "- Test reports: **OK** — the pipeline publishes JUnit XML, so `tests_total` / "
            "`tests_passed` / `tests_failed` are real."
        )
    else:
        add(
            "- Test reports: **not available** — `tests_*` stay NULL (never 0). Publishing JUnit "
            "XML from the pipeline is the change required."
        )
    add(
        "- Coverage: **not available from any endpoint.** Design §4.3 marks this **[G]** — it needs "
        "a pipeline change to upload `jacoco.xml`/`lcov`. `coverage_pct` is emitted as NULL."
    )

    if statuses_ok and providers:
        add(
            "- **Build statuses found on the sampled commit — this names the real CI system:**"
        )
        for provider in providers:
            add(
                f"  - key=`{provider.get('key')}` name=`{provider.get('name')}` "
                f"state=`{provider.get('state')}` host=`{provider.get('host')}`"
            )
        add(
            "  - If those hosts are not `bitbucket.org`, CI is **external** and this poller's "
            "Pipelines path is the wrong integration. Point OQ-3 at that vendor's API instead."
        )
    elif statuses_ok:
        add(
            "- Build statuses endpoint readable but empty on the sampled commit — no external CI "
            "posts statuses to Bitbucket for that commit."
        )

    add("")
    add("## Answer to record against OQ-3")
    add("")
    if list_ok and captured.get("pipeline_size"):
        answer = "Bitbucket Pipelines, retrievable"
    elif providers:
        answer = "external CI (see build-status providers above), Bitbucket Pipelines unused"
    elif repo_ok:
        answer = "no CI retrievable through Bitbucket for this repository"
    else:
        answer = "inconclusive — repository access failed"
    add(f"> **{answer}**")
    add("")
    add(
        "Interim source that needs no CI at all (design §4.3 **[V]**): the local quality-gate "
        "results in `.tmp/quality-gates-*.log` and `.tmp/test-results/cucumber-report.json`."
    )
    add("")
    add("<details><summary>Raw probe JSON</summary>")
    add("")
    add("```json")
    add(json.dumps(probe, indent=2, sort_keys=True))
    add("```")
    add("")
    add("</details>")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="poll_ci.py",
        description="Emit ci.pipeline.completed events, or probe which CI endpoints work (OQ-3).",
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--since", help="ISO8601 window start (overrides the watermark)")
    parser.add_argument("--out", help="NDJSON output file, or the probe report file with --probe")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="One call per candidate CI endpoint; print a copy-pasteable OQ-3 report and exit",
    )
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--state-file")
    parser.add_argument("--no-watermark", action="store_true")
    parser.add_argument("--max-pipelines", type=int, default=0)
    parser.add_argument(
        "--no-test-reports",
        action="store_true",
        help="Skip step/test-report fetches (much cheaper; tests_* become NULL)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None, client: Optional[HttpClient] = None) -> int:
    """``client`` is injectable so the test-suite never touches the network."""
    args = build_arg_parser().parse_args(argv)
    config = Config.from_env()
    if args.state_file:
        config.state_path = args.state_file

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

    if args.probe:
        probe = run_probe(client, args.workspace, args.repo, config.bitbucket_api_base)
        report = render_probe_report(probe)
        if args.out:
            directory = os.path.dirname(os.path.abspath(args.out))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(report)
            log("probe_report_written", path=args.out, requests=client.request_count)
        else:
            sys.stdout.write(report)
        return 0

    poller = CiPoller(
        client, args.workspace, args.repo, config, fetch_test_reports=not args.no_test_reports
    )
    store = WatermarkStore(config.state_path)
    watermark_key = f"bitbucket:pipelines:{poller.repo_full_name}"
    since = args.since or (
        None if args.no_watermark else default_since(store.get(watermark_key), args.lookback_days)
    )
    if since is None:
        since = default_since(None, args.lookback_days)

    counts = {"pipelines": 0, "events": 0, "incomplete": 0}
    try:
        with NdjsonWriter(args.out) as writer:
            if args.no_watermark:
                _run(poller, writer, since, args, counts, None)
            else:
                with store.checkpoint(watermark_key) as mark:
                    _run(poller, writer, since, args, counts, mark)
            counts["events"] = writer.count
    except HttpError as exc:
        if exc.status in (403, 404):
            log(
                "ci_not_available",
                status=exc.status,
                url=exc.url,
                oq="OQ-3",
                hint="Bitbucket Pipelines is not enabled for this repository, or the token lacks "
                "pipeline:read. This is an ANSWER, not a crash: re-run with --probe to produce "
                "the full OQ-3 report. No events emitted; watermark not advanced.",
            )
            return 4
        log("ci_api_error", status=exc.status, url=exc.url, hint="watermark not advanced")
        return 3
    except KeyboardInterrupt:
        log("interrupted", hint="watermark not advanced")
        return 130

    log(
        "ci_poll_complete",
        repo=poller.repo_full_name,
        since=since,
        ci_system="bitbucket-pipelines",
        ci_system_verified=False,
        requests=client.request_count,
        retries=client.retry_count,
        **counts,
    )
    return 0


def _run(poller: CiPoller, writer: NdjsonWriter, since: str, args, counts, mark) -> None:
    for pipeline in poller.iter_pipelines(since, args.max_pipelines):
        counts["pipelines"] += 1
        event = poller.build_pipeline_event(pipeline)
        if event is None:
            counts["incomplete"] += 1
            continue
        writer.write(event)
        if mark is not None:
            mark.propose(pipeline.get("created_on"))


if __name__ == "__main__":
    raise SystemExit(main())
