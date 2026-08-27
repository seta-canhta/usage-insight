#!/usr/bin/env python3
"""Tests for the VS Code Copilot Chat reader.

The thing under test is a reader pointed at a directory full of prompts,
responses and source code, whose job is to emit none of it. Most of what
follows is about that.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CLI = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_CLI)
for _p in (_CLI, _ROOT, os.path.join(_ROOT, "collector")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import vscode_read  # noqa: E402
import main as collector  # noqa: E402


SECRET = "sk-live-DO-NOT-SHIP-0123456789abcdef"


def a_request(index=0, model="copilot/claude-sonnet-4.5", tools=("read_file",),
              elapsed=1200):
    """A request shaped like the real thing -- content and all."""
    return {
        "requestId": "request_{}".format(index),
        "responseId": "response_{}".format(index),
        "modelId": model,
        "timestamp": 1768906912816 + index * 1000,
        "timeSpentWaiting": 0,
        # Every one of these is content and must never leave.
        "message": {"text": "refactor this and here is my key " + SECRET,
                    "parts": [{"text": SECRET}]},
        "response": [{"value": "here is the code\n" + SECRET}],
        "variableData": {"variables": [{"name": "/Users/someone/secret.py"}]},
        "result": {
            "timings": {"firstProgress": 300, "totalElapsed": elapsed},
            "metadata": {
                "agentId": "github.copilot.editsAgent",
                "responseId": "response_{}".format(index),
                "sessionId": "sess",
                "modelMessageId": "m",
                "codeBlocks": [{"code": SECRET}],
                "renderedUserMessage": [{"text": SECRET}],
                "toolCallRounds": [{
                    "id": "round-0",
                    "response": SECRET,
                    "toolCalls": [
                        {"name": name,
                         "arguments": {"filePath": "/Users/someone/x.py",
                                       "content": SECRET}}
                        for name in tools
                    ],
                }],
            },
        },
    }


def jsonl_workspace_lines(requests, patches=()):
    """The append-only format: a header, then records that add and patch."""
    lines = [json.dumps({"kind": 0, "v": {"version": 3, "sessionId": "s1",
                                          "requests": []}})]
    lines.append(json.dumps({"kind": 2, "k": 1, "v": requests}))
    for patch in patches:
        lines.append(json.dumps({"kind": 2, "k": 2, "v": [patch]}))
    return "\n".join(lines) + "\n"


class ReaderTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vscode-read-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def workspace(self, name="ws1", folder="/nowhere/repo", requests=None,
                  write_workspace_json=True):
        storage = os.path.join(self.root, "workspaceStorage", name)
        os.makedirs(os.path.join(storage, "chatSessions"), exist_ok=True)
        if write_workspace_json:
            with open(os.path.join(storage, "workspace.json"), "w",
                      encoding="utf-8") as handle:
                json.dump({"folder": "file://" + folder}, handle)
        with open(os.path.join(storage, "chatSessions", "s1.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"sessionId": "s1", "requests": requests or []}, handle)
        return storage

    def jsonl(self, name, body):
        storage = os.path.join(self.root, "workspaceStorage", name)
        os.makedirs(os.path.join(storage, "chatSessions"), exist_ok=True)
        with open(os.path.join(storage, "workspace.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"folder": "file:///nowhere/repo"}, handle)
        with open(os.path.join(storage, "chatSessions", "s1.jsonl"), "w",
                  encoding="utf-8") as handle:
            handle.write(body)
        return storage

    def read(self, **kw):
        return vscode_read.to_events(self.root, **kw)

    def test_an_unconfigured_reader_does_not_turn_a_date_into_a_ticket(self):
        """`fix/AUG-25` is August 25, not ticket AUG-25. AR-1.

        Measured 2026-08-26 on a laptop enrolled that morning: all 28 events
        from a session on branch `fix/AUG-25` carried
        `jira_issue_key="AUG-25"`. `insight setup` never sets `jira_projects`,
        so the reader ran with no allow-list and accepted anything key-shaped.
        """
        events = vscode_read.session_events(
            self._one_session(), "s1", "acme/repo", "fix/AUG-25")
        self.assertTrue(events)
        for event in events:
            self.assertIsNone(event["context"]["jira_issue_key"])
            self.assertEqual(event["context"]["branch_name"], "fix/AUG-25")

    def test_a_configured_reader_keeps_a_real_key(self):
        events = vscode_read.session_events(
            self._one_session(), "s1", "acme/repo", "IML-6500/release/26.8",
            jira_projects=("IML", "APR", "AERLABS"))
        self.assertTrue(events)
        self.assertEqual(events[0]["context"]["jira_issue_key"], "IML-6500")

    def _one_session(self):
        storage = self.workspace(name="wsk", requests=[a_request(0)])
        return os.path.join(storage, "chatSessions", "s1.json")


# ---------------------------------------------------------------------------
# 1. Content. The whole reason the module is written the way it is.
# ---------------------------------------------------------------------------

class TestNothingLeaks(ReaderTestCase):
    def setUp(self):
        super().setUp()
        self.workspace(requests=[a_request(0), a_request(1)])
        self.result = self.read()
        self.blob = json.dumps(self.result["events"])

    def test_the_prompt_never_appears(self):
        self.assertNotIn(SECRET, self.blob)

    def test_no_absolute_path_appears(self):
        # Tool arguments carry them, and tool arguments are never read.
        self.assertNotIn("/Users/", self.blob)
        self.assertNotIn("/nowhere/repo", self.blob)

    def test_the_guard_agrees(self):
        self.assertEqual(vscode_read.verify_no_content(self.result["events"]), [])

    def test_an_unnamed_field_is_not_carried_even_if_harmless(self):
        # The allow-list is the whole surface: a new key appearing in a future
        # VS Code build must be dropped without anyone editing this module.
        payload = a_request(0)
        payload["someFutureField"] = "harmless today"
        self.workspace(name="ws2", requests=[payload])
        blob = json.dumps(self.read()["events"])
        self.assertNotIn("harmless today", blob)
        self.assertNotIn("someFutureField", blob)


# ---------------------------------------------------------------------------
# 2. Usage is absent, and says so.
# ---------------------------------------------------------------------------

class TestUsageIsNullNotZero(ReaderTestCase):
    def setUp(self):
        super().setUp()
        self.workspace(requests=[a_request(0)])
        self.result = self.read()
        self.calls = [e for e in self.result["events"]
                      if e["event_type"] == "model.call"]

    def test_token_fields_are_null(self):
        # CONTRACT.md §1: an unmeasured quantity and a measured zero must never
        # look the same. This fixture is the older `.json` format, which
        # records no token count -- so zero would claim a request that cost
        # nothing. The `.jsonl` format does carry them; see
        # TestTheAppendOnlyFormat.
        attrs = self.calls[0]["attributes"]
        for field in ("input_tokens", "output_tokens", "cached_input_tokens",
                      "premium_requests"):
            self.assertIsNone(attrs[field], field)

    def test_coverage_reports_that_no_tokens_were_seen(self):
        cov = self.result["coverage"]
        self.assertFalse(cov["usage_available"])
        self.assertEqual(cov["requests_with_tokens"], 0)
        # The cache split and premium requests are absent on this surface in
        # BOTH formats, and stay listed as such.
        self.assertIn("premium_requests", cov["unavailable_fields"])


# ---------------------------------------------------------------------------
# 3. What this surface uniquely provides.
# ---------------------------------------------------------------------------

class TestWhatItDoesCarry(ReaderTestCase):
    def test_latency_is_real_and_per_call(self):
        # The CLI journal totals usage at shutdown and carries no per-call
        # latency. This surface does, and it is the reason to read it.
        self.workspace(requests=[a_request(0, elapsed=20095)])
        call = [e for e in self.read()["events"]
                if e["event_type"] == "model.call"][0]
        self.assertEqual(call["attributes"]["latency_ms"], 20095)

    def test_the_vendor_route_is_stripped_from_the_model_id(self):
        # `copilot/claude-sonnet-4.5` and the journal's `claude-sonnet-4.5` are
        # the same model; left prefixed it would split every by-model figure.
        self.workspace(requests=[a_request(0)])
        call = [e for e in self.read()["events"]
                if e["event_type"] == "model.call"][0]
        self.assertEqual(call["attributes"]["model_id"], "claude-sonnet-4.5")

    def test_tool_names_and_kinds(self):
        self.workspace(requests=[a_request(
            0, tools=("read_file", "create_file", "run_in_terminal"))])
        tools = [e["attributes"] for e in self.read()["events"]
                 if e["event_type"] == "tool.call"]
        self.assertEqual([t["tool_name"] for t in tools],
                         ["read_file", "create_file", "run_in_terminal"])
        self.assertEqual([t["tool_kind"] for t in tools],
                         ["read", "edit", "execute"])

    def test_an_unknown_tool_is_other_not_guessed(self):
        # Mapping is exact-match against a closed set. Substring matching would
        # put `manage_todo_list` in `read` for containing "list".
        self.workspace(requests=[a_request(0, tools=("brand_new_tool",))])
        tool = [e["attributes"] for e in self.read()["events"]
                if e["event_type"] == "tool.call"][0]
        self.assertEqual(tool["tool_kind"], "other")

    def test_tool_status_is_null_not_assumed_ok(self):
        # The store records no per-tool outcome. "ok" would be an assumption
        # that every tool succeeded.
        self.workspace(requests=[a_request(0)])
        tool = [e["attributes"] for e in self.read()["events"]
                if e["event_type"] == "tool.call"][0]
        self.assertIsNone(tool["status"])


# ---------------------------------------------------------------------------
# 4. Shape, and the collector.
# ---------------------------------------------------------------------------

class TestTheEventsAreContractLegal(ReaderTestCase):
    def test_every_event_survives_the_collector(self):
        self.workspace(requests=[a_request(0), a_request(1)])
        for event in self.read()["events"]:
            collector.normalise_event(event)   # raises on rejection

    def test_run_id_is_null_because_chat_has_no_runs(self):
        # Inventing a run to fill the column would manufacture a join key.
        self.workspace(requests=[a_request(0)])
        for event in self.read()["events"]:
            self.assertIn("run_id", event)
            self.assertIsNone(event["run_id"])

    def test_re_reading_produces_identical_event_ids(self):
        self.workspace(requests=[a_request(0), a_request(1)])
        first = [e["event_id"] for e in self.read()["events"]]
        second = [e["event_id"] for e in self.read()["events"]]
        self.assertEqual(first, second)
        self.assertEqual(len(set(first)), len(first), "ids collide")

    def test_the_surface_is_named_distinctly(self):
        self.workspace(requests=[a_request(0)])
        event = self.read()["events"][0]
        self.assertEqual(event["agent"]["surface"], "vscode-copilot-chat")


# ---------------------------------------------------------------------------
# 5. Absence, reported rather than crashed on.
# ---------------------------------------------------------------------------

class TestAbsence(ReaderTestCase):
    def test_a_machine_with_no_vs_code_is_reported_not_an_error(self):
        result = vscode_read.to_events(os.path.join(self.root, "nope"))
        self.assertFalse(result["present"])
        self.assertEqual(result["events"], [])

    def test_an_empty_session_contributes_nothing_but_is_counted(self):
        self.workspace(requests=[])
        result = self.read()
        self.assertEqual(result["events"], [])
        self.assertEqual(result["coverage"]["sessions_seen"], 1)
        self.assertEqual(result["coverage"]["sessions_empty"], 1)

    def test_a_deleted_workspace_folder_leaves_the_repo_null(self):
        # Measured 2026-08-26: 24 of 27 folders were already gone. The reader
        # reports what it cannot resolve rather than guessing a repository.
        self.workspace(folder="/definitely/not/here", requests=[a_request(0)])
        event = self.read()["events"][0]
        self.assertIsNone(event["context"]["repo_full_name"])

    def test_a_multi_root_window_is_not_attributed_to_one_repo(self):
        storage = os.path.join(self.root, "workspaceStorage", "multi")
        os.makedirs(os.path.join(storage, "chatSessions"))
        with open(os.path.join(storage, "workspace.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"workspace": "file:///some/thing.code-workspace"}, handle)
        with open(os.path.join(storage, "chatSessions", "s1.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"requests": [a_request(0)]}, handle)
        self.assertIsNone(vscode_read.workspace_folder(storage))

    def test_a_half_written_session_does_not_lose_the_others(self):
        self.workspace(name="good", requests=[a_request(0)])
        bad = os.path.join(self.root, "workspaceStorage", "bad", "chatSessions")
        os.makedirs(bad)
        with open(os.path.join(bad, "s1.json"), "w", encoding="utf-8") as handle:
            handle.write('{"requests": [')     # truncated mid-write
        self.assertEqual(len(self.read()["coverage"]["sessions_seen"] * [0]), 2)
        self.assertTrue(self.read()["events"])


class TestTheAppendOnlyFormat(ReaderTestCase):
    """`.jsonl` is the current format, and reading only `.json` hid 98% of it.

    Measured 2026-08-26: the first version of this module saw 93 requests on a
    machine holding 5,036, and reported the newest activity as six months old
    on a machine in use that afternoon. A file-extension filter decided a
    finding.
    """

    def test_a_jsonl_session_is_read_at_all(self):
        self.jsonl("ws", jsonl_workspace_lines([a_request(0)]))
        self.assertEqual(
            len([e for e in self.read()["events"]
                 if e["event_type"] == "model.call"]), 1)

    def test_a_later_record_patches_an_earlier_one(self):
        # A request's `result` -- and with it its tokens -- can arrive after
        # the request. Taking the first sighting would report them absent.
        bare = {"requestId": "request_0", "modelId": "copilot/gpt-5.3-codex",
                "timestamp": 1768906912816}
        patch = {"requestId": "request_0",
                 "result": {"timings": {"totalElapsed": 11957},
                            "metadata": {"promptTokens": 31991,
                                         "outputTokens": 170,
                                         "resolvedModel": "gpt-5.3-codex"}}}
        self.jsonl("ws", jsonl_workspace_lines([bare], patches=[patch]))
        call = [e for e in self.read()["events"]
                if e["event_type"] == "model.call"][0]
        self.assertEqual(call["attributes"]["input_tokens"], 31991)
        self.assertEqual(call["attributes"]["output_tokens"], 170)
        self.assertEqual(call["attributes"]["latency_ms"], 11957)

    def test_tokens_are_null_when_the_result_never_landed(self):
        bare = {"requestId": "request_0", "modelId": "copilot/gpt-5.3-codex",
                "timestamp": 1768906912816}
        self.jsonl("ws", jsonl_workspace_lines([bare]))
        attrs = [e["attributes"] for e in self.read()["events"]
                 if e["event_type"] == "model.call"][0]
        self.assertIsNone(attrs["input_tokens"])
        self.assertIsNone(attrs["output_tokens"])

    def test_a_truncated_final_line_does_not_lose_the_session(self):
        body = jsonl_workspace_lines([a_request(0)]) + '{"kind": 2, "v": [{"req'
        self.jsonl("ws", body)
        self.assertTrue(self.read()["events"])

    def test_one_model_is_not_split_by_two_spellings(self):
        # `resolvedModel` writes `claude-sonnet-4-6`; `modelId` writes
        # `claude-sonnet-4.6`. Measured: 12 rows under one and 7 under the
        # other, for one model.
        dotted = dict(a_request(0), modelId="copilot/claude-sonnet-4.6")
        dashed = {"requestId": "request_1", "timestamp": 1768906913816,
                  "modelId": "copilot/claude-sonnet-4.6",
                  "result": {"metadata": {"resolvedModel": "claude-sonnet-4-6"}}}
        self.jsonl("ws", jsonl_workspace_lines([dotted, dashed]))
        models = {e["attributes"]["model_id"] for e in self.read()["events"]
                  if e["event_type"] == "model.call"}
        self.assertEqual(models, {"claude-sonnet-4.6"})


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# 8. The prompt is matched, never kept.
#
# `branch` and a pull request are work records and are read whole. A prompt is
# not, so exactly one path is read and only a key that a real project claims
# escapes. These tests are as much about what must NOT happen.
# ---------------------------------------------------------------------------

class TestPromptDerivedKey(ReaderTestCase):
    PROJECTS = ("IML", "APR", "AERLABS")

    def _session(self, text, name="wsp"):
        request = a_request(0)
        request["message"] = {"text": text}
        storage = self.workspace(name=name, requests=[request])
        return os.path.join(storage, "chatSessions", "s1.json")

    def test_a_ticket_named_in_the_prompt_is_found(self):
        events = vscode_read.session_events(
            self._session("please fix IML-6500, the grid does not sort"),
            "s1", "acme/repo", "feature/26.8", jira_projects=self.PROJECTS)
        self.assertTrue(events)
        self.assertEqual(events[0]["context"]["jira_issue_key"], "IML-6500")

    def test_it_is_weaker_evidence_and_says_so(self):
        """0.5, not 0.9. Mentioning a ticket is not working on it."""
        events = vscode_read.session_events(
            self._session("look at IML-6500"), "s1", "acme/repo",
            "feature/26.8", jira_projects=self.PROJECTS)
        self.assertEqual(events[0]["link"]["confidence"], 0.5)
        self.assertEqual(events[0]["link"]["method"], "heuristic")

    def test_the_branch_wins_and_keeps_its_confidence(self):
        """A weaker signal may fill a null. It may never overwrite a value."""
        events = vscode_read.session_events(
            self._session("but first look at APR-1"), "s1", "acme/repo",
            "IML-6500-sorting", jira_projects=self.PROJECTS)
        self.assertEqual(events[0]["context"]["jira_issue_key"], "IML-6500")
        self.assertEqual(events[0]["link"]["confidence"], 0.9)

    def test_without_an_allow_list_nothing_is_read_at_all(self):
        """AR-1. The permissive path is what minted `AUG-25`; prose is worse."""
        self.assertIsNone(vscode_read.scan_for_key(
            {"message": {"text": "fix IML-6500"}}, ()))
        events = vscode_read.session_events(
            self._session("fix IML-6500"), "s1", "acme/repo", "feature/26.8")
        self.assertIsNone(events[0]["context"]["jira_issue_key"])

    def test_key_shaped_noise_from_another_system_is_refused(self):
        for text in ("ERR-500 on startup", "see CVE-2024 advisory",
                     "colleague filed TC-12018", "released in PY-311"):
            self.assertIsNone(
                vscode_read.scan_for_key({"message": {"text": text}},
                                         self.PROJECTS), text)

    def test_the_prompt_itself_never_reaches_an_event(self):
        secret = "IML-6500 and the password is hunter2"
        events = vscode_read.session_events(
            self._session(secret), "s1", "acme/repo", "feature/26.8",
            jira_projects=self.PROJECTS)
        blob = json.dumps(events)
        self.assertNotIn("hunter2", blob)
        self.assertNotIn("password", blob)
        self.assertIn("IML-6500", blob)

    def test_only_message_text_is_ever_read(self):
        """The response, the code blocks and tool arguments stay unread."""
        self.assertEqual(vscode_read.SCAN, ("message.text",))
        self.assertIsNone(vscode_read.scan_for_key(
            {"response": [{"value": "IML-6500"}],
             "metadata": {"codeBlocks": ["IML-6500"]}}, self.PROJECTS))

    def test_two_requests_naming_two_tickets_are_not_merged(self):
        """Context is per-event, so a session may span tickets honestly."""
        first, second = a_request(0), a_request(1)
        first["message"] = {"text": "start on IML-6500"}
        second["message"] = {"text": "now APR-42 instead"}
        storage = self.workspace(name="wstwo", requests=[first, second])
        events = vscode_read.session_events(
            os.path.join(storage, "chatSessions", "s1.json"), "s1",
            "acme/repo", "feature/26.8", jira_projects=self.PROJECTS)
        keys = [e["context"]["jira_issue_key"] for e in events]
        self.assertIn("IML-6500", keys)
        self.assertIn("APR-42", keys)


class TestRepoDiscovery(ReaderTestCase):
    """A VS Code-only machine reported `repos: 0` while holding 512 events."""

    def test_a_workspace_with_a_git_dir_is_discovered(self):
        repo = os.path.join(self.root, "tree")
        os.makedirs(os.path.join(repo, ".git"))
        self.workspace(name="wsg", folder=repo, requests=[a_request(0)])
        self.assertEqual(vscode_read.discover_repos(self.root), [repo])

    def test_a_folder_that_is_not_a_repository_is_not_reported(self):
        plain = os.path.join(self.root, "notes")
        os.makedirs(plain)
        self.workspace(name="wsn", folder=plain, requests=[a_request(0)])
        self.assertEqual(vscode_read.discover_repos(self.root), [])

    def test_a_deleted_folder_is_left_out_rather_than_reported(self):
        """Evidence perishes: 24 of 27 workspace folders were already gone."""
        self.workspace(name="wsd", folder=os.path.join(self.root, "gone"),
                       requests=[a_request(0)])
        self.assertEqual(vscode_read.discover_repos(self.root), [])


class TestBranchAtTheTimeOfTheSession(unittest.TestCase):
    """A branch read today is not evidence about a session three weeks old.

    `insight backfill --since 2026-08-01` walks weeks of chat sessions and, for
    each one, used to stamp whatever branch happened to be checked out on the
    morning of the backfill -- at `link.confidence` 0.9, which is the
    confidence of a measurement. The reflog is the record of the thing being
    asked about, so the question now goes there.
    """

    REFLOG = "\n".join([
        "ccc HEAD@{2026-08-20T15:00:00+07:00}: commit: work",
        "ccc HEAD@{2026-08-20T14:00:00+07:00}: checkout: moving from feature/b to main",
        "bbb HEAD@{2026-08-10T09:00:00+07:00}: checkout: moving from main to feature/b",
        "aaa HEAD@{2026-08-01T08:00:00+07:00}: commit: first",
    ]) + "\n"

    def _history(self, reflog=None, current="main"):
        text = self.REFLOG if reflog is None else reflog

        def run(*args):
            if args[0] == "reflog":
                return text
            if args[:2] == ("rev-parse", "--abbrev-ref"):
                return current + "\n"
            return None

        with tempfile.TemporaryDirectory() as tmp:
            return vscode_read.checkout_history(tmp, run=run)

    def test_a_session_is_attributed_to_the_branch_it_was_held_on(self):
        history = self._history()
        self.assertEqual(
            vscode_read.branch_at(history, "2026-08-15T10:00:00Z"), "feature/b")
        self.assertEqual(
            vscode_read.branch_at(history, "2026-08-21T10:00:00Z"), "main")

    def test_before_the_first_recorded_move_head_was_where_it_moved_from(self):
        self.assertEqual(
            vscode_read.branch_at(self._history(), "2026-08-05T10:00:00Z"),
            "main")

    def test_older_than_the_reflog_is_none_not_todays_branch(self):
        # The whole fix. git expires reflogs, and an expired range is not an
        # empty one -- this machine cannot speak about that session.
        self.assertIsNone(
            vscode_read.branch_at(self._history(), "2026-07-01T10:00:00Z"))

    def test_a_clone_that_never_switched_branch_still_answers(self):
        history = self._history(
            reflog="aaa HEAD@{2026-08-01T08:00:00+07:00}: clone: from x\n",
            current="release/2.0")
        self.assertEqual(
            vscode_read.branch_at(history, "2026-08-02T10:00:00Z"), "release/2.0")
        self.assertIsNone(
            vscode_read.branch_at(history, "2026-07-30T10:00:00Z"))

    def test_a_detached_head_is_not_reported_as_a_branch(self):
        history = self._history(
            reflog="aaa HEAD@{2026-08-01T08:00:00+07:00}: "
                   "checkout: moving from main to 4f2c9ab\n")
        self.assertIsNone(
            vscode_read.branch_at(history, "2026-08-02T10:00:00Z"))

    def test_no_reflog_means_no_history_rather_than_an_empty_one(self):
        self.assertIsNone(self._history(reflog=""))

    def test_a_session_spanning_a_checkout_is_split_at_the_checkout(self):
        # Two requests, either side of the 2026-08-20 move from feature/b to
        # main. `context` is per event in the schema, so representing this
        # honestly costs nothing.
        history = self._history()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"requests": [
                    {"requestId": "r1", "timestamp": 1786788000000,
                     "message": {"text": "before"}},
                    {"requestId": "r2", "timestamp": 1787306400000,
                     "message": {"text": "after"}},
                ]}, handle)
            events = vscode_read.session_events(
                path, "sess", "acme/watchtower", "main",
                jira_projects=("IML",), history=history)
        branches = [e["context"].get("branch_name") for e in events]
        self.assertIn("feature/b", branches)
        self.assertIn("main", branches)


class ToolPathCaseKeyTests(unittest.TestCase):
    """The path route: a tool call's file name naming an AIO case.

    Added 0.9.0. It is the only route from a chat session to an AIO case that
    needs nobody to type a key, and it reads an object that sits next to the
    file's own content -- so half of what follows is about the content staying
    unread.
    """

    PROJECTS = ("IML", "APR")

    def call(self, **arguments):
        return {"name": "replace_string_in_file", "arguments": arguments}

    def test_a_spec_file_named_after_a_case_yields_that_case(self):
        found = vscode_read.scan_tool_for_keys(
            self.call(filePath="tests/e2e/IML-TC-12893.spec.ts",
                      content=SECRET),
            self.PROJECTS)
        self.assertEqual(found["test_case_key"], "IML-TC-12893")

    def test_the_content_beside_the_path_is_never_read(self):
        """`content` sits in the same dict. Naming keys, not the dict, is why."""
        found = vscode_read.scan_tool_for_keys(
            self.call(filePath="src/util.ts",
                      content="see IML-TC-99999 in the docstring"),
            self.PROJECTS)
        self.assertIsNone(found["test_case_key"])

    def test_the_path_itself_never_comes_back(self):
        found = vscode_read.scan_tool_for_keys(
            self.call(filePath="/Users/someone/IML-TC-1.spec.ts"),
            self.PROJECTS)
        self.assertEqual(set(found), {"test_case_key", "test_cycle_key"})
        self.assertNotIn("/Users/someone", json.dumps(found))

    def test_no_allow_list_reads_nothing(self):
        """AR-1. The permissive path is what minted `AUG-25`."""
        self.assertEqual(
            vscode_read.scan_tool_for_keys(
                self.call(filePath="tests/IML-TC-12893.spec.ts"), ()),
            {"test_case_key": None, "test_cycle_key": None})

    def test_a_project_outside_the_allow_list_is_not_taken(self):
        self.assertIsNone(
            vscode_read.scan_tool_for_keys(
                self.call(filePath="tests/ZZZ-TC-1.spec.ts"),
                self.PROJECTS)["test_case_key"])

    def test_no_jira_key_is_ever_taken_from_a_path(self):
        """A directory named after a branch is exactly the `AUG-25` shape."""
        found = vscode_read.scan_tool_for_keys(
            self.call(filePath="fix/IML-6532/regression.ts"), self.PROJECTS)
        self.assertNotIn("jira_issue_key", found)

    def test_a_cycle_key_is_taken_too(self):
        self.assertEqual(
            vscode_read.scan_tool_for_keys(
                self.call(path="cycles/IML-CY-214/run.json"),
                self.PROJECTS)["test_cycle_key"],
            "IML-CY-214")

    def test_arguments_that_are_not_a_dict_are_survivable(self):
        self.assertEqual(
            vscode_read.scan_tool_for_keys(
                {"name": "x", "arguments": "IML-TC-5"}, self.PROJECTS),
            {"test_case_key": None, "test_cycle_key": None})


class KeyCaptureTests(unittest.TestCase):
    """The capture rate is printed because a zero nobody prints is a zero
    somebody rediscovers in a report six weeks later."""

    def event(self, confidence, **context):
        return {"context": context, "link": {"confidence": confidence}}

    def test_routes_are_counted_separately(self):
        out = vscode_read.key_capture([
            self.event(0.9, branch_name="fix/IML-1", jira_issue_key="IML-1"),
            self.event(0.7, branch_name="main", test_case_key="IML-TC-5"),
            self.event(0.5, jira_issue_key="IML-2"),
        ])
        self.assertEqual(out["by_route"],
                         {"branch": 1, "path": 1, "prompt": 1})
        self.assertEqual(out["with_branch_name"], 2)
        self.assertEqual(out["named_nothing"], 0)

    def test_a_branch_consulted_with_no_answer_is_not_a_branch_capture(self):
        """0.9 with no key means the branch was asked and had nothing."""
        out = vscode_read.key_capture([self.event(0.9, branch_name="main")])
        self.assertEqual(out["by_route"]["branch"], 0)
        self.assertEqual(out["named_nothing"], 1)

    def test_an_empty_run_reports_zeroes_rather_than_nothing(self):
        out = vscode_read.key_capture([])
        self.assertEqual(out["events"], 0)
        self.assertEqual(out["named_nothing"], 0)
        self.assertIn("by_route", out)


class ToolPathWiringTests(unittest.TestCase):
    """End to end: the path route reaching a real `tool.call` event."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vscode-path-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def session(self, file_path, branch):
        request = a_request()
        request["result"]["metadata"]["toolCallRounds"] = [{
            "id": "round-0",
            "toolCalls": [{"name": "replace_string_in_file",
                           "arguments": {"filePath": file_path,
                                         "content": SECRET}}],
        }]
        storage = os.path.join(self.root, "workspaceStorage", "ws1")
        os.makedirs(os.path.join(storage, "chatSessions"), exist_ok=True)
        with open(os.path.join(storage, "workspace.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"folder": "file:///nowhere/repo"}, handle)
        path = os.path.join(storage, "chatSessions", "s1.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(jsonl_workspace_lines([request]))
        return vscode_read.session_events(
            path, "s1", "acme/repo", branch,
            jira_projects=("IML", "APR"))

    def tool_events(self, events):
        return [e for e in events if e["event_type"] == "tool.call"]

    def test_the_case_reaches_the_tool_call_event_at_0_7(self):
        events = self.session("tests/e2e/IML-TC-12893.spec.ts", "main")
        tools = self.tool_events(events)
        self.assertTrue(tools)
        self.assertEqual(tools[0]["context"]["test_case_key"], "IML-TC-12893")
        self.assertEqual(tools[0]["link"]["confidence"], 0.7)
        self.assertEqual(tools[0]["link"]["method"], "heuristic")

    def test_it_does_not_leak_onto_the_prompt_or_the_model_call(self):
        """The tool call touched the file. The turn did not."""
        events = self.session("tests/e2e/IML-TC-12893.spec.ts", "main")
        for event in events:
            if event["event_type"] != "tool.call":
                self.assertIsNone(event["context"]["test_case_key"])

    def test_a_branch_that_already_named_a_case_is_not_overwritten(self):
        """Fills, never overwrites -- the branch is the better evidence."""
        events = self.session("tests/e2e/IML-TC-999.spec.ts",
                              "IML-TC-12893/rework")
        for event in self.tool_events(events):
            self.assertEqual(event["context"]["test_case_key"], "IML-TC-12893")
            self.assertEqual(event["link"]["confidence"], 0.9)

    def test_an_ordinary_path_changes_nothing(self):
        events = self.session("src/util.ts", "main")
        tools = self.tool_events(events)
        self.assertTrue(tools)
        self.assertIsNone(tools[0]["context"]["test_case_key"])
        self.assertEqual(tools[0]["link"]["confidence"], 0.9)

    def test_the_file_content_never_appears_in_any_event(self):
        events = self.session("tests/e2e/IML-TC-12893.spec.ts", "main")
        self.assertNotIn(SECRET, json.dumps(events))
