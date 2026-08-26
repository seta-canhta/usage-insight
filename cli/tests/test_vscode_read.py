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
