"""Tests for the bundle downloader.

    python3 -m pytest importers/tests/test_pull.py -q
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importers import pull as pull_mod  # noqa: E402


class Endpoint:
    """A stand-in for the proxy's read routes."""

    def __init__(self, objects, bodies=None, status=200):
        self.objects = objects
        self.bodies = bodies or {}
        self.status = status
        self.calls = []

    def __call__(self, url, token, timeout):
        self.calls.append({"url": url, "token": token})
        if self.status != 200:
            return self.status, b'{"error":"nope"}'
        if "/v1/bundles?" in url:
            return 200, json.dumps(
                {"week": "2026-W34", "objects": self.objects}).encode("utf-8")
        key = url.split("/v1/bundle/", 1)[1]
        from urllib.parse import unquote
        return 200, self.bodies.get(unquote(key), b"line\n")


OBJECTS = [
    {"key": "bundles/2026-W34/a3f9c2b1/aaa.ndjson", "machine": "a3f9c2b1",
     "email": "canh@seta-international.vn",
     "bytes": 10, "uploaded_at": "2026-08-24T09:00:00Z"},
    {"key": "bundles/2026-W34/7b1e0092/bbb.ndjson", "machine": "7b1e0092",
     "email": "minh@seta-international.vn",
     "bytes": 20, "uploaded_at": "2026-08-24T10:00:00Z"},
]


class PullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.inbox = os.path.join(self.tmp, "inbox")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_every_listed_bundle_into_the_inbox(self):
        endpoint = Endpoint(OBJECTS, {
            "bundles/2026-W34/a3f9c2b1/aaa.ndjson": b"A\n",
            "bundles/2026-W34/7b1e0092/bbb.ndjson": b"B\n"})

        result = pull_mod.pull_week("https://x.test/", "2026-W34", "tok",
                                    self.inbox, get=endpoint)

        self.assertEqual(sorted(result["files_written"]),
                         ["7b1e0092-bbb.ndjson", "a3f9c2b1-aaa.ndjson"])
        with open(os.path.join(self.inbox, "a3f9c2b1-aaa.ndjson"), "rb") as h:
            self.assertEqual(h.read(), b"A\n")

    def test_a_contract_shaped_key_is_not_prefixed_twice(self):
        # The spec's key already ends in `<machine>-<digest>.ndjson`. Prefixing
        # it again gives `a3f9c2b1-a3f9c2b1-...`, which is only ugly until
        # someone tries to match a file back to an object.
        objects = [{"key": "bundles/2026-W34/8c1d40b9e2af/a3f9c2b1-dead.ndjson",
                    "machine": "a3f9c2b1", "email": "canh@seta-international.vn"}]
        result = pull_mod.pull_week("https://x.test", "2026-W34", "tok",
                                    self.inbox, get=Endpoint(objects))
        self.assertEqual(result["files_written"], ["a3f9c2b1-dead.ndjson"])

    def test_the_admin_token_goes_on_every_request(self):
        endpoint = Endpoint(OBJECTS)
        pull_mod.pull_week("https://x.test", "2026-W34", "tok", self.inbox,
                           get=endpoint)
        self.assertTrue(all(c["token"] == "tok" for c in endpoint.calls))

    def test_pulling_the_same_week_twice_does_not_refetch(self):
        # A week gets pulled again whenever someone ships late, so this is the
        # normal case rather than an edge one.
        endpoint = Endpoint(OBJECTS)
        pull_mod.pull_week("https://x.test", "2026-W34", "tok", self.inbox,
                           get=endpoint)
        first = len(endpoint.calls)

        result = pull_mod.pull_week("https://x.test", "2026-W34", "tok",
                                    self.inbox, get=endpoint)

        self.assertEqual(result["files_written"], [])
        self.assertEqual(len(result["files_already_present"]), 2)
        self.assertEqual(len(endpoint.calls), first + 1)  # the list call only

    def test_a_rejected_token_says_why_reading_is_authenticated(self):
        endpoint = Endpoint(OBJECTS, status=401)
        with self.assertRaises(pull_mod.PullError) as caught:
            pull_mod.pull_week("https://x.test", "2026-W34", "bad", self.inbox,
                               get=endpoint)
        self.assertIn("TRANSPORT", str(caught.exception))

    def test_a_server_error_writes_nothing(self):
        endpoint = Endpoint(OBJECTS, status=500)
        with self.assertRaises(pull_mod.PullError):
            pull_mod.pull_week("https://x.test", "2026-W34", "tok", self.inbox,
                               get=endpoint)
        self.assertFalse(os.path.isdir(self.inbox))

    def test_an_empty_week_is_not_an_error(self):
        result = pull_mod.pull_week("https://x.test", "2026-W34", "tok",
                                    self.inbox, get=Endpoint([]))
        self.assertEqual(result["files_written"], [])
        self.assertEqual(result["objects_listed"], 0)


class CoverageTests(unittest.TestCase):
    ROSTER = ["canh@seta-international.vn", "minh@seta-international.vn",
              "lan@seta-international.vn"]

    def test_names_the_people_who_did_not_report(self):
        report = pull_mod.coverage(OBJECTS, self.ROSTER)
        self.assertEqual(report["expected"], 3)
        self.assertEqual(report["arrived"], 2)
        self.assertEqual(report["missing"], ["lan@seta-international.vn"])

    def test_someone_who_never_reported_is_still_counted(self):
        # bundle.py's coverage cannot see this person at all -- it derives
        # coverage from bundles that arrived. Only a roster knows they exist.
        report = pull_mod.coverage([], ["lan@seta-international.vn"])
        self.assertEqual(report["missing"], ["lan@seta-international.vn"])
        self.assertEqual(report["arrived"], 0)

    def test_surfaces_someone_nobody_put_on_the_roster(self):
        report = pull_mod.coverage(OBJECTS, ["canh@seta-international.vn"])
        self.assertEqual(report["unexpected"], ["minh@seta-international.vn"])

    def test_case_and_whitespace_do_not_split_one_person_into_two(self):
        report = pull_mod.coverage(
            [{"key": "k", "machine": "m", "email": "Canh@SETA-International.VN"}],
            ["  canh@seta-international.vn  "])
        self.assertEqual(report["missing"], [])
        self.assertEqual(report["unexpected"], [])

    def test_falls_back_to_machine_ids_when_the_proxy_sends_no_email(self):
        # A proxy deployed before the whitelist still produces a coverage
        # report, rather than one that silently says nobody reported.
        report = pull_mod.coverage(
            [{"key": "k", "machine": "a3f9c2b1"}], ["a3f9c2b1", "4c8d1104"])
        self.assertEqual(report["missing"], ["4c8d1104"])


class RosterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reads_emails_and_ignores_comments_names_and_blanks(self):
        path = os.path.join(self.tmp, "roster.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# who is expected to report\n"
                         "canh@seta-international.vn  Canh\n"
                         "\n"
                         "minh@seta-international.vn  Minh  # joined 2026-08-01\n")
        self.assertEqual(pull_mod.read_roster(path),
                         ["canh@seta-international.vn",
                          "minh@seta-international.vn"])

    def test_a_pasted_whitelist_entry_works_as_a_roster_line(self):
        # The two files list the same people, so pasting one into the other is
        # the obvious thing to try and should not silently produce nonsense.
        path = os.path.join(self.tmp, "roster.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("canh@seta-international.vn:9f2ac41:aa11bb22\n")
        self.assertEqual(pull_mod.read_roster(path),
                         ["canh@seta-international.vn"])


if __name__ == "__main__":
    unittest.main()
