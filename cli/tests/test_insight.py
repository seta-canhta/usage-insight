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


class TestConsentTextDescribesReality(ClientTestCase):
    """The consent record has to say what is actually collected.

    It drifted once already: it went on naming "latency" for a fortnight after
    the source stopped carrying it, and gained premium requests, tool outcomes
    and gate verdicts without saying so. A consent text that describes the
    wrong thing is not a weaker consent record, it is a false one -- so the
    drift is held down by a test rather than by remembering.
    """

    def flowed(self):
        """The text with its display wrapping collapsed.

        It is hard-wrapped for a terminal, so a phrase can straddle a newline.
        Asserting against the wrapping would make a reflow look like a change
        in what is collected.
        """
        return " ".join(self.insight.CONSENT_TEXT.lower().split())

    def test_it_names_what_the_reader_actually_emits(self):
        text = self.flowed()
        for phrase in ("premium requests", "token counts", "model ids",
                       "quality gates", "commit hashes"):
            self.assertIn(phrase, text)

    def test_it_does_not_promise_what_the_source_no_longer_carries(self):
        # `latency_ms` is NULL except on single-model sessions, and per-call
        # latency is gone entirely with the span source.
        self.assertNotIn("latency", self.flowed())

    def test_it_names_the_surfaces_it_cannot_see(self):
        # Every read publishes `surfaces_not_covered`; the person agreeing to
        # collection is the one who most needs to know the Chat panel is not in
        # it, or absence of measurement reads to them as absence of work.
        text = self.flowed()
        self.assertIn("chat", text)
        self.assertIn("inline completions", text)

    def test_it_says_the_journal_is_not_altered(self):
        # The command this replaced truncated its source on every run. Somebody
        # agreeing to have ~/.copilot read is entitled to know it stays intact.
        self.assertIn("never alters or deletes", self.flowed())

    def test_it_is_shown_before_the_question(self):
        _, out = self.run_cli("init", "--email", "ngoc@aeris.net", "--yes")
        self.assertIn("This collects, from this machine", out)


class TestSecretsAreMintedHereOnly(ClientTestCase):
    """There used to be `init --token`, which adopted a secret issued by
    whoever runs the server.

    It existed for pre-provisioning: adding somebody to the whitelist before
    their laptop had been touched, so the secret had to exist before the config
    file did. It is gone, and its absence is the property under test. That path
    put the live secret through whatever channel carried it -- Slack, usually --
    which `cli/identity.py` names as the reason the direction was granted only
    as an exception. Removing the exception means every secret on every machine
    is one that has never travelled.
    """

    def test_a_secret_is_minted_locally(self):
        self.run_cli("init", "--yes", "--email", "ngoc@aeris.net")
        token = self.insight.load_config()["endpoint_token"]
        self.assertTrue(token)

    def test_two_machines_do_not_share_a_secret(self):
        self.run_cli("init", "--yes", "--email", "ngoc@aeris.net")
        first = self.insight.load_config()["endpoint_token"]
        self.setUp()
        self.run_cli("init", "--yes", "--email", "ngoc@aeris.net")
        self.assertNotEqual(self.insight.load_config()["endpoint_token"], first)

    def test_the_whitelist_line_is_always_asked_for(self):
        # There is no longer a case where the server already has the line, so
        # the paragraph that used to say "you are already on the whitelist" is
        # gone too. One direction, one message.
        _, out = self.run_cli("init", "--yes", "--email", "ngoc@aeris.net")
        self.assertIn("Send this line", out)
        self.assertNotIn("already on the server whitelist", out)

    def test_the_printed_line_matches_the_stored_secret(self):
        import identity
        _, out = self.run_cli("init", "--yes", "--email", "ngoc@aeris.net")
        self.assertIn(
            identity.fingerprint(self.insight.load_config()["endpoint_token"]),
            out)

    def test_init_rejects_an_issued_token(self):
        # Not silently ignored -- argparse refuses the flag, so anyone with the
        # old command in their notes gets told rather than getting a machine
        # whose fingerprint is on nobody's list.
        with self.assertRaises(SystemExit):
            self.run_cli("init", "--yes", "--email", "ngoc@aeris.net",
                         "--token", "issued-by-the-admin")

    def test_rotation_still_keeps_the_old_secret_valid(self):
        # Rotation is unrelated to adoption and still needs the two-secret
        # window, or a machine stops uploading the moment it rotates.
        self.run_cli("init", "--yes", "--email", "ngoc@aeris.net")
        first = self.insight.load_config()["endpoint_token"]
        self.run_cli("rotate-token")
        config = self.insight.load_config()
        self.assertNotEqual(config["endpoint_token"], first)
        self.assertEqual(config["endpoint_token_previous"], first)


class TestSetup(ClientTestCase):
    """`setup` on a machine that has been collecting for weeks.

    It used to adopt a secret issued by the server admin (`--token`) and to
    take a repeated `--repo`. Both are gone: secrets are only ever minted here,
    and repositories are discovered from Copilot's own session journals. What
    is left is a wizard, and these tests drive its non-interactive form.
    """

    def setUp(self):
        super().setUp()
        import vscode_setup
        vscode_setup.settings_path = lambda: None
        # Point discovery away from the developer's real ~/.copilot so the
        # result does not depend on whose laptop the suite runs on.
        self.insight.COPILOT_ROOT = os.path.join(self.home, "no-copilot")

    def setup_cli(self, *extra):
        return self.run_cli("setup", "--yes", "--no-schedule", *extra)

    def test_it_mints_a_secret_and_asks_for_the_line(self):
        self.init()
        _, out = self.setup_cli("--email", "ngoc@aeris.net")
        self.assertTrue(self.insight.load_config()["endpoint_token"])
        self.assertIn("Send this line", out)

    def test_nothing_already_collected_is_disturbed(self):
        self.init()
        before = self.insight.load_config()
        self.setup_cli("--email", "ngoc@aeris.net")
        config = self.insight.load_config()
        self.assertEqual(config["email"], "ngoc@aeris.net")
        self.assertEqual(config["salt"], before["salt"])
        self.assertEqual(config["machine_id"], before["machine_id"])

    def test_running_it_twice_does_not_shuffle_the_secrets(self):
        # `setup` is the command people re-run when they are not sure it
        # worked, and a second run must not push the live secret into
        # `previous` and leave nothing valid.
        self.init()
        self.setup_cli("--email", "ngoc@aeris.net")
        first = self.insight.load_config()["endpoint_token"]
        self.setup_cli("--email", "ngoc@aeris.net")
        config = self.insight.load_config()
        self.assertEqual(config["endpoint_token"], first)
        self.assertIsNone(config.get("endpoint_token_previous"))

    def test_the_paragraph_is_printed_once(self):
        # `setup` runs `init`, and both used to print it. A paragraph somebody
        # has already scrolled past is one they stop reading.
        _, out = self.setup_cli("--email", "ngoc@aeris.net")
        self.assertEqual(out.count("Send this line"), 1)

    def test_it_refuses_an_issued_token(self):
        self.init()
        with self.assertRaises(SystemExit):
            self.setup_cli("--email", "ngoc@aeris.net", "--token", "issued")

    def test_it_refuses_a_repo_flag(self):
        # Repositories are discovered, not declared. Someone with the old
        # command in their notes gets told rather than getting a setup that
        # quietly ignored half of what they typed.
        self.init()
        with self.assertRaises(SystemExit):
            self.setup_cli("--email", "ngoc@aeris.net", "--repo", self.home)

    def test_it_does_not_complain_about_unregistered_repositories(self):
        # It used to end in `[ FAIL ] repos` and a non-zero exit whenever none
        # were registered. Nothing is registered any more, so that step would
        # fail on every single run.
        self.init()
        code, out = self.setup_cli("--email", "ngoc@aeris.net")
        self.assertEqual(code, 0)
        self.assertNotIn("No repository is registered", out)
        self.assertNotIn("FAIL ] repos", out)

    def test_it_reports_what_discovery_found(self):
        self.init()
        _, out = self.setup_cli("--email", "ngoc@aeris.net")
        self.assertIn("repos", out)

    def test_the_endpoint_defaults_to_the_real_one(self):
        self.init()
        self.setup_cli("--email", "ngoc@aeris.net")
        self.assertEqual(self.insight.load_config()["endpoint"],
                         self.insight.SETA_ENDPOINT)

    def test_it_removes_the_span_file_the_retired_exporter_wrote(self):
        # Removing the settings without removing what they produced leaves a
        # document of somebody's work in a directory nobody looks at. That file
        # exists only because this tool asked for it, so this tool clears it.
        self.init()
        spans = os.path.join(self.home, "copilot-spans.jsonl")
        with open(spans, "w", encoding="utf-8") as handle:
            handle.write('{"resourceSpans": []}\n')
        _, out = self.setup_cli("--email", "ngoc@aeris.net")
        self.assertFalse(os.path.exists(spans))
        self.assertIn("retired span file", out)

    def test_cleanup_is_silent_when_there_is_nothing_to_clean(self):
        self.init()
        _, out = self.setup_cli("--email", "ngoc@aeris.net")
        self.assertNotIn("retired span file", out)

    def test_the_wizard_refuses_to_guess_without_a_terminal(self):
        # `input()` on a closed stdin used to raise EOFError and traceback.
        # With a wizard as the normal path -- and the installer telling people
        # to run `setup` right after a `curl | sh` -- that is one piped command
        # away.
        self.init()
        with self.assertRaises(SystemExit) as caught:
            self.run_cli("setup", "--no-schedule")
        self.assertIn("needs a terminal", str(caught.exception))


class TestPackDeclaresSources(ClientTestCase):
    """What the machine was in a position to measure, recorded in the bundle.

    Without it, an empty bundle from a machine that was never finished being
    set up is indistinguishable from a genuinely quiet day, and gets averaged
    in as a zero.
    """

    def manifest(self):
        import ship as ship_mod
        _, out = self.run_cli("pack")
        return ship_mod.read_manifest(json.loads(out)["bundle"])

    def test_a_fresh_machine_declares_that_it_has_nothing(self):
        self.init()
        # Pointed away from the developer's own ~/.copilot: the whole point of
        # this flag is that it reports the machine, so a test that reads the
        # real one passes or fails depending on whose laptop it runs on.
        self.insight.COPILOT_ROOT = os.path.join(self.home, "no-copilot-here")
        sources = self.manifest()["sources"]
        self.assertEqual(sources["repos"], 0)
        self.assertFalse(sources["copilot"])

    def test_a_machine_that_has_run_copilot_says_so(self):
        self.init()
        root = os.path.join(self.home, "copilot", "session-state")
        os.makedirs(root, exist_ok=True)
        self.insight.COPILOT_ROOT = os.path.dirname(root)
        self.assertTrue(self.manifest()["sources"]["copilot"])

    def test_a_registered_repository_is_counted(self):
        self.init()
        self.run_cli("scan", "--repo", os.path.dirname(
            os.path.dirname(os.path.abspath(self.insight.__file__))))
        self.assertEqual(self.manifest()["sources"]["repos"], 1)

    def test_it_carries_counts_and_flags_but_never_paths(self):
        # CONTRACT.md 1.1: the bundle is the thing that leaves the machine.
        self.init()
        self.run_cli("scan", "--repo", os.path.dirname(
            os.path.dirname(os.path.abspath(self.insight.__file__))))
        blob = json.dumps(self.manifest()["sources"])
        self.assertNotIn("/", blob)

    def test_declaring_sources_does_not_disturb_the_event_checksum(self):
        # `auto` dedupes on the manifest's sha256, which covers the events
        # alone. If this had moved it, every hourly run would upload again.
        self.init()
        first = self.manifest()["sha256"]
        self.assertEqual(self.manifest()["sha256"], first)


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


class TestCopilotCommand(ClientTestCase):
    """Reading Copilot's own session journals.

    The journal replaced an OTel span exporter that had to be configured per
    machine and silently collected nothing when it was not. These tests hold
    the three properties that made the switch worth making, plus the one rule
    the old command had that this one deliberately inverts.
    """

    def journal(self, records, session="sess-1"):
        directory = os.path.join(self.home, "copilot", "session-state", session)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "events.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for index, record in enumerate(records):
                record.setdefault("id", "rec-{}".format(index))
                record.setdefault("timestamp", "2026-08-20T09:00:0{}.000Z".format(index))
                handle.write(json.dumps(record) + "\n")
        return os.path.join(self.home, "copilot")

    def a_session(self, cwd="/home/someone/work/repo"):
        return {"type": "session.start", "data": {
            "sessionId": "sess-1", "selectedModel": "claude-sonnet-4.6",
            "context": {"repository": "acme/repo", "branch": "feat/PRJ-1-thing",
                        "gitRoot": cwd, "cwd": cwd}}}

    def a_shutdown(self, tokens=100):
        return {"type": "session.shutdown", "data": {
            "shutdownType": "routine", "totalApiDurationMs": 4200,
            "totalPremiumRequests": 3,
            "modelMetrics": {"claude-sonnet-4.6": {
                "requests": {"count": 7, "cost": 3},
                "usage": {"inputTokens": tokens, "outputTokens": 12,
                          "cacheReadTokens": 40, "cacheWriteTokens": 5,
                          "reasoningTokens": 0}}}}}

    def bound_rows(self):
        return [e for e in self.insight.read_buffer()
                if e["event_type"] == "run.bound"]

    def test_a_moved_tree_is_bound_by_its_commit_range(self):
        # The bridge CONTRACT.md §2.4 names, finally carrying evidence. The
        # session recorded these SHAs itself, so the link is `explicit`.
        self.init()
        start = self.a_session()
        start["data"]["context"]["baseCommit"] = "a" * 40
        start["data"]["context"]["headCommit"] = "b" * 40
        start["data"]["context"]["repositoryHost"] = "github.com"
        root = self.journal([start, self.a_shutdown()])
        self.run_cli("copilot", "--root", root)
        rows = self.bound_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attributes"]["base_commit_sha"], "a" * 40)
        self.assertEqual(rows[0]["attributes"]["head_commit_sha"], "b" * 40)
        self.assertEqual(rows[0]["attributes"]["repository_host"], "github.com")
        self.assertEqual(rows[0]["link"], {"method": "explicit", "confidence": 1.0})

    def test_a_session_that_committed_nothing_binds_nothing(self):
        # base == head is a session that read and did not write. An empty
        # range would have to be special-cased downstream into this same
        # answer, so it is not emitted.
        self.init()
        start = self.a_session()
        start["data"]["context"]["baseCommit"] = "a" * 40
        start["data"]["context"]["headCommit"] = "a" * 40
        root = self.journal([start, self.a_shutdown()])
        self.run_cli("copilot", "--root", root)
        self.assertEqual(self.bound_rows(), [])

    def test_a_branch_switch_does_not_merge_two_ranges_into_one(self):
        # Measured on real journals: a session starts on `main` and resumes on
        # a feature branch. First-base-to-last-head across that spans two
        # unrelated lines of work and would charge this session for every
        # commit in the gap. One range per branch instead.
        self.init()
        start = self.a_session()
        start["data"]["context"]["branch"] = "main"
        start["data"]["context"]["baseCommit"] = "1" * 40
        start["data"]["context"]["headCommit"] = "2" * 40
        resume = {"type": "session.resume", "data": {
            "sessionId": "sess-1",
            "context": {"repository": "acme/repo", "branch": "feat/thing",
                        "gitRoot": "/home/someone/work/repo",
                        "baseCommit": "3" * 40, "headCommit": "4" * 40}}}
        root = self.journal([start, resume, self.a_shutdown()])
        self.run_cli("copilot", "--root", root)
        ranges = {r["context"]["branch_name"]:
                  (r["attributes"]["base_commit_sha"],
                   r["attributes"]["head_commit_sha"])
                  for r in self.bound_rows()}
        self.assertEqual(ranges, {"main": ("1" * 40, "2" * 40),
                                  "feat/thing": ("3" * 40, "4" * 40)})

    def test_the_bound_row_publishes_no_jira_key_it_cannot_evidence(self):
        # `a_session` uses branch `feat/PRJ-1-thing`, so a key IS derivable
        # from the name -- and this row still reports NULL. Measured
        # 2026-08-26, 0 of 37 real branches carried one; a key that is right
        # by luck on a test fixture is not evidence (AR-1).
        self.init()
        start = self.a_session()
        start["data"]["context"]["baseCommit"] = "a" * 40
        start["data"]["context"]["headCommit"] = "b" * 40
        root = self.journal([start, self.a_shutdown()])
        self.run_cli("copilot", "--root", root)
        self.assertIsNone(self.bound_rows()[0]["attributes"]["jira_issue_key"])

    def test_a_missing_copilot_home_is_reported_not_an_error(self):
        # Copilot CLI may not be installed. That is a fact about the machine,
        # not a failure of this one.
        self.init()
        code, output = self.run_cli("copilot", "--root",
                                    os.path.join(self.home, "nope"))
        self.assertEqual(code, 0)
        self.assertIn('"present": false', output)

    def test_usage_becomes_a_model_call_carrying_the_billing_unit(self):
        self.init()
        root = self.journal([self.a_session(), self.a_shutdown()])
        self.run_cli("copilot", "--root", root)
        events = [e for e in self.insight.read_buffer()
                  if e["event_type"] == "model.call"]
        self.assertEqual(len(events), 1)
        attributes = events[0]["attributes"]
        self.assertEqual(attributes["input_tokens"], 100)
        self.assertEqual(attributes["cache_write_tokens"], 5)
        self.assertEqual(attributes["request_count"], 7)
        # The reason the whole source changed: Copilot bills per premium
        # request, and the span stream never carried the number.
        self.assertEqual(attributes["premium_requests"], 3)

    def test_the_journal_is_never_deleted(self):
        # The command this replaced truncated its source every run, correctly:
        # that file existed only because we asked for it. A session journal is
        # Copilot's own history of somebody's work and is not ours to clear.
        self.init()
        root = self.journal([self.a_session(), self.a_shutdown()])
        path = os.path.join(root, "session-state", "sess-1", "events.jsonl")
        before = os.path.getsize(path)
        self.run_cli("copilot", "--root", root)
        self.assertEqual(os.path.getsize(path), before)

    def test_reading_twice_buffers_once(self):
        # Which is what makes not deleting it affordable. A session stays open
        # for days and the scheduler reads it hourly.
        self.init()
        root = self.journal([self.a_session(), self.a_shutdown()])
        self.run_cli("copilot", "--root", root)
        first = len(self.insight.read_buffer())
        _, output = self.run_cli("copilot", "--root", root)
        self.assertEqual(len(self.insight.read_buffer()), first)
        self.assertIn('"written": 0', output)

    def test_content_never_reaches_the_buffer(self):
        self.init()
        root = self.journal([
            self.a_session(),
            {"type": "user.message", "data": {"content": "a very secret prompt"}},
            {"type": "assistant.message", "data": {
                "content": "a very secret reply", "outputTokens": 9,
                "model": "claude-sonnet-4.6"}},
            {"type": "tool.execution_start", "data": {
                "toolCallId": "t1", "toolName": "bash",
                "arguments": {"command": "pytest -q  # secret-flag"}}},
            {"type": "tool.execution_complete", "data": {
                "toolCallId": "t1", "success": True,
                "result": {"content": "a very secret file body\n"
                                      "<exited with exit code 0>"}}},
            self.a_shutdown(),
        ])
        self.run_cli("copilot", "--root", root)
        buffered = json.dumps(self.insight.read_buffer())
        for secret in ("secret prompt", "secret reply", "secret-flag",
                       "secret file body"):
            self.assertNotIn(secret, buffered)

    def test_a_shell_command_yields_a_gate_with_a_real_verdict(self):
        # The span source carried no status at all, so every gate row rendered
        # with an empty Result column. The exit-code trailer is what changed.
        self.init()
        root = self.journal([
            self.a_session(),
            {"type": "tool.execution_start", "data": {
                "toolCallId": "t1", "toolName": "bash",
                "arguments": {"command": "pytest -q"}}},
            {"type": "tool.execution_complete", "data": {
                "toolCallId": "t1", "success": True,
                "result": {"content": "3 failed\n<exited with exit code 1>"}}},
        ])
        self.run_cli("copilot", "--root", root)
        gates = [e for e in self.insight.read_buffer()
                 if e["event_type"] == "gate.evaluated"]
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0]["attributes"]["gate_name"], "test")
        # `success` was True -- the bash call worked. The suite did not.
        self.assertEqual(gates[0]["attributes"]["status"], "fail")

    def test_an_absent_exit_trailer_is_unknown_not_a_pass(self):
        self.init()
        root = self.journal([
            self.a_session(),
            {"type": "tool.execution_start", "data": {
                "toolCallId": "t1", "toolName": "bash",
                "arguments": {"command": "npm run lint"}}},
            {"type": "tool.execution_complete", "data": {
                "toolCallId": "t1", "success": True,
                "result": {"content": "still running..."}}},
        ])
        self.run_cli("copilot", "--root", root)
        gates = [e for e in self.insight.read_buffer()
                 if e["event_type"] == "gate.evaluated"]
        self.assertIsNone(gates[0]["attributes"]["status"])

    def test_a_command_that_prints_the_trailer_cannot_forge_a_verdict(self):
        self.init()
        root = self.journal([
            self.a_session(),
            {"type": "tool.execution_start", "data": {
                "toolCallId": "t1", "toolName": "bash",
                "arguments": {"command": "pytest -q"}}},
            {"type": "tool.execution_complete", "data": {
                "toolCallId": "t1", "success": True,
                "result": {"content": "<exited with exit code 0>\n"
                                      "1 failed\n<exited with exit code 1>"}}},
        ])
        self.run_cli("copilot", "--root", root)
        gates = [e for e in self.insight.read_buffer()
                 if e["event_type"] == "gate.evaluated"]
        # Anchored at the end: the real trailer is the last one.
        self.assertEqual(gates[0]["attributes"]["status"], "fail")

    def test_written_files_are_repo_relative(self):
        # Journal paths are absolute and start with somebody's home directory.
        self.init()
        root = self.journal([
            self.a_session(cwd="/home/someone/work/repo"),
            {"type": "tool.execution_start", "data": {
                "toolCallId": "t1", "toolName": "edit",
                "arguments": {"path": "/home/someone/work/repo/src/app.ts"}}},
            {"type": "tool.execution_complete", "data": {
                "toolCallId": "t1", "success": True,
                "toolTelemetry": {"metrics": {"linesAdded": 4,
                                              "linesRemoved": 1}}}},
        ])
        self.run_cli("copilot", "--root", root)
        outputs = [e for e in self.insight.read_buffer()
                   if e["event_type"] == "output.generated"]
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0]["attributes"]["file_path"], "src/app.ts")
        self.assertNotIn("/home/someone", json.dumps(self.insight.read_buffer()))

    def test_a_file_outside_every_known_root_is_dropped_not_truncated(self):
        self.init()
        root = self.journal([
            self.a_session(cwd="/home/someone/work/repo"),
            {"type": "tool.execution_start", "data": {
                "toolCallId": "t1", "toolName": "edit",
                "arguments": {"path": "/home/someone/.ssh/config"}}},
            {"type": "tool.execution_complete", "data": {
                "toolCallId": "t1", "success": True,
                "toolTelemetry": {"metrics": {"linesAdded": 1,
                                              "linesRemoved": 0}}}},
        ])
        self.run_cli("copilot", "--root", root)
        outputs = [e for e in self.insight.read_buffer()
                   if e["event_type"] == "output.generated"]
        self.assertEqual(outputs, [])

    def test_a_session_with_no_shutdown_is_named_not_counted_as_zero(self):
        self.init()
        root = self.journal([self.a_session()])   # no shutdown: still running
        _, output = self.run_cli("copilot", "--root", root)
        coverage = json.loads(output)["coverage"]
        self.assertEqual(coverage["sessions"], 1)
        self.assertEqual(coverage["sessions_without_usage"], 1)
        self.assertEqual(coverage["usage_coverage"], 0.0)
        # The surface this source cannot see is stated on every read, so that a
        # surface nobody measured never reads as a surface nobody used.
        self.assertIn("vscode-copilot-chat", coverage["surfaces_not_covered"])

    def test_a_session_activation_is_a_run(self):
        # Without this the report's adoption, speed, reliability and human
        # sections are all structurally empty -- they key off run.* events, and
        # the journal was producing none.
        self.init()
        root = self.journal([self.a_session(), self.a_shutdown()])
        self.run_cli("copilot", "--root", root)
        started = [e for e in self.insight.read_buffer()
                   if e["event_type"] == "run.started"]
        completed = [e for e in self.insight.read_buffer()
                     if e["event_type"] == "run.completed"]
        self.assertEqual(len(started), 1)
        self.assertEqual(len(completed), 1)
        self.assertEqual(started[0]["run_id"], completed[0]["run_id"])
        self.assertEqual(started[0]["trace_id"], "sess-1")

    def test_a_resume_is_a_second_run_not_a_continuation(self):
        # Measured 2026-08-26: 38 resumes against 22 starts. Treating a resume
        # as a continuation would hide most of the activity on this surface.
        self.init()
        root = self.journal([
            self.a_session(), self.a_shutdown(),
            {"type": "session.resume", "data": {
                "sessionId": "sess-1",
                "context": {"repository": "acme/repo", "branch": "main",
                            "gitRoot": "/home/someone/work/repo"}}},
            self.a_shutdown(),
        ])
        self.run_cli("copilot", "--root", root)
        started = [e for e in self.insight.read_buffer()
                   if e["event_type"] == "run.started"]
        self.assertEqual(len(started), 2)
        self.assertEqual(len({e["run_id"] for e in started}), 2)
        self.assertEqual(sorted(e["attributes"]["input_source"] for e in started),
                         ["resume", "start"])

    def test_a_subagent_is_a_child_run_and_owns_its_tool_calls(self):
        self.init()
        root = self.journal([
            self.a_session(),
            {"type": "subagent.started", "data": {
                "toolCallId": "task1", "agentName": "reviewer",
                "agentDisplayName": "Reviewer Agent"}},
            {"type": "tool.execution_start", "data": {
                "toolCallId": "t1", "parentToolCallId": "task1",
                "toolName": "view"}},
            {"type": "tool.execution_complete", "data": {
                "toolCallId": "t1", "parentToolCallId": "task1",
                "success": True}},
            {"type": "subagent.completed", "data": {"toolCallId": "task1"}},
        ])
        self.run_cli("copilot", "--root", root)
        events = self.insight.read_buffer()
        session_run = [e for e in events if e["event_type"] == "run.started"
                       and e["attributes"]["input_source"] == "start"][0]
        child = [e for e in events if e["event_type"] == "run.started"
                 and e["attributes"]["input_source"] == "subagent"][0]
        self.assertEqual(child["parent_run_id"], session_run["run_id"])
        self.assertEqual(child["agent"]["agent_name"], "reviewer")
        # AR-4 rolls a supervisor up by trace_id, so both must share one.
        self.assertEqual(child["trace_id"], session_run["trace_id"])
        tool = [e for e in events if e["event_type"] == "tool.call"][0]
        self.assertEqual(tool["run_id"], child["run_id"])
        self.assertEqual(tool["agent"]["agent_name"], "reviewer")

    def test_usage_is_never_attributed_to_a_run(self):
        # CONTRACT §3: the journal totals usage per session, and a session
        # hosts several runs. Stamping the open run would charge one agent for
        # what the others spent.
        self.init()
        root = self.journal([self.a_session(), self.a_shutdown()])
        self.run_cli("copilot", "--root", root)
        usage = [e for e in self.insight.read_buffer()
                 if e["event_type"] == "model.call"]
        self.assertIsNone(usage[0]["run_id"])

    def test_a_write_with_no_line_metrics_is_still_an_output(self):
        # It used to be dropped, taking the acceptance denominator with it.
        # An absent count is NULL; a counted zero is 0.
        self.init()
        root = self.journal([
            self.a_session(),
            {"type": "tool.execution_start", "data": {
                "toolCallId": "t1", "toolName": "create",
                "arguments": {"path": "/home/someone/work/repo/new.py"}}},
            {"type": "tool.execution_complete", "data": {
                "toolCallId": "t1", "success": True}},
        ])
        self.run_cli("copilot", "--root", root)
        outputs = [e for e in self.insight.read_buffer()
                   if e["event_type"] == "output.generated"]
        self.assertEqual(len(outputs), 1)
        self.assertIsNone(outputs[0]["attributes"]["lines_added"])

    def test_one_skill_is_attributed_and_two_are_not(self):
        # Two skills cannot both be credited with the outcome, and picking the
        # most recent would make the attribution depend on ordering.
        self.init()
        root = self.journal([
            self.a_session(),
            {"type": "skill.invoked", "data": {"name": "brainstorming"}},
            self.a_shutdown(),
        ])
        self.run_cli("copilot", "--root", root)
        usage = [e for e in self.insight.read_buffer()
                 if e["event_type"] == "model.call"][0]
        self.assertEqual(usage["agent"]["skill_name"], "brainstorming")
        # And the agent version is unknown, not the poller's own.
        self.assertIsNone(usage["agent"]["agent_version"])

        self.setUp()
        self.init()
        root = self.journal([
            self.a_session(),
            {"type": "skill.invoked", "data": {"name": "brainstorming"}},
            {"type": "skill.invoked", "data": {"name": "executing-plans"}},
            self.a_shutdown(),
        ])
        self.run_cli("copilot", "--root", root)
        usage = [e for e in self.insight.read_buffer()
                 if e["event_type"] == "model.call"][0]
        self.assertIsNone(usage["agent"]["skill_name"])

    def test_an_injected_message_is_not_a_human_turn(self):
        # A `source` means a skill or hook wrote it. Counting those would
        # inflate the manual-intervention rate with the agent talking to itself.
        self.init()
        root = self.journal([
            self.a_session(),
            {"type": "user.message", "data": {"content": "hello", "source": None}},
            {"type": "user.message", "data": {
                "content": "injected", "source": "skill-brainstorming"}},
        ])
        self.run_cli("copilot", "--root", root)
        turns = [e for e in self.insight.read_buffer()
                 if e["event_type"] == "human.turn"]
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["attributes"]["chars"], 5)

    def test_repositories_are_discovered_from_the_journal(self):
        # This is what replaced `setup --repo <path>`, repeated once per
        # repository. The one somebody forgot to name was the one that
        # silently reported nothing; `session.start` has been recording
        # `context.gitRoot` all along.
        import copilot_read
        repo = tempfile.mkdtemp(prefix="insight-discovered-")
        self.addCleanup(shutil.rmtree, repo, True)
        os.makedirs(os.path.join(repo, ".git"))
        root = self.journal([self.a_session(cwd=repo)])
        self.assertEqual(copilot_read.discover_repos(root), [repo])

    def test_a_worktree_is_discovered_too(self):
        # In a linked worktree `.git` is a *file* holding a gitdir pointer,
        # not a directory, and worktrees are where a good deal of agent work
        # happens.
        import copilot_read
        tree = tempfile.mkdtemp(prefix="insight-worktree-")
        self.addCleanup(shutil.rmtree, tree, True)
        with open(os.path.join(tree, ".git"), "w", encoding="utf-8") as handle:
            handle.write("gitdir: /somewhere/.git/worktrees/x\n")
        root = self.journal([self.a_session(cwd=tree)])
        self.assertEqual(copilot_read.discover_repos(root), [tree])

    def test_a_deleted_clone_is_not_discovered(self):
        # A repository removed last month should not become an error every
        # hour for the rest of the year.
        import copilot_read
        root = self.journal([self.a_session(cwd="/gone/for/good")])
        self.assertEqual(copilot_read.discover_repos(root), [])

    def test_a_torn_last_line_does_not_lose_the_session(self):
        # The journal is appended to live; a read can catch a partial write.
        self.init()
        root = self.journal([self.a_session(), self.a_shutdown()])
        path = os.path.join(root, "session-state", "sess-1", "events.jsonl")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"type": "tool.execution_st')
        code, _ = self.run_cli("copilot", "--root", root)
        self.assertEqual(code, 0)
        self.assertTrue([e for e in self.insight.read_buffer()
                         if e["event_type"] == "model.call"])


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
    commands — the one they forget is the one that silently reports nothing.

    Registration is now a fallback rather than the mechanism: `scan` reads the
    registered list *and* every git tree Copilot's journals name. These tests
    cover the registered half, so they point discovery at an empty directory —
    otherwise the result depends on what is in the developer's own ~/.copilot.
    """

    def setUp(self):
        super().setUp()
        self.insight.COPILOT_ROOT = os.path.join(self.home, "no-copilot")

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

    def test_scan_with_nothing_to_walk_says_so_rather_than_failing(self):
        # It used to raise. That was right when repositories were registered by
        # hand -- an empty list meant somebody had not finished setting up. Now
        # they are discovered from Copilot's journals, so an empty list means
        # Copilot has not been run in a git tree yet, which is a fact about the
        # machine and not a fault. Raising would put a failure in `auto.log`
        # every hour, and an hourly failure is one nobody reads.
        self.init()
        self.insight.COPILOT_ROOT = os.path.join(self.home, "no-copilot")
        code, output = self.run_cli("scan", "--since-days", "3650")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["repos"], 0)

    def test_init_keeps_the_repo_list(self):
        self.init()
        self.run_cli("install-hook", "--repo", self.repo)
        self.run_cli("init", "--yes", "--force")
        self.assertIn(self.repo, self.insight.known_repos())
