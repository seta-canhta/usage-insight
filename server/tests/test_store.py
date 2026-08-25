"""Tests for the two storage backends.

    python3 -m pytest server/tests/test_store.py -q

The S3 backend is the one that runs in production and the one that cannot be
exercised by the end-to-end suite, so it is driven here against a stub client.
What matters is not that boto3 works -- it does -- but that this module asks it
for the right things: a conditional write, the right error mapped to ``Exists``,
and a key prefix that goes on the way in and comes off on the way out.
"""

import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import store as store_mod  # noqa: E402

try:
    from botocore.exceptions import ClientError
    HAVE_BOTO = True
except ImportError:                                    # pragma: no cover
    HAVE_BOTO = False


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "PutObject")


class FakeS3:
    """Enough of the S3 client to hold this module honest."""

    def __init__(self, put_errors=None, existing=None):
        self.objects = dict(existing or {})
        self.put_errors = list(put_errors or [])
        self.put_calls = []
        self.head_calls = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        if self.put_errors:
            error = self.put_errors.pop(0)
            if error is not None:
                raise error
        key = kwargs["Key"]
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise client_error("PreconditionFailed")
        self.objects[key] = kwargs["Body"]

    def head_object(self, Bucket, Key):     # noqa: N803 -- boto3's own naming
        self.head_calls.append(Key)
        if Key not in self.objects:
            raise client_error("404")
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket, Key):      # noqa: N803
        if Key not in self.objects:
            raise client_error("NoSuchKey")

        class Body:
            def __init__(self, data):
                self.data = data

            def read(self):
                return self.data

        return {"Body": Body(self.objects[Key])}

    def list_objects_v2(self, **kwargs):
        prefix = kwargs.get("Prefix", "")
        keys = sorted(k for k in self.objects if k.startswith(prefix))
        token = kwargs.get("ContinuationToken")
        start = int(token) if token else 0
        page, nxt = keys[start:start + 2], start + 2
        return {
            "Contents": [{"Key": k, "Size": len(self.objects[k]),
                          "LastModified": None} for k in page],
            "IsTruncated": nxt < len(keys),
            "NextContinuationToken": str(nxt),
        }


class FileStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = store_mod.FileStore(os.path.join(self.tmp, "s"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_round_trips(self):
        self.store.put("bundles/2026-W34/aa/bb.ndjson", b"data\n")
        self.assertEqual(self.store.get("bundles/2026-W34/aa/bb.ndjson"), b"data\n")

    def test_a_second_write_to_the_same_key_is_refused(self):
        self.store.put("bundles/a/b/c.ndjson", b"first")
        with self.assertRaises(store_mod.Exists):
            self.store.put("bundles/a/b/c.ndjson", b"second")
        self.assertEqual(self.store.get("bundles/a/b/c.ndjson"), b"first")

    def test_a_missing_key_raises_keyerror_not_oserror(self):
        # The proxy turns KeyError into 404 and OSError into 503. Getting this
        # wrong tells the caller to retry something that will never appear.
        with self.assertRaises(KeyError):
            self.store.get("bundles/a/b/nope.ndjson")

    def test_a_key_cannot_escape_the_root(self):
        for bad in ("../outside.ndjson", "bundles/../../outside.ndjson",
                    "/etc/passwd"):
            with self.assertRaises(store_mod.StoreError, msg=bad):
                self.store.put(bad, b"x")

    def test_reading_outside_the_root_is_refused_too(self):
        with self.assertRaises(store_mod.StoreError):
            self.store.get("../../etc/passwd")

    def test_metadata_sidecars_are_not_listed_as_bundles(self):
        self.store.put("bundles/w/p/x.ndjson", b"x", metadata={"machine": "aaa"})
        listed = self.store.list("bundles/")
        self.assertEqual([o["key"] for o in listed], ["bundles/w/p/x.ndjson"])

    def test_listing_reports_size_and_a_timestamp(self):
        self.store.put("bundles/w/p/x.ndjson", b"12345")
        entry = self.store.list("bundles/")[0]
        self.assertEqual(entry["bytes"], 5)
        self.assertTrue(entry["uploaded_at"].endswith("Z"))

    def test_listing_an_absent_prefix_is_empty_not_an_error(self):
        self.assertEqual(self.store.list("bundles/2026-W01/"), [])


@unittest.skipUnless(HAVE_BOTO, "botocore not installed")
class S3StoreTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeS3()
        self.store = store_mod.S3Store("bucket", "prefix", client=self.fake)

    def test_a_write_is_conditional_so_nothing_is_overwritten(self):
        self.store.put("bundles/a/b/c.ndjson", b"data")
        self.assertEqual(self.fake.put_calls[0]["IfNoneMatch"], "*")
        self.assertEqual(self.fake.put_calls[0]["ContentType"],
                         "application/x-ndjson")

    def test_the_prefix_is_added_on_the_way_in(self):
        self.store.put("bundles/a/b/c.ndjson", b"data")
        self.assertEqual(self.fake.put_calls[0]["Key"],
                         "prefix/bundles/a/b/c.ndjson")

    def test_the_prefix_comes_off_on_the_way_out(self):
        # The proxy hands these keys straight back to pull.py, which asks for
        # them again. A prefix left on would make every fetch a 404.
        self.store.put("bundles/a/b/c.ndjson", b"data")
        listed = self.store.list("bundles/")
        self.assertEqual([o["key"] for o in listed], ["bundles/a/b/c.ndjson"])

    def test_no_prefix_is_handled_without_a_leading_slash(self):
        store = store_mod.S3Store("bucket", "", client=FakeS3())
        store.put("bundles/a/b/c.ndjson", b"data")
        self.assertEqual(store.client.put_calls[0]["Key"], "bundles/a/b/c.ndjson")

    def test_a_precondition_failure_is_Exists_not_an_error(self):
        # This is what makes the client's 409 mean "already handed over".
        self.store.put("bundles/a/b/c.ndjson", b"data")
        with self.assertRaises(store_mod.Exists):
            self.store.put("bundles/a/b/c.ndjson", b"data")

    def test_the_412_spelling_is_recognised_too(self):
        fake = FakeS3(put_errors=[client_error("412")])
        store = store_mod.S3Store("bucket", "", client=fake)
        with self.assertRaises(store_mod.Exists):
            store.put("bundles/a/b/c.ndjson", b"data")

    def test_any_other_failure_is_a_StoreError_so_the_client_retries(self):
        fake = FakeS3(put_errors=[client_error("AccessDenied")])
        store = store_mod.S3Store("bucket", "", client=fake)
        with self.assertRaises(store_mod.StoreError):
            store.put("bundles/a/b/c.ndjson", b"data")

    def test_a_store_without_conditional_writes_falls_back(self):
        # Some S3-compatible stores do not implement IfNoneMatch. Falling back
        # is racy, and the comment in store.py says why that is survivable: the
        # key is the content digest, so the worst case is a duplicate object.
        fake = FakeS3(put_errors=[client_error("NotImplemented"), None])
        store = store_mod.S3Store("bucket", "", client=fake)
        store.put("bundles/a/b/c.ndjson", b"data")
        self.assertFalse(store._conditional)
        self.assertEqual(fake.head_calls, ["bundles/a/b/c.ndjson"])
        self.assertNotIn("IfNoneMatch", fake.put_calls[-1])

    def test_the_fallback_still_refuses_an_existing_key(self):
        fake = FakeS3(put_errors=[client_error("NotImplemented")],
                      existing={"bundles/a/b/c.ndjson": b"already"})
        store = store_mod.S3Store("bucket", "", client=fake)
        with self.assertRaises(store_mod.Exists):
            store.put("bundles/a/b/c.ndjson", b"data")

    def test_the_probe_happens_once_not_on_every_write(self):
        fake = FakeS3(put_errors=[client_error("NotImplemented"), None, None])
        store = store_mod.S3Store("bucket", "", client=fake)
        store.put("bundles/a/b/one.ndjson", b"1")
        store.put("bundles/a/b/two.ndjson", b"2")
        self.assertNotIn("IfNoneMatch", fake.put_calls[-1])

    def test_a_missing_key_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.store.get("bundles/a/b/nope.ndjson")

    def test_get_returns_the_bytes_that_were_written(self):
        self.store.put("bundles/a/b/c.ndjson", b"exactly these bytes")
        self.assertEqual(self.store.get("bundles/a/b/c.ndjson"),
                         b"exactly these bytes")

    def test_listing_follows_every_page(self):
        # A truncated listing silently loses bundles, and the week just looks
        # quieter than it was.
        for i in range(5):
            self.store.put("bundles/w/p/{}.ndjson".format(i), b"x")
        self.assertEqual(len(self.store.list("bundles/")), 5)

    def test_metadata_is_passed_through(self):
        self.store.put("bundles/a/b/c.ndjson", b"x", metadata={"machine": "aaa"})
        self.assertEqual(self.fake.put_calls[0]["Metadata"], {"machine": "aaa"})


class OpenStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_file_url_opens_a_directory(self):
        store = store_mod.open_store("file://" + os.path.join(self.tmp, "a"))
        self.assertIsInstance(store, store_mod.FileStore)

    def test_a_bare_path_works_too(self):
        store = store_mod.open_store(os.path.join(self.tmp, "b"))
        self.assertIsInstance(store, store_mod.FileStore)

    @unittest.skipUnless(HAVE_BOTO, "botocore not installed")
    def test_an_s3_url_splits_bucket_from_prefix(self):
        store = store_mod.open_store("s3://my-bucket/some/prefix",
                                     client=FakeS3())
        self.assertEqual(store.bucket, "my-bucket")
        self.assertEqual(store.prefix, "some/prefix")

    @unittest.skipUnless(HAVE_BOTO, "botocore not installed")
    def test_an_s3_url_without_a_prefix_is_fine(self):
        store = store_mod.open_store("s3://my-bucket", client=FakeS3())
        self.assertEqual(store.prefix, "")

    def test_s3_without_a_bucket_is_refused(self):
        with self.assertRaises(store_mod.StoreError):
            store_mod.open_store("s3://")

    def test_an_unsupported_scheme_names_the_ones_that_work(self):
        with self.assertRaises(store_mod.StoreError) as caught:
            store_mod.open_store("gs://bucket/prefix")
        self.assertIn("s3://", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_BOTO, "botocore not installed")
class FallbackDurabilityTests(unittest.TestCase):
    """Once the fallback is on, it has to stay a check-then-write.

    The tempting shape -- probe once, then write plainly -- does not degrade
    write-once to "racy", it removes it. Every later upload would overwrite,
    and because keys are content digests the damage is invisible: the object
    still parses, still checksums, and is simply the wrong copy.
    """

    def test_write_once_survives_the_fallback(self):
        fake = FakeS3(put_errors=[client_error("NotImplemented"), None])
        store = store_mod.S3Store("bucket", "", client=fake)
        store.put("bundles/a/b/c.ndjson", b"first")
        self.assertFalse(store._conditional)

        with self.assertRaises(store_mod.Exists):
            store.put("bundles/a/b/c.ndjson", b"second")
        self.assertEqual(fake.objects["bundles/a/b/c.ndjson"], b"first")

    def test_a_new_key_still_writes_after_the_fallback(self):
        fake = FakeS3(put_errors=[client_error("NotImplemented"), None, None])
        store = store_mod.S3Store("bucket", "", client=fake)
        store.put("bundles/a/b/one.ndjson", b"1")
        store.put("bundles/a/b/two.ndjson", b"2")
        self.assertEqual(fake.objects["bundles/a/b/two.ndjson"], b"2")


@unittest.skipUnless(HAVE_BOTO, "botocore not installed")
class BotoCoreFailureTests(unittest.TestCase):
    """Credentials, region and DNS fail differently from S3 saying no.

    ClientError is what S3 *answers*; BotoCoreError is what happens before it is
    ever asked. They share no base class, so catching only the first turns a
    missing credential into an unhandled traceback -- a 500 from the endpoint,
    where 503 is the truth and the one that makes `ship` retry.
    """

    def setUp(self):
        from botocore.exceptions import NoCredentialsError
        self.boom = NoCredentialsError

    def _store(self, method):
        fake = FakeS3()
        setattr(fake, method, self._raise)
        return store_mod.S3Store("bucket", "", client=fake)

    def _raise(self, *a, **kw):
        raise self.boom()

    def test_a_missing_credential_on_put_is_a_StoreError(self):
        with self.assertRaises(store_mod.StoreError):
            self._store("put_object").put("bundles/a/b/c.ndjson", b"x")

    def test_a_missing_credential_on_get_is_a_StoreError_not_KeyError(self):
        # KeyError would become a 404: "that bundle does not exist", when the
        # truth is "this server cannot talk to S3".
        with self.assertRaises(store_mod.StoreError):
            self._store("get_object").get("bundles/a/b/c.ndjson")

    def test_a_missing_credential_on_list_is_a_StoreError(self):
        with self.assertRaises(store_mod.StoreError):
            self._store("list_objects_v2").list("bundles/")

    def test_exists_treats_an_unreachable_store_as_absent(self):
        # Only reached on the fallback path, where a StoreError from the put
        # that follows is the honest report.
        self.assertFalse(self._store("head_object").exists("bundles/a/b/c.ndjson"))
