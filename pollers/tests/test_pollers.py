#!/usr/bin/env python3
"""Unit tests for the AI-telemetry backfill pollers.

stdlib ``unittest`` only. **No live network calls**: every HTTP interaction goes
through :class:`FakeTransport`, which serves recorded/synthetic payloads shaped
like real Bitbucket Cloud v2.0 and Jira Cloud v3 responses.

Run::

    python3 -m unittest discover -s pollers/tests -v
    python3 pollers/tests/test_pollers.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

POLLERS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, POLLERS_DIR)

import common  # noqa: E402
import poll_bitbucket  # noqa: E402
import poll_ci  # noqa: E402
import poll_aio  # noqa: E402
import poll_jira  # noqa: E402
from common import (  # noqa: E402
    Config,
    ContractViolation,
    HttpClient,
    HttpError,
    WatermarkStore,
    build_event,
    days_between,
    has_ai_commit_marker,
    has_ai_pr_title_marker,
    hash_email,
    parse_ai_trailers,
    sanitise_url,
    unrecognised_ai_labels,
)

BASE = "https://api.bitbucket.org/2.0/repositories/acme/watchtower"
JIRA_BASE = "https://acme.atlassian.net"

AUTHOR = {"account_id": "acct-author", "nickname": "devone", "display_name": "DevOne"}
REVIEWER = {"account_id": "acct-reviewer", "nickname": "devtwo", "display_name": "Dev Two"}
REVIEWER2 = {"account_id": "acct-reviewer2", "nickname": "bob", "display_name": "Bob Smith"}
BOT = {"account_id": None, "nickname": "sonarcloud-bot", "display_name": "SonarCloud Bot"}


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------


def jsonr(payload, status=200, headers=None):
    return (status, headers or {}, json.dumps(payload).encode("utf-8"))


class FakeTransport:
    """Route table over (method, url). First match wins, so order specific first."""

    def __init__(self, routes, fallback=None):
        self.routes = routes
        self.fallback = fallback or jsonr({"type": "error", "error": {"message": "no route"}}, 404)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url))
        for matcher, responder in self.routes:
            hit = matcher(url) if callable(matcher) else matcher in url
            if hit:
                return responder(url) if callable(responder) else responder
        return self.fallback

    def urls(self):
        return [url for _method, url in self.calls]

    def count_matching(self, fragment):
        return sum(1 for url in self.urls() if fragment in url)


def client_for(transport, **kwargs):
    kwargs.setdefault("sleep", lambda _seconds: None)
    return HttpClient(auth=("user", "token"), transport=transport, **kwargs)


# ---------------------------------------------------------------------------
# Bitbucket fixtures
# ---------------------------------------------------------------------------

PR_MERGED = {
    "id": 101,
    "state": "MERGED",
    "title": "[PRJ-6383] add rate-limit enforcement route [Authored By Copilot]",
    "author": AUTHOR,
    "created_on": "2026-08-01T09:00:00.000000+00:00",
    "updated_on": "2026-08-01T15:00:00.000000+00:00",
    "merge_commit": {"hash": "ffff1111ffff1111ffff1111ffff1111ffff1111"},
    "source": {"branch": {"name": "feature/PRJ-6383-rate-limit"}},
    "destination": {"branch": {"name": "main"}},
}

PR_DECLINED = {
    "id": 102,
    "state": "DECLINED",
    "title": "[PRJ-6400] spike on caching",
    "author": REVIEWER2,
    "created_on": "2026-08-02T09:00:00.000000+00:00",
    "updated_on": "2026-08-02T18:00:00.000000+00:00",
    "source": {"branch": {"name": "feature/PRJ-6400-cache"}},
}

# Activity feed, deliberately out of chronological order (the real one is too).
PR_101_ACTIVITY = [
    {"update": {"date": "2026-08-01T15:00:00+00:00", "state": "MERGED", "author": AUTHOR}},
    {
        "approval": {"date": "2026-08-01T14:00:00+00:00", "user": REVIEWER},
    },
    {
        # The PR author commenting on their own PR. NOT review.
        "comment": {
            "id": 1,
            "created_on": "2026-08-01T10:00:00+00:00",
            "user": AUTHOR,
            "content": {"raw": "note to self"},
        }
    },
    {
        # A bot. NOT review either.
        "comment": {
            "id": 2,
            "created_on": "2026-08-01T09:30:00+00:00",
            "user": BOT,
            "content": {"raw": "Quality gate passed"},
        }
    },
    {
        # The genuine first review: 12:00.
        "comment": {
            "id": 3,
            "created_on": "2026-08-01T12:00:00+00:00",
            "user": REVIEWER,
            "inline": {"path": "src/limiter.py", "to": 42},
            "content": {"raw": "extract this"},
        }
    },
    {
        "changes_requested": {"date": "2026-08-01T13:00:00+00:00", "user": REVIEWER2},
    },
]

PR_101_COMMENTS_PAGE1 = {
    "values": [
        {"id": 1, "created_on": "2026-08-01T10:00:00+00:00", "user": AUTHOR},
        {"id": 2, "created_on": "2026-08-01T09:30:00+00:00", "user": BOT},
    ],
    "next": f"{BASE}/pullrequests/101/comments?page=2",
}
PR_101_COMMENTS_PAGE2 = {
    "values": [
        {
            "id": 3,
            "created_on": "2026-08-01T12:00:00+00:00",
            "user": REVIEWER,
            "inline": {"path": "src/limiter.py", "to": 42},
        },
        {"id": 4, "created_on": "2026-08-01T12:05:00+00:00", "user": REVIEWER},
        {"id": 5, "created_on": "2026-08-01T12:06:00+00:00", "user": REVIEWER, "deleted": True},
    ]
}

PR_101_DIFFSTAT_PAGE1 = {
    "values": [
        {"status": "modified", "lines_added": 40, "lines_removed": 5, "new": {"path": "src/a.py"}},
        {"status": "added", "lines_added": 100, "lines_removed": 0, "new": {"path": "src/b.py"}},
    ],
    "next": f"{BASE}/pullrequests/101/diffstat?page=2",
}
PR_101_DIFFSTAT_PAGE2 = {
    "values": [
        {"status": "removed", "lines_added": 0, "lines_removed": 30, "old": {"path": "src/c.py"}},
    ]
}

COMMIT_HUMAN = {
    "hash": "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111",
    "date": "2026-08-01T08:00:00+00:00",
    "message": "feat(api): add endpoint\n",
    "author": {"raw": "Ann Lee <ann.lee@example.com>", "user": REVIEWER},
}
COMMIT_AI = {
    "hash": "bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222",
    "date": "2026-08-01T08:30:00+00:00",
    "message": (
        "[PRJ-6383] [AUTH_BY_COPILOT] : add rate-limit enforcement route\n"
        "\n"
        "AI-Run-Id: run_01hq8f3zk9m2nx7b4c6d\n"
        "AI-Trace-Id: trc_01hq8f3zk1aabbccddee\n"
        "AI-Agent: Platform Developer 2.0@a3f21c9\n"
        "AI-Model: GPT-5.3-Codex\n"
    ),
    "author": {"raw": "DevOne <>", "user": AUTHOR},
}
COMMIT_REVERT = {
    "hash": "cccc3333cccc3333cccc3333cccc3333cccc3333",
    "date": "2026-08-04T08:30:00+00:00",
    "message": (
        'Revert "[PRJ-6383] [AUTH_BY_COPILOT] : add rate-limit enforcement route"\n'
        "\n"
        "This reverts commit bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222.\n"
    ),
    "author": {"raw": "Ann Lee <ann.lee@example.com>", "user": REVIEWER},
}
COMMIT_REVERT_UNRESOLVED = {
    "hash": "dddd4444dddd4444dddd4444dddd4444dddd4444",
    "date": "2026-08-05T08:30:00+00:00",
    "message": 'Revert "something that predates the window"\n',
    "author": {"raw": "Bob Smith <bob@example.com>", "user": REVIEWER2},
}

ALL_COMMITS = [COMMIT_REVERT_UNRESOLVED, COMMIT_REVERT, COMMIT_AI, COMMIT_HUMAN]


def bitbucket_routes():
    """Route table covering every endpoint the Bitbucket poller touches."""
    return [
        (lambda u: "/pullrequests/101/activity" in u, jsonr({"values": PR_101_ACTIVITY})),
        (
            lambda u: "/pullrequests/101/comments" in u and "page=2" in u,
            jsonr(PR_101_COMMENTS_PAGE2),
        ),
        (lambda u: "/pullrequests/101/comments" in u, jsonr(PR_101_COMMENTS_PAGE1)),
        (
            lambda u: "/pullrequests/101/diffstat" in u and "page=2" in u,
            jsonr(PR_101_DIFFSTAT_PAGE2),
        ),
        (lambda u: "/pullrequests/101/diffstat" in u, jsonr(PR_101_DIFFSTAT_PAGE1)),
        (lambda u: "/pullrequests/101/commits" in u, jsonr({"values": [COMMIT_AI, COMMIT_HUMAN]})),
        (
            lambda u: "/pullrequests/102/activity" in u,
            jsonr(
                {
                    "values": [
                        {
                            "update": {
                                "date": "2026-08-02T18:00:00+00:00",
                                "state": "DECLINED",
                                "author": REVIEWER2,
                            }
                        }
                    ]
                }
            ),
        ),
        (lambda u: "/pullrequests/102/comments" in u, jsonr({"values": []})),
        (lambda u: "/pullrequests/102/diffstat" in u, jsonr({"values": []})),
        (lambda u: "/pullrequests/102/commits" in u, jsonr({"values": []})),
        (
            lambda u: "/pullrequests" in u and "page=2" in u,
            jsonr({"values": [PR_DECLINED]}),
        ),
        (
            lambda u: "/pullrequests" in u,
            jsonr({"values": [PR_MERGED], "next": f"{BASE}/pullrequests?page=2"}),
        ),
        (lambda u: u.rstrip("/").endswith("/commits") or "/commits?" in u, jsonr({"values": ALL_COMMITS})),
    ]


def make_bitbucket_poller(transport=None):
    transport = transport or FakeTransport(bitbucket_routes())
    poller = poll_bitbucket.BitbucketPoller(
        client_for(transport), "acme", "watchtower", Config(email_salt="test-salt")
    )
    return poller, transport


# ---------------------------------------------------------------------------
# 1. first_review_at -- the metric-critical derivation
# ---------------------------------------------------------------------------


class TestFirstReviewAt(unittest.TestCase):
    def setUp(self):
        self.timeline = poll_bitbucket.derive_review_timeline(PR_MERGED, PR_101_ACTIVITY)

    def test_excludes_the_pr_authors_own_comments(self):
        """The author's 10:00 self-comment must not become first_review_at."""
        self.assertEqual(self.timeline["first_review_at"], "2026-08-01T12:00:00.000Z")
        self.assertEqual(self.timeline["first_reviewer_person_id"], "acct-reviewer")
        self.assertEqual(self.timeline["self_comment_count"], 1)
        reviewer_ids = {r["person_id"] for r in self.timeline["reviewers"]}
        self.assertNotIn("acct-author", reviewer_ids)

    def test_excludes_bot_activity(self):
        """The bot's 09:30 comment is earlier still, and equally not review."""
        self.assertGreater(self.timeline["bot_activity_count"], 0)
        self.assertNotEqual(self.timeline["first_review_at"], "2026-08-01T09:30:00.000Z")

    def test_earliest_of_comment_approval_or_changes_requested(self):
        by_person = {r["person_id"]: r for r in self.timeline["reviewers"]}
        # Reviewer commented 12:00 then approved 14:00 -> earliest wins for timing,
        # approval wins for the reported action.
        self.assertEqual(by_person["acct-reviewer"]["reviewed_at"], "2026-08-01T12:00:00.000Z")
        self.assertEqual(by_person["acct-reviewer"]["action"], "approved")
        self.assertTrue(by_person["acct-reviewer"]["is_first_review"])
        # Reviewer2 only requested changes, at 13:00.
        self.assertEqual(by_person["acct-reviewer2"]["action"], "changes_requested")
        self.assertEqual(by_person["acct-reviewer2"]["reviewed_at"], "2026-08-01T13:00:00.000Z")
        self.assertFalse(by_person["acct-reviewer2"]["is_first_review"])

    def test_approval_alone_can_be_the_first_review(self):
        activity = [{"approval": {"date": "2026-08-01T11:00:00+00:00", "user": REVIEWER}}]
        timeline = poll_bitbucket.derive_review_timeline(PR_MERGED, activity)
        self.assertEqual(timeline["first_review_at"], "2026-08-01T11:00:00.000Z")

    def test_author_only_activity_yields_no_review_at_all(self):
        activity = [
            {"comment": {"id": 9, "created_on": "2026-08-01T10:00:00+00:00", "user": AUTHOR}},
            {"approval": {"date": "2026-08-01T10:05:00+00:00", "user": AUTHOR}},
        ]
        timeline = poll_bitbucket.derive_review_timeline(PR_MERGED, activity)
        self.assertIsNone(timeline["first_review_at"])
        self.assertEqual(timeline["reviewers"], [])

    def test_merge_and_decline_timestamps_come_from_the_activity_feed(self):
        self.assertEqual(self.timeline["merged_at"], "2026-08-01T15:00:00.000Z")


class TestCommentSummary(unittest.TestCase):
    def test_inline_vs_toplevel_bots_and_self_are_separated(self):
        comments = PR_101_COMMENTS_PAGE1["values"] + PR_101_COMMENTS_PAGE2["values"]
        summary = poll_bitbucket.summarise_comments(comments, "acct-author")
        self.assertEqual(summary["comment_count"], 2)
        self.assertEqual(summary["inline_comment_count"], 1)
        self.assertEqual(summary["toplevel_comment_count"], 1)
        self.assertEqual(summary["author_self_comment_count"], 1)
        self.assertEqual(summary["bot_comment_count"], 1)
        self.assertEqual(summary["deleted_comment_count"], 1)
        self.assertEqual(summary["comment_count_by_person"], {"acct-reviewer": 2})


# ---------------------------------------------------------------------------
# 2. AI marker detection
# ---------------------------------------------------------------------------


class TestAiMarkerDetection(unittest.TestCase):
    def test_matches_all_three_real_world_placements(self):
        for subject in (
            "AUTH_BY_COPILOT: add rate-limit enforcement route",              # prefix, bare
            "[AUTH_BY_COPILOT] [PRJ-6383] add rate-limit enforcement route",  # prefix, bracketed
            "[PRJ-6383] [AUTH_BY_COPILOT] : add rate-limit enforcement",      # infix
            "[PRJ-6383] add rate-limit enforcement route AUTH_BY_COPILOT",    # suffix
        ):
            with self.subTest(subject=subject):
                self.assertTrue(has_ai_commit_marker(subject))

    def test_gen_by_copilot_matches_in_every_placement(self):
        """Regression: the regex matched AUTH_ only, so GEN_-marked work read as human.

        `GEN_BY_COPILOT` is a commit-subject marker with no Jira-label counterpart
        (supervisor-test-spec.agent.md:958,1181; test-executor-committer.agent.md:267,270).
        Missing it is why a 60-day pull of acme/qa-automation reported
        ai_commit_count = 0 across all 102 PRs.
        """
        for subject in (
            "GEN_BY_COPILOT: add rate-limit enforcement route",              # prefix, bare
            "[GEN_BY_COPILOT] [PRJ-6383] add rate-limit enforcement route",  # prefix, bracketed
            "[PRJ-6383] [GEN_BY_COPILOT] : add rate-limit enforcement",      # infix
            "[PRJ-6383] add rate-limit enforcement route GEN_BY_COPILOT",    # suffix
        ):
            with self.subTest(subject=subject):
                self.assertTrue(has_ai_commit_marker(subject))

    def test_gen_by_copilot_is_case_insensitive_like_auth(self):
        self.assertTrue(has_ai_commit_marker("[gen_by_copilot] feat(x): tidy"))

    def test_commit_marker_set_is_exactly_the_two_subject_markers(self):
        """CONTRACT.md §3.1: the commit set is a strict subset of the label set."""
        self.assertEqual(common.AI_COMMIT_MARKERS, ("AUTH_BY_COPILOT", "GEN_BY_COPILOT"))
        self.assertTrue(set(common.AI_COMMIT_MARKERS) < set(common.AI_LABELS))

    def test_label_only_markers_never_match_a_commit_subject(self):
        """PLANNED_/REVIEW_BY_COPILOT are Jira labels; no agent writes them on a subject.

        A commit that mentions one is a human commit *about* the label feature.
        """
        for subject in (
            "feat: PLANNED_BY_COPILOT label handling",
            "[PLANNED_BY_COPILOT] [PRJ-6383] add planner label",
            "feat: REVIEW_BY_COPILOT ingest",
            "[PRJ-6383] wire REVIEW_BY_COPILOT webhook",
        ):
            with self.subTest(subject=subject):
                self.assertFalse(has_ai_commit_marker(subject))

    def test_does_not_match_plain_conventional_commits(self):
        """The aiep-impact-report-generator failure mode, guarded against.

        Its pattern ends `|^feat[\\(:]|^feat\\b|^fix[\\(:]|^fix\\b|^chore[\\(:]|^chore\\b`,
        and conventional commits are mandated repo-wide, so it labels every human
        commit AI-authored (design §3.5).
        """
        for subject in (
            "feat(api): add endpoint",
            "feat: add endpoint",
            "fix(auth): correct null check",
            "fix: correct null check",
            "chore(deps): bump requests",
            "chore: tidy imports",
            "refactor(limiter): extract helper",
            "docs: update README",
        ):
            with self.subTest(subject=subject):
                self.assertFalse(has_ai_commit_marker(subject))

    def test_respects_token_boundaries(self):
        self.assertFalse(has_ai_commit_marker("feat: rename XAUTH_BY_COPILOTX helper"))
        self.assertFalse(has_ai_commit_marker("feat: PLANNED_BY_COPILOT label handling"))
        self.assertFalse(has_ai_commit_marker(""))
        self.assertFalse(has_ai_commit_marker(None))

    def test_widening_did_not_weaken_the_boundary_guards(self):
        """The alternation must not let a longer identifier match either marker."""
        for subject in (
            "feat: rename XGEN_BY_COPILOTX helper",
            "feat: drop OXYGEN_BY_COPILOT constant",   # `GEN` preceded by a letter
            "feat: bump MY_GEN_BY_COPILOTS counter",   # trailing letter
            "feat: tidy AUTH_BY_COPILOT2 fixture",     # trailing digit
            "feat: tidy GEN_BY_COPILOT_V2 fixture",    # trailing underscore
        ):
            with self.subTest(subject=subject):
                self.assertFalse(has_ai_commit_marker(subject))

    def test_only_the_subject_line_counts(self):
        self.assertFalse(
            has_ai_commit_marker("feat(api): add endpoint\n\nmentions AUTH_BY_COPILOT in the body")
        )
        self.assertFalse(
            has_ai_commit_marker("feat(api): add endpoint\n\nmentions GEN_BY_COPILOT in the body")
        )


class TestAiLabelDrift(unittest.TestCase):
    """Labels shaped like a provenance marker but outside the closed set."""

    def test_closed_set_is_the_four_aiep_markers(self):
        self.assertEqual(
            common.AI_LABELS,
            ("AUTH_BY_COPILOT", "PLANNED_BY_COPILOT", "GEN_BY_COPILOT", "REVIEW_BY_COPILOT"),
        )

    def test_known_labels_are_never_reported_as_drift(self):
        self.assertEqual(unrecognised_ai_labels(list(common.AI_LABELS)), [])
        self.assertEqual(unrecognised_ai_labels(["auth_by_copilot", " gen_by_copilot "]), [])

    def test_the_three_live_drifted_labels_are_surfaced(self):
        self.assertEqual(
            unrecognised_ai_labels(
                ["PLANNER_BY_COPILOT", "DEV_BY_COPILOT", "COPILOT_TESTING",
                 "AUTH_BY_COPILOT", "backend"]
            ),
            ["COPILOT_TESTING", "DEV_BY_COPILOT", "PLANNER_BY_COPILOT"],
        )

    def test_ordinary_labels_are_not_drift(self):
        self.assertEqual(
            unrecognised_ai_labels(["backend", "tech-debt", "PILOT", "COPILOTS", "", None]),
            [],
        )

    def test_names_are_normalised_and_deduplicated(self):
        self.assertEqual(
            unrecognised_ai_labels(["dev_by_copilot", "DEV_BY_COPILOT", " Dev_By_Copilot "]),
            ["DEV_BY_COPILOT"],
        )

    def test_drift_is_not_counted_as_an_ai_label(self):
        """The whole point: a drifted name is a DQ signal, never AI attribution."""
        self.assertEqual(poll_jira.ai_labels_on(["PLANNER_BY_COPILOT", "DEV_BY_COPILOT"]), [])

    def test_drift_shape_is_never_used_on_commit_subjects(self):
        """Classifying commits by shape is the §3.5 defect. Names only."""
        self.assertFalse(has_ai_commit_marker("feat: add DEV_BY_COPILOT support"))
        self.assertFalse(has_ai_commit_marker("[COPILOT_TESTING] feat(x): tidy"))

    def test_pr_title_marker(self):
        self.assertTrue(has_ai_pr_title_marker(PR_MERGED["title"]))
        self.assertTrue(has_ai_pr_title_marker("[PRJ-1] x [authored by copilot]"))
        self.assertFalse(has_ai_pr_title_marker("[PRJ-6400] spike on caching"))

    def test_ai_run_id_trailer_is_parsed(self):
        trailers = parse_ai_trailers(COMMIT_AI["message"])
        self.assertEqual(trailers["ai-run-id"], "run_01hq8f3zk9m2nx7b4c6d")
        self.assertEqual(trailers["ai-trace-id"], "trc_01hq8f3zk1aabbccddee")
        self.assertEqual(trailers["ai-model"], "GPT-5.3-Codex")
        self.assertEqual(parse_ai_trailers(COMMIT_HUMAN["message"]), {})

    def test_trailer_earns_an_explicit_link_and_a_marker_alone_does_not(self):
        summary = poll_bitbucket.summarise_pr_commits([COMMIT_AI, COMMIT_HUMAN])
        self.assertEqual(summary["ai_commit_count"], 1)
        link, run_id, trace_id = poll_bitbucket.resolve_link(True, summary, "PRJ-6383")
        self.assertEqual(link, {"method": "explicit", "confidence": 1.0})
        self.assertEqual(run_id, "run_01hq8f3zk9m2nx7b4c6d")
        self.assertEqual(trace_id, "trc_01hq8f3zk1aabbccddee")

        marker_only = poll_bitbucket.summarise_pr_commits(
            [{"hash": "e" * 40, "date": "2026-08-01T00:00:00Z", "message": "AUTH_BY_COPILOT: x"}]
        )
        gen_marker_only = poll_bitbucket.summarise_pr_commits(
            [{"hash": "f" * 40, "date": "2026-08-01T00:00:00Z",
              "message": "[GEN_BY_COPILOT] [PRJ-6383] add generated spec"}]
        )
        # The measured consequence of the old regex: this counted 0, and a whole
        # repository's test-automation work read as having no AI provenance.
        self.assertEqual(gen_marker_only["ai_commit_count"], 1)
        self.assertEqual(
            poll_bitbucket.resolve_link(False, gen_marker_only, "PRJ-6383")[0]["method"],
            "marker_only",
        )
        link, run_id, _ = poll_bitbucket.resolve_link(False, marker_only, "PRJ-6383")
        self.assertEqual(link["method"], "marker_only")
        self.assertIsNone(run_id)

        plain = poll_bitbucket.summarise_pr_commits([COMMIT_HUMAN])
        self.assertEqual(poll_bitbucket.resolve_link(False, plain, "PRJ-6383")[0]["method"], "heuristic")
        self.assertEqual(poll_bitbucket.resolve_link(False, plain, None)[0]["confidence"], 0.0)


# ---------------------------------------------------------------------------
# 3. Revert detection
# ---------------------------------------------------------------------------


class TestRevertDetection(unittest.TestCase):
    def test_resolves_the_reverted_commit_and_days_to_revert(self):
        reverts = poll_bitbucket.find_reverts(ALL_COMMITS)
        self.assertEqual(len(reverts), 2)
        resolved = [r for r in reverts if r["reverted_commit"] is not None]
        self.assertEqual(len(resolved), 1)
        found = resolved[0]
        self.assertEqual(found["revert_commit"]["hash"], COMMIT_REVERT["hash"])
        self.assertEqual(found["reverted_commit_sha"], COMMIT_AI["hash"])
        self.assertEqual(found["resolution"], "reverts_commit_trailer")
        # 2026-08-01T08:30 -> 2026-08-04T08:30 is exactly 3 days.
        self.assertEqual(found["days_to_revert"], 3.0)

    def test_unresolvable_revert_is_still_reported(self):
        reverts = poll_bitbucket.find_reverts(ALL_COMMITS)
        unresolved = [r for r in reverts if r["reverted_commit"] is None][0]
        self.assertEqual(unresolved["revert_commit"]["hash"], COMMIT_REVERT_UNRESOLVED["hash"])
        self.assertIsNone(unresolved["days_to_revert"])
        self.assertEqual(unresolved["resolution"], "unresolved")

    def test_resolves_by_quoted_subject_when_there_is_no_reverts_line(self):
        original = {
            "hash": "1" * 40,
            "date": "2026-08-01T00:00:00+00:00",
            "message": "feat(api): add endpoint\n",
        }
        revert = {
            "hash": "2" * 40,
            "date": "2026-08-03T12:00:00+00:00",
            "message": 'Revert "feat(api): add endpoint"\n',
        }
        found = poll_bitbucket.find_reverts([revert, original])[0]
        self.assertEqual(found["resolution"], "subject_match")
        self.assertEqual(found["reverted_commit_sha"], "1" * 40)
        self.assertEqual(found["days_to_revert"], 2.5)

    def test_a_short_sha_is_expanded_to_the_full_hash(self):
        original = {"hash": "abcdef1234567890" + "0" * 24, "date": "2026-08-01T00:00:00Z", "message": "x"}
        revert = {
            "hash": "9" * 40,
            "date": "2026-08-02T00:00:00Z",
            "message": 'Revert "x"\n\nThis reverts commit abcdef1.\n',
        }
        found = poll_bitbucket.find_reverts([revert, original])[0]
        self.assertEqual(found["reverted_commit_sha"], original["hash"])

    def test_revert_event_carries_the_reverted_commits_ai_provenance(self):
        poller, _ = make_bitbucket_poller()
        events = poller.build_revert_events(ALL_COMMITS)
        resolved = [e for e in events if e["attributes"]["reverted_commit_sha"] == COMMIT_AI["hash"]][0]
        self.assertEqual(resolved["event_type"], "scm.revert")
        self.assertEqual(resolved["attributes"]["days_to_revert"], 3.0)
        self.assertTrue(resolved["attributes"]["reverted_commit_has_ai_marker"])
        # AR-9: credit is withdrawn from the run that produced the reverted work,
        # so the revert must carry that run id.
        self.assertEqual(resolved["run_id"], "run_01hq8f3zk9m2nx7b4c6d")
        self.assertEqual(resolved["link"]["method"], "explicit")

    def test_days_between_helper(self):
        self.assertEqual(days_between("2026-08-01T00:00:00Z", "2026-08-04T12:00:00Z"), 3.5)
        self.assertIsNone(days_between(None, "2026-08-04T12:00:00Z"))


# ---------------------------------------------------------------------------
# 4. Pagination
# ---------------------------------------------------------------------------


class TestPagination(unittest.TestCase):
    def test_diffstat_pagination_is_followed_to_completion(self):
        poller, transport = make_bitbucket_poller()
        entries = poller.pr_diffstat(101)
        self.assertEqual(len(entries), 3)
        summary = poll_bitbucket.summarise_diffstat(entries)
        self.assertEqual(summary["lines_added"], 140)   # 40 + 100 + 0
        self.assertEqual(summary["lines_removed"], 35)  # 5 + 0 + 30
        self.assertEqual(summary["files_changed"], 3)
        self.assertEqual(transport.count_matching("/diffstat"), 2)

    def test_pull_request_list_pagination_is_followed(self):
        poller, transport = make_bitbucket_poller()
        prs = list(poller.iter_pull_requests(["MERGED", "DECLINED"], None))
        self.assertEqual([pr["id"] for pr in prs], [101, 102])
        self.assertGreaterEqual(transport.count_matching("page=2"), 1)

    def test_comment_pagination_is_followed(self):
        poller, _ = make_bitbucket_poller()
        self.assertEqual(len(poller.pr_comments(101)), 5)

    def test_paginate_stops_without_a_next_link(self):
        transport = FakeTransport([(lambda u: True, jsonr({"values": [{"n": 1}]}))])
        items = list(common.paginate(client_for(transport), "https://example.invalid/x"))
        self.assertEqual(items, [{"n": 1}])
        self.assertEqual(len(transport.calls), 1)

    def test_repeated_state_params_are_encoded(self):
        url = common.with_params("https://x/y", {"state": ["OPEN", "MERGED"], "pagelen": 50})
        self.assertIn("state=OPEN", url)
        self.assertIn("state=MERGED", url)


# ---------------------------------------------------------------------------
# 5. Watermarks
# ---------------------------------------------------------------------------


class TestWatermarkStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "state", "poller-state.json")
        self.store = WatermarkStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_advances_on_a_clean_run(self):
        with self.store.checkpoint("bitbucket:prs:acme/watchtower") as mark:
            mark.propose("2026-08-01T09:00:00Z")
            mark.propose("2026-08-03T09:00:00Z")
        self.assertEqual(
            self.store.get("bitbucket:prs:acme/watchtower"), "2026-08-03T09:00:00.000Z"
        )
        # Persisted, not just in memory.
        self.assertEqual(
            WatermarkStore(self.path).get("bitbucket:prs:acme/watchtower"),
            "2026-08-03T09:00:00.000Z",
        )

    def test_does_not_advance_when_the_run_fails_partway(self):
        key = "bitbucket:prs:acme/watchtower"
        with self.store.checkpoint(key) as mark:
            mark.propose("2026-08-01T09:00:00Z")
        self.assertEqual(self.store.get(key), "2026-08-01T09:00:00.000Z")

        with self.assertRaises(HttpError):
            with self.store.checkpoint(key) as mark:
                mark.propose("2026-08-09T09:00:00Z")  # processed some rows...
                raise HttpError(503, "https://api.bitbucket.org/2.0/x")  # ...then died

        self.assertEqual(self.store.get(key), "2026-08-01T09:00:00.000Z")
        self.assertEqual(WatermarkStore(self.path).get(key), "2026-08-01T09:00:00.000Z")

    def test_never_moves_backwards(self):
        key = "jira:issues:PRJ"
        self.store.advance(key, "2026-08-05T00:00:00Z")
        self.store.advance(key, "2026-07-01T00:00:00Z")
        self.assertEqual(self.store.get(key), "2026-08-05T00:00:00.000Z")

    def test_unknown_key_and_missing_file_are_safe(self):
        self.assertIsNone(WatermarkStore(os.path.join(self.tmp.name, "nope.json")).get("k"))
        self.assertIsNone(self.store.get("never-seen"))

    def test_corrupt_state_file_does_not_crash_the_poller(self):
        path = os.path.join(self.tmp.name, "corrupt.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertIsNone(WatermarkStore(path).get("k"))

    def test_default_since_falls_back_to_a_bounded_lookback(self):
        self.assertEqual(common.default_since("2026-08-01T00:00:00Z"), "2026-08-01T00:00:00Z")
        self.assertIsNotNone(common.default_since(None, 7))


# ---------------------------------------------------------------------------
# 6. Envelope, redaction guard and secret screening
# ---------------------------------------------------------------------------


class TestEnvelopeAndRedaction(unittest.TestCase):
    def test_envelope_matches_the_contract(self):
        event = build_event("scm.pr.merged", "2026-08-01T15:00:00Z", ("acme/watchtower", 101), {"pr_id": 101})
        self.assertEqual(set(event), set(common.ENVELOPE_KEYS))
        self.assertEqual(event["schema_version"], "1.0.0")
        self.assertTrue(event["event_id"].startswith("evt_"))
        self.assertEqual(len(event["event_id"]), 4 + 32)
        self.assertIsNone(event["ingested_at"])  # collector's job, never the client's
        for block, keys in (
            ("actor", {"person_id", "person_email_hash", "team_id", "role"}),
            (
                "context",
                {
                    "jira_issue_key",
                    "jira_project_key",
                    "repo_full_name",
                    "branch_name",
                    "product_profile",
                    "environment",
                },
            ),
            (
                "agent",
                {"agent_name", "agent_version", "skill_name", "skill_version", "surface"},
            ),
            ("link", {"method", "confidence"}),
        ):
            self.assertEqual(set(event[block]), keys, block)

    def test_event_id_is_deterministic_so_re_polling_deduplicates(self):
        first = build_event("scm.pr.merged", "2026-08-01T15:00:00Z", ("acme/watchtower", 101), {"pr_id": 101})
        second = build_event("scm.pr.merged", "2026-08-01T15:00:00Z", ("acme/watchtower", 101), {"pr_id": 101})
        third = build_event("scm.pr.merged", "2026-08-01T15:00:00Z", ("acme/watchtower", 102), {"pr_id": 102})
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertNotEqual(first["event_id"], third["event_id"])

    def test_unknown_event_type_is_rejected(self):
        with self.assertRaises(ValueError):
            build_event("jira.issue.snapshot", "2026-08-01T00:00:00Z", ("x",), {})

    def test_forbidden_attribute_names_are_rejected(self):
        for name in ("prompt", "response", "content", "message", "diff", "body", "text", "email"):
            with self.subTest(name=name):
                with self.assertRaises(ContractViolation):
                    build_event("scm.pr.merged", "2026-08-01T00:00:00Z", ("k",), {name: "x"})

    def test_forbidden_names_are_rejected_when_nested(self):
        with self.assertRaises(ContractViolation):
            build_event(
                "scm.pr.merged",
                "2026-08-01T00:00:00Z",
                ("k",),
                {"issue": {"links": [{"diff": "..."}]}},
            )

    def test_key_matching_is_exact_not_substring(self):
        """Regression: substring matching would drop model.call and output.generated.

        `output_content_hash` contains "content"; `input_tokens`,
        `output_tokens` and `cached_input_tokens` contain "token";
        `error_class` contains "error". All are mandated by CONTRACT.md §3 and
        MUST survive the guard.
        """
        legal = {
            "output_content_hash": "9f" * 32,
            "input_tokens": 1200,
            "output_tokens": 340,
            "cached_input_tokens": 800,
            "reasoning_tokens": 64,
            "error_class": "TimeoutError",
            "decline_reason_class": "declined_by_author",
            "merge_commit_sha": "f" * 40,
        }
        event = build_event("model.call", "2026-08-01T00:00:00Z", ("k",), legal)
        self.assertEqual(event["attributes"]["output_content_hash"], "9f" * 32)
        self.assertEqual(event["attributes"]["input_tokens"], 1200)
        self.assertEqual(event["attributes"]["cached_input_tokens"], 800)
        self.assertEqual(event["attributes"]["error_class"], "TimeoutError")
        # ...and nested in exactly the same way.
        nested = build_event(
            "output.generated", "2026-08-01T00:00:00Z", ("k2",), {"usage": legal}
        )
        self.assertEqual(nested["attributes"]["usage"]["output_tokens"], 340)

    def test_raw_email_addresses_are_rejected_anywhere_in_the_envelope(self):
        with self.assertRaises(ContractViolation):
            build_event(
                "scm.revert",
                "2026-08-01T00:00:00Z",
                ("k",),
                {"revert_commit_sha": "a" * 40},
                actor=common.make_actor(person_id="ann.lee@example.com"),
            )

    def test_secret_values_are_screened_as_a_second_layer(self):
        for value in (
            "password = hunter2",
            "api_key=abc123",
            "-----BEGIN RSA PRIVATE KEY-----",
            "AKIAIOSFODNN7EXAMPLE",
            "ATATT3xFfGF0abcdefghijklmnop",
            "ghp_abcdefghijklmnopqrstuvwxyz0123",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ContractViolation) as caught:
                    build_event("scm.pr.merged", "2026-08-01T00:00:00Z", ("k",), {"note": value})
                # The rejection names the field and the check, never the value.
                self.assertNotIn(value, str(caught.exception))

    def test_email_hash_is_salted_and_never_the_raw_address(self):
        digest = hash_email("Ann.Lee@Example.com", "salty")
        self.assertEqual(digest, hash_email("ann.lee@example.com", "salty"))
        self.assertNotEqual(digest, hash_email("ann.lee@example.com", "other-salt"))
        self.assertNotIn("@", digest)
        self.assertIsNone(hash_email("ann.lee@example.com", None))  # no salt, no hash
        self.assertIsNone(hash_email(None, "salty"))

    def test_explicit_link_forces_confidence_one(self):
        self.assertEqual(common.make_link("explicit", 0.2)["confidence"], 1.0)
        with self.assertRaises(ValueError):
            common.make_link("guessed", 0.5)


# ---------------------------------------------------------------------------
# 7. HTTP client behaviour
# ---------------------------------------------------------------------------


class TestHttpClient(unittest.TestCase):
    def test_429_honours_retry_after_then_succeeds(self):
        responses = [
            (429, {"retry-after": "7"}, b"{}"),
            (200, {}, b'{"values": [{"ok": true}]}'),
        ]
        transport = FakeTransport([(lambda u: True, lambda u: responses.pop(0))])
        slept = []
        client = HttpClient(auth=("u", "t"), transport=transport, sleep=slept.append)
        payload = client.get_json("https://api.bitbucket.org/2.0/x")
        self.assertEqual(payload["values"], [{"ok": True}])
        self.assertEqual(slept, [7.0])
        self.assertEqual(client.retry_count, 1)

    def test_5xx_backs_off_exponentially_and_gives_up(self):
        transport = FakeTransport([(lambda u: True, (503, {}, b"{}"))])
        slept = []
        client = HttpClient(auth=("u", "t"), transport=transport, sleep=slept.append, max_retries=4)
        with self.assertRaises(HttpError) as caught:
            client.get_json("https://api.bitbucket.org/2.0/x")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(len(transport.calls), 4)
        self.assertEqual(len(slept), 3)
        self.assertLess(slept[0], slept[-1])  # exponential

    def test_4xx_is_not_retried(self):
        transport = FakeTransport([(lambda u: True, (404, {}, b"{}"))])
        client = client_for(transport)
        with self.assertRaises(HttpError):
            client.get_json("https://api.bitbucket.org/2.0/x")
        self.assertEqual(len(transport.calls), 1)

    def test_errors_never_carry_the_response_body(self):
        secret_body = b'{"error": {"message": "token sk-live-DO-NOT-LEAK expired"}}'
        transport = FakeTransport([(lambda u: True, (403, {}, secret_body))])
        with self.assertRaises(HttpError) as caught:
            client_for(transport).get_json("https://api.bitbucket.org/2.0/x")
        self.assertNotIn("DO-NOT-LEAK", str(caught.exception))
        self.assertEqual(caught.exception.status, 403)

    def test_urls_are_sanitised_before_logging(self):
        dirty = "https://user:pw@api.bitbucket.org/2.0/x?access_token=abc123&state=OPEN"
        clean = sanitise_url(dirty)
        self.assertNotIn("abc123", clean)
        self.assertNotIn("pw@", clean)
        self.assertIn("state=OPEN", clean)

    def test_auth_header_is_basic_never_bearer(self):
        transport = FakeTransport([(lambda u: True, jsonr({}))])
        client = HttpClient(auth=("user", "token"), transport=transport)
        client.get_json("https://api.bitbucket.org/2.0/x")
        header = client._headers["Authorization"]
        self.assertTrue(header.startswith("Basic "))
        self.assertNotIn("Bearer", header)

    def test_transport_falls_back_when_requests_is_absent(self):
        self.assertTrue(callable(common.default_transport()))


# ---------------------------------------------------------------------------
# 8. Bitbucket poller end to end (no network)
# ---------------------------------------------------------------------------


class TestBitbucketPollerEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "events.ndjson")
        self.state = os.path.join(self.tmp.name, "state.json")
        self.transport = FakeTransport(bitbucket_routes())

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, extra=()):
        code = poll_bitbucket.main(
            [
                "--workspace", "acme",
                "--repo", "watchtower",
                "--out", self.out,
                "--state-file", self.state,
                "--since", "2026-07-01T00:00:00Z",
                *extra,
            ],
            client=client_for(self.transport),
        )
        with open(self.out, "r", encoding="utf-8") as handle:
            events = [json.loads(line) for line in handle if line.strip()]
        return code, events

    def test_emits_the_expected_event_mix(self):
        code, events = self._run()
        self.assertEqual(code, 0)
        kinds = [event["event_type"] for event in events]
        self.assertIn("scm.pr.merged", kinds)
        self.assertIn("scm.pr.declined", kinds)
        self.assertIn("scm.pr.reviewed", kinds)
        self.assertIn("scm.revert", kinds)
        for event in events:
            common.validate_event(event)

    def test_merged_event_carries_the_contract_attributes(self):
        _code, events = self._run()
        merged = [e for e in events if e["event_type"] == "scm.pr.merged"][0]
        attributes = merged["attributes"]
        self.assertEqual(attributes["pr_id"], 101)
        self.assertEqual(attributes["merged_at"], "2026-08-01T15:00:00.000Z")
        self.assertEqual(attributes["merge_commit_sha"], PR_MERGED["merge_commit"]["hash"])
        self.assertEqual(attributes["first_review_at"], "2026-08-01T12:00:00.000Z")
        self.assertEqual(attributes["lines_added"], 140)
        self.assertEqual(attributes["lines_removed"], 35)
        self.assertEqual(attributes["comment_count"], 2)
        self.assertEqual(attributes["inline_comment_count"], 1)
        self.assertTrue(attributes["pr_title_has_ai_marker"])
        self.assertEqual(attributes["ai_commit_count"], 1)
        self.assertEqual(merged["context"]["jira_issue_key"], "PRJ-6383")
        self.assertEqual(merged["context"]["jira_project_key"], "PRJ")
        self.assertEqual(merged["link"]["method"], "explicit")
        self.assertEqual(merged["run_id"], "run_01hq8f3zk9m2nx7b4c6d")
        # review lead time = 09:00 -> 12:00 = 3h
        self.assertEqual(attributes["review_lead_time_ms"], 3 * 3600 * 1000)

    def test_reviewed_events_are_one_per_non_author_reviewer(self):
        _code, events = self._run()
        reviewed = [e for e in events if e["event_type"] == "scm.pr.reviewed"]
        self.assertEqual(len(reviewed), 2)
        self.assertEqual(
            {e["attributes"]["reviewer_person_id"] for e in reviewed},
            {"acct-reviewer", "acct-reviewer2"},
        )
        self.assertNotIn("acct-author", {e["attributes"]["reviewer_person_id"] for e in reviewed})
        first = [e for e in reviewed if e["attributes"]["is_first_review"]]
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["attributes"]["reviewer_person_id"], "acct-reviewer")

    def test_declined_event_classifies_the_reason(self):
        _code, events = self._run()
        declined = [e for e in events if e["event_type"] == "scm.pr.declined"][0]
        self.assertEqual(declined["attributes"]["pr_id"], 102)
        self.assertEqual(declined["attributes"]["decline_reason_class"], "declined_by_author")
        self.assertIn(
            declined["attributes"]["decline_reason_class"], poll_bitbucket.DECLINE_REASONS
        )

    def test_output_contains_no_names_or_email_addresses(self):
        _code, _events = self._run()
        with open(self.out, "r", encoding="utf-8") as handle:
            raw = handle.read()
        for leak in ("ann.lee@example.com", "Ann Lee", "DevOne", "display_name", "note to self"):
            self.assertNotIn(leak, raw, f"{leak!r} leaked into the event stream")

    def test_watermark_advances_after_a_successful_run(self):
        self._run()
        store = WatermarkStore(self.state)
        self.assertEqual(
            store.get("bitbucket:pullrequests:acme/watchtower"), "2026-08-02T18:00:00.000Z"
        )

    def test_a_failing_api_leaves_the_watermark_and_output_untouched(self):
        WatermarkStore(self.state).advance(
            "bitbucket:pullrequests:acme/watchtower", "2026-07-15T00:00:00Z"
        )
        broken = FakeTransport(
            [
                (lambda u: "/pullrequests/101/activity" in u, (500, {}, b"{}")),
                *bitbucket_routes(),
            ]
        )
        code = poll_bitbucket.main(
            [
                "--workspace", "acme",
                "--repo", "watchtower",
                "--out", self.out,
                "--state-file", self.state,
                "--since", "2026-07-01T00:00:00Z",
            ],
            client=client_for(broken, max_retries=2),
        )
        self.assertEqual(code, 3)
        self.assertEqual(
            WatermarkStore(self.state).get("bitbucket:pullrequests:acme/watchtower"),
            "2026-07-15T00:00:00.000Z",
        )
        self.assertFalse(os.path.exists(self.out), "a failed run must not leave a partial batch")


# ---------------------------------------------------------------------------
# 9. Jira poller
# ---------------------------------------------------------------------------

JIRA_ISSUE_FEATURE = {
    "id": "10001",
    "key": "PRJ-6383",
    "fields": {
        "project": {"key": "PRJ"},
        "issuetype": {"name": "Story", "subtask": False},
        "status": {"name": "Done", "statusCategory": {"key": "done"}},
        "priority": {"name": "High"},
        "assignee": {
            "accountId": "5f8a1b2c3d4e5f6a7b8c9d0e",
            "displayName": "Ann Lee",
            "emailAddress": "ann.lee@example.com",
        },
        "reporter": {"accountId": "acct-reporter", "displayName": "Bob Smith"},
        "creator": {"accountId": "acct-reporter", "displayName": "Bob Smith"},
        "labels": ["AUTH_BY_COPILOT", "backend"],
        "created": "2026-07-28T09:00:00.000+0000",
        "updated": "2026-08-03T17:00:00.000+0000",
        "resolutiondate": "2026-08-03T17:00:00.000+0000",
        "timeoriginalestimate": 28800,
        "timespent": 21600,
        "issuelinks": [],
        "subtasks": [],
    },
    "changelog": {
        "startAt": 0,
        "maxResults": 2,
        "total": 2,
        "histories": [
            {
                "id": "9001",
                "created": "2026-07-29T10:00:00.000+0000",
                "author": {"accountId": "acct-reporter", "displayName": "Bob Smith"},
                "items": [
                    {"field": "status", "from": "1", "fromString": "To Do", "to": "3", "toString": "In Progress"},
                    {"field": "assignee", "fromString": None, "toString": "Ann Lee"},
                ],
            },
            {
                "id": "9002",
                "created": "2026-08-03T17:00:00.000+0000",
                "author": {"accountId": "5f8a1b2c3d4e5f6a7b8c9d0e", "displayName": "Ann Lee"},
                "items": [
                    {"field": "status", "from": "3", "fromString": "In Progress", "to": "5", "toString": "Done"}
                ],
            },
        ],
    },
}

# The qd_jira_key hazard: the AI labels sit on the QualDev delivery ticket.
JIRA_ISSUE_DELIVERY = {
    "id": "10002",
    "key": "QD-12",
    "fields": {
        "project": {"key": "QD"},
        "issuetype": {"name": "Task", "subtask": False},
        "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        "assignee": {"accountId": "acct-qa", "displayName": "Dev.Three"},
        "reporter": {"accountId": "acct-qa", "displayName": "devthree"},
        # Three of the four closed-set markers, plus two of the drifted names
        # found in live data (design §4.1 drift).
        "labels": [
            "AUTH_BY_COPILOT", "PLANNED_BY_COPILOT", "GEN_BY_COPILOT",
            "PLANNER_BY_COPILOT", "COPILOT_TESTING",
        ],
        "created": "2026-08-01T09:00:00.000+0000",
        "updated": "2026-08-02T09:00:00.000+0000",
        "issuelinks": [
            {
                "type": {"name": "Relates", "outward": "relates to", "inward": "relates to"},
                "outwardIssue": {"key": "PRJ-6383", "fields": {"status": {"name": "Done"}}},
            }
        ],
        "subtasks": [],
    },
    "changelog": {"startAt": 0, "maxResults": 0, "total": 0, "histories": []},
}


def jira_routes(issues=None, search_api="legacy"):
    issues = issues if issues is not None else [JIRA_ISSUE_FEATURE, JIRA_ISSUE_DELIVERY]
    routes = [
        (
            lambda u: "/rest/api/3/status" in u,
            jsonr(
                [
                    {"name": "To Do", "statusCategory": {"key": "new"}},
                    {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
                    {"name": "Done", "statusCategory": {"key": "done"}},
                ]
            ),
        ),
    ]
    if search_api == "legacy":
        routes.append(
            (
                lambda u: "/rest/api/3/search/jql" in u,
                jsonr({"errorMessages": ["not found"]}, 404),
            )
        )
        routes.append(
            (
                lambda u: "/rest/api/3/search" in u and "startAt=0" in u,
                jsonr({"issues": issues, "total": len(issues), "startAt": 0, "maxResults": 50}),
            )
        )
        routes.append(
            (lambda u: "/rest/api/3/search" in u, jsonr({"issues": [], "total": len(issues)})),
        )
    else:
        routes.append(
            (
                lambda u: "/rest/api/3/search/jql" in u,
                jsonr({"issues": issues, "isLast": True}),
            )
        )
    return routes


class TestJiraPoller(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "jira.ndjson")
        self.state = os.path.join(self.tmp.name, "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _events(self, search_api="legacy", issues=None):
        transport = FakeTransport(jira_routes(issues, search_api))
        code = poll_jira.main(
            [
                "--project", "PRJ",
                "--out", self.out,
                "--state-file", self.state,
                "--since", "2026-07-01T00:00:00Z",
                "--delivery-project", "QD",
            ],
            client=client_for(transport),
            base_url=JIRA_BASE,
        )
        with open(self.out, "r", encoding="utf-8") as handle:
            return code, [json.loads(line) for line in handle if line.strip()], transport

    def test_emits_transitions_with_a_synthesised_creation(self):
        code, events, _ = self._events()
        self.assertEqual(code, 0)
        for event in events:
            self.assertEqual(event["event_type"], "jira.transition")
            common.validate_event(event)
        feature = [e for e in events if e["attributes"]["jira_issue_key"] == "PRJ-6383"]
        self.assertEqual(len(feature), 3)  # created + 2 status changes
        created = [e for e in feature if e["attributes"]["is_synthesised_creation"]][0]
        self.assertIsNone(created["attributes"]["from_status"])
        self.assertEqual(created["attributes"]["to_status"], "To Do")
        real = [e for e in feature if not e["attributes"]["is_synthesised_creation"]]
        self.assertEqual(
            [(e["attributes"]["from_status"], e["attributes"]["to_status"]) for e in real],
            [("To Do", "In Progress"), ("In Progress", "Done")],
        )
        self.assertEqual(real[-1]["attributes"]["status_category"], "done")
        self.assertEqual(real[0]["attributes"]["status_category"], "indeterminate")

    def test_non_status_changelog_items_are_ignored(self):
        _code, events, _ = self._events()
        feature = [e for e in events if e["attributes"]["jira_issue_key"] == "PRJ-6383"]
        self.assertTrue(all(e["attributes"]["to_status"] != "Ann Lee" for e in feature))

    def test_person_id_is_the_account_id_and_names_never_leak(self):
        _code, events, _ = self._events()
        feature = [e for e in events if e["attributes"]["jira_issue_key"] == "PRJ-6383"]
        done = [e for e in feature if e["attributes"]["to_status"] == "Done"][0]
        self.assertEqual(done["actor"]["person_id"], "5f8a1b2c3d4e5f6a7b8c9d0e")
        self.assertEqual(
            done["attributes"]["issue"]["assignee_person_id"], "5f8a1b2c3d4e5f6a7b8c9d0e"
        )
        with open(self.out, "r", encoding="utf-8") as handle:
            raw = handle.read()
        for leak in ("Ann Lee", "displayName", "emailAddress", "ann.lee@example.com", "DevThree"):
            self.assertNotIn(leak, raw, f"{leak!r} leaked into the event stream")

    def test_issue_snapshot_rides_on_the_transition(self):
        _code, events, _ = self._events()
        snapshot = [
            e for e in events if e["attributes"]["jira_issue_key"] == "PRJ-6383"
        ][0]["attributes"]["issue"]
        self.assertEqual(snapshot["issue_type"], "Story")
        self.assertEqual(snapshot["status"], "Done")
        self.assertEqual(snapshot["labels"], ["AUTH_BY_COPILOT", "backend"])
        self.assertEqual(snapshot["estimate_original_seconds"], 28800)
        self.assertEqual(snapshot["time_spent_seconds"], 21600)
        self.assertNotIn("summary", snapshot)  # content is never emitted

    def test_qd_attribution_hazard_is_surfaced_for_ar3(self):
        _code, events, _ = self._events()
        delivery = [e for e in events if e["attributes"]["jira_issue_key"] == "QD-12"][0]
        attribution = delivery["attributes"]["attribution"]
        self.assertEqual(attribution["rule"], "AR-3")
        self.assertTrue(attribution["has_ai_labels"])
        self.assertTrue(attribution["label_authored_by_ai"])
        self.assertTrue(attribution["label_planned_by_ai"])
        self.assertTrue(attribution["is_delivery_ticket_candidate"])
        self.assertTrue(attribution["label_generated_by_ai"])
        # Nothing in this repository writes REVIEW_BY_COPILOT -- an external AI
        # code-review system does -- so its absence must read as absent, not unknown.
        self.assertFalse(attribution["label_reviewed_by_ai"])
        self.assertEqual(attribution["delivery_ticket_key"], "QD-12")
        self.assertEqual(attribution["feature_ticket_key"], "PRJ-6383")
        self.assertEqual(attribution["linked_issues"][0]["issue_key"], "PRJ-6383")
        # Labels alone are never an explicit link (design §5.3 L3).
        self.assertEqual(delivery["link"]["method"], "marker_only")

    def test_marker_drift_reaches_the_event_instead_of_being_dropped(self):
        _code, events, _ = self._events()
        delivery = [e for e in events if e["attributes"]["jira_issue_key"] == "QD-12"][0]
        attribution = delivery["attributes"]["attribution"]
        self.assertTrue(attribution["has_ai_label_drift"])
        self.assertEqual(
            attribution["unrecognised_ai_labels"],
            ["COPILOT_TESTING", "PLANNER_BY_COPILOT"],
        )
        # Drift is a DQ signal, never attribution: the drifted names must not
        # appear in the counted set.
        self.assertEqual(
            attribution["ai_labels"],
            ["AUTH_BY_COPILOT", "PLANNED_BY_COPILOT", "GEN_BY_COPILOT"],
        )

    def test_ticket_without_drift_reports_no_drift(self):
        _code, events, _ = self._events()
        feature = [e for e in events if e["attributes"]["jira_issue_key"] == "PRJ-6383"][0]
        attribution = feature["attributes"]["attribution"]
        self.assertFalse(attribution["has_ai_label_drift"])
        self.assertEqual(attribution["unrecognised_ai_labels"], [])

    def test_feature_ticket_without_links_is_not_a_delivery_candidate(self):
        _code, events, _ = self._events()
        feature = [e for e in events if e["attributes"]["jira_issue_key"] == "PRJ-6383"][0]
        attribution = feature["attributes"]["attribution"]
        self.assertTrue(attribution["has_ai_labels"])
        self.assertFalse(attribution["is_delivery_ticket_candidate"])
        self.assertIsNone(attribution["feature_ticket_key"])

    def test_falls_back_from_the_new_search_endpoint_to_the_legacy_one(self):
        _code, events, transport = self._events(search_api="legacy")
        self.assertTrue(any("/rest/api/3/search/jql" in u for u in transport.urls()))
        self.assertTrue(
            any("/rest/api/3/search?" in u or "/rest/api/3/search&" in u for u in transport.urls())
        )
        self.assertTrue(events)

    def test_new_search_jql_endpoint_is_used_when_available(self):
        _code, events, transport = self._events(search_api="jql")
        self.assertTrue(any("/rest/api/3/search/jql" in u for u in transport.urls()))
        self.assertTrue(events)

    def test_truncated_changelog_is_refetched_in_full(self):
        truncated = json.loads(json.dumps(JIRA_ISSUE_FEATURE))
        truncated["changelog"] = {
            "startAt": 0,
            "maxResults": 1,
            "total": 2,
            "histories": [JIRA_ISSUE_FEATURE["changelog"]["histories"][0]],
        }
        routes = [
            (
                lambda u: "/rest/api/3/issue/PRJ-6383/changelog" in u,
                jsonr(
                    {
                        "values": JIRA_ISSUE_FEATURE["changelog"]["histories"],
                        "isLast": True,
                        "total": 2,
                    }
                ),
            ),
            *jira_routes([truncated], "jql"),
        ]
        transport = FakeTransport(routes)
        poller = poll_jira.JiraPoller(client_for(transport), JIRA_BASE, Config())
        events = poller.build_issue_events(truncated)
        self.assertEqual(len(events), 3)
        self.assertTrue(any("/changelog" in u for u in transport.urls()))

    def test_watermark_advances_to_the_newest_updated(self):
        self._events()
        self.assertEqual(
            WatermarkStore(self.state).get("jira:issues:PRJ"), "2026-08-03T17:00:00.000Z"
        )

    def test_build_jql_is_windowed_and_ordered(self):
        poller = poll_jira.JiraPoller(client_for(FakeTransport([])), JIRA_BASE)
        jql = poller.build_jql("PRJ", "2026-08-01T00:00:00Z")
        self.assertIn('project = "PRJ"', jql)
        self.assertIn('updated >= "2026-08-01 00:00"', jql)
        self.assertTrue(jql.endswith("ORDER BY updated ASC"))

    def test_status_category_falls_back_without_the_status_endpoint(self):
        self.assertEqual(poll_jira.status_category("In Progress"), "indeterminate")
        self.assertEqual(poll_jira.status_category("Done"), "done")
        self.assertIsNone(poll_jira.status_category("Awaiting Sign-off"))
        self.assertEqual(
            poll_jira.status_category("Awaiting Sign-off", {"awaiting sign-off": "indeterminate"}),
            "indeterminate",
        )


# ---------------------------------------------------------------------------
# 10. CI poller and the OQ-3 probe
# ---------------------------------------------------------------------------

PIPELINE = {
    "uuid": "{9c1b2d3e-4f50-6789-abcd-ef0123456789}",
    "build_number": 412,
    "state": {"name": "COMPLETED", "result": {"name": "SUCCESSFUL"}},
    "created_on": "2026-08-01T10:00:00.000000Z",
    "completed_on": "2026-08-01T10:04:00.000000Z",
    "build_seconds_used": 240,
    "creator": {"account_id": "acct-author"},
    "trigger": {"name": "PUSH"},
    "target": {
        "ref_name": "feature/PRJ-6383-rate-limit",
        "commit": {"hash": COMMIT_AI["hash"]},
    },
}


def ci_routes(pipelines_status=200):
    return [
        (
            lambda u: "/pipelines/" in u and "/steps/" in u and "test_reports" in u,
            jsonr({"successful": 12, "failed": 1, "skipped": 2, "error": 0, "total": 15}),
        ),
        (
            lambda u: "/pipelines/" in u and "/steps/" in u,
            jsonr(
                {
                    "values": [
                        {
                            "uuid": "{step-1}",
                            "name": "Build and test",
                            "state": {"result": {"name": "SUCCESSFUL"}},
                        }
                    ]
                }
            ),
        ),
        (
            lambda u: "/pipelines/" in u,
            jsonr({"values": [PIPELINE], "size": 1}, pipelines_status)
            if pipelines_status == 200
            else (pipelines_status, {}, b'{"type":"error"}'),
        ),
    ]


class TestCiPoller(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "ci.ndjson")
        self.state = os.path.join(self.tmp.name, "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_emits_ci_pipeline_completed_with_test_counts(self):
        transport = FakeTransport(ci_routes())
        code = poll_ci.main(
            [
                "--workspace", "acme",
                "--repo", "watchtower",
                "--out", self.out,
                "--state-file", self.state,
                "--since", "2026-07-01T00:00:00Z",
            ],
            client=client_for(transport),
        )
        self.assertEqual(code, 0)
        with open(self.out, "r", encoding="utf-8") as handle:
            events = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(events), 1)
        event = events[0]
        common.validate_event(event)
        self.assertEqual(event["event_type"], "ci.pipeline.completed")
        attributes = event["attributes"]
        self.assertEqual(attributes["pipeline_id"], PIPELINE["uuid"])
        self.assertEqual(attributes["commit_sha"], COMMIT_AI["hash"])
        self.assertEqual(attributes["status"], "passed")
        self.assertEqual(attributes["duration_ms"], 240000)
        self.assertEqual(attributes["tests_total"], 15)
        self.assertEqual(attributes["tests_passed"], 12)
        self.assertEqual(attributes["tests_failed"], 1)
        self.assertIsNone(attributes["coverage_pct"])  # NULL, never 0
        self.assertFalse(attributes["ci_system_verified"])  # OQ-3 unresolved
        self.assertEqual(event["context"]["jira_issue_key"], "PRJ-6383")

    def test_in_flight_pipelines_are_skipped(self):
        running = json.loads(json.dumps(PIPELINE))
        running["state"] = {"name": "IN_PROGRESS"}
        status, complete = poll_ci.map_pipeline_status(running)
        self.assertIsNone(status)
        self.assertFalse(complete)

    def test_status_mapping_is_bounded(self):
        for result, expected in (
            ("SUCCESSFUL", "passed"),
            ("FAILED", "failed"),
            ("ERROR", "error"),
            ("STOPPED", "stopped"),
            ("EXPIRED", "expired"),
        ):
            pipeline = {"state": {"name": "COMPLETED", "result": {"name": result}}}
            self.assertEqual(poll_ci.map_pipeline_status(pipeline)[0], expected)

    def test_test_report_shapes_are_handled_defensively(self):
        self.assertEqual(
            poll_ci.summarise_test_report({"successful": 3, "failed": 1, "error": 1, "total": 4})[
                "tests_failed"
            ],
            2,
        )
        cases = {"values": [{"status": "SUCCESSFUL"}, {"status": "FAILED"}, {"status": "SKIPPED"}]}
        summary = poll_ci.summarise_test_report(cases)
        self.assertEqual((summary["tests_total"], summary["tests_passed"]), (3, 1))
        # Unrecognised payload -> NULLs, never fabricated zeros.
        self.assertEqual(poll_ci.summarise_test_report({"weird": True})["tests_total"], None)

    def test_pipelines_not_enabled_fails_gracefully_and_informatively(self):
        transport = FakeTransport(ci_routes(pipelines_status=404))
        code = poll_ci.main(
            [
                "--workspace", "acme",
                "--repo", "watchtower",
                "--out", self.out,
                "--state-file", self.state,
                "--since", "2026-07-01T00:00:00Z",
            ],
            client=client_for(transport),
        )
        self.assertEqual(code, 4, "a missing CI must be an answer to OQ-3, not a crash")
        self.assertFalse(os.path.exists(self.out))
        self.assertIsNone(WatermarkStore(self.state).get("bitbucket:pipelines:acme/watchtower"))


class TestOq3Probe(unittest.TestCase):
    def _probe_transport(self, pipelines_enabled):
        routes = [
            (
                lambda u: u.endswith("/watchtower"),
                jsonr({"uuid": "{repo}", "mainbranch": {"name": "develop"}}),
            ),
            (
                lambda u: "/pipelines_config" in u,
                jsonr({"enabled": True}) if pipelines_enabled else (404, {}, b"{}"),
            ),
            (lambda u: "bitbucket-pipelines.yml" in u, (404, {}, b"{}")),
            (
                lambda u: "test_reports" in u,
                jsonr({"successful": 12, "failed": 1, "total": 13}),
            ),
            (
                lambda u: "/steps/" in u,
                jsonr({"values": [{"uuid": "{step-1}", "name": "Build and test"}]}),
            ),
            (
                lambda u: "/pipelines/" in u,
                jsonr({"values": [PIPELINE], "size": 1}) if pipelines_enabled else (403, {}, b"{}"),
            ),
            (lambda u: "/deployments/" in u, (404, {}, b"{}")),
            (lambda u: "/environments/" in u, jsonr({"values": []})),
            (lambda u: "/commit/" in u and "/statuses" in u, jsonr({
                "values": [
                    {
                        "key": "JENKINS",
                        "name": "watchtower-ci",
                        "state": "SUCCESSFUL",
                        "url": "https://jenkins.internal.example.com/job/watchtower/412/",
                    }
                ]
            })),
            (lambda u: "/commits" in u, jsonr({"values": [COMMIT_AI]})),
        ]
        return FakeTransport(routes)

    def test_probe_makes_exactly_one_call_per_endpoint(self):
        transport = self._probe_transport(pipelines_enabled=False)
        probe = poll_ci.run_probe(client_for(transport), "acme", "watchtower", "https://api.bitbucket.org")
        attempted = [r for r in probe["results"] if r["status"] is not None]
        self.assertEqual(len(transport.calls), len(attempted))
        # A 403 must NOT be retried in probe mode -- one call means one answer.
        self.assertEqual(transport.count_matching("/pipelines/?"), 1)

    def test_probe_reports_statuses_and_identifies_an_external_ci(self):
        transport = self._probe_transport(pipelines_enabled=False)
        probe = poll_ci.run_probe(client_for(transport), "acme", "watchtower", "https://api.bitbucket.org")
        by_id = {r["id"]: r for r in probe["results"]}
        self.assertTrue(by_id["repo"]["retrievable"])
        self.assertEqual(by_id["pipelines_config"]["status"], 404)
        self.assertEqual(by_id["pipelines_list"]["status"], 403)
        self.assertIn("scope", by_id["pipelines_list"]["note"])
        self.assertTrue(by_id["commit_statuses"]["retrievable"])
        # Steps could not be probed because no pipeline uuid was obtainable.
        self.assertIsNone(by_id["pipeline_steps"]["status"])
        self.assertIn("prerequisite", by_id["pipeline_steps"]["note"])

        report = poll_ci.render_probe_report(probe)
        self.assertIn("OQ-3 probe", report)
        self.assertIn("Bitbucket Pipelines is NOT retrievable", report)
        self.assertIn("jenkins.internal.example.com", report)
        self.assertIn("external CI", report)
        self.assertIn("| # | Endpoint | HTTP |", report)  # copy-pasteable markdown
        self.assertIn("Answer to record against OQ-3", report)

    def test_probe_reports_a_working_pipelines_setup(self):
        transport = self._probe_transport(pipelines_enabled=True)
        probe = poll_ci.run_probe(client_for(transport), "acme", "watchtower", "https://api.bitbucket.org")
        report = poll_ci.render_probe_report(probe)
        self.assertIn("Bitbucket Pipelines IS in use", report)
        self.assertIn("Bitbucket Pipelines, retrievable", report)
        # The main branch discovered from the repo probe is used for the yml lookup.
        self.assertTrue(any("/src/develop/bitbucket-pipelines.yml" in u for u in transport.urls()))

    def test_probe_report_never_contains_a_credential(self):
        transport = self._probe_transport(pipelines_enabled=True)
        client = client_for(transport)
        report = poll_ci.render_probe_report(
            poll_ci.run_probe(client, "acme", "watchtower", "https://api.bitbucket.org")
        )
        # The rendered Authorization header value, its base64 payload and the raw
        # secret must never appear. ("token" as an English word does appear, in
        # prose explaining what a 403 means -- that is not a credential.)
        for secret in (
            client._headers["Authorization"],
            "dXNlcjp0b2tlbg==",
            "Authorization:",
            "Basic ",
        ):
            self.assertNotIn(secret, report)

    def test_probe_report_uses_the_step_uuid_from_the_steps_call(self):
        transport = self._probe_transport(pipelines_enabled=True)
        poll_ci.run_probe(client_for(transport), "acme", "watchtower", "https://api.bitbucket.org")
        self.assertTrue(
            any("%7Bstep-1%7D" in url and "test_reports" in url for url in transport.urls()),
            "the test-report probe must use the step uuid captured from /steps/",
        )


# ---------------------------------------------------------------------------
# AIO TCMS poller (CONTRACT.md §3 event 22)
# ---------------------------------------------------------------------------

AIO_BASE = "https://tcms.aiojiraapps.com"


def aio_client(transport, **kwargs):
    kwargs.setdefault("sleep", lambda _seconds: None)
    return HttpClient(auth_header="AioAuth k", transport=transport, **kwargs)


def aio_case(case_key, status_name, executed_by="acct-qa", run_id=1,
             updated=1787133074301, automated=False, defects=0,
             assigned_to="acct-qa", folder="E2E Awareness", priority="High"):
    latest = {
        "ID": run_id,
        "testRunStatus": {"ID": 3, "name": status_name} if status_name else None,
        "effort": None,
        "isAutomated": automated,
        "executedByID": executed_by,
        "updatedDate": updated,
        "jiraDefectIDs": list(range(defects)),
    }
    return {
        "ID": 900 + run_id,
        "assignedToID": assigned_to,
        "testCase": {
            "ID": 100 + run_id,
            "key": case_key,
            "folder": {"ID": 1, "name": folder},
            "priority": {"ID": 2, "name": priority},
        },
        "latestRun": latest,
    }


def aio_routes(cycles, cases_by_cycle):
    routes = []
    for cycle_key, cases in cases_by_cycle.items():
        routes.append(
            (lambda u, k=cycle_key: f"/testcycle/{k}/testcase" in u,
             jsonr({"items": cases, "startAt": 0, "maxResults": 100, "isLast": True})))
    routes.append((lambda u: "/testcycle" in u,
                   jsonr({"items": cycles, "startAt": 0, "maxResults": 100,
                          "isLast": True})))
    return routes


class TestAioHelpers(unittest.TestCase):
    def test_epoch_unit_is_inferred_not_assumed(self):
        # AIO mixes seconds (cycles) and milliseconds (runs) in the same API.
        # Reading milliseconds as seconds yields the year 57490, which formats
        # and sorts perfectly well and is wrong.
        self.assertTrue(poll_aio.epoch_to_rfc3339(1787133074301).startswith("2026-"))
        self.assertTrue(poll_aio.epoch_to_rfc3339(1787133074).startswith("2026-"))
        self.assertIsNone(poll_aio.epoch_to_rfc3339(None))
        self.assertIsNone(poll_aio.epoch_to_rfc3339(0))
        self.assertIsNone(poll_aio.epoch_to_rfc3339("not a number"))

    def test_status_category_maps_and_falls_back_to_other(self):
        self.assertEqual(poll_aio.status_category("Passed"), "passed")
        self.assertEqual(poll_aio.status_category("FAILED"), "failed")
        self.assertEqual(poll_aio.status_category("Not Run"), "not_run")
        self.assertEqual(poll_aio.status_category(None), "not_run")
        # A custom status an administrator added must not be forced into
        # pass/fail; it lands in 'other' and is counted separately.
        self.assertEqual(poll_aio.status_category("Deferred to 26.9"), "other")

    def test_not_run_is_never_in_the_executed_set(self):
        self.assertNotIn("not_run", poll_aio.EXECUTED_CATEGORIES)
        self.assertNotIn("other", poll_aio.EXECUTED_CATEGORIES)

    def test_cycle_window_keeps_a_long_running_cycle(self):
        since = "2026-08-01T00:00:00Z"
        old_created_still_active = {"createdDate": 1740000000,
                                    "updatedDate": 1787133074301}
        self.assertTrue(poll_aio.cycle_in_window(old_created_still_active, since))
        stale = {"createdDate": 1740000000, "updatedDate": 1740000000}
        self.assertFalse(poll_aio.cycle_in_window(stale, since))
        # No usable timestamp: keep it rather than silently dropping work.
        self.assertTrue(poll_aio.cycle_in_window({}, since))
        self.assertTrue(poll_aio.cycle_in_window(stale, None))

    def test_effort_stays_null_rather_than_becoming_zero(self):
        self.assertIsNone(poll_aio.effort_seconds({"effort": None}))
        self.assertIsNone(poll_aio.effort_seconds({}))
        self.assertEqual(poll_aio.effort_seconds({"effort": 90}), 90)


class TestAioEvents(unittest.TestCase):
    def poller(self, transport):
        return poll_aio.AioPoller(aio_client(transport), AIO_BASE, "PRJ")

    def test_event_conforms_to_the_contract(self):
        p = self.poller(FakeTransport([]))
        event = p.build_event({"key": "PRJ-CY-1", "updatedDate": 1787133000000},
                              aio_case("PRJ-TC-1", "Passed", defects=2))
        self.assertEqual(event["event_type"], "test.run.completed")
        self.assertEqual(event["attributes"]["status_category"], "passed")
        self.assertEqual(event["attributes"]["defect_count"], 2)
        self.assertEqual(event["actor"]["person_id"], "acct-qa")
        # build_event() runs validate_event(), so reaching here proves the
        # attribute set is inside the §3 allow-list.
        self.assertTrue(event["event_id"].startswith("evt_"))

    def test_titles_are_never_carried(self):
        p = self.poller(FakeTransport([]))
        entry = aio_case("PRJ-TC-1", "Passed")
        entry["testCase"]["title"] = "Verify ACME Corp device 8613 on 10.0.0.1"
        event = p.build_event({"key": "PRJ-CY-1"}, entry)
        blob = json.dumps(event)
        self.assertNotIn("ACME", blob)
        self.assertNotIn("title", event["attributes"])

    def test_never_executed_row_has_no_execution_time_or_executor(self):
        p = self.poller(FakeTransport([]))
        entry = aio_case("PRJ-TC-2", "Not Run", executed_by="acct-someone")
        event = p.build_event({"key": "PRJ-CY-1", "updatedDate": 1787133000000}, entry)
        attrs = event["attributes"]
        self.assertEqual(attrs["status_category"], "not_run")
        self.assertIsNone(attrs["executed_at"])
        # AIO leaves a stale executedByID on seeded rows; trusting it would
        # credit an execution that never happened.
        self.assertIsNone(attrs["executed_by_person_id"])
        # It still lands in a defensible week rather than at the epoch.
        self.assertTrue(event["event_time"].startswith("2026-"))

    def test_event_id_is_stable_across_repolls(self):
        p = self.poller(FakeTransport([]))
        cycle = {"key": "PRJ-CY-1", "updatedDate": 1787133000000}
        first = p.build_event(cycle, aio_case("PRJ-TC-1", "Passed"))
        again = p.build_event(cycle, aio_case("PRJ-TC-1", "Passed"))
        self.assertEqual(first["event_id"], again["event_id"])
        # A re-run of the same case is a different fact and a different row.
        rerun = p.build_event(cycle, aio_case("PRJ-TC-1", "Failed", run_id=2,
                                              updated=1787200000000))
        self.assertNotEqual(first["event_id"], rerun["event_id"])

    def test_case_without_a_key_is_skipped_not_crashed(self):
        p = self.poller(FakeTransport([]))
        entry = aio_case("PRJ-TC-1", "Passed")
        entry["testCase"].pop("key")
        self.assertIsNone(p.build_event({"key": "PRJ-CY-1"}, entry))


class TestAioPollerRun(unittest.TestCase):
    def _run(self, argv, transport):
        original = poll_aio.HttpClient
        poll_aio.HttpClient = lambda **kw: aio_client(transport)
        try:
            return poll_aio.main(argv)
        finally:
            poll_aio.HttpClient = original

    def test_not_run_rows_are_excluded_by_default(self):
        cycles = [{"key": "PRJ-CY-1", "createdDate": 1787000000,
                   "updatedDate": 1787133074301}]
        cases = [aio_case("PRJ-TC-1", "Passed"),
                 aio_case("PRJ-TC-2", "Not Run", run_id=2),
                 aio_case("PRJ-TC-3", "Failed", run_id=3)]
        transport = FakeTransport(aio_routes(cycles, {"PRJ-CY-1": cases}))
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.ndjson")
            os.environ["AIO_API_TOKEN"] = "k"
            rc = self._run(["--project", "PRJ", "--no-watermark", "--out", out,
                            "--state-file", os.path.join(tmp, "s.json")], transport)
            self.assertEqual(rc, 0)
            rows = [json.loads(line) for line in open(out)]
        keys = {r["attributes"]["test_case_key"] for r in rows}
        self.assertEqual(keys, {"PRJ-TC-1", "PRJ-TC-3"})

    def test_include_not_run_opts_them_back_in(self):
        cycles = [{"key": "PRJ-CY-1", "createdDate": 1787000000,
                   "updatedDate": 1787133074301}]
        cases = [aio_case("PRJ-TC-2", "Not Run")]
        transport = FakeTransport(aio_routes(cycles, {"PRJ-CY-1": cases}))
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.ndjson")
            os.environ["AIO_API_TOKEN"] = "k"
            self._run(["--project", "PRJ", "--no-watermark", "--include-not-run",
                       "--out", out, "--state-file", os.path.join(tmp, "s.json")],
                      transport)
            rows = [json.loads(line) for line in open(out)]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attributes"]["status_category"], "not_run")

    def test_one_paged_call_per_cycle_not_one_per_test_case(self):
        cycles = [{"key": "PRJ-CY-1", "createdDate": 1787000000,
                   "updatedDate": 1787133074301}]
        cases = [aio_case(f"PRJ-TC-{i}", "Passed", run_id=i) for i in range(1, 25)]
        transport = FakeTransport(aio_routes(cycles, {"PRJ-CY-1": cases}))
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["AIO_API_TOKEN"] = "k"
            self._run(["--project", "PRJ", "--no-watermark",
                       "--out", os.path.join(tmp, "o.ndjson"),
                       "--state-file", os.path.join(tmp, "s.json")], transport)
        # The list endpoint inlines latestRun, so 24 cases must not cost 24 calls.
        self.assertEqual(transport.count_matching("/testcase"), 1)

    def test_auth_header_is_aioauth_never_basic(self):
        transport = FakeTransport([(lambda u: True, jsonr({"items": [], "isLast": True}))])
        client = aio_client(transport)
        self.assertEqual(client._headers["Authorization"], "AioAuth k")

    def test_auth_and_auth_header_are_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            HttpClient(auth=("u", "p"), auth_header="AioAuth k")


# ---------------------------------------------------------------------------
# Automation Output -- diffstat path classification (metric 1)
# ---------------------------------------------------------------------------


def diffentry(path, status="added", added=10, removed=0, deleted=False):
    entry = {"status": status, "lines_added": added, "lines_removed": removed}
    if deleted:
        entry["old"] = {"path": path}
        entry["new"] = None
    else:
        entry["new"] = {"path": path}
        entry["old"] = None
    return entry


class TestPathClassification(unittest.TestCase):
    def test_real_repository_paths(self):
        # Taken from acme/qa-automation PR 532.
        cases = {
            "features/unifiedPortal/API.feature": "feature",
            "features/step-definitions/enforcementRule.steps.ts": "step_definition",
            "tests/login.spec.ts": "spec",
            "e2e/checkout.test.ts": "spec",
            "test_api.py": "spec",
            "src/pages/LoginPage.ts": "page_object",
            "fixtures/users.json": "fixture",
            "tests/data/payload.json": "fixture",
            "src/utils/helper.ts": "other",
            "README.md": "other",
        }
        for path, expected in cases.items():
            self.assertEqual(poll_bitbucket.classify_path(path), expected, path)

    def test_config_files_are_not_fixtures(self):
        # The first rule matched every .json/.yaml in the tree, which classified
        # package.json and every CI workflow as test data.
        for path in ("package.json", "tsconfig.json", ".github/workflows/ci.yml",
                     "playwright.config.ts", "docker-compose.yaml"):
            self.assertEqual(poll_bitbucket.classify_path(path), "other", path)

    def test_step_definition_beats_generic_typescript(self):
        self.assertEqual(
            poll_bitbucket.classify_path("features/step-definitions/login.steps.ts"),
            "step_definition")

    def test_missing_path_is_other_not_a_crash(self):
        self.assertEqual(poll_bitbucket.classify_path(None), "other")
        self.assertEqual(poll_bitbucket.classify_path(""), "other")

    def test_deleted_file_classifies_by_its_old_path(self):
        entry = diffentry("features/Old.feature", status="removed", deleted=True)
        self.assertEqual(poll_bitbucket.diffstat_path(entry), "features/Old.feature")


class TestDiffstatSummary(unittest.TestCase):
    def test_scripts_are_counted_apart_from_scaffolding(self):
        summary = poll_bitbucket.summarise_diffstat([
            diffentry("features/A.feature"),
            diffentry("features/step-definitions/a.steps.ts"),
            diffentry("src/pages/APage.ts"),        # scaffolding, not a script
            diffentry("fixtures/data.json"),        # scaffolding, not a script
            diffentry("README.md"),
        ])
        self.assertEqual(summary["automation_scripts_added"], 2)
        self.assertEqual(summary["files_added"], 5)
        self.assertEqual(summary["automation_files_by_kind"]["page_object"]["added"], 1)

    def test_modified_and_removed_are_separated(self):
        summary = poll_bitbucket.summarise_diffstat([
            diffentry("features/A.feature", status="added"),
            diffentry("features/B.feature", status="modified"),
            diffentry("features/C.feature", status="removed", deleted=True),
        ])
        self.assertEqual(summary["automation_scripts_added"], 1)
        self.assertEqual(summary["automation_scripts_modified"], 1)
        self.assertEqual(summary["automation_scripts_removed"], 1)

    def test_unknown_bitbucket_status_folds_into_modified_not_dropped(self):
        summary = poll_bitbucket.summarise_diffstat([
            diffentry("features/A.feature", status="merge conflict"),
        ])
        self.assertEqual(summary["files_modified"], 1)
        self.assertEqual(summary["files_changed"], 1)

    def test_paths_never_reach_the_event(self):
        # Classification happens here; the path list must not be carried on.
        summary = poll_bitbucket.summarise_diffstat([
            diffentry("features/CustomerAcme_10.0.0.1.feature")])
        self.assertNotIn("Acme", json.dumps(summary))
        self.assertNotIn("10.0.0.1", json.dumps(summary))

    def test_empty_diffstat_is_all_zero_not_a_crash(self):
        summary = poll_bitbucket.summarise_diffstat([])
        self.assertEqual(summary["files_changed"], 0)
        self.assertEqual(summary["automation_scripts_added"], 0)


# ---------------------------------------------------------------------------
# Automation Coverage -- AIO test case inventory (metric 2)
# ---------------------------------------------------------------------------


def aio_testcase(key, automation_status="Automated", owner="acct-qa",
                 automation_key=None, archived=False, updated=1787133074301):
    return {
        "ID": 1,
        "key": key,
        "ownedByID": owner,
        "automationStatus": automation_status,
        "automationOwnerID": None,
        "automationKey": automation_key,
        "status": {"ID": 3, "name": "Published"},
        "scriptType": {"ID": 1, "name": "Classic"},
        "folder": {"ID": 1, "name": "Regression"},
        "priority": {"ID": 2, "name": "High"},
        "isArchived": archived,
        "createdDate": 1757589554646,
        "updatedDate": updated,
        "title": "Verify ACME Corp device on 10.0.0.1",
    }


class TestAioCoverage(unittest.TestCase):
    def poller(self):
        return poll_aio.AioPoller(aio_client(FakeTransport([])), AIO_BASE, "PRJ")

    def test_snapshot_conforms_to_the_contract(self):
        event = self.poller().build_case_event(aio_testcase("PRJ-TC-1"))
        self.assertEqual(event["event_type"], "test.case.snapshot")
        self.assertEqual(event["attributes"]["automation_status"], "Automated")
        self.assertEqual(event["attributes"]["folder_name"], "Regression")

    def test_unset_status_is_null_not_a_guess(self):
        # Roughly half this estate has never had the field set. Defaulting it to
        # "Not Automated" would report a coverage figure nobody measured.
        for empty in (None, ""):
            event = self.poller().build_case_event(
                aio_testcase("PRJ-TC-2", automation_status=empty))
            self.assertIsNone(event["attributes"]["automation_status"])

    def test_status_object_form_is_unwrapped(self):
        event = self.poller().build_case_event(
            aio_testcase("PRJ-TC-3", automation_status={"ID": 2, "name": "Automated"}))
        self.assertEqual(event["attributes"]["automation_status"], "Automated")

    def test_titles_are_never_carried(self):
        event = self.poller().build_case_event(aio_testcase("PRJ-TC-4"))
        blob = json.dumps(event)
        self.assertNotIn("ACME", blob)
        self.assertNotIn("10.0.0.1", blob)

    def test_snapshot_id_changes_when_the_case_is_edited(self):
        first = self.poller().build_case_event(aio_testcase("PRJ-TC-5"))
        edited = self.poller().build_case_event(
            aio_testcase("PRJ-TC-5", updated=1787200000000))
        self.assertNotEqual(first["event_id"], edited["event_id"])
        again = self.poller().build_case_event(aio_testcase("PRJ-TC-5"))
        self.assertEqual(first["event_id"], again["event_id"])

    def test_case_without_a_key_is_skipped(self):
        case = aio_testcase("PRJ-TC-6")
        case.pop("key")
        self.assertIsNone(self.poller().build_case_event(case))

    def test_coverage_run_divides_by_known_status_only(self):
        # --coverage-scope project is explicit: the default is `cycles`, because
        # a coverage figure over the whole historical inventory answers a
        # question about the archive rather than about the work being reported.
        cases = ([aio_testcase(f"PRJ-TC-a{i}", "Automated") for i in range(3)] +
                 [aio_testcase(f"PRJ-TC-b{i}", "To Be Automated") for i in range(1)] +
                 [aio_testcase(f"PRJ-TC-c{i}", None) for i in range(6)])
        transport = FakeTransport([(lambda u: "/testcase" in u,
                                    jsonr({"items": cases, "isLast": True}))])
        original = poll_aio.HttpClient
        poll_aio.HttpClient = lambda **kw: aio_client(transport)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = os.path.join(tmp, "cov.ndjson")
                os.environ["AIO_API_TOKEN"] = "k"
                rc = poll_aio.main(["--project", "PRJ", "--coverage",
                                    "--coverage-scope", "project", "--out", out,
                                    "--state-file", os.path.join(tmp, "s.json")])
                self.assertEqual(rc, 0)
                rows = [json.loads(line) for line in open(out)]
        finally:
            poll_aio.HttpClient = original
        # All ten are emitted -- the aggregator decides the denominator, not the
        # poller -- but only four carry a status at all.
        self.assertEqual(len(rows), 10)
        known = [r for r in rows if r["attributes"]["automation_status"]]
        self.assertEqual(len(known), 4)

    def test_error_hint_is_status_specific(self):
        self.assertIn("rate-limited", poll_aio.error_hint(429))
        self.assertIn("--probe", poll_aio.error_hint(401))
        self.assertIn("--project", poll_aio.error_hint(404))
        # 429 must not tell the reader to go and check their token.
        self.assertNotIn("AIO_API_TOKEN", poll_aio.error_hint(429))


class TestAioReleaseAndPriorityScope(unittest.TestCase):
    """Coverage is only answerable once it is scoped. Two scopes matter here:
    the priorities the team commits to, and the release under test."""

    def cycle(self, key, title, release_id=None):
        return {"key": key, "title": title, "jiraReleaseID": release_id,
                "jiraReleaseIDs": [release_id] if release_id else [],
                "createdDate": 1787000000, "updatedDate": 1787133074301}

    def test_release_id_match_is_reported_separately_from_title_match(self):
        # The id is a field an administrator set; the title is free text someone
        # typed. A number that depends on spelling has to say so.
        by_id = self.cycle("CY-1", "26.8 regression", 28771)
        by_title = self.cycle("CY-2", "26.8 Dev Integration Testing")
        neither = self.cycle("CY-3", "26.7 regression", 28770)
        self.assertEqual(
            poll_aio.cycle_release_match(by_id, [28771], ["26.8"]), "release_id")
        self.assertEqual(
            poll_aio.cycle_release_match(by_title, [28771], ["26.8"]), "title")
        self.assertIsNone(
            poll_aio.cycle_release_match(neither, [28771], ["26.8"]))

    def test_no_release_requested_matches_nothing(self):
        self.assertIsNone(
            poll_aio.cycle_release_match(self.cycle("CY-1", "26.8", 28771), [], []))

    def test_title_match_is_case_insensitive_and_substring(self):
        cycle = self.cycle("CY-1", "Regression for RELEASE 26.8 on PRE")
        self.assertEqual(poll_aio.cycle_release_match(cycle, [], ["26.8"]), "title")

    def test_release_scope_ignores_the_time_window(self):
        # A release's own evidence can predate the reporting window; intersecting
        # the two would silently drop it.
        cycles = [self.cycle("CY-old", "26.8 regression", 28771)]
        cycles[0]["updatedDate"] = 1700000000      # long before any --since
        cases = [aio_case("PRJ-TC-1", "Passed")]
        transport = FakeTransport(aio_routes(cycles, {"CY-old": cases}))
        poller = poll_aio.AioPoller(aio_client(transport), AIO_BASE, "PRJ")
        keys, matched = poller.cycle_case_keys(
            "2026-08-01T00:00:00Z", 0, [28771], ["26.8"])
        self.assertEqual(matched, {"CY-old": "release_id"})
        self.assertEqual(keys, {"PRJ-TC-1"})

    def test_window_scope_still_applies_when_no_release_is_given(self):
        cycles = [self.cycle("CY-old", "old cycle")]
        cycles[0]["createdDate"] = 1700000000
        cycles[0]["updatedDate"] = 1700000000
        transport = FakeTransport(aio_routes(cycles, {"CY-old": []}))
        poller = poll_aio.AioPoller(aio_client(transport), AIO_BASE, "PRJ")
        keys, matched = poller.cycle_case_keys("2026-08-01T00:00:00Z", 0, [], [])
        self.assertEqual(matched, {})
        self.assertEqual(keys, set())

    def test_priority_filter_keeps_only_the_named_priorities(self):
        cycles = [self.cycle("CY-1", "26.8 regression", 28771)]
        cases = [aio_case("PRJ-TC-1", "Passed"), aio_case("PRJ-TC-2", "Passed",
                                                          run_id=2)]
        inventory = [
            aio_testcase("PRJ-TC-1"), aio_testcase("PRJ-TC-2"),
            aio_testcase("PRJ-TC-3"),          # out of scope: not in the cycle
        ]
        inventory[0]["priority"] = {"ID": 2, "name": "High"}      # P1
        inventory[1]["priority"] = {"ID": 4, "name": "Low"}       # P3
        routes = aio_routes(cycles, {"CY-1": cases})
        routes.insert(0, (lambda u: "/project/PRJ/testcase?" in u,
                          jsonr({"items": inventory, "isLast": True})))
        transport = FakeTransport(routes)
        original = poll_aio.HttpClient
        poll_aio.HttpClient = lambda **kw: aio_client(transport)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = os.path.join(tmp, "cov.ndjson")
                os.environ["AIO_API_TOKEN"] = "k"
                rc = poll_aio.main([
                    "--project", "PRJ", "--coverage", "--no-watermark",
                    "--release-id", "28771", "--priority", "High",
                    "--out", out, "--state-file", os.path.join(tmp, "s.json")])
                self.assertEqual(rc, 0)
                rows = [json.loads(line) for line in open(out)]
        finally:
            poll_aio.HttpClient = original
        # TC-2 is in the cycle but is P3; TC-3 is P1 but not in the release.
        self.assertEqual([r["attributes"]["test_case_key"] for r in rows],
                         ["PRJ-TC-1"])

    def test_priority_matching_is_case_insensitive(self):
        case = {"priority": {"ID": 2, "name": "High"}}
        self.assertEqual(poll_aio.priority_name(case), "High")


if __name__ == "__main__":
    unittest.main(verbosity=2)
