"""Tests for the OTel-to-run join.

    python3 -m unittest discover -s cli/tests

The interesting cases are all refusals. A join that guesses when it should
abstain is worse than no join: the number it produces looks exactly like a
measured one.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cli"))

import link_runs  # noqa: E402


def run_started(run_id, at, agent="Platform Developer 2.0", jira="PRJ-1"):
    return {"event_type": "run.started", "run_id": run_id, "event_time": at,
            "agent": {"agent_name": agent}, "context": {"jira_issue_key": jira},
            "attributes": {}}


def run_completed(run_id, at):
    return {"event_type": "run.completed", "run_id": run_id, "event_time": at,
            "agent": {}, "context": {}, "attributes": {}}


def run_bound(run_id, conversation, at):
    return {"event_type": "run.bound", "run_id": run_id, "event_time": at,
            "agent": {}, "context": {},
            "attributes": {"otel_conversation_id": conversation}}


def model_call(conversation, at, agent="Platform Developer 2.0", tokens=100):
    return {"event_type": "model.call", "trace_id": conversation,
            "event_time": at, "agent": {"agent_name": agent},
            "attributes": {"input_tokens": tokens, "output_tokens": 10,
                           "cached_input_tokens": 0}}


class LinkTestCase(unittest.TestCase):

    def link(self, events):
        return link_runs.link(link_runs.runs_from(events),
                              link_runs.conversations_from(events))


class TestExplicit(LinkTestCase):

    def test_run_bound_wins_outright(self):
        # The designed bridge. Nothing else earns confidence 1.0, and only
        # explicit rows may feed cost-per-output.
        result = self.link([
            run_started("run_a", "2026-08-24T10:00:00Z"),
            run_bound("run_a", "conv_1", "2026-08-24T10:00:01Z"),
            run_completed("run_a", "2026-08-24T10:05:00Z"),
            model_call("conv_1", "2026-08-24T10:02:00Z"),
        ])
        link = result["links"][0]
        self.assertEqual(link["method"], "explicit")
        self.assertEqual(link["confidence"], 1.0)
        self.assertEqual(result["usable_for_cost_per_output"], 1)

    def test_a_heuristic_link_may_not_price_an_output(self):
        # CONTRACT.md 2.4. The limit is a property of the evidence, and a
        # consumer should not have to re-derive the rule to discover it.
        result = self.link([
            run_started("run_a", "2026-08-24T10:00:00Z"),
            run_completed("run_a", "2026-08-24T10:05:00Z"),
            model_call("conv_1", "2026-08-24T10:02:00Z"),
        ])
        self.assertEqual(result["links"][0]["method"], "heuristic")
        self.assertEqual(result["usable_for_cost_per_output"], 0)


class TestCorroboration(LinkTestCase):

    def test_agreeing_agent_names_raise_confidence(self):
        # Two independent signals: mode_name comes from Copilot, --agent from
        # the agent's own instructions. Agreement is corroboration, not one
        # fact counted twice.
        result = self.link([
            run_started("run_a", "2026-08-24T10:00:00Z", agent="Platform Developer 2.0"),
            run_completed("run_a", "2026-08-24T10:05:00Z"),
            model_call("conv_1", "2026-08-24T10:02:00Z", agent="Platform Developer 2.0"),
        ])
        link = result["links"][0]
        self.assertEqual(link["confidence"], 0.8)
        self.assertTrue(any("agent name agrees" in e for e in link["evidence"]))

    def test_time_alone_is_weaker(self):
        result = self.link([
            run_started("run_a", "2026-08-24T10:00:00Z", agent="Platform Planner 2.0"),
            run_completed("run_a", "2026-08-24T10:05:00Z"),
            model_call("conv_1", "2026-08-24T10:02:00Z", agent="somethingElse"),
        ])
        link = result["links"][0]
        self.assertEqual(link["confidence"], 0.5)
        self.assertEqual(len(link["evidence"]), 1)


class TestRefusal(LinkTestCase):
    """A join that guesses when it should abstain produces a number that looks
    exactly like a measured one."""

    def test_two_overlapping_runs_are_not_attributed(self):
        result = self.link([
            run_started("run_a", "2026-08-24T10:00:00Z"),
            run_completed("run_a", "2026-08-24T10:10:00Z"),
            run_started("run_b", "2026-08-24T10:00:00Z"),
            run_completed("run_b", "2026-08-24T10:10:00Z"),
            model_call("conv_1", "2026-08-24T10:05:00Z", tokens=999),
        ])
        self.assertEqual(result["links"], [])
        unlinked = result["unlinked_conversations"][0]
        self.assertIn("2 runs overlap", unlinked["reason"])
        self.assertEqual(sorted(unlinked["candidate_run_ids"]), ["run_a", "run_b"])
        self.assertEqual(unlinked["input_tokens"], 999)

    def test_a_conversation_outside_every_run_is_not_attributed(self):
        result = self.link([
            run_started("run_a", "2026-08-24T10:00:00Z"),
            run_completed("run_a", "2026-08-24T10:05:00Z"),
            model_call("conv_1", "2026-08-24T14:00:00Z"),
        ])
        self.assertEqual(result["links"], [])
        self.assertIn("no run covers",
                      result["unlinked_conversations"][0]["reason"])

    def test_background_traffic_without_a_conversation_id_is_ignored(self):
        # Copilot's own housekeeping agents emit spans with no conversation id.
        # Sweeping them into the nearest run would bill a platform agent for the
        # tokens Copilot spent naming the chat.
        result = self.link([
            run_started("run_a", "2026-08-24T10:00:00Z"),
            run_completed("run_a", "2026-08-24T10:05:00Z"),
            {"event_type": "model.call", "trace_id": None,
             "event_time": "2026-08-24T10:02:00Z",
             "agent": {"agent_name": "title"},
             "attributes": {"input_tokens": 260, "output_tokens": 9}},
        ])
        self.assertEqual(result["conversations_seen"], 0)
        self.assertEqual(result["links"], [])


class TestWindow(LinkTestCase):

    def test_a_span_flushed_just_after_run_end_still_links(self):
        # The exporter batches and the emitter's run-end fires before the last
        # span is flushed, so an exact window would drop real calls.
        result = self.link([
            run_started("run_a", "2026-08-24T10:00:00Z"),
            run_completed("run_a", "2026-08-24T10:05:00Z"),
            model_call("conv_1", "2026-08-24T10:05:30Z"),
        ])
        self.assertEqual(len(result["links"]), 1)

    def test_a_run_that_never_finished_does_not_swallow_later_traffic(self):
        # An open run is open-ended. Without a bound it would claim every
        # conversation for the rest of the day.
        result = self.link([
            run_started("run_a", "2026-08-24T10:00:00Z"),
            model_call("conv_1", "2026-08-24T16:00:00Z"),
        ])
        self.assertEqual(result["links"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
