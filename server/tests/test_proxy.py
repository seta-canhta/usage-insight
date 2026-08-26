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
import registry as registry_mod
import store as store_mod  # noqa: E402

ADMIN = "admin-token-for-tests"
CANH = "canh@seta-international.vn"
MINH = "minh@seta-international.vn"


def bundle_bytes(events=1, machine="a3f9c2b1f0e1"):
    body = "".join(json.dumps({"event_id": "evt_%d" % i}, sort_keys=True) + "\n"
                   for i in range(events))
    manifest = {
        "format": "seta-insight-bundle/1", "schema_version": "1.1.0",
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

        self.allowed = allowed
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
            "X-Insight-Schema": "1.1.0",
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

    def test_healthz_advertises_the_schemas_it_will_accept(self):
        # A client deciding whether to replace itself needs to know what this
        # endpoint takes. Protocol, not disclosure -- and worth nothing to
        # anyone who cannot already authenticate.
        _, raw = self.get("/healthz", token=None)
        self.assertEqual(self.json_of(raw)["schemas"],
                         sorted(proxy_mod.KNOWN_SCHEMAS))

    def test_healthz_needs_no_credential(self):
        status, raw = self.get("/healthz", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(self.json_of(raw)["ok"])


class UploadOnlyListenerTests(ProxyTestCase):
    """The listener that faces the internet when the gateway is elsewhere.

    A reverse proxy on another machine cannot reach a container network, so it
    is given a host port -- and its own config decides which paths it forwards.
    When it forwards all of them, the route split `docs/TRANSPORT.md` describes
    stops being enforced by the network, and the admin token becomes the only
    thing between the internet and every engineer's telemetry. So this listener
    enforces it in the process instead.
    """

    def setUp(self):
        super().setUp()
        handler = proxy_mod.build_handler(
            self.store, self.allowed, ADMIN, read_routes=False)
        self.public = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.public_base = "http://127.0.0.1:{}".format(
            self.public.server_address[1])
        threading.Thread(target=self.public.serve_forever, daemon=True).start()
        self.addCleanup(self.public.server_close)
        self.addCleanup(self.public.shutdown)

    def public_get(self, path, token=ADMIN):
        headers = {"Authorization": "Bearer " + token} if token else {}
        request = urllib.request.Request(self.public_base + path, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_uploads_still_work(self):
        request = urllib.request.Request(
            self.public_base + "/v1/bundle", data=bundle_bytes(), method="PUT",
            headers={
                "Content-Type": "application/x-ndjson",
                "X-Insight-Machine": "a3f9c2b1f0e1",
                "X-Insight-Window": "2026-08-17/2026-08-23",
                "X-Insight-Schema": "1.1.0",
                "X-Insight-Format": "seta-insight-bundle/1",
                "X-Insight-Digest": "sha256=" + hashlib.sha256(
                    bundle_bytes()).hexdigest(),
                "Authorization": "Bearer " + self.secrets[CANH],
            })
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 201)

    def test_healthz_still_works(self):
        self.assertEqual(self.public_get("/healthz", token=None), 200)

    def test_listing_is_404_even_with_the_admin_token(self):
        # Not 401, and not 403. A listener that does not serve these does not
        # need to admit they exist, and a leaked admin token must not be enough
        # on its own -- which is the whole reason the routes were split.
        self.assertEqual(self.public_get("/v1/bundles?week=2026-W34"), 404)

    def test_fetching_a_bundle_is_404_even_with_the_admin_token(self):
        _, raw = self.put()
        key = self.json_of(raw)["key"]
        self.assertEqual(self.public_get("/v1/bundle/" + key), 404)

    def test_the_private_listener_still_reads(self):
        # Same process, same store: the split is between listeners, not a
        # feature switch that turns reading off everywhere.
        self.put()
        status, _ = self.get("/v1/bundles?week=2026-W34")
        self.assertEqual(status, 200)


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


#: A rendered ``server/install.sh.in``, cut down to the four assignments the
#: endpoint reads. Kept in the shape the real one has -- the point of
#: ``install_manifest`` is that it parses what the release job produces.
RENDERED = ("""#!/bin/sh
set -eu
VERSION="0.4.0"
SHA256="%s"
URL="https://github.com/seta-canhta/usage-insight/releases/download/v0.4.0/insight-0.4.0.pyz"
ENDPOINT="https://aeris-insight.seta-international.com"
SCHEMA="1.1.0"
main "$@"
""" % ("d" * 64)).encode()


class InstallManifestTests(unittest.TestCase):
    """``/install.json`` is derived from ``install.sh``, never configured twice.

    A second file holding the same version and digest is a second file that can
    disagree with the first, and the disagreement is invisible from outside:
    laptops either update to something the installer does not serve, or refuse
    an update that exists. So the endpoint parses the script it already has.
    """

    def test_the_four_fields_come_out(self):
        got = json.loads(proxy_mod.install_manifest(RENDERED))
        self.assertEqual(got["version"], "0.4.0")
        self.assertEqual(got["sha256"], "d" * 64)
        self.assertEqual(got["client_schema"], "1.1.0")

    def test_it_advertises_what_this_endpoint_actually_accepts(self):
        # Not a constant typed twice: a client compares its candidate release
        # against exactly the set check_upload() enforces.
        got = json.loads(proxy_mod.install_manifest(RENDERED))
        self.assertEqual(got["schemas"], sorted(proxy_mod.KNOWN_SCHEMAS))

    def test_nothing_configured_is_no_manifest_rather_than_an_error(self):
        self.assertIsNone(proxy_mod.install_manifest(None))
        self.assertIsNone(proxy_mod.install_manifest(b""))

    def test_an_unrendered_template_refuses_to_start(self):
        # The failure this exists to catch: server/install.sh.in copied into
        # /etc/insight instead of the rendered install.sh from the release.
        template = RENDERED.replace(b'"0.4.0"', b'"@VERSION@"')
        with self.assertRaises(SystemExit) as caught:
            proxy_mod.install_manifest(template)
        self.assertIn("@VERSION@", str(caught.exception))

    def test_a_script_with_no_digest_refuses_to_start(self):
        for broken in (RENDERED.replace(b'SHA256="' + b"d" * 64 + b'"', b""),
                       RENDERED.replace(b"d" * 64, b"not-a-digest")):
            with self.assertRaises(SystemExit):
                proxy_mod.install_manifest(broken)

    def test_a_non_https_artifact_url_refuses_to_start(self):
        broken = RENDERED.replace(b"https://github.com", b"http://github.com")
        with self.assertRaises(SystemExit):
            proxy_mod.install_manifest(broken)

    def test_something_that_is_not_the_installer_refuses_to_start(self):
        with self.assertRaises(SystemExit):
            proxy_mod.install_manifest(b"#!/bin/sh\necho hello\n")


class InstallRouteTests(ProxyTestCase):
    """``GET /install`` -- the one public route that is not an upload.

    Exercised on the *public* listener, because that is the one it exists for:
    ``curl -fsSL https://.../install | sh`` comes from a laptop that has no
    credential yet and, on a first install, no ``insight`` either.

    Everything here is really one assertion said four ways -- the bytes served
    are a constant, and nothing in a request can change which bytes they are.
    """

    def setUp(self):
        super().setUp()
        self.script = RENDERED
        handler = proxy_mod.build_handler(
            self.store, self.allowed, ADMIN, read_routes=False,
            install_script=self.script)
        self.public = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.public_base = "http://127.0.0.1:{}".format(
            self.public.server_address[1])
        threading.Thread(target=self.public.serve_forever, daemon=True).start()
        self.addCleanup(self.public.server_close)
        self.addCleanup(self.public.shutdown)

    def fetch(self, path):
        request = urllib.request.Request(self.public_base + path)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def test_the_installer_is_served_with_no_credential(self):
        status, body, _ = self.fetch("/install")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.script)

    def test_install_sh_is_the_same_bytes(self):
        self.assertEqual(self.fetch("/install.sh")[1], self.fetch("/install")[1])

    def test_it_is_served_as_text_a_shell_can_read(self):
        _, _, headers = self.fetch("/install")
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "public, max-age=300")

    def test_a_traversal_cannot_reach_the_admin_token(self):
        # The route takes no path parameter, so there is nothing to traverse
        # *through* -- this asserts the absence stays absent. /etc/insight is
        # mounted into the container beside allowed.env and admin.token.
        for path in ("/install/../etc/insight/admin.token",
                     "/install/../../etc/insight/allowed.env",
                     "/install.sh/../admin.token"):
            status, body, _ = self.fetch(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"insight installer", body, path)

    def test_a_query_string_changes_nothing(self):
        for path in ("/install?f=/etc/insight/admin.token",
                     "/install?../../etc/passwd", "/install.sh?v=2"):
            status, body, _ = self.fetch(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(body, self.script, path)

    def test_a_path_under_install_is_not_a_route(self):
        for path in ("/install.sh/x", "/install/x", "/install/", "/installer",
                     "/install.json/x"):
            self.assertEqual(self.fetch(path)[0], 404, path)

    def test_the_manifest_is_served_beside_the_script(self):
        status, body, headers = self.fetch("/install.json")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        got = json.loads(body.decode())
        self.assertEqual(got["version"], "0.4.0")
        self.assertEqual(got["schemas"], sorted(proxy_mod.KNOWN_SCHEMAS))

    def test_the_manifest_and_the_script_cannot_disagree(self):
        # Derived from the same bytes, so this is a tautology -- which is the
        # entire point of deriving it rather than configuring it separately.
        manifest = json.loads(self.fetch("/install.json")[1].decode())
        script = self.fetch("/install")[1].decode()
        self.assertIn('VERSION="{}"'.format(manifest["version"]), script)
        self.assertIn('SHA256="{}"'.format(manifest["sha256"]), script)

    def test_the_manifest_carries_no_secret(self):
        body = self.fetch("/install.json")[1]
        self.assertNotIn(ADMIN.encode(), body)
        for person in self.allowed:
            self.assertNotIn(person.encode(), body)

    def test_the_read_routes_are_still_404_on_this_listener(self):
        # Adding a public route must not have opened the others by accident.
        request = urllib.request.Request(
            self.public_base + "/v1/bundles?week=2026-W34",
            headers={"Authorization": "Bearer " + ADMIN})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 404)


class InstallRouteUnsetTests(ProxyTestCase):
    """No ``--install-script`` means the route does not exist.

    404 rather than 503: the same shape as the read-routes guard. A deployment
    that does not serve this does not need to admit the route is there.
    """

    def test_unset_is_404_not_an_empty_200(self):
        status, body = self.get("/install", token=None)
        self.assertEqual(status, 404)
        self.assertEqual(self.get("/install.sh", token=None)[0], 404)
        self.assertEqual(self.get("/install.json", token=None)[0], 404)
        self.assertNotIn(b"#!/bin/sh", body)


class InstallScriptLoadingTests(unittest.TestCase):
    """It is read once, at startup, from a name given on the command line.

    That is not a performance choice. It is the reason the route can have no
    path parameter: there is no per-request filesystem access to point at
    anything, so there is no traversal to find.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="insight-install-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, body):
        path = os.path.join(self.tmp, "install.sh")
        with open(path, "wb") as handle:
            handle.write(body)
        return path

    def test_nothing_configured_is_none_rather_than_an_error(self):
        self.assertIsNone(proxy_mod.load_install_script(None))
        self.assertIsNone(proxy_mod.load_install_script(""))

    def test_a_file_is_read_verbatim(self):
        body = b"#!/bin/sh\nset -eu\nmain \"$@\"\n"
        self.assertEqual(proxy_mod.load_install_script(self.write(body)), body)

    def test_a_missing_file_refuses_to_start_and_names_it(self):
        missing = os.path.join(self.tmp, "nope.sh")
        with self.assertRaises(SystemExit) as caught:
            proxy_mod.load_install_script(missing)
        self.assertIn(missing, str(caught.exception))

    def test_an_empty_file_refuses_to_start(self):
        # Serving zero bytes to `| sh` is a silent no-op install: the shell
        # exits 0 having done nothing, and the engineer types `insight setup`
        # into a machine with no insight on it.
        with self.assertRaises(SystemExit):
            proxy_mod.load_install_script(self.write(b"\n  \n"))

    def test_something_far_too_large_is_not_the_installer(self):
        with self.assertRaises(SystemExit):
            proxy_mod.load_install_script(
                self.write(b"x" * (proxy_mod.MAX_INSTALL_SCRIPT + 1)))


class TraversalTests(ProxyTestCase):
    def test_a_key_cannot_escape_the_store(self):
        status, _ = self.get("/v1/bundle/../../../../etc/passwd")
        self.assertIn(status, (400, 404))

    def test_an_encoded_traversal_cannot_escape_either(self):
        status, _ = self.get("/v1/bundle/bundles%2F..%2F..%2Fetc%2Fpasswd")
        self.assertIn(status, (400, 404))


if __name__ == "__main__":
    unittest.main()


class EnrolmentTests(unittest.TestCase):
    """A laptop registers itself. Nobody relays a fingerprint over chat."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="insight-enroll-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.allowed_path = os.path.join(self.tmp, "allowed.env")
        self.roster_path = os.path.join(self.tmp, "roster.txt")
        with open(self.roster_path, "w", encoding="utf-8") as handle:
            handle.write("# who is expected\n{}\n".format(CANH))
        self.people = registry_mod.Registry(
            allowed_path=self.allowed_path, roster_path=self.roster_path)
        self.store = store_mod.FileStore(os.path.join(self.tmp, "store"))
        handler = proxy_mod.build_handler(self.store, self.people, ADMIN)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = "http://127.0.0.1:{}".format(self.httpd.server_address[1])
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def call(self, method, path, payload=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.base + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def enroll(self, email, secret, token=None):
        return self.call("POST", "/v1/enroll",
                         {"email": email,
                          "fingerprint": identity.fingerprint(secret)},
                         token=token)

    # -- the path that replaces a person ----------------------------------

    def test_someone_on_the_roster_enrols_and_can_upload_immediately(self):
        secret = identity.mint_secret()
        status, body = self.enroll(CANH, secret)
        self.assertEqual(status, 201)
        self.assertEqual(body["outcome"], "created")
        # No restart, no reload command: the upload path sees it at once.
        self.assertEqual(self.people.identify(secret), CANH)

    def test_it_is_written_down_so_a_restart_does_not_forget(self):
        secret = identity.mint_secret()
        self.enroll(CANH, secret)
        reopened = registry_mod.Registry(allowed_path=self.allowed_path,
                                         roster_path=self.roster_path)
        self.assertEqual(reopened.identify(secret), CANH)

    def test_re_running_setup_is_not_an_error(self):
        secret = identity.mint_secret()
        self.enroll(CANH, secret)
        status, body = self.enroll(CANH, secret)
        self.assertEqual(status, 200)
        self.assertEqual(body["outcome"], "known")

    # -- what it refuses ---------------------------------------------------

    def test_an_address_nobody_expects_is_refused(self):
        status, body = self.enroll(MINH, identity.mint_secret())
        self.assertEqual(status, 403)
        self.assertIn("not expected", body["error"])

    def test_knowing_a_colleagues_address_is_not_enough_to_upload_as_them(self):
        # Trust on first use. Once an entry exists, a second fingerprint for
        # the same person needs an admin -- otherwise the roster alone would
        # be the credential.
        theirs = identity.mint_secret()
        self.enroll(CANH, theirs)
        impostor = identity.mint_secret()
        status, _ = self.enroll(CANH, impostor)
        self.assertEqual(status, 409)
        self.assertIsNone(self.people.identify(impostor))
        self.assertEqual(self.people.identify(theirs), CANH)

    def test_a_fingerprint_that_is_not_a_digest_is_refused(self):
        status, _ = self.call("POST", "/v1/enroll",
                              {"email": CANH, "fingerprint": "../../etc/passwd"})
        self.assertEqual(status, 400)

    # -- rotation ----------------------------------------------------------

    def test_a_machine_holding_a_working_secret_may_rotate_itself(self):
        first = identity.mint_secret()
        self.enroll(CANH, first)
        second = identity.mint_secret()
        status, body = self.enroll(CANH, second, token=first)
        self.assertEqual(status, 200)
        self.assertEqual(body["outcome"], "rotated")
        # Both work through the window, which is what lets a rotation finish
        # without the two sides changing in the same minute.
        self.assertEqual(self.people.identify(second), CANH)
        self.assertEqual(self.people.identify(first), CANH)

    def test_a_secret_cannot_rotate_somebody_else(self):
        mine = identity.mint_secret()
        self.enroll(CANH, mine)
        self.people.add_person(MINH)
        status, _ = self.enroll(MINH, identity.mint_secret(), token=mine)
        self.assertEqual(status, 403)

    # -- the admin routes --------------------------------------------------

    def test_an_admin_adds_a_person_over_http_not_by_editing_a_file(self):
        status, body = self.call("POST", "/v1/people", {"email": MINH},
                                 token=ADMIN)
        self.assertEqual(status, 201)
        self.assertEqual(body["results"][0]["outcome"], "added")
        self.assertEqual(self.enroll(MINH, identity.mint_secret())[0], 201)

    def test_adding_people_needs_the_admin_token(self):
        status, _ = self.call("POST", "/v1/people", {"email": MINH})
        self.assertEqual(status, 401)

    def test_listing_says_who_is_expected_and_has_not_arrived(self):
        self.call("POST", "/v1/people", {"email": MINH}, token=ADMIN)
        self.enroll(CANH, identity.mint_secret())
        status, body = self.call("GET", "/v1/people", token=ADMIN)
        self.assertEqual(status, 200)
        self.assertEqual(body["expected"], 2)
        self.assertEqual(body["enrolled"], 1)
        self.assertEqual(body["waiting"], [MINH])

    def test_listing_needs_the_admin_token(self):
        self.assertEqual(self.call("GET", "/v1/people")[0], 401)

    def test_a_replacement_laptop_is_a_reset_not_a_fingerprint(self):
        old = identity.mint_secret()
        self.enroll(CANH, old)
        status, _ = self.call("POST", "/v1/people/reset", {"email": CANH},
                              token=ADMIN)
        self.assertEqual(status, 200)
        self.assertIsNone(self.people.identify(old))
        new = identity.mint_secret()
        self.assertEqual(self.enroll(CANH, new)[0], 201)

    def test_removing_someone_stops_uploads_and_says_data_is_not_deleted(self):
        secret = identity.mint_secret()
        self.enroll(CANH, secret)
        status, body = self.call(
            "DELETE", "/v1/people?email=" + CANH, token=ADMIN)
        self.assertEqual(status, 200)
        self.assertIsNone(self.people.identify(secret))
        self.assertIn("not deleted", body["detail"])
        # Off the roster too, so they cannot simply enrol again.
        self.assertEqual(self.enroll(CANH, identity.mint_secret())[0], 403)

    def test_a_hand_edited_file_is_still_obeyed_without_a_restart(self):
        # The whole point of keeping these as files: an operator who edits one
        # does not have to be told about an API.
        secret = identity.mint_secret()
        with open(self.allowed_path, "w", encoding="utf-8") as handle:
            handle.write(identity.whitelist_line(MINH, secret) + "\n")
        self.assertEqual(self.people.identify(secret), MINH)


class DockerfileTests(unittest.TestCase):
    """The image has to carry every module the process imports.

    `server/registry.py` was added and not named in the Dockerfile. That
    container starts, passes its health check, and fails on the first request
    that reaches the missing import -- which is a worse failure than not
    starting, because the health check says it is fine.
    """

    def test_every_server_module_is_copied_into_the_image(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        dockerfile = os.path.join(root, "Dockerfile")
        with open(dockerfile, "r", encoding="utf-8") as handle:
            text = handle.read()
        for name in sorted(os.listdir(root)):
            if not name.endswith(".py") or name.startswith("test"):
                continue
            if name in ("verify_s3.py",):   # a diagnostic, run from a checkout
                continue
            self.assertIn("server/" + name, text,
                          "{} is not COPYd into the image".format(name))


class ProjectTests(unittest.TestCase):
    """A project is a team and the Jira boards its work lives on.

    It exists for one reason: a laptop cannot know which project keys are real,
    and a reader that does not know invents them. Measured 2026-08-26,
    `fix/AUG-25` became ticket "AUG-25" on 28 of 28 events from one machine.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="registry-projects-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.allowed = os.path.join(self.dir, "allowed.env")
        self.roster = os.path.join(self.dir, "roster.txt")
        self.projects = os.path.join(self.dir, "projects.env")
        with open(self.roster, "w", encoding="utf-8") as handle:
            handle.write("ngoc.nguyen@aeris.net\nlinh.hoang@aeris.net\n")
        self.registry = registry_mod.Registry(
            allowed_path=self.allowed, roster_path=self.roster,
            projects_path=self.projects)

    def test_a_member_is_told_their_boards(self):
        self.registry.set_project(
            "WatchtowerQD", ["IML", "APR", "AERLABS"],
            ["ngoc.nguyen@aeris.net", "linh.hoang@aeris.net"])
        self.assertEqual(self.registry.boards_for("ngoc.nguyen@aeris.net"),
                         ["AERLABS", "APR", "IML"])

    def test_someone_on_no_project_is_told_nothing_not_everything(self):
        """Empty is safe: an empty allow-list emits no key rather than any."""
        self.registry.set_project("WatchtowerQD", ["IML"],
                                  ["ngoc.nguyen@aeris.net"])
        self.assertEqual(self.registry.boards_for("stranger@aeris.net"), [])

    def test_boards_are_upper_cased_and_members_lower_cased(self):
        self.registry.set_project("WatchtowerQD", ["iml", "Apr"],
                                  ["Ngoc.Nguyen@Aeris.net"])
        self.assertEqual(self.registry.boards_for("ngoc.nguyen@aeris.net"),
                         ["APR", "IML"])

    def test_a_second_project_onboards_without_touching_the_first(self):
        self.registry.set_project("WatchtowerQD", ["IML"],
                                  ["ngoc.nguyen@aeris.net"])
        self.registry.set_project("Nightwatch", ["AERLABS"],
                                  ["linh.hoang@aeris.net"])
        self.assertEqual(self.registry.boards_for("ngoc.nguyen@aeris.net"),
                         ["IML"])
        self.assertEqual(self.registry.boards_for("linh.hoang@aeris.net"),
                         ["AERLABS"])
        self.assertEqual(sorted(self.registry.projects()),
                         ["Nightwatch", "WatchtowerQD"])

    def test_someone_on_two_projects_gets_the_union(self):
        """A person really can work across teams. Narrowing would drop keys."""
        self.registry.set_project("WatchtowerQD", ["IML"],
                                  ["ngoc.nguyen@aeris.net"])
        self.registry.set_project("Nightwatch", ["AERLABS"],
                                  ["ngoc.nguyen@aeris.net"])
        self.assertEqual(self.registry.boards_for("ngoc.nguyen@aeris.net"),
                         ["AERLABS", "IML"])

    def test_setting_a_project_replaces_rather_than_merges(self):
        """An admin removing a board means it."""
        self.registry.set_project("WatchtowerQD", ["IML", "APR"],
                                  ["ngoc.nguyen@aeris.net"])
        self.registry.set_project("WatchtowerQD", ["IML"],
                                  ["ngoc.nguyen@aeris.net"])
        self.assertEqual(self.registry.boards_for("ngoc.nguyen@aeris.net"),
                         ["IML"])

    def test_a_hand_edit_is_picked_up_without_a_restart(self):
        self.registry.set_project("WatchtowerQD", ["IML"],
                                  ["ngoc.nguyen@aeris.net"])
        with open(self.projects, "w", encoding="utf-8") as handle:
            handle.write("WatchtowerQD:IML,APR:ngoc.nguyen@aeris.net\n")
        self.assertEqual(self.registry.boards_for("ngoc.nguyen@aeris.net"),
                         ["APR", "IML"])

    def test_no_projects_file_at_all_is_not_an_error(self):
        """Deployments that have not defined one keep working, telling nobody
        anything -- which is the safe direction."""
        bare = registry_mod.Registry(allowed_path=self.allowed,
                                 roster_path=self.roster)
        self.assertEqual(bare.boards_for("ngoc.nguyen@aeris.net"), [])
        self.assertEqual(bare.projects(), {})
