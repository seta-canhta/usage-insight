"""Tests for the local collector.

    python3 -m unittest discover -s cli/tests -v
    python3 -m pytest cli/tests -q
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class ClientTestCase(unittest.TestCase):
    """Each test gets its own store, so nothing touches a real machine's data."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="insight-test-")
        os.environ["SETA_INSIGHT_HOME"] = self.home
        for module in ("insight",):
            sys.modules.pop(module, None)
        import insight
        self.insight = insight
        self.addCleanup(shutil.rmtree, self.home, True)
        self.addCleanup(os.environ.pop, "SETA_INSIGHT_HOME", None)

    def init(self):
        self.run_cli("init", "--yes")

    def run_cli(self, *argv):
        """Run a command with its output captured.

        These commands print to stdout by design -- that is their interface.
        A test suite that lets it through is unreadable, and the noise hides
        the one line that matters when something fails.
        """
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = self.insight.main(list(argv))
        return code, buffer.getvalue()


class TestConsent(ClientTestCase):

    def test_commands_refuse_to_run_before_consent(self):
        # Collection without a recorded consent is the thing this whole design
        # is trying not to do, so it fails rather than defaulting to on.
        with self.assertRaises(SystemExit):
            self.run_cli("scan", "--repo", ".")

    def test_init_records_consent_and_a_salt(self):
        self.init()
        config = self.insight.load_config()
        self.assertTrue(config["consent_at"])
        self.assertTrue(config["salt"])
        self.assertTrue(config["machine_id"])

    def test_init_is_idempotent_and_keeps_the_salt(self):
        # Rotating the salt would silently break every historical join, so a
        # second init must not generate a new one.
        self.init()
        first = self.insight.load_config()
        self.run_cli("init", "--yes", "--force")
        self.assertEqual(self.insight.load_config()["salt"], first["salt"])
        self.assertEqual(
            self.insight.load_config()["machine_id"], first["machine_id"])


class TestAdoptedToken(ClientTestCase):
    """`init --token` — a secret issued by whoever runs the server.

    The onboarding case: somebody is added to the whitelist before their laptop
    has been touched, so the secret has to exist before the config file does.
    """

    def test_the_issued_secret_is_the_one_used(self):
        self.run_cli("init", "--yes", "--email", "ngoc@aeris.net",
                     "--token", "issued-by-the-admin")
        self.assertEqual(self.insight.load_config()["endpoint_token"],
                         "issued-by-the-admin")

    def test_the_printed_line_matches_the_issued_secret(self):
        # The admin already put a fingerprint in the whitelist. If `whoami`
        # printed a different one, the two would disagree and every upload
        # would 401 with nothing to point at.
        import identity
        _, out = self.run_cli("init", "--yes", "--email", "ngoc@aeris.net",
                              "--token", "issued-by-the-admin")
        self.assertIn(identity.fingerprint("issued-by-the-admin"), out)

    def test_init_with_an_issued_secret_says_so(self):
        _, out = self.run_cli("init", "--yes", "--email", "ngoc@aeris.net",
                              "--token", "issued")
        self.assertIn("already on the server whitelist", out)
        self.assertNotIn("Send this line", out)

    def test_without_it_a_secret_is_minted_locally(self):
        self.run_cli("init", "--yes", "--email", "ngoc@aeris.net")
        token = self.insight.load_config()["endpoint_token"]
        self.assertTrue(token)
        self.assertNotEqual(token, "issued-by-the-admin")

    def test_a_token_on_its_own_is_enough(self):
        # No --email alongside it. The server resolves the fingerprint to a
        # person itself -- that is the whole mechanism -- and `ship` has never
        # sent an address in any header, so asking for one here would be
        # asking twice for the same fact.
        self.run_cli("init", "--yes", "--token", "issued-by-the-admin")
        config = self.insight.load_config()
        self.assertEqual(config["endpoint_token"], "issued-by-the-admin")
        self.assertTrue(config["endpoint"])

    def test_whoami_works_without_a_local_address(self):
        import identity
        self.run_cli("init", "--yes", "--token", "issued-by-the-admin")
        _, out = self.run_cli("whoami")
        self.assertIn(identity.fingerprint("issued-by-the-admin"), out)

    def test_rotating_works_without_a_local_address(self):
        self.run_cli("init", "--yes", "--token", "issued-by-the-admin")
        self.run_cli("rotate-token")
        config = self.insight.load_config()
        self.assertNotEqual(config["endpoint_token"], "issued-by-the-admin")
        self.assertEqual(config["endpoint_token_previous"], "issued-by-the-admin")

    def test_rotating_afterwards_replaces_it_with_a_local_one(self):
        # The whole point of adopting one: it travelled over some channel to
        # get here. Rotation is where this machine starts holding a secret
        # nobody else has seen, and the old one stays valid meanwhile.
        self.run_cli("init", "--yes", "--email", "ngoc@aeris.net",
                     "--token", "issued-by-the-admin")
        self.run_cli("rotate-token")
        config = self.insight.load_config()
        self.assertNotEqual(config["endpoint_token"], "issued-by-the-admin")
        self.assertEqual(config["endpoint_token_previous"], "issued-by-the-admin")


class TestSetupAdoptsAnIssuedToken(ClientTestCase):
    """The case this was written for: a machine that has been collecting for
    weeks, and an address that was added to the server whitelist yesterday."""

    def setUp(self):
        super().setUp()
        import vscode_setup
        # Do not touch the real editor settings from a test.
        self.addCleanup(setattr, vscode_setup, "settings_path",
                        vscode_setup.settings_path)
        vscode_setup.settings_path = lambda: None

    def setup_cli(self, *extra):
        return self.run_cli("setup", "--yes", "--no-schedule", *extra)

    def test_an_existing_machine_adopts_the_issued_secret(self):
        self.init()                                  # collecting, no address yet
        before = self.insight.load_config()
        self.setup_cli("--email", "ngoc@aeris.net", "--token", "issued")
        config = self.insight.load_config()
        self.assertEqual(config["endpoint_token"], "issued")
        self.assertEqual(config["email"], "ngoc@aeris.net")
        # and nothing already collected is disturbed
        self.assertEqual(config["salt"], before["salt"])
        self.assertEqual(config["machine_id"], before["machine_id"])

    def test_without_a_token_setup_mints_one_as_before(self):
        self.init()
        self.setup_cli("--email", "ngoc@aeris.net")
        token = self.insight.load_config()["endpoint_token"]
        self.assertTrue(token)
        self.assertNotEqual(token, "issued")

    def test_a_machine_that_was_already_uploading_keeps_working(self):
        # Replacing the secret outright would break every upload between now
        # and the server being told. The old one stays valid, which is exactly
        # what `rotate-token` does.
        self.init()
        self.setup_cli("--email", "ngoc@aeris.net")
        original = self.insight.load_config()["endpoint_token"]
        self.setup_cli("--email", "ngoc@aeris.net", "--token", "issued")
        config = self.insight.load_config()
        self.assertEqual(config["endpoint_token"], "issued")
        self.assertEqual(config["endpoint_token_previous"], original)

    def test_running_it_twice_does_not_shuffle_the_secrets(self):
        # Idempotence matters here: `setup` is the command people re-run when
        # they are not sure it worked, and a second run must not push the
        # issued secret into `previous` and leave nothing valid.
        self.init()
        self.setup_cli("--email", "ngoc@aeris.net", "--token", "issued")
        self.setup_cli("--email", "ngoc@aeris.net", "--token", "issued")
        config = self.insight.load_config()
        self.assertEqual(config["endpoint_token"], "issued")
        self.assertIsNone(config.get("endpoint_token_previous"))

    def test_it_does_not_ask_for_a_line_the_server_already_has(self):
        # Telling somebody to send a line that is already in place trains them
        # to skip this paragraph -- and the thing they do have to do, rotate,
        # is in the same paragraph.
        self.init()
        _, out = self.setup_cli("--email", "ngoc@aeris.net", "--token", "issued")
        self.assertIn("already on the server whitelist", out)
        self.assertIn("rotate-token", out)
        self.assertNotIn("Send this line", out)

    def test_the_paragraph_is_printed_once(self):
        # `setup` runs `init`, and both used to print it. A paragraph somebody
        # has already scrolled past is one they stop reading.
        _, out = self.setup_cli("--email", "ngoc@aeris.net", "--token", "issued")
        self.assertEqual(out.count("already on the server whitelist"), 1)

    def test_a_minted_secret_still_asks_for_the_line(self):
        self.init()
        _, out = self.setup_cli("--email", "ngoc@aeris.net")
        self.assertIn("Send this line", out)

    def test_the_endpoint_defaults_to_the_real_one(self):
        self.init()
        self.setup_cli("--email", "ngoc@aeris.net", "--token", "issued")
        self.assertEqual(self.insight.load_config()["endpoint"],
                         self.insight.SETA_ENDPOINT)


class TestAllowList(ClientTestCase):

    def test_an_attribute_outside_the_allow_list_is_reported(self):
        event = {"event_type": "scm.commit",
                 "attributes": {"commit_sha": "abc", "prompt_text": "secret"}}
        problems = self.insight.check_allowed(event)
        self.assertEqual(len(problems), 1)
        self.assertIn("prompt_text", problems[0])

    def test_an_unknown_event_type_is_rejected(self):
        problems = self.insight.check_allowed(
            {"event_type": "run.invented", "attributes": {}})
        self.assertEqual(len(problems), 1)
        self.assertIn("contract enum", problems[0])

    def test_a_conforming_event_passes(self):
        self.assertEqual(self.insight.check_allowed({
            "event_type": "scm.commit",
            "attributes": {"commit_sha": "abc", "lines_added": 1,
                           "lines_removed": 0, "has_ai_marker": False},
        }), [])

    def test_collect_does_not_store_events_that_fail_the_allow_list(self):
        self.init()
        source = os.path.join(self.home, "emit")
        os.makedirs(source)
        with open(os.path.join(source, "a.ndjson"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "event_id": "evt_bad", "event_type": "scm.commit",
                "event_time": "2026-08-01T00:00:00Z",
                "attributes": {"commit_sha": "x", "source_code": "print(1)"},
            }) + "\n")
        self.run_cli("collect", "--source", source)
        stored = json.dumps(self.insight.read_buffer())
        self.assertNotIn("source_code", stored)
        self.assertNotIn("print(1)", stored)


class TestBuffer(ClientTestCase):

    def _event(self, event_id):
        return {"event_id": event_id, "event_type": "scm.commit",
                "event_time": "2026-08-01T00:00:00Z", "attributes": {}}

    def test_the_same_event_is_not_stored_twice(self):
        # event_id is deterministic for anything re-readable, so re-running a
        # scan over the same window is expected rather than exceptional.
        self.init()
        self.insight.append_events([self._event("evt_1")])
        written, duplicates = self.insight.append_events(
            [self._event("evt_1"), self._event("evt_2")])
        self.assertEqual((written, duplicates), (1, 1))
        self.assertEqual(len(self.insight.read_buffer()), 2)


class TestPack(ClientTestCase):

    def test_the_manifest_checksum_covers_the_events(self):
        self.init()
        self.insight.append_events([
            {"event_id": "evt_1", "event_type": "scm.commit",
             "event_time": "2026-08-01T00:00:00Z", "attributes": {}}])
        self.run_cli("pack")
        bundle = self._latest_bundle()
        with open(bundle, "r", encoding="utf-8") as handle:
            manifest = json.loads(handle.readline())["_manifest"]
            body = handle.read()
        import hashlib
        self.assertEqual(
            manifest["sha256"], hashlib.sha256(body.encode("utf-8")).hexdigest())

    def test_a_bundle_is_self_describing(self):
        # A bundle found on disk in six months must be readable without the
        # tool that produced it.
        self.init()
        self.run_cli("pack")
        with open(self._latest_bundle(), "r", encoding="utf-8") as handle:
            manifest = json.loads(handle.readline())["_manifest"]
        for key in ("format", "schema_version", "machine_id", "packed_at",
                    "window_start", "window_end", "event_count", "sha256"):
            self.assertIn(key, manifest)

    def test_an_empty_week_still_produces_a_bundle(self):
        # A week with no activity is a measured zero. A week with no bundle is
        # missing data. Reports have to be able to tell those apart, which they
        # cannot do if a quiet week produces nothing at all.
        self.init()
        self.run_cli("pack", "--window-start", "2026-08-01T00:00:00Z",
                     "--window-end", "2026-08-07T00:00:00Z")
        with open(self._latest_bundle(), "r", encoding="utf-8") as handle:
            manifest = json.loads(handle.readline())["_manifest"]
        self.assertEqual(manifest["event_count"], 0)
        self.assertEqual(manifest["window_start"], "2026-08-01T00:00:00Z")
        self.assertEqual(manifest["window_end"], "2026-08-07T00:00:00Z")

    def _latest_bundle(self):
        names = sorted(os.listdir(self.insight.REPORTS_DIR))
        return os.path.join(self.insight.REPORTS_DIR, names[-1])


class TestScan(ClientTestCase):
    """Runs against a real throwaway repository -- git's output format is the
    thing under test as much as the parsing is."""

    def setUp(self):
        super().setUp()
        self.repo = tempfile.mkdtemp(prefix="insight-repo-")
        self.addCleanup(shutil.rmtree, self.repo, True)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "dev@example.com")
        self._git("config", "user.name", "Dev")

    def _git(self, *args):
        subprocess.run(["git", "-C", self.repo] + list(args),
                       check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)

    def _commit(self, message, filename="a.txt", body="x\n"):
        target = os.path.join(self.repo, filename)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(body)
        self._git("add", ".")
        self._git("commit", "-q", "-m", message)

    def commits(self, events):
        """Only the scm.commit events.

        scan_commits also returns one output.generated per file an AI-marked
        commit touched, so indexing by position couples a test to the order two
        different kinds of event happen to be appended in.
        """
        return [e for e in events if e["event_type"] == "scm.commit"]

    def test_commit_subjects_are_never_emitted(self):
        # The subject is read to classify and then discarded. Anything else
        # would put content in the store, which CONTRACT.md forbids outright.
        self.init()
        self._commit("[AUTH_BY_COPILOT] [PRJ-1] a very distinctive subject line")
        events = self.insight.scan_commits(self.repo, 3650, self.insight.load_config())
        self.assertNotIn("very distinctive subject", json.dumps(events))

    def test_both_commit_markers_are_recognised(self):
        self.init()
        self._commit("[AUTH_BY_COPILOT] [PRJ-1] one", filename="a.txt")
        self._commit("[GEN_BY_COPILOT] [PRJ-2] two", filename="b.txt")
        self._commit("chore: three", filename="c.txt")
        events = self.commits(
            self.insight.scan_commits(self.repo, 3650, self.insight.load_config()))
        self.assertEqual(
            sum(1 for e in events if e["attributes"]["has_ai_marker"]), 2)

    def test_an_ai_run_id_trailer_produces_an_explicit_link(self):
        # A marker says AI was involved; only the trailer says which run. They
        # are not the same claim and must not carry the same confidence.
        self.init()
        self._commit("[AUTH_BY_COPILOT] [PRJ-3] with trailer\n\nAI-Run-Id: run_abc123")
        events = self.commits(
            self.insight.scan_commits(self.repo, 3650, self.insight.load_config()))
        self.assertEqual(events[0]["run_id"], "run_abc123")
        self.assertEqual(events[0]["link"]["method"], "explicit")

    def test_a_marker_without_a_trailer_is_only_a_heuristic(self):
        self.init()
        self._commit("[AUTH_BY_COPILOT] [PRJ-4] no trailer")
        events = self.commits(
            self.insight.scan_commits(self.repo, 3650, self.insight.load_config()))
        self.assertIsNone(events[0]["run_id"])
        self.assertEqual(events[0]["link"]["method"], "marker_only")

    def test_the_author_email_is_hashed(self):
        self.init()
        self._commit("chore: one")
        events = self.commits(
            self.insight.scan_commits(self.repo, 3650, self.insight.load_config()))
        self.assertNotIn("dev@example.com", json.dumps(events))
        self.assertTrue(events[0]["actor"]["person_email_hash"])

    def test_line_counts_are_collected(self):
        self.init()
        self._commit("chore: three lines", body="1\n2\n3\n")
        events = self.commits(
            self.insight.scan_commits(self.repo, 3650, self.insight.load_config()))
        self.assertEqual(events[0]["attributes"]["lines_added"], 3)


class TestPurge(ClientTestCase):

    def test_purge_removes_events_and_bundles(self):
        # Someone who cannot delete their own telemetry has not consented to it.
        self.init()
        self.insight.append_events([
            {"event_id": "evt_1", "event_type": "scm.commit",
             "event_time": "2026-08-01T00:00:00Z", "attributes": {}}])
        self.run_cli("pack")
        self.run_cli("purge", "--yes")
        self.assertEqual(self.insight.read_buffer(), [])
        self.assertEqual(os.listdir(self.insight.REPORTS_DIR), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestHook(ClientTestCase):
    """The hook runs inside somebody's commit. Everything here is about it
    doing nothing rather than doing something wrong."""

    def setUp(self):
        super().setUp()
        self.init()
        self.repo = tempfile.mkdtemp(prefix="insight-hook-")
        self.addCleanup(shutil.rmtree, self.repo, True)
        subprocess.run(["git", "-C", self.repo, "init", "-q", "-b", "main"],
                       check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        self.emit = os.path.join(self.home, "aiep-buffer")
        os.makedirs(self.emit)

    def hook_path(self):
        return os.path.join(self.repo, ".git", "hooks", "prepare-commit-msg")

    def install(self, *extra):
        return self.run_cli("install-hook", "--repo", self.repo, *extra)

    def run_hook(self, message, source=None):
        """Invoke the hook the way git does, pointed at a test buffer."""
        path = os.path.join(self.home, "COMMIT_EDITMSG")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(message)
        script = self.hook_path()
        patched = os.path.join(self.home, "hook.py")
        with open(script, "r", encoding="utf-8") as handle:
            body = handle.read()
        body = body.replace(
            'BUFFER = os.path.join(os.path.expanduser("~"), ".aiep", "telemetry")',
            'BUFFER = {!r}'.format(self.emit))
        with open(patched, "w", encoding="utf-8") as handle:
            handle.write(body)
        argv = [sys.executable, patched, path] + ([source] if source else [])
        result = subprocess.run(argv, capture_output=True)
        with open(path, "r", encoding="utf-8") as handle:
            return result.returncode, handle.read()

    def write_run(self, *events):
        with open(os.path.join(self.emit, "a.ndjson"), "w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event) + "\n")

    def _run_started(self, run_id="run_abc", agent="developer.implementer"):
        return {"event_type": "run.started", "run_id": run_id,
                "trace_id": "trc_1", "event_time": "2026-08-24T09:00:00Z",
                "agent": {"agent_name": agent},
                "attributes": {"model_declared_id": "claude-opus-5"}}

    def test_install_refuses_to_clobber_someone_elses_hook(self):
        # Overwriting a hook silently would break whatever it does, and this is
        # not important enough to cost anyone that.
        os.makedirs(os.path.dirname(self.hook_path()), exist_ok=True)
        with open(self.hook_path(), "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho theirs\n")
        with self.assertRaises(SystemExit):
            self.install()
        self.install("--force")
        with open(self.hook_path(), "r", encoding="utf-8") as handle:
            self.assertIn("AI-Run-Id", handle.read())

    def test_installing_twice_is_not_an_error(self):
        self.install()
        _, output = self.install()
        self.assertIn("already installed", output)

    def test_an_open_run_is_stamped_onto_the_message(self):
        self.install()
        self.write_run(self._run_started())
        code, message = self.run_hook("[AUTH_BY_COPILOT] [PRJ-1] add a route\n")
        self.assertEqual(code, 0)
        self.assertIn("AI-Run-Id: run_abc", message)
        self.assertIn("AI-Agent: developer.implementer", message)
        self.assertIn("AI-Model: claude-opus-5", message)

    def test_the_subject_line_is_left_alone(self):
        # The subject carries the provenance markers other tooling parses.
        self.install()
        self.write_run(self._run_started())
        _, message = self.run_hook("[AUTH_BY_COPILOT] [PRJ-1] add a route\n")
        self.assertEqual(message.splitlines()[0],
                         "[AUTH_BY_COPILOT] [PRJ-1] add a route")

    def test_a_closed_run_is_not_stamped(self):
        self.install()
        self.write_run(
            self._run_started(),
            {"event_type": "run.completed", "run_id": "run_abc",
             "event_time": "2026-08-24T09:30:00Z"})
        _, message = self.run_hook("chore: something\n")
        self.assertNotIn("AI-Run-Id", message)

    def test_no_run_means_no_trailer_rather_than_an_invented_one(self):
        # A fabricated run id would manufacture a join key and put a human
        # commit's cost on an agent's account.
        self.install()
        _, message = self.run_hook("chore: hand written\n")
        self.assertNotIn("AI-Run-Id", message)

    def test_a_merge_commit_is_left_alone(self):
        self.install()
        self.write_run(self._run_started())
        _, message = self.run_hook("Merge branch 'x'\n", source="merge")
        self.assertNotIn("AI-Run-Id", message)

    def test_stamping_is_not_repeated_on_amend(self):
        self.install()
        self.write_run(self._run_started())
        _, once = self.run_hook("chore: one\n")
        path = os.path.join(self.home, "COMMIT_EDITMSG")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(once)
        _, twice = self.run_hook(once)
        self.assertEqual(twice.count("AI-Run-Id:"), 1)

    def test_a_corrupt_buffer_never_fails_the_commit(self):
        # Telemetry that can block a commit gets deleted within the week.
        self.install()
        with open(os.path.join(self.emit, "a.ndjson"), "w", encoding="utf-8") as handle:
            handle.write("{not json at all\n")
        code, message = self.run_hook("chore: one\n")
        self.assertEqual(code, 0)
        self.assertEqual(message, "chore: one\n")


class TestOutputs(TestScan):
    """Artifacts are derived from git, not from the agent -- the agents emit
    run.started and run.completed and nothing between."""

    def outputs(self, events):
        return [e for e in events if e["event_type"] == "output.generated"]

    def test_an_ai_marked_commit_yields_one_output_per_file(self):
        self.init()
        self._commit("[AUTH_BY_COPILOT] [PRJ-1] two files", filename="a.py")
        self._commit("[AUTH_BY_COPILOT] [PRJ-1] and a test",
                     filename="tests/test_a.py")
        outs = self.outputs(
            self.insight.scan_commits(self.repo, 3650, self.insight.load_config()))
        self.assertEqual(len(outs), 2)
        self.assertEqual({o["attributes"]["file_path"] for o in outs},
                         {"a.py", "tests/test_a.py"})

    def test_a_human_commit_yields_no_artifacts(self):
        # Emitting these for every commit would inflate "AI output" with work
        # AI never touched.
        self.init()
        self._commit("chore: written by hand")
        self.assertEqual(self.outputs(
            self.insight.scan_commits(self.repo, 3650, self.insight.load_config())), [])

    def test_artifact_type_comes_from_the_path(self):
        self.init()
        self._commit("[AUTH_BY_COPILOT] [PRJ-1] a test", filename="tests/test_x.py")
        self._commit("[AUTH_BY_COPILOT] [PRJ-1] a doc", filename="README.md")
        self._commit("[AUTH_BY_COPILOT] [PRJ-1] code", filename="lib/thing.py")
        types = {o["attributes"]["file_path"]: o["attributes"]["artifact_type"]
                 for o in self.outputs(self.insight.scan_commits(
                     self.repo, 3650, self.insight.load_config()))}
        self.assertEqual(types["tests/test_x.py"], "test")
        self.assertEqual(types["README.md"], "doc")
        self.assertEqual(types["lib/thing.py"], "code")

    def test_outputs_from_a_trailered_commit_carry_the_run_id(self):
        self.init()
        self._commit("[AUTH_BY_COPILOT] [PRJ-2] with trailer\n\nAI-Run-Id: run_xyz")
        outs = self.outputs(
            self.insight.scan_commits(self.repo, 3650, self.insight.load_config()))
        self.assertEqual(outs[0]["run_id"], "run_xyz")
        self.assertEqual(outs[0]["link"]["method"], "explicit")

    def test_every_output_passes_the_allow_list(self):
        self.init()
        self._commit("[AUTH_BY_COPILOT] [PRJ-1] a file")
        for out in self.outputs(self.insight.scan_commits(
                self.repo, 3650, self.insight.load_config())):
            self.assertEqual(self.insight.check_allowed(out), [])


class TestPartitioning(ClientTestCase):
    """The day an event belongs to is the day it happened."""

    def _event(self, event_id, at):
        return {"event_id": event_id, "event_type": "scm.commit",
                "event_time": at, "attributes": {}}

    def test_events_land_in_the_day_they_happened(self):
        # A span produced on Friday and collected on Monday belongs in Friday.
        # Filing it under Monday moves work between weeks -- the totals stay
        # right and every week is wrong.
        self.init()
        self.insight.append_events([
            self._event("evt_1", "2026-08-21T09:00:00Z"),
            self._event("evt_2", "2026-08-24T09:00:00Z"),
        ])
        days = sorted(f for f in os.listdir(self.insight.BUFFER_DIR)
                      if f.endswith(".ndjson"))
        self.assertEqual(days, ["2026-08-21.ndjson", "2026-08-24.ndjson"])

    def test_one_call_may_write_several_partitions(self):
        self.init()
        written, _ = self.insight.append_events([
            self._event("evt_1", "2026-08-21T09:00:00Z"),
            self._event("evt_2", "2026-08-22T09:00:00Z"),
            self._event("evt_3", "2026-08-21T18:00:00Z"),
        ])
        self.assertEqual(written, 3)
        self.assertEqual(len(self.insight.read_buffer()), 3)

    def test_an_event_with_no_timestamp_falls_back_to_today(self):
        # Rather than being dropped or guessed at.
        self.init()
        self.insight.append_events([
            {"event_id": "evt_x", "event_type": "scm.commit",
             "event_time": None, "attributes": {}}])
        self.assertEqual(len(self.insight.read_buffer()), 1)

    def test_dedup_still_spans_partitions(self):
        self.init()
        self.insight.append_events([self._event("evt_1", "2026-08-21T09:00:00Z")])
        written, dupes = self.insight.append_events(
            [self._event("evt_1", "2026-08-24T09:00:00Z")])
        self.assertEqual((written, dupes), (0, 1))


class TestOtelCommand(ClientTestCase):

    def spans_file(self, spans):
        path = os.path.join(self.home, "copilot-spans.jsonl")
        payload = {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
        return path

    def a_span(self, name="chat gpt-5.3-codex", tokens=100, leak=False):
        attrs = [
            {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
            {"key": "gen_ai.response.model", "value": {"stringValue": "gpt-5.3-codex"}},
            {"key": "gen_ai.usage.input_tokens", "value": {"intValue": str(tokens)}},
            {"key": "gen_ai.conversation.id", "value": {"stringValue": "conv_1"}},
        ]
        if leak:
            attrs.append({"key": "gen_ai.input.messages",
                          "value": {"stringValue": "a very secret prompt"}})
        return {"name": name, "spanId": "s1",
                "startTimeUnixNano": "1787000000000000000",
                "endTimeUnixNano": "1787000001000000000",
                "attributes": attrs}

    def test_a_missing_span_file_is_reported_not_an_error(self):
        self.init()
        code, output = self.run_cli("otel", "--source",
                                    os.path.join(self.home, "nope.jsonl"))
        self.assertEqual(code, 0)
        self.assertIn('"present": false', output)

    def test_spans_become_events_and_the_source_is_truncated(self):
        # The raw file can hold prompts; keeping copies multiplies that
        # exposure for no gain once the events exist.
        self.init()
        path = self.spans_file([self.a_span()])
        self.run_cli("otel", "--source", path)
        self.assertEqual(os.path.getsize(path), 0)
        self.assertEqual(len(self.insight.read_buffer()), 1)

    def test_keep_raw_leaves_the_file_alone(self):
        self.init()
        path = self.spans_file([self.a_span()])
        self.run_cli("otel", "--source", path, "--keep-raw")
        self.assertGreater(os.path.getsize(path), 0)

    def test_content_never_reaches_the_buffer(self):
        self.init()
        path = self.spans_file([self.a_span(leak=True)])
        self.run_cli("otel", "--source", path)
        self.assertNotIn("a very secret prompt",
                         json.dumps(self.insight.read_buffer()))


class TestPackWindow(ClientTestCase):
    """Packing a week is the reason the buffer is partitioned by day."""

    def _event(self, event_id, at):
        return {"event_id": event_id, "event_type": "scm.commit",
                "event_time": at, "attributes": {}}

    def setUp(self):
        super().setUp()
        self.init()
        self.insight.append_events([
            self._event("evt_mon", "2026-08-17T09:00:00Z"),
            self._event("evt_wed", "2026-08-19T09:00:00Z"),
            self._event("evt_next", "2026-08-25T09:00:00Z"),
        ])

    def manifest(self):
        names = sorted(os.listdir(self.insight.REPORTS_DIR))
        with open(os.path.join(self.insight.REPORTS_DIR, names[-1]),
                  "r", encoding="utf-8") as handle:
            return json.loads(handle.readline())["_manifest"]

    def test_a_window_packs_only_its_days(self):
        self.run_cli("pack", "--since", "2026-08-17", "--until", "2026-08-23")
        manifest = self.manifest()
        self.assertEqual(manifest["event_count"], 2)
        self.assertEqual(manifest["days_covered"], ["2026-08-17", "2026-08-19"])

    def test_the_requested_window_is_declared_even_when_empty(self):
        # "That week was quiet" and "nobody sent that week" are opposite
        # findings, and only the person packing knows which window they meant.
        self.run_cli("pack", "--since", "2026-07-01", "--until", "2026-07-07")
        manifest = self.manifest()
        self.assertEqual(manifest["event_count"], 0)
        self.assertEqual(manifest["window_start"], "2026-07-01T00:00:00Z")
        self.assertEqual(manifest["window_end"], "2026-07-07T23:59:59Z")

    def test_clear_removes_only_the_packed_partitions(self):
        # Clearing everything would throw away days the bundle does not cover.
        self.run_cli("pack", "--since", "2026-08-17", "--until", "2026-08-23",
                     "--clear")
        self.assertEqual(self.insight.buffer_days(), ["2026-08-25"])
        self.assertEqual(len(self.insight.read_buffer()), 1)

    def test_status_lists_the_days_held(self):
        _, output = self.run_cli("status")
        self.assertEqual(json.loads(output)["days_buffered"],
                         ["2026-08-17", "2026-08-19", "2026-08-25"])


class TestMultipleRepos(TestScan):
    """An engineer with four repositories should not have to remember four
    commands — the one they forget is the one that silently reports nothing."""

    def second_repo(self):
        other = tempfile.mkdtemp(prefix="insight-repo2-")
        self.addCleanup(shutil.rmtree, other, True)
        subprocess.run(["git", "-C", other, "init", "-q", "-b", "main"],
                       check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        for args in (("config", "user.email", "dev@example.com"),
                     ("config", "user.name", "Dev")):
            subprocess.run(["git", "-C", other] + list(args), check=True)
        with open(os.path.join(other, "b.txt"), "w", encoding="utf-8") as handle:
            handle.write("x\n")
        subprocess.run(["git", "-C", other, "add", "."], check=True)
        subprocess.run(["git", "-C", other, "commit", "-q", "-m",
                        "[AUTH_BY_COPILOT] [PRJ-9] second repo"], check=True)
        return other

    def test_scanning_a_repo_remembers_it(self):
        self.init()
        self._commit("chore: one")
        self.run_cli("scan", "--repo", self.repo, "--since-days", "3650")
        self.assertIn(self.repo, self.insight.known_repos())

    def test_installing_a_hook_remembers_the_repo(self):
        self.init()
        self.run_cli("install-hook", "--repo", self.repo)
        self.assertIn(self.repo, self.insight.known_repos())

    def test_scan_with_no_repo_covers_every_registered_one(self):
        self.init()
        self._commit("[AUTH_BY_COPILOT] [PRJ-1] first repo")
        other = self.second_repo()
        self.run_cli("scan", "--repo", self.repo, "--since-days", "3650")
        self.run_cli("scan", "--repo", other, "--since-days", "3650")

        before = len(self.insight.read_buffer())
        code, output = self.run_cli("scan", "--since-days", "3650")
        self.assertEqual(code, 0)
        self.assertEqual(len(output.strip().splitlines()), 2)
        # Same commits, so nothing new -- the point is that both were visited.
        self.assertEqual(len(self.insight.read_buffer()), before)

    def test_a_repo_is_not_registered_twice(self):
        self.init()
        self.run_cli("install-hook", "--repo", self.repo)
        self.run_cli("scan", "--repo", self.repo, "--since-days", "3650")
        self.assertEqual(self.insight.known_repos().count(self.repo), 1)

    def test_one_missing_clone_does_not_stop_the_others(self):
        # A machine where somebody deleted a clone should still report the rest.
        self.init()
        self._commit("chore: one")
        gone = self.second_repo()
        self.run_cli("scan", "--repo", self.repo, "--since-days", "3650")
        self.run_cli("scan", "--repo", gone, "--since-days", "3650")
        shutil.rmtree(gone)

        code, output = self.run_cli("scan", "--since-days", "3650")
        lines = [json.loads(l) for l in output.strip().splitlines()]
        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 2)
        self.assertTrue(any("error" in l for l in lines))
        self.assertTrue(any("commits" in l for l in lines))

    def test_scan_with_nothing_registered_explains_itself(self):
        self.init()
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("scan", "--since-days", "3650")
        self.assertIn("no repositories registered", str(ctx.exception))

    def test_init_keeps_the_repo_list(self):
        self.init()
        self.run_cli("install-hook", "--repo", self.repo)
        self.run_cli("init", "--yes", "--force")
        self.assertIn(self.repo, self.insight.known_repos())
