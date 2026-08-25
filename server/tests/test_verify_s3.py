"""Tests for the bucket preflight.

    python3 -m pytest server/tests/test_verify_s3.py -q

The preflight is the thing that runs when nobody is sure the bucket is right,
so the failure worth guarding against is a green result on a bucket that would
lose data. Most of these assert that it *fails* when it should.
"""

import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import store as store_mod  # noqa: E402
import verify_s3  # noqa: E402


class Overwriting:
    """A store that silently accepts a second write — the thing to catch."""

    def __init__(self):
        self.objects = {}

    def put(self, key, body, metadata=None):
        self.objects[key] = body

    def get(self, key):
        return self.objects[key]

    def list(self, prefix):
        return [{"key": k, "bytes": len(v), "uploaded_at": ""}
                for k, v in sorted(self.objects.items()) if k.startswith(prefix)]


class Corrupting(Overwriting):
    def put(self, key, body, metadata=None):
        if key in self.objects:
            raise store_mod.Exists(key)
        self.objects[key] = body + b"tampered"


class Unreachable:
    def list(self, prefix):
        raise store_mod.StoreError("could not connect")


def names(checks):
    return {c.name: c for c in checks}


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_correct_store_passes_every_check(self):
        store = store_mod.FileStore(os.path.join(self.tmp, "s"))
        checks = verify_s3.verify(store, "bucket")
        self.assertTrue(all(c.ok for c in checks), [c.line() for c in checks])

    def test_running_twice_still_passes(self):
        # The probe key is fixed, so the second run finds it already there.
        # If that read as a failure nobody could re-run this after a policy change.
        store = store_mod.FileStore(os.path.join(self.tmp, "s"))
        verify_s3.verify(store, "bucket")
        checks = verify_s3.verify(store, "bucket")
        self.assertTrue(all(c.ok for c in checks), [c.line() for c in checks])

    def test_a_store_that_overwrites_is_caught(self):
        # The whole reason this script exists. Without conditional writes a
        # re-upload replaces a bundle and nothing downstream notices.
        checks = names(verify_s3.verify(Overwriting(), "bucket"))
        self.assertFalse(checks["write-once (IfNoneMatch)"].ok)
        self.assertIn("OVERWROTE", checks["write-once (IfNoneMatch)"].detail)

    def test_a_store_that_alters_content_is_caught(self):
        checks = names(verify_s3.verify(Corrupting(), "bucket"))
        self.assertFalse(checks["GetObject"].ok)

    def test_an_unreachable_bucket_stops_immediately(self):
        # Every later check would fail for the same reason, and a wall of
        # failures hides the one that matters.
        checks = verify_s3.verify(Unreachable(), "bucket")
        self.assertEqual(len(checks), 1)
        self.assertFalse(checks[0].ok)

    def test_the_probe_is_one_object_not_one_per_run(self):
        # The role has no DeleteObject, so a per-run key would accumulate
        # forever with nothing able to clean it up.
        store = store_mod.FileStore(os.path.join(self.tmp, "s"))
        for _ in range(3):
            verify_s3.verify(store, "bucket")
        self.assertEqual(len(store.list("_preflight/")), 1)

    def test_the_probe_is_not_mistaken_for_a_bundle(self):
        store = store_mod.FileStore(os.path.join(self.tmp, "s"))
        verify_s3.verify(store, "bucket")
        self.assertEqual(store.list("bundles/"), [])

    def test_the_probe_says_what_it_is(self):
        # Someone will find this object in the bucket and wonder.
        self.assertIn(b"Safe to delete", verify_s3.PROBE_BODY)
        self.assertIn(b"usage-insight", verify_s3.PROBE_BODY)


class ExitCodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_good_store_exits_zero(self):
        code = verify_s3.main(["--store", os.path.join(self.tmp, "ok"), "--json"])
        self.assertEqual(code, 0)

    def test_no_store_is_an_error_naming_the_bucket(self):
        with self.assertRaises(SystemExit) as caught:
            verify_s3.main(["--json"])
        self.assertIn("aeris-insight", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
