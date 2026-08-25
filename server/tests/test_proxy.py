"""Tests for the collection endpoint.

    python3 -m pytest server/tests -q

Driven over real HTTP against a real filesystem store, not against the handler
functions. The things worth asserting here -- status codes, write-once, who is
turned away -- are the contract `cli/ship.py` and `importers/pull.py` were
written against, and a test that calls the functions directly would not notice
if the routing stopped matching the contract.
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVER = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SERVER)
for _p in (_SERVER, os.path.join(_ROOT, "cli")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import identity  # noqa: E402
import proxy as proxy_mod  # noqa: E402
import store as store_mod  # noqa: E402

ADMIN = "admin-token-for-tests"
CANH = "canh@seta-international.vn"
MINH = "minh@seta-international.vn"


def bundle_bytes(events=1, machine="a3f9c2b1f0e1"):
    body = "".join(json.dumps({"event_id": "evt_%d" % i}, sort_keys=True) + "\n"
                   for i in range(events))
    manifest = {
        "format": "seta-insight-bundle/1", "schema_version": "1.0.0",
        "machine_id": machine, "packed_at": "2026-08-24T00:00:00Z",
        "window_start": "2026-08-17T00:00:00Z",
        "window_end": "2026-08-23T23:59:59Z",
        "event_count": events,
        "sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    return (json.dumps({"_manifest": manifest}, sort_keys=True) + "\n" + body).encode()


class ProxyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="insight-proxy-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.secrets = {CANH: identity.mint_secret(), MINH: identity.mint_secret()}
        allowed = identity.parse_whitelist(",".join(
            identity.whitelist_line(person, secret)
            for person, secret in self.secrets.items()))

        self.store = store_mod.FileStore(os.path.join(self.tmp, "store"))
        handler = proxy_mod.build_handler(self.store, allowed, ADMIN)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = "http://127.0.0.1:{}".format(self.httpd.server_address[1])
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    # -- helpers ----------------------------------------------------------

    def put(self, body=None, secret=None, person=CANH, **over):
        body = bundle_bytes() if body is None else body
        headers = {
            "Content-Type": "application/x-ndjson",
            "X-Insight-Machine": "a3f9c2b1f0e1",
            "X-Insight-Window": "2026-08-17/2026-08-23",
            "X-Insight-Schema": "1.0.0",
            "X-Insight-Format": "seta-insight-bundle/1",
            "X-Insight-Digest": "sha256=" + hashlib.sha256(body).hexdigest(),
            "Authorization": "Bearer " + (secret or self.secrets[person]),
        }
        for name, value in over.items():
            key = name.replace("_", "-")
            if value is None:
                headers.pop(key, None)
            else:
                headers[key] = value
        return self._request("PUT", "/v1/bundle", body, headers)

    def get(self, path, token=ADMIN):
        headers = {"Authorization": "Bearer " + token} if token else {}
        return self._request("GET", path, None, headers)

    def _request(self, method, path, body, headers):
        request = urllib.request.Request(
            self.base + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def json_of(self, raw):
        return json.loads(raw.decode())


class UploadTests(ProxyTestCase):
    def test_a_whitelisted_upload_is_stored(self):
        status, raw = self.put()
        self.assertEqual(status, 201)
        payload = self.json_of(raw)
        self.assertTrue(payload["key"].startswith("bundles/2026-W34/"))
        self.assertEqual(self.store.get(payload["key"]), bundle_bytes())

    def test_the_key_is_built_from_the_authenticated_identity(self):
        # Not from any header. A client naming its own key could overwrite
        # someone else's bundle or scribble outside its prefix.
        status, raw = self.put(person=MINH)
        self.assertEqual(status, 201)
        self.assertIn(identity.person_key(MINH), self.json_of(raw)["key"])
        self.assertNotIn(identity.person_key(CANH), self.json_of(raw)["key"])

    def test_two_people_are_filed_separately(self):
        _, first = self.put(person=CANH)
        _, second = self.put(person=MINH)
        self.assertNotEqual(self.json_of(first)["key"], self.json_of(second)["key"])

    def test_one_person_on_two_machines_lands_under_one_prefix(self):
        # Someone with a laptop and a desktop is one line of coverage, not two.
        self.put(X_Insight_Machine="1111aaaa")
        self.put(body=bundle_bytes(events=2), X_Insight_Machine="2222bbbb")
        stored = self.store.list("bundles/")
        prefixes = {k["key"].rsplit("/", 1)[0] for k in stored}
        self.assertEqual(len(prefixes), 1)

    def test_the_same_bundle_twice_is_409_not_a_duplicate_object(self):
        first, _ = self.put()
        second, raw = self.put()
        self.assertEqual((first, second), (201, 409))
        self.assertEqual(len(self.store.list("bundles/")), 1)
        self.assertIn("key", self.json_of(raw))

    def test_a_stored_bundle_is_never_overwritten(self):
        status, raw = self.put()
        key = self.json_of(raw)["key"]
        original = self.store.get(key)
        self.put()                       # same digest, same key
        self.assertEqual(self.store.get(key), original)


class AuthTests(ProxyTestCase):
    def test_an_unknown_secret_is_401_so_a_rotation_can_retry(self):
        # 401, never 403: `ship` retries a 401 with its previous secret, which
        # is exactly what a rotation in flight looks like.
        status, _ = self.put(secret="not-a-real-secret")
        self.assertEqual(status, 401)

    def test_a_fingerprint_is_not_a_credential(self):
        # The whole point of hashing the whitelist: a leaked .env uploads nothing.
        leaked = identity.fingerprint(self.secrets[CANH])
        status, _ = self.put(secret=leaked)
        self.assertEqual(status, 401)

    def test_no_credential_at_all_is_refused(self):
        status, _ = self.put(Authorization=None)
        self.assertEqual(status, 401)

    def test_an_unknown_caller_sending_junk_stores_nothing(self):
        oversized = b"x" * (proxy_mod.MAX_BYTES + 1)
        status, _ = self.put(body=oversized, secret="nope")
        self.assertIn(status, (401, 413))
        self.assertEqual(self.store.list("bundles/"), [])


class ValidationTests(ProxyTestCase):
    def test_a_digest_mismatch_stores_nothing(self):
        status, raw = self.put(X_Insight_Digest="sha256=" + "0" * 64)
        self.assertEqual(status, 400)
        self.assertIn("digest", self.json_of(raw)["error"])
        self.assertEqual(self.store.list("bundles/"), [])

    def test_a_missing_digest_is_refused(self):
        status, _ = self.put(X_Insight_Digest=None)
        self.assertEqual(status, 400)

    def test_an_unknown_schema_is_refused_rather_than_stored(self):
        # Silently accepting the future is how you find out in three months.
        status, raw = self.put(X_Insight_Schema="9.9.9")
        self.assertEqual(status, 400)
        self.assertIn("schema", self.json_of(raw)["error"])

    def test_an_undeclared_window_is_refused(self):
        # There would be no week to file it under, and it would quietly land in
        # the wrong one, making two weeks wrong at once.
        for bad in ("", "2026-08-17", "/", "2026-08-17/"):
            status, _ = self.put(X_Insight_Window=bad)
            self.assertEqual(status, 400, bad)

    def test_a_window_that_is_not_a_date_is_refused(self):
        status, _ = self.put(X_Insight_Window="last-week/this-week")
        self.assertEqual(status, 400)

    def test_an_oversized_body_is_refused(self):
        # nginx caps the body at the same 1 MiB one hop earlier, so in
        # production the truly large ones never reach this process at all.
        big = b"x" * (proxy_mod.MAX_BYTES + 1)
        status, _ = self.put(body=big)
        self.assertEqual(status, 413)

    def test_an_empty_body_is_refused(self):
        status, _ = self.put(body=b"")
        self.assertEqual(status, 400)

    def test_an_unknown_route_is_404(self):
        status, _ = self.put()
        request = urllib.request.Request(
            self.base + "/v1/anything", data=b"x", method="PUT",
            headers={"Authorization": "Bearer " + self.secrets[CANH]})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                code = response.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        self.assertEqual(code, 404)


class ReadRouteTests(ProxyTestCase):
    def test_listing_requires_the_admin_token(self):
        # Reading exposes every engineer at once. An engineer's own upload
        # secret must not open it.
        status, _ = self.get("/v1/bundles?week=2026-W34", token=self.secrets[CANH])
        self.assertEqual(status, 401)

    def test_listing_with_no_token_is_refused(self):
        status, _ = self.get("/v1/bundles?week=2026-W34", token=None)
        self.assertEqual(status, 401)

    def test_a_listing_names_people_so_coverage_can_chase_them(self):
        self.put(person=CANH)
        status, raw = self.get("/v1/bundles?week=2026-W34")
        self.assertEqual(status, 200)
        objects = self.json_of(raw)["objects"]
        self.assertEqual(len(objects), 1)
        self.assertEqual(objects[0]["email"], CANH)
        self.assertEqual(objects[0]["machine"], "a3f9c2b1")
        self.assertTrue(objects[0]["uploaded_at"])

    def test_a_bundle_comes_back_byte_for_byte(self):
        # The checksum inside was computed on a laptop a week earlier and is
        # verified again at import. A single byte changed here breaks that.
        _, raw = self.put()
        key = self.json_of(raw)["key"]
        status, body = self.get("/v1/bundle/" + key)
        self.assertEqual(status, 200)
        self.assertEqual(body, bundle_bytes())

    def test_fetching_needs_the_admin_token_too(self):
        _, raw = self.put()
        key = self.json_of(raw)["key"]
        status, _ = self.get("/v1/bundle/" + key, token=self.secrets[CANH])
        self.assertEqual(status, 401)

    def test_a_missing_key_is_404(self):
        status, _ = self.get("/v1/bundle/bundles/2026-W34/aaa/bbb.ndjson")
        self.assertEqual(status, 404)

    def test_a_malformed_week_is_refused(self):
        for bad in ("", "2026-34", "2026-W99", "../../etc"):
            status, _ = self.get("/v1/bundles?week=" + bad)
            self.assertEqual(status, 400, bad)

    def test_an_empty_week_lists_nothing_rather_than_failing(self):
        status, raw = self.get("/v1/bundles?week=2026-W01")
        self.assertEqual(status, 200)
        self.assertEqual(self.json_of(raw)["objects"], [])

    def test_healthz_needs_no_credential(self):
        status, raw = self.get("/healthz", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(self.json_of(raw)["ok"])


class AdminTokenSourceTests(unittest.TestCase):
    """Where the admin token is read from.

    A file is offered because of how this is deployed. In a container the
    environment is readable by anyone who can reach the Docker daemon --
    `docker inspect` prints it -- which on a shared host is a wider audience
    than root.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="insight-token-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, text):
        path = os.path.join(self.tmp, "token")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_a_file_holds_the_token(self):
        path = self.write("s3cret-token\n")
        self.assertEqual(proxy_mod.load_admin_token(None, path), "s3cret-token")

    def test_a_trailing_newline_is_not_part_of_the_token(self):
        # `echo token > file` is how this file gets written by hand, and a
        # token that silently includes \n never matches the header.
        path = self.write("s3cret-token\n")
        self.assertNotIn("\n", proxy_mod.load_admin_token(None, path))

    def test_the_file_wins_over_the_environment(self):
        # Both set means the deploy moved to a file and left the old variable
        # behind. The file is the one that was edited on purpose.
        path = self.write("from-file")
        self.assertEqual(proxy_mod.load_admin_token("from-env", path), "from-file")

    def test_the_environment_still_works_on_its_own(self):
        self.assertEqual(proxy_mod.load_admin_token("from-env", None), "from-env")

    def test_an_empty_file_refuses_to_start(self):
        # Not "no admin token, so no read routes" -- a truncated file must not
        # quietly become an open door or a silent one.
        with self.assertRaises(SystemExit):
            proxy_mod.load_admin_token(None, self.write("   \n"))

    def test_a_missing_file_names_the_path(self):
        missing = os.path.join(self.tmp, "not-there")
        with self.assertRaises(SystemExit) as caught:
            proxy_mod.load_admin_token(None, missing)
        self.assertIn(missing, str(caught.exception))

    def test_nothing_at_all_refuses_to_start(self):
        with self.assertRaises(SystemExit):
            proxy_mod.load_admin_token(None, None)


class TraversalTests(ProxyTestCase):
    def test_a_key_cannot_escape_the_store(self):
        status, _ = self.get("/v1/bundle/../../../../etc/passwd")
        self.assertIn(status, (400, 404))

    def test_an_encoded_traversal_cannot_escape_either(self):
        status, _ = self.get("/v1/bundle/bundles%2F..%2F..%2Fetc%2Fpasswd")
        self.assertIn(status, (400, 404))


if __name__ == "__main__":
    unittest.main()
