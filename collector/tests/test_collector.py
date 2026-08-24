#!/usr/bin/env python3
"""
Tests for the AI telemetry ingestion collector.

stdlib `unittest` only — no pytest, no HTTP client library. Each test drives a
real ThreadingHTTPServer over a real socket on an ephemeral port, so what is
exercised is the shipped request path, not a framework test double.

Run:
    python3 -m unittest discover -s collector/tests -v
    python3 collector/tests/test_collector.py
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as collector_main  # noqa: E402

TOKEN = "test-token-not-a-real-credential"

# Fixture values that MUST NEVER appear in any log line.
SECRET_VALUE = "s3cr3t-value-that-must-never-be-logged"
ATLASSIAN_TOKEN = "ATATT3xFfGF0" + "T" * 40
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPL"[:12] + "ABCD"   # AKIA + exactly 16 [0-9A-Z]
GITHUB_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8J9k0"


def make_event(**overrides):
    """A minimal valid `run.completed` envelope per CONTRACT.md §2."""
    event = {
        "schema_version": "1.0.0",
        "event_id": "evt_" + uuid.uuid4().hex,
        "event_type": "run.completed",
        "event_time": collector_main.rfc3339(collector_main.utcnow()),
        "trace_id": "trc_" + uuid.uuid4().hex,
        "run_id": "run_" + uuid.uuid4().hex,
        "parent_run_id": None,
        "span_id": "spn_" + uuid.uuid4().hex,
        "workflow_id": "wf-PRJ-6383-20260819",
        "actor": {
            "person_id": "5b10a2844c20165700ede21g",
            "person_email_hash": "a" * 64,
            "team_id": None,
            "role": "dev",
        },
        "context": {
            "jira_issue_key": "PRJ-6383",
            "jira_project_key": "PRJ",
            "repo_full_name": "acme/ai-engineering-platform",
            "branch_name": "feature/ai-observability",
            "product_profile": "watchtower",
            "environment": "local",
        },
        "agent": {
            "agent_name": "Platform Developer 2.0",
            "agent_version": "a3f21c9",
            "skill_name": None,
            "skill_version": None,
            "surface": "vscode-copilot-chat",
        },
        "attributes": {"duration_ms": 41230, "phases_completed": 6},
        "link": {"method": "explicit", "confidence": 1.0},
    }
    for key, value in overrides.items():
        event[key] = value
    return event


class RecordingPublisher:
    """Stands in for Pub/Sub. Records what would have been published."""

    def __init__(self):
        self.published = []
        self.sink = "pubsub"

    def publish(self, envelope):
        self.published.append(envelope)
        return self.sink


class CollectorTestCase(unittest.TestCase):
    """Boots a real server per test and captures the collector's log output."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.fallback_path = os.path.join(self.tmpdir.name, "spill.ndjson")
        self.findings_path = os.path.join(self.tmpdir.name, "dq_findings.ndjson")

        self.publisher = RecordingPublisher()
        self.findings = collector_main.FindingsSink(self.findings_path)
        self.collector = collector_main.Collector(
            token=TOKEN,
            publisher=self.publisher,
            findings=self.findings,
            dedup_ttl=900,
        )

        # Capture everything the collector logs, at DEBUG, into a buffer.
        self.log_stream = io.StringIO()
        handler = logging.StreamHandler(self.log_stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger = collector_main.LOG
        self._prev_level = logger.level
        self._prev_propagate = logger.propagate
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        def restore():
            logger.handlers = []
            logger.setLevel(self._prev_level)
            logger.propagate = self._prev_propagate

        self.addCleanup(restore)

        self.server = collector_main.create_server(self.collector, "127.0.0.1", 0)
        self.host, self.port = self.server.server_address[0], self.server.server_address[1]
        # poll_interval keeps shutdown() from costing 0.5s per test.
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

        def shutdown():
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)

        self.addCleanup(shutdown)

    # -- helpers ----------------------------------------------------------- #

    def logs(self) -> str:
        return self.log_stream.getvalue()

    def post(self, payload, token=TOKEN, content_type="application/json", raw=None):
        url = f"http://{self.host}:{self.port}/v1/events"
        if raw is not None:
            body = raw.encode("utf-8") if isinstance(raw, str) else raw
        else:
            body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", content_type)
        request.add_header("Connection", "close")
        if token is not None:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            raw_body = err.read().decode("utf-8")
            try:
                return err.code, json.loads(raw_body)
            except json.JSONDecodeError:
                return err.code, {"raw": raw_body}

    def get(self, path):
        url = f"http://{self.host}:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, {}

    def findings_rows(self):
        if not os.path.exists(self.findings_path):
            return []
        with open(self.findings_path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


# --------------------------------------------------------------------------- #


class TestHealth(CollectorTestCase):
    def test_healthz_needs_no_auth_and_returns_ok(self):
        status, body = self.get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})


class TestHappyPath(CollectorTestCase):
    def test_valid_event_is_accepted_and_published(self):
        event = make_event()
        status, body = self.post([event])

        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 1)
        self.assertEqual(body["rejected"], 0)
        self.assertEqual(body["sinks"], {"pubsub": 1})

        self.assertEqual(len(self.publisher.published), 1)
        published = self.publisher.published[0]
        self.assertEqual(published["event_id"], event["event_id"])
        self.assertEqual(published["event_type"], "run.completed")
        self.assertEqual(published["attributes"], {"duration_ms": 41230, "phases_completed": 6})
        self.assertEqual(published["link"], {"method": "explicit", "confidence": 1.0})

    def test_ndjson_body_is_accepted(self):
        lines = "\n".join(json.dumps(make_event()) for _ in range(3))
        status, body = self.post(None, raw=lines, content_type="application/x-ndjson")
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 3)

    def test_ingested_at_is_server_set_and_client_value_discarded(self):
        # CONTRACT.md §2: ingested_at is "set by the collector, never by the client".
        event = make_event()
        event["ingested_at"] = "1999-01-01T00:00:00Z"
        event["dq_flags"] = ["client_should_not_set_this"]
        status, _ = self.post([event])
        self.assertEqual(status, 200)

        published = self.publisher.published[0]
        self.assertNotEqual(published["ingested_at"], "1999-01-01T00:00:00Z")
        self.assertNotIn("client_should_not_set_this", published["dq_flags"])
        ingested = collector_main.parse_rfc3339(published["ingested_at"])
        self.assertLess(
            abs((collector_main.utcnow() - ingested).total_seconds()), 30
        )

    def test_unknown_fields_are_stripped_by_allow_list(self):
        event = make_event()
        event["totally_new_top_level_field"] = "dropme"
        event["actor"]["manager_name"] = "dropme"
        event["attributes"]["not_in_contract"] = 12345
        status, _ = self.post([event])
        self.assertEqual(status, 200)

        published = self.publisher.published[0]
        self.assertNotIn("totally_new_top_level_field", published)
        self.assertNotIn("manager_name", published["actor"])
        self.assertNotIn("not_in_contract", published["attributes"])
        # The DROPPED NAME is reported (names are safe); no value is reported.
        self.assertIn("strip:attributes:not_in_contract", published["dq_flags"])

    def test_jira_transition_snapshot_and_attribution_survive_ingest(self):
        """CONTRACT.md §3 row 21 — `issue` and `attribution` are mandated attributes.

        Regression: both were emitted by poll_jira.py and both were absent from
        ATTRIBUTE_ALLOWLIST, so every provenance marker and the AR-3 evidence was
        stripped at ingest. Adding a field to the poller alone is not enough.
        """
        event = make_event(
            event_type="jira.transition",
            attributes={
                "from_status": "To Do",
                "to_status": "In Progress",
                "transitioned_at": collector_main.rfc3339(collector_main.utcnow()),
                "status_category": "indeterminate",
                "issue": {"issue_key": "QD-12", "labels": ["AUTH_BY_COPILOT"]},
                "attribution": {
                    "rule": "AR-3",
                    "ai_labels": ["AUTH_BY_COPILOT", "PLANNED_BY_COPILOT", "GEN_BY_COPILOT"],
                    "has_ai_labels": True,
                    "label_authored_by_ai": True,
                    "label_planned_by_ai": True,
                    "label_generated_by_ai": True,
                    "label_reviewed_by_ai": False,
                    "unrecognised_ai_labels": ["COPILOT_TESTING", "PLANNER_BY_COPILOT"],
                    "has_ai_label_drift": True,
                },
            },
        )
        status, _ = self.post([event])
        self.assertEqual(status, 200)

        attributes = self.publisher.published[0]["attributes"]
        self.assertEqual(attributes["issue"]["issue_key"], "QD-12")
        attribution = attributes["attribution"]
        for field in (
            "label_authored_by_ai", "label_planned_by_ai",
            "label_generated_by_ai", "label_reviewed_by_ai",
            "unrecognised_ai_labels", "has_ai_label_drift",
        ):
            self.assertIn(field, attribution, f"{field} was stripped at ingest")
        # The drift NAMES have to reach the warehouse -- a bare boolean cannot
        # tell an operator which labels to reconcile.
        self.assertEqual(
            attribution["unrecognised_ai_labels"],
            ["COPILOT_TESTING", "PLANNER_BY_COPILOT"],
        )
        self.assertFalse(
            [f for f in self.publisher.published[0]["dq_flags"]
             if f.startswith("strip:attributes:")]
        )


class TestSchemaValidation(CollectorTestCase):
    def test_unknown_event_type_is_400(self):
        event = make_event(event_type="run.exploded")
        status, body = self.post([event])
        self.assertEqual(status, 400)
        self.assertEqual(body["accepted"], 0)
        self.assertEqual(body["rejected"], 1)
        self.assertEqual(body["rejections"][0]["check_id"], "envelope.event_type_unknown")
        self.assertEqual(self.publisher.published, [])

    def test_every_contract_event_type_is_accepted(self):
        # The closed enum in main.py must match CONTRACT.md §3 exactly (23 rows:
        # 21 at launch, plus test.run.completed and test.case.snapshot once an
        # AioAuth key made AIO TCMS reachable on 2026-08-20). This count is meant
        # to fail when the enum changes -- update it together with CONTRACT.md §3
        # and common.py, never on its own.
        self.assertEqual(len(collector_main.EVENT_TYPES), 23)
        for event_type in sorted(collector_main.EVENT_TYPES):
            status, body = self.post([make_event(event_type=event_type, attributes={})])
            self.assertEqual(status, 200, f"{event_type} was rejected: {body}")

    def test_the_three_copies_of_the_enum_agree(self):
        # The enum is written down three times -- CONTRACT.md §3, the collector,
        # and the pollers' common.py. A drift between them is a rejected event at
        # runtime, so it is checked here rather than discovered in production.
        import importlib.util
        import os
        # main.py lives in collector/, so two levels up is the repository root,
        # which is where pollers/ sits.
        pollers = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(collector_main.__file__))),
            "pollers", "common.py")
        spec = importlib.util.spec_from_file_location("pollers_common", pollers)
        module = importlib.util.module_from_spec(spec)
        # @dataclass resolves annotations through sys.modules, so the module has
        # to be registered before it is executed.
        import sys as _sys
        _sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            _sys.modules.pop(spec.name, None)
        self.assertEqual(set(collector_main.EVENT_TYPES), set(module.EVENT_TYPES))

    def test_unknown_major_schema_version_is_400(self):
        status, body = self.post([make_event(schema_version="2.0.0")])
        self.assertEqual(status, 400)
        self.assertEqual(
            body["rejections"][0]["check_id"], "envelope.schema_version_unsupported"
        )

    def test_newer_minor_schema_version_is_accepted(self):
        status, _ = self.post([make_event(schema_version="1.4.2")])
        self.assertEqual(status, 200)

    def test_malformed_body_is_400(self):
        status, body = self.post(None, raw="{not json}")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "malformed_body")


class TestAuth(CollectorTestCase):
    def test_absent_bearer_token_is_401(self):
        status, body = self.post([make_event()], token=None)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")
        self.assertEqual(self.publisher.published, [])

    def test_wrong_bearer_token_is_401(self):
        status, body = self.post([make_event()], token="wrong-token")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")
        self.assertEqual(self.publisher.published, [])

    def test_token_prefix_is_not_enough(self):
        status, _ = self.post([make_event()], token=TOKEN[:-1])
        self.assertEqual(status, 401)

    def test_unconfigured_token_denies_everything(self):
        empty = collector_main.Collector(
            token="", publisher=RecordingPublisher(), findings=self.findings
        )
        self.assertFalse(empty.authenticate(f"Bearer {TOKEN}"))
        self.assertFalse(empty.authenticate(None))

    def test_constant_time_comparison_is_used(self):
        # Guard against someone "simplifying" this to ==.
        with open(collector_main.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("hmac.compare_digest", source)


class TestRedactionEnforcement(CollectorTestCase):
    def test_forbidden_attribute_key_is_rejected_and_value_never_logged(self):
        event = make_event(event_type="run.failed")
        event["attributes"] = {
            "duration_ms": 100,
            "failure_class": "assertion",
            # `error_message` is on the CONTRACT.md §3 forbidden list.
            "error_message": SECRET_VALUE,
        }
        status, body = self.post([event])

        self.assertEqual(status, 400)
        self.assertEqual(body["rejected"], 1)
        rejection = body["rejections"][0]
        self.assertEqual(rejection["check_id"], "redaction.forbidden_attribute_key")
        self.assertEqual(rejection["field"], "attributes.error_message")
        self.assertEqual(self.publisher.published, [])

        logs = self.logs()
        # The FIELD NAME is present ...
        self.assertIn("dq_payload_rejected", logs)
        self.assertIn("attributes.error_message", logs)
        self.assertIn("redaction.forbidden_attribute_key", logs)
        # ... and the VALUE is absent. This is the whole point of §11.3.
        self.assertNotIn(SECRET_VALUE, logs)
        # Nor may it reach the HTTP response or the dq_findings row.
        self.assertNotIn(SECRET_VALUE, json.dumps(body))
        rows = self.findings_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["check_id"], "dq_payload_rejected")
        self.assertEqual(rows[0]["field"], "attributes.error_message")
        self.assertNotIn(SECRET_VALUE, json.dumps(rows[0]))

    def test_every_forbidden_key_from_the_contract_is_rejected(self):
        for key in sorted(collector_main.FORBIDDEN_ATTRIBUTE_KEYS):
            event = make_event()
            event["attributes"] = {"duration_ms": 1, key: "x"}
            status, body = self.post([event])
            self.assertEqual(status, 400, f"forbidden key {key!r} was not rejected")
            self.assertEqual(
                body["rejections"][0]["check_id"], "redaction.forbidden_attribute_key"
            )

    def test_forbidden_key_nested_deeply_is_still_rejected(self):
        event = make_event()
        event["attributes"] = {"duration_ms": 1, "phases_completed": [{"prompt": "x"}]}
        status, body = self.post([event])
        self.assertEqual(status, 400)
        self.assertEqual(
            body["rejections"][0]["check_id"], "redaction.forbidden_attribute_key"
        )

    def test_contract_attribute_names_do_not_falsely_trip_the_forbidden_list(self):
        # `input_tokens` contains "token"; `output_content_hash` contains "content".
        # Exact-match semantics must let both through.
        event = make_event(event_type="model.call")
        event["attributes"] = {
            "model_id": "GPT-5.3-Codex",
            "input_tokens": 1200,
            "output_tokens": 340,
            "cached_input_tokens": 900,
            "reasoning_tokens": 128,
        }
        status, _ = self.post([event])
        self.assertEqual(status, 200)

        event2 = make_event(event_type="output.generated")
        event2["attributes"] = {
            "output_id": "out_1",
            "artifact_type": "code",
            "file_path": "src/x.ts",
            "output_content_hash": "b" * 64,
        }
        status2, _ = self.post([event2])
        self.assertEqual(status2, 200)


class TestSecretScreening(CollectorTestCase):
    def _assert_rejected_without_leaking(self, event, secret, expected_check):
        status, body = self.post([event])
        self.assertEqual(status, 400)
        self.assertEqual(body["rejected"], 1)
        self.assertEqual(body["rejections"][0]["check_id"], expected_check)
        self.assertEqual(self.publisher.published, [])
        logs = self.logs()
        self.assertIn("dq_payload_rejected", logs)
        self.assertNotIn(secret, logs)
        self.assertNotIn(secret, json.dumps(body))
        self.assertNotIn(secret, json.dumps(self.findings_rows()))

    def test_atlassian_atatt_token_is_rejected(self):
        event = make_event(event_type="tool.call")
        event["attributes"] = {
            "tool_name": "atlassian-mcp",
            "tool_kind": "mcp",
            "status": "failed",
            "error_class": ATLASSIAN_TOKEN,
        }
        self._assert_rejected_without_leaking(
            event, ATLASSIAN_TOKEN, "secret.atlassian_api_token"
        )

    def test_aws_akia_key_is_rejected(self):
        event = make_event(event_type="output.generated")
        event["attributes"] = {
            "output_id": "out_2",
            "artifact_type": "config",
            "file_path": f"infra/{AWS_KEY}.tf",
        }
        self._assert_rejected_without_leaking(event, AWS_KEY, "secret.aws_access_key_id")

    def test_github_token_is_rejected(self):
        event = make_event()
        event["context"]["branch_name"] = f"feature/{GITHUB_TOKEN}"
        self._assert_rejected_without_leaking(event, GITHUB_TOKEN, "secret.github_token")

    def test_private_key_block_is_rejected(self):
        blob = "-----BEGIN OPENSSH PRIVATE KEY-----"
        event = make_event()
        event["context"]["product_profile"] = blob
        self._assert_rejected_without_leaking(event, blob, "secret.private_key_block")

    def test_password_and_api_key_assignments_are_rejected(self):
        for value, check in (
            ("password=hunter2789", "secret.password_assignment"),
            ("api_key=ABCDEFGH12345678", "secret.api_key_assignment"),
            ("API-KEY: ABCDEFGH12345678", "secret.api_key_assignment"),
        ):
            event = make_event()
            event["context"]["branch_name"] = value
            status, body = self.post([event])
            self.assertEqual(status, 400, f"{value!r} was not rejected")
            self.assertEqual(body["rejections"][0]["check_id"], check)
            self.assertNotIn(value, self.logs())


class TestIdempotency(CollectorTestCase):
    def test_duplicate_event_id_within_ttl_is_deduped(self):
        event = make_event()
        first_status, first = self.post([event])
        second_status, second = self.post([event])

        self.assertEqual(first_status, 200)
        self.assertEqual(first["accepted"], 1)
        self.assertEqual(first["duplicates"], 0)

        self.assertEqual(second_status, 200)
        self.assertEqual(second["accepted"], 0)
        self.assertEqual(second["duplicates"], 1)

        # Published exactly once — DQ-11 "idempotent drop".
        self.assertEqual(len(self.publisher.published), 1)
        self.assertIn("DQ-11 duplicate dropped", self.logs())

    def test_duplicate_inside_one_batch_is_deduped(self):
        event = make_event()
        status, body = self.post([event, dict(event)])
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 1)
        self.assertEqual(body["duplicates"], 1)
        self.assertEqual(len(self.publisher.published), 1)

    def test_cache_entry_expires_after_the_ttl(self):
        cache = collector_main.TTLCache(ttl_seconds=10)
        self.assertTrue(cache.add_if_absent("evt_a", now=1000.0))
        self.assertFalse(cache.add_if_absent("evt_a", now=1005.0))
        self.assertTrue(cache.add_if_absent("evt_a", now=1011.0))


class TestClockSkew(CollectorTestCase):
    def test_future_event_time_is_clamped_and_flagged(self):
        future = collector_main.utcnow() + timedelta(hours=3)
        event = make_event(event_time=collector_main.rfc3339(future))
        status, _ = self.post([event])
        self.assertEqual(status, 200)

        published = self.publisher.published[0]
        self.assertIn("DQ-10:event_time_future_clamped", published["dq_flags"])
        clamped = collector_main.parse_rfc3339(published["event_time"])
        self.assertLessEqual(clamped, collector_main.utcnow() + timedelta(seconds=5))

    def test_small_skew_inside_tolerance_is_not_clamped(self):
        near = collector_main.utcnow() + timedelta(minutes=2)
        event = make_event(event_time=collector_main.rfc3339(near))
        status, _ = self.post([event])
        self.assertEqual(status, 200)
        published = self.publisher.published[0]
        self.assertEqual(published["dq_flags"], [])
        self.assertEqual(published["event_time"], event["event_time"])

    def test_end_before_start_is_clamped_and_flagged(self):
        start = collector_main.utcnow() - timedelta(hours=2)
        end = start - timedelta(minutes=30)          # end BEFORE start
        event = make_event(event_type="scm.pr.merged")
        # `created_at` is not a contract attribute for this event type, so drive
        # the pair check directly on the normaliser to prove the clamp logic.
        clamped_time, attributes, flags = collector_main._clamp_timestamps(
            collector_main.utcnow(),
            {
                "created_at": collector_main.rfc3339(start),
                "merged_at": collector_main.rfc3339(end),
            },
            collector_main.utcnow(),
        )
        self.assertIn("DQ-10:merged_at_before_created_at_clamped", flags)
        self.assertEqual(attributes["merged_at"], collector_main.rfc3339(start))

    def test_future_attribute_timestamp_is_clamped_and_flagged(self):
        future = collector_main.utcnow() + timedelta(days=2)
        event = make_event(event_type="scm.pr.merged")
        event["attributes"] = {
            "pr_id": "42",
            "merged_at": collector_main.rfc3339(future),
            "merge_commit_sha": "c" * 40,
        }
        status, _ = self.post([event])
        self.assertEqual(status, 200)
        published = self.publisher.published[0]
        self.assertIn("DQ-10:merged_at_future_clamped", published["dq_flags"])
        merged = collector_main.parse_rfc3339(published["attributes"]["merged_at"])
        self.assertLessEqual(merged, collector_main.utcnow() + timedelta(seconds=5))

    def test_negative_duration_is_clamped_and_flagged(self):
        event = make_event()
        event["attributes"] = {"duration_ms": -5, "phases_completed": 1}
        status, _ = self.post([event])
        self.assertEqual(status, 200)
        published = self.publisher.published[0]
        self.assertEqual(published["attributes"]["duration_ms"], 0)
        self.assertIn("DQ-10:duration_ms_negative_clamped", published["dq_flags"])


class TestPubSubFallback(CollectorTestCase):
    """Pub/Sub unavailable -> local NDJSON file, 200, nothing lost."""

    def _publisher_with_broken_pubsub(self):
        publisher = collector_main.Publisher(
            project="", topic="ai-run-events", fallback_path=self.fallback_path
        )

        class Boom:
            def publish(self, *args, **kwargs):
                raise RuntimeError("pubsub unreachable")

        # Simulate "the client exists but every publish fails" — the harder case
        # than "no client at all", because it exercises the except path.
        publisher._client = Boom()
        publisher._topic_path = "projects/example/topics/ai-run-events"
        return publisher

    def test_publish_falls_back_to_local_file_and_returns_200(self):
        self.collector.publisher = self._publisher_with_broken_pubsub()

        events = [make_event() for _ in range(4)]
        status, body = self.post(events)

        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 4)
        self.assertEqual(body["sinks"], {"file": 4})

        with open(self.fallback_path, encoding="utf-8") as handle:
            spilled = [json.loads(line) for line in handle if line.strip()]

        # NOTHING LOST: every event id is on disk, exactly once.
        self.assertEqual(len(spilled), 4)
        self.assertEqual(
            sorted(row["event_id"] for row in spilled),
            sorted(event["event_id"] for event in events),
        )
        for row in spilled:
            self.assertIn("ingested_at", row)
            self.assertEqual(row["schema_version"], "1.0.0")

        self.assertIn("pubsub publish failed", self.logs())

    def test_no_pubsub_project_configured_uses_the_file_sink(self):
        publisher = collector_main.Publisher(
            project="", topic="ai-run-events", fallback_path=self.fallback_path
        )
        self.assertIsNone(publisher._client)
        self.collector.publisher = publisher

        status, body = self.post([make_event()])
        self.assertEqual(status, 200)
        self.assertEqual(body["sinks"], {"file": 1})
        self.assertTrue(os.path.exists(self.fallback_path))


class TestMixedBatch(CollectorTestCase):
    def test_valid_events_in_a_rejected_batch_are_still_published(self):
        good = make_event()
        bad = make_event(event_type="not.a.real.type")
        status, body = self.post([good, bad])

        self.assertEqual(status, 400)          # the batch signals a rejection
        self.assertEqual(body["accepted"], 1)  # ... but the good row still landed
        self.assertEqual(body["rejected"], 1)
        self.assertEqual(len(self.publisher.published), 1)
        self.assertEqual(self.publisher.published[0]["event_id"], good["event_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
