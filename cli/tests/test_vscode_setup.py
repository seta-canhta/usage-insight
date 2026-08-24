"""Tests for the VS Code configuration step.

Every failure this guards against was one that happened by hand, and none of
them surfaced as an error.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cli"))

import vscode_setup  # noqa: E402


class SettingsTestCase(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vscode-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "settings.json")

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def read(self):
        with open(self.path, "r", encoding="utf-8") as handle:
            return handle.read()


class TestJsonc(SettingsTestCase):

    def test_a_url_inside_a_string_is_not_mistaken_for_a_comment(self):
        # A naive regex eats the // in a URL and reports a valid file as
        # broken, sending someone hunting for a syntax error that is not there.
        parsed = vscode_setup.parse_jsonc(
            '{ "a": "https://example.com/x", // real comment\n "b": 1 }')
        self.assertEqual(parsed, {"a": "https://example.com/x", "b": 1})

    def test_trailing_commas_are_tolerated(self):
        self.assertEqual(vscode_setup.parse_jsonc('{ "a": 1, }'), {"a": 1})

    def test_block_comments_are_stripped(self):
        self.assertEqual(
            vscode_setup.parse_jsonc('{ /* note */ "a": 1 }'), {"a": 1})


class TestPaths(unittest.TestCase):

    def test_a_home_path_becomes_tilde_relative(self):
        # VS Code rejects absolute paths in the chat location settings, with a
        # renderer-log warning and nothing in the UI.
        home = os.path.expanduser("~")
        self.assertEqual(
            vscode_setup.tilde(os.path.join(home, "Projects", "x")),
            "~/Projects/x")

    def test_a_path_outside_home_is_left_absolute(self):
        self.assertEqual(vscode_setup.tilde("/opt/thing"), "/opt/thing")

    def test_the_agent_locations_are_tilde_relative(self):
        home = os.path.expanduser("~")
        wanted = vscode_setup.desired(
            os.path.join(home, ".seta-insight"),
            os.path.join(home, "Projects", "aiep"))
        for key in wanted["chat.agentFilesLocations"]:
            self.assertTrue(key.startswith("~/"), key)


class TestApply(SettingsTestCase):

    def wanted(self):
        return {"github.copilot.chat.otel.enabled": True,
                "chat.agentFilesLocations": {"~/a": True}}

    def test_existing_settings_survive(self):
        self.write('{ "editor.fontSize": 14 }')
        vscode_setup.apply(self.path, self.wanted())
        self.assertEqual(vscode_setup.parse_jsonc(self.read())["editor.fontSize"], 14)

    def test_someone_elses_agent_locations_are_not_unregistered(self):
        # Replacing the map instead of merging would silently remove agents
        # the developer registered themselves.
        self.write(json.dumps({"chat.agentFilesLocations": {"~/mine": True}}))
        vscode_setup.apply(self.path, self.wanted())
        locations = vscode_setup.parse_jsonc(self.read())["chat.agentFilesLocations"]
        self.assertEqual(locations, {"~/mine": True, "~/a": True})

    def test_running_twice_changes_nothing_the_second_time(self):
        self.write("{}")
        vscode_setup.apply(self.path, self.wanted())
        changed, detail, _ = vscode_setup.apply(self.path, self.wanted())
        self.assertFalse(changed)
        self.assertEqual(detail, "already configured")

    def test_a_backup_is_written(self):
        self.write('{ "editor.fontSize": 14 }')
        vscode_setup.apply(self.path, self.wanted())
        self.assertTrue([f for f in os.listdir(self.dir) if ".bak." in f])

    def test_an_unparseable_file_is_refused_not_overwritten(self):
        # Their file is broken already; replacing it would destroy settings we
        # cannot read.
        self.write('{ "a": 1 ')
        changed, detail, _ = vscode_setup.apply(self.path, self.wanted())
        self.assertFalse(changed)
        self.assertIn("does not parse", detail)
        self.assertEqual(self.read(), '{ "a": 1 ')

    def test_dry_run_writes_nothing(self):
        self.write("{}")
        changed, _, keys = vscode_setup.apply(self.path, self.wanted(), dry_run=True)
        self.assertTrue(changed)
        self.assertTrue(keys)
        self.assertEqual(self.read(), "{}")

    def test_a_new_file_is_created_when_none_exists(self):
        vscode_setup.apply(self.path, self.wanted())
        self.assertTrue(
            vscode_setup.parse_jsonc(self.read())["github.copilot.chat.otel.enabled"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
