"""Tests for the bundle uploader.

    python3 -m pytest cli/tests/test_ship.py -q
"""

import hashlib
import json
import os
import shutil
import ssl
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ship as ship_mod  # noqa: E402


def write_bundle(path, events=None, machine="a3f9c2b16d4e", schema="1.0.0",
                 window=("2026-08-17T00:00:00Z", "2026-08-23T23:59:59Z"),
                 fmt="seta-insight-bundle/1"):
    events = events if events is not None else [{"event_id": "evt_1"}]
    body = "".join(json.dumps(e, sort_keys=True) + "\n" for e in events)
    manifest = {
        "format": fmt,
        "schema_version": schema,
        "machine_id": machine,
        "packed_at": "2026-08-24T00:00:00Z",
        "window_start": window[0],
        "window_end": window[1],
        "event_count": len(events),
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"_manifest": manifest}, sort_keys=True) + "\n")
        handle.write(body)
    return manifest


class Recorder:
    """A stand-in for the proxy that records what it was sent."""

    def __init__(self, *responses):
        self.responses = list(responses) or [(201, {"key": "bundles/k", "bytes": 1})]
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": body, "headers": headers,
                           "timeout": timeout})
        response = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response


class ShipBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "bundle.ndjson")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sends_manifest_facts_as_headers(self):
        write_bundle(self.path)
        post = Recorder((201, {"key": "bundles/2026-W34/a3f9c2b1/x.ndjson",
                               "sha256": "z", "bytes": 42}))

        receipt = ship_mod.ship_bundle(self.path, "https://x.test/", post=post)

        headers = post.calls[0]["headers"]
        self.assertEqual(post.calls[0]["url"], "https://x.test/v1/bundle")
        self.assertEqual(headers["X-Insight-Machine"], "a3f9c2b16d4e")
        self.assertEqual(headers["X-Insight-Window"], "2026-08-17/2026-08-23")
        self.assertEqual(headers["X-Insight-Schema"], "1.0.0")
        self.assertEqual(headers["Content-Type"], "application/x-ndjson")
        self.assertNotIn("Authorization", headers)
        self.assertFalse(receipt["already_stored"])
        self.assertEqual(receipt["key"], "bundles/2026-W34/a3f9c2b1/x.ndjson")

    def test_digest_covers_the_whole_file_not_just_the_events(self):
        manifest = write_bundle(self.path)
        post = Recorder()
        ship_mod.ship_bundle(self.path, "https://x.test", post=post)

        sent = post.calls[0]["headers"]["X-Insight-Digest"]
        with open(self.path, "rb") as handle:
            whole = hashlib.sha256(handle.read()).hexdigest()
        self.assertEqual(sent, "sha256=" + whole)
        # The manifest's own checksum covers the event lines only. The two must
        # not collapse into one -- they catch different truncations.
        self.assertNotEqual(sent, "sha256=" + manifest["sha256"])

    def test_body_is_sent_byte_for_byte(self):
        write_bundle(self.path)
        post = Recorder()
        ship_mod.ship_bundle(self.path, "https://x.test", post=post)
        with open(self.path, "rb") as handle:
            self.assertEqual(post.calls[0]["body"], handle.read())

    def test_token_sent_only_when_configured(self):
        write_bundle(self.path)
        post = Recorder()
        ship_mod.ship_bundle(self.path, "https://x.test", token="s3cret", post=post)
        self.assertEqual(post.calls[0]["headers"]["Authorization"], "Bearer s3cret")

    def test_409_is_success_because_resending_must_be_safe(self):
        write_bundle(self.path)
        post = Recorder((409, {"key": "bundles/k", "sha256": "z", "bytes": 42}))
        receipt = ship_mod.ship_bundle(self.path, "https://x.test", post=post)
        self.assertTrue(receipt["already_stored"])
        self.assertEqual(receipt["status"], 409)
        self.assertEqual(len(post.calls), 1)

    def test_refuses_a_bundle_that_declares_no_window(self):
        write_bundle(self.path, window=(None, None))
        post = Recorder()
        with self.assertRaises(ship_mod.ShipError) as caught:
            ship_mod.ship_bundle(self.path, "https://x.test", post=post)
        self.assertIn("--since", str(caught.exception))
        self.assertEqual(post.calls, [])

    def test_refuses_a_file_that_is_not_a_bundle(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("just some text\n")
        with self.assertRaises(ship_mod.ShipError):
            ship_mod.ship_bundle(self.path, "https://x.test", post=Recorder())

    def test_refuses_an_empty_file(self):
        open(self.path, "w").close()
        with self.assertRaises(ship_mod.ShipError):
            ship_mod.ship_bundle(self.path, "https://x.test", post=Recorder())

    def test_refuses_a_bundle_over_the_size_cap(self):
        write_bundle(self.path, events=[{"event_id": "e", "pad": "x" * 4096}
                                        for _ in range(400)])
        post = Recorder()
        with self.assertRaises(ship_mod.ShipError) as caught:
            ship_mod.ship_bundle(self.path, "https://x.test", post=post)
        self.assertIn("cap", str(caught.exception))
        self.assertEqual(post.calls, [])

    def test_4xx_is_not_retried(self):
        write_bundle(self.path)
        post = Recorder((400, {"error": "digest mismatch"}))
        with self.assertRaises(ship_mod.ShipError) as caught:
            ship_mod.ship_bundle(self.path, "https://x.test", post=post)
        self.assertIn("digest mismatch", str(caught.exception))
        self.assertEqual(len(post.calls), 1)

    def test_5xx_is_retried_then_gives_up(self):
        write_bundle(self.path)
        post = Recorder((503, {"error": "upstream"}))
        with self.assertRaises(ship_mod.ShipError):
            ship_mod.ship_bundle(self.path, "https://x.test", retries=3, post=post)
        self.assertEqual(len(post.calls), 3)

    def test_a_transient_failure_recovers(self):
        write_bundle(self.path)
        post = Recorder(urllib.error.URLError("reset"),
                        (201, {"key": "bundles/k", "bytes": 1}))
        receipt = ship_mod.ship_bundle(self.path, "https://x.test", post=post)
        self.assertEqual(receipt["status"], 201)
        self.assertEqual(len(post.calls), 2)


class RotationFallbackTests(unittest.TestCase):
    """A rotation must never make an engineer unable to upload.

    The new fingerprint reaches the server's .env when a second person edits
    it, which is not the same minute the engineer rotates. If that window broke
    uploading, nobody would rotate twice.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "bundle.ndjson")
        write_bundle(self.path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_new_secret_is_tried_first(self):
        post = Recorder((201, {"key": "k"}))
        ship_mod.ship_bundle(self.path, "https://x.test", token="new",
                             previous_token="old", post=post)
        self.assertEqual(post.calls[0]["headers"]["Authorization"], "Bearer new")
        self.assertEqual(len(post.calls), 1)

    def test_falls_back_to_the_old_secret_while_the_env_catches_up(self):
        post = Recorder((401, {"error": "unknown fingerprint"}), (201, {"key": "k"}))
        receipt = ship_mod.ship_bundle(self.path, "https://x.test", token="new",
                                       previous_token="old", post=post)
        self.assertEqual(receipt["status"], 201)
        self.assertEqual([c["headers"]["Authorization"] for c in post.calls],
                         ["Bearer new", "Bearer old"])

    def test_a_401_with_nothing_to_fall_back_to_says_what_to_do(self):
        post = Recorder((401, {"error": "unknown fingerprint"}))
        with self.assertRaises(ship_mod.ShipError) as caught:
            ship_mod.ship_bundle(self.path, "https://x.test", token="only",
                                 post=post)
        self.assertIn("whoami", str(caught.exception))
        self.assertEqual(len(post.calls), 1)

    def test_a_403_is_never_retried_with_another_secret(self):
        # 403 is "you are known and not allowed". Trying an older secret for the
        # same person cannot change that, and looks like credential stuffing.
        post = Recorder((403, {"error": "not on the whitelist"}))
        with self.assertRaises(ship_mod.ShipError):
            ship_mod.ship_bundle(self.path, "https://x.test", token="new",
                                 previous_token="old", post=post)
        self.assertEqual(len(post.calls), 1)

    def test_both_secrets_rejected_reports_the_rotation_as_the_likely_cause(self):
        post = Recorder((401, {"error": "unknown"}))
        with self.assertRaises(ship_mod.ShipError) as caught:
            ship_mod.ship_bundle(self.path, "https://x.test", token="new",
                                 previous_token="old", post=post)
        self.assertIn("rotated", str(caught.exception))
        self.assertEqual(len(post.calls), 2)


class InFrontOfTheEndpointTests(unittest.TestCase):
    """A refusal from the CDN must not be read as a refusal from the endpoint.

    Found in deployment: Cloudflare answered a valid upload with `error code:
    1010` and this reported that the engineer's address had been removed from
    the whitelist -- sending someone to edit a file that was never wrong.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="insight-front-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "b.ndjson")
        write_bundle(self.path)

    def test_a_403_from_a_cdn_does_not_blame_the_whitelist(self):
        post = Recorder((403, {"body": "error code: 1010"}))
        with self.assertRaises(ship_mod.ShipError) as caught:
            ship_mod.ship_bundle(self.path, "https://x.test", token="t", post=post)
        message = str(caught.exception)
        self.assertIn("did not reach the endpoint", message)
        self.assertNotIn("whitelist", message)
        self.assertIn("1010", message)

    def test_a_403_from_a_cdn_is_not_retried_with_the_previous_secret(self):
        # No credential of any kind would get past it, and trying a second one
        # is how a blocked client turns into a blocked client that looks like
        # credential stuffing.
        post = Recorder((403, {"body": "error code: 1010"}))
        with self.assertRaises(ship_mod.ShipError):
            ship_mod.ship_bundle(self.path, "https://x.test", token="new",
                                 previous_token="old", post=post)
        self.assertEqual(len(post.calls), 1)

    def test_a_401_from_a_cdn_does_not_look_like_a_rotation(self):
        post = Recorder((401, {"body": "<html>Access denied</html>"}))
        with self.assertRaises(ship_mod.ShipError) as caught:
            ship_mod.ship_bundle(self.path, "https://x.test", token="new",
                                 previous_token="old", post=post)
        self.assertIn("in front of it", str(caught.exception))
        self.assertEqual(len(post.calls), 1)

    def test_the_endpoints_own_refusals_are_unaffected(self):
        post = Recorder((403, {"error": "not on the whitelist"}))
        with self.assertRaises(ship_mod.ShipError) as caught:
            ship_mod.ship_bundle(self.path, "https://x.test", token="t", post=post)
        self.assertIn("whitelist", str(caught.exception))


class UserAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="insight-ua-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "b.ndjson")
        write_bundle(self.path)

    def test_the_client_names_itself(self):
        # urllib's default is `Python-urllib/3.x`, which Cloudflare's browser
        # integrity check answers with 403 (`error code: 1010`) before the
        # request reaches the endpoint. Nothing downstream can recover from
        # that, so the header is part of the contract, not decoration.
        post = Recorder((201, {"key": "k"}))
        ship_mod.ship_bundle(self.path, "https://x.test", token="t", post=post)
        agent = post.calls[0]["headers"]["User-Agent"]
        self.assertTrue(agent)
        self.assertNotIn("Python-urllib", agent)


class TrustStoreTests(unittest.TestCase):
    """HTTPS has to work on a stock Mac.

    A python.org build ships with an empty trust store until somebody runs
    `Install Certificates.command`, which nobody does -- so every upload fails
    with "unable to get local issuer certificate" on a machine where `curl` to
    the same URL works.
    """

    def test_the_context_has_certificates_to_verify_against(self):
        self.assertTrue(ship_mod._ssl_context().get_ca_certs())

    def test_verification_is_never_switched_off(self):
        # The tempting fix. An unverified upload of a sealed bundle is worse
        # than a failed one, because it looks like it worked.
        context = ship_mod._ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_an_empty_default_store_falls_back_to_the_system_bundle(self):
        self.addCleanup(setattr, ssl, "create_default_context",
                        ssl.create_default_context)
        ssl.create_default_context = lambda *a, **k: ssl.SSLContext(
            ssl.PROTOCOL_TLS_CLIENT)          # loads nothing
        context = ship_mod._ssl_context()
        self.assertTrue(context.get_ca_certs(),
                        "no CA bundle found on this machine to fall back to")

    def test_a_certificate_failure_is_not_retried(self):
        # It will not verify on the third attempt either, and the retries only
        # delay the message that says what to do about it.
        attempts = []

        def post(url, body, headers, timeout):
            attempts.append(url)
            raise urllib.error.URLError(
                ssl.SSLCertVerificationError("unable to get local issuer certificate"))

        tmp = tempfile.mkdtemp(prefix="insight-cert-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "b.ndjson")
        write_bundle(path)
        with self.assertRaises(ship_mod.ShipError) as caught:
            ship_mod.ship_bundle(path, "https://x.test", token="t", post=post)
        self.assertEqual(len(attempts), 1)
        self.assertIn("Install Certificates", str(caught.exception))
        self.assertIn("Nothing was uploaded", str(caught.exception))


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.reports = os.path.join(self.tmp, ".reports")
        os.makedirs(self.reports)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bundles(self, *names):
        for name in names:
            write_bundle(os.path.join(self.reports, name))

    def test_unshipped_excludes_what_has_a_receipt_and_sorts_oldest_first(self):
        self._bundles("a-2.ndjson", "a-1.ndjson", "a-3.ndjson")
        pending = ship_mod.unshipped(self.reports, {"a-1.ndjson": {"status": 201}})
        self.assertEqual([os.path.basename(p) for p in pending],
                         ["a-2.ndjson", "a-3.ndjson"])

    def test_unshipped_ignores_non_bundles(self):
        self._bundles("a-1.ndjson")
        open(os.path.join(self.reports, "notes.txt"), "w").close()
        pending = ship_mod.unshipped(self.reports, {})
        self.assertEqual([os.path.basename(p) for p in pending], ["a-1.ndjson"])

    def test_missing_reports_dir_is_not_an_error(self):
        self.assertEqual(ship_mod.unshipped(os.path.join(self.tmp, "nope"), {}), [])

    def test_receipts_round_trip(self):
        path = os.path.join(self.tmp, "shipped.json")
        ship_mod.save_receipts(path, {"a.ndjson": {"status": 201}})
        self.assertEqual(ship_mod.load_receipts(path),
                         {"a.ndjson": {"status": 201}})

    def test_a_damaged_receipt_file_forgets_rather_than_refuses(self):
        # Resending costs a 409. Refusing to ship costs a week of data.
        path = os.path.join(self.tmp, "shipped.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        self.assertEqual(ship_mod.load_receipts(path), {})


if __name__ == "__main__":
    unittest.main()
