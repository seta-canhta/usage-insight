"""Tests for the insights app: the asset map, its routes, and the snapshot.

    python3 -m unittest discover -s server/tests

Driven over real HTTP against a real filesystem store, like ``test_proxy.py``
and ``test_dashboard.py``, because what is worth asserting is the contract the
frontend was written against -- which paths exist on which listener, what a
cookie is worth, and what comes back when there is nothing to show.

Three properties get the attention here, and each of them is a rule this
project has already written down:

* **No path from a request reaches the filesystem.** ``/dashboard`` gets that
  by holding one fixed file; a bundle of many files gets it by exact-key
  lookup into a dict built at startup. The traversal tests assert the absence
  of a filesystem step stays absent -- ``/etc/insight`` is mounted beside the
  code and holds every engineer's fingerprint next to the admin token.
* **Absent is never zero.** A snapshot that has not been generated is a 503
  with a sentence, not ``{}`` and not a page of noughts.
* **The routes are not on the public listener.** ``read_routes=False`` is the
  listener that faces the internet, and these screens are the whole team.
"""

import http.client
import http.cookies
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

import dashboard as dashboard_mod  # noqa: E402
import identity  # noqa: E402
import proxy as proxy_mod  # noqa: E402
import registry as registry_mod  # noqa: E402
import store as store_mod  # noqa: E402
import webapp as webapp_mod  # noqa: E402

ADMIN = "admin-token-for-tests"
PASSCODE = "981022"
NGOC = "ngoc.nguyen@aeris.net"
LINH = "linh.hoang@aeris.net"

INDEX = b"<!doctype html><title>Insights</title><div id=\"root\"></div>"
SCRIPT = b"export const screens = ['insights', 'activities'];\n"
STYLE = b":root{color-scheme:light dark}\n"
LOGO = b"<svg xmlns='http://www.w3.org/2000/svg'/>"

SNAPSHOT = {"generated_at": "2026-08-27T09:00:00Z", "people": [], "metrics": {}}


def build_app(root):
    """A stand-in for what the frontend build writes. No React here.

    Deliberately a fixture rather than a checked-in directory: the real one is
    produced by a build that is not part of a checkout, and a test that needed
    it to have been run would fail for the wrong reason.
    """
    os.makedirs(os.path.join(root, "assets"), exist_ok=True)
    files = {
        "index.html": INDEX,
        "assets/index-DiwrgTda.js": SCRIPT,
        "assets/index-a1b2c3d4.css": STYLE,
        "assets/logo.svg": LOGO,
        "notes.txt": b"a file the build left behind",
    }
    for name, body in files.items():
        with open(os.path.join(root, name.replace("/", os.sep)), "wb") as handle:
            handle.write(body)
    return root


# --------------------------------------------------------------------------
# the asset map
# --------------------------------------------------------------------------

class AssetMapTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="insights-app-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = build_app(os.path.join(self.dir, "app"))

    def test_it_is_keyed_by_the_exact_relative_path(self):
        bundle = webapp_mod.load_app(self.root)
        self.assertTrue(bundle.built)
        self.assertEqual(sorted(bundle.files), [
            "assets/index-DiwrgTda.js", "assets/index-a1b2c3d4.css",
            "assets/logo.svg", "index.html"])
        self.assertEqual(bundle.get("index.html").body, INDEX)

    def test_a_key_is_never_a_file_name(self):
        # The whole property, asserted at the level it is implemented: nothing
        # here is joined, resolved or normalised, so every spelling of "the
        # file next door" is simply a key nobody put in the dict.
        bundle = webapp_mod.load_app(self.root)
        for key in ("../dashboard.html", "/index.html", "./index.html",
                    "assets/../index.html", "%2e%2e%2findex.html",
                    "..\\index.html", ""):
            self.assertIsNone(bundle.get(key), key)

    def test_a_type_it_does_not_serve_is_not_loaded_at_all(self):
        # An allow-list, so a file the build did not mean to ship cannot become
        # servable because the host knows a media type for it.
        bundle = webapp_mod.load_app(self.root)
        self.assertNotIn("notes.txt", bundle.files)
        self.assertIn("notes.txt", bundle.skipped)

    def test_content_types_come_from_the_extension(self):
        bundle = webapp_mod.load_app(self.root)
        self.assertEqual(bundle.get("index.html").content_type,
                         "text/html; charset=utf-8")
        self.assertEqual(bundle.get("assets/index-DiwrgTda.js").content_type,
                         "text/javascript; charset=utf-8")
        self.assertEqual(bundle.get("assets/logo.svg").content_type,
                         "image/svg+xml")

    def test_only_a_hashed_name_is_cached_forever(self):
        # The false positive is the dangerous direction: a year of immutable
        # caching on a name that will be reused cannot be undone from here.
        for name in ("assets/index-DiwrgTda.js", "assets/index-a1b2c3d4.css",
                     "assets/font-9f8e7d6c.woff2"):
            self.assertEqual(webapp_mod.cache_control_for(name),
                             webapp_mod.IMMUTABLE, name)
        for name in ("index.html", "assets/logo.svg", "assets/my-component.js",
                     "assets/app.js", "nested/index-DiwrgTda.html"):
            self.assertEqual(webapp_mod.cache_control_for(name), "no-store", name)

    def test_a_directory_that_is_not_there_is_not_an_error(self):
        # `dashboard.load_page` exits on a missing file and is right to; this
        # directory is written by a build that is not part of a checkout, so
        # its absence must not take the upload endpoint down with it.
        bundle = webapp_mod.load_app(os.path.join(self.dir, "never-built"))
        self.assertFalse(bundle.built)
        self.assertEqual(bundle.files, {})

    def test_a_half_run_build_reads_as_not_built(self):
        empty = os.path.join(self.dir, "half")
        os.makedirs(os.path.join(empty, "assets"))
        with open(os.path.join(empty, "assets", "index-DiwrgTda.js"), "wb") as h:
            h.write(SCRIPT)
        self.assertFalse(webapp_mod.load_app(empty).built)


# --------------------------------------------------------------------------
# the snapshot
# --------------------------------------------------------------------------

class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="insights-snapshot-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "insights.json")

    def write(self, payload):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True))

    def test_absent_raises_rather_than_returning_an_empty_document(self):
        with self.assertRaises(webapp_mod.SnapshotMissing):
            webapp_mod.Snapshot(self.path).body()

    def test_a_redeploy_is_picked_up_without_a_restart(self):
        self.write(SNAPSHOT)
        snapshot = webapp_mod.Snapshot(self.path)
        self.assertEqual(json.loads(snapshot.body())["people"], [])
        self.write(dict(SNAPSHOT, people=[{"label": NGOC}]))
        self.assertEqual(json.loads(snapshot.body())["people"],
                         [{"label": NGOC}])

    def test_a_half_written_file_is_refused_not_served(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write('{"people": [{"label": "ngoc"')
        with self.assertRaises(webapp_mod.SnapshotMissing):
            webapp_mod.Snapshot(self.path).body()

    def test_a_file_that_reappears_is_read_again(self):
        # The failure is not cached: the generator writing the file is the
        # ordinary way this stops being missing.
        snapshot = webapp_mod.Snapshot(self.path)
        with self.assertRaises(webapp_mod.SnapshotMissing):
            snapshot.body()
        self.write(SNAPSHOT)
        self.assertEqual(json.loads(snapshot.body())["metrics"], {})


# --------------------------------------------------------------------------
# the routes
# --------------------------------------------------------------------------

class AppRouteTestCase(unittest.TestCase):
    read_routes = True
    passcode = PASSCODE
    with_app = True
    with_snapshot = True

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="insights-routes-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        allowed = os.path.join(self.tmp, "allowed.env")
        roster = os.path.join(self.tmp, "roster.txt")
        with open(allowed, "w", encoding="utf-8") as handle:
            handle.write("{}:{}\n".format(NGOC, identity.fingerprint(
                identity.mint_secret())))
        with open(roster, "w", encoding="utf-8") as handle:
            handle.write("{}\n{}\n".format(NGOC, LINH))

        # A secret in the directory *above* the app root, which is how the
        # container is laid out: /etc/insight holds allowed.env and
        # admin.token beside whatever is being served.
        self.secret_file = os.path.join(self.tmp, "admin.token")
        with open(self.secret_file, "w", encoding="utf-8") as handle:
            handle.write(ADMIN)

        self.app_dir = os.path.join(self.tmp, "app")
        if self.with_app:
            build_app(self.app_dir)

        self.snapshot_path = os.path.join(self.tmp, "insights.json")
        if self.with_snapshot:
            with open(self.snapshot_path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(SNAPSHOT, sort_keys=True))

        self.store = store_mod.FileStore(os.path.join(self.tmp, "store"))
        people = registry_mod.Registry(allowed_path=allowed, roster_path=roster)
        daybook = None
        if self.passcode:
            daybook = {
                "sessions": dashboard_mod.Sessions(self.passcode),
                "attendance": dashboard_mod.Attendance(
                    os.path.join(self.tmp, "attendance.tsv")),
                "activity": dashboard_mod.ActivityIndex(
                    self.store, dashboard_mod.parse_offset("+07:00")),
                "daybook_page": dashboard_mod.load_page(),
                "tz_label": "+07:00",
                "insights_app": webapp_mod.load_app(self.app_dir),
                "snapshot": webapp_mod.Snapshot(self.snapshot_path),
            }
        handler = proxy_mod.build_handler(
            self.store, people, ADMIN, read_routes=self.read_routes,
            daybook=daybook)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.base = "http://127.0.0.1:{}".format(self.port)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.cookie = None

    def call(self, method, path, body=None, cookie=True):
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body).encode()
        if cookie and self.cookie:
            headers["Cookie"] = self.cookie
        request = urllib.request.Request(
            self.base + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers

    def raw(self, path):
        """A request line sent verbatim.

        ``urllib`` is not asked to build these: the point of a traversal test
        is that the server sees the characters the attacker typed, and a
        client that tidied them up first would be testing the client.
        """
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("GET", path, skip_accept_encoding=True)
            conn.endheaders()
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def open_it(self, passcode=PASSCODE):
        status, _, headers = self.call(
            "POST", "/dashboard/login", {"passcode": passcode}, cookie=False)
        raw = headers.get("Set-Cookie")
        if raw:
            jar = http.cookies.SimpleCookie()
            jar.load(raw)
            self.cookie = "{}={}".format(dashboard_mod.COOKIE,
                                         jar[dashboard_mod.COOKIE].value)
        return status


class ScreenTests(AppRouteTestCase):
    def test_both_screens_are_the_same_bytes(self):
        # One bundle, two URLs: which screen is shown is decided in the
        # browser, so anything else here would be two apps to keep in step.
        first = self.call("GET", "/insights")
        second = self.call("GET", "/activities")
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200)
        self.assertEqual(first[1], second[1])
        self.assertEqual(first[1], INDEX)

    def test_a_trailing_slash_is_the_same_screen(self):
        for path in ("/insights/", "/activities/"):
            status, body, _ = self.call("GET", path)
            self.assertEqual(status, 200, path)
            self.assertEqual(body, INDEX, path)

    def test_the_shell_is_served_without_a_session(self):
        # It carries no data, and a sign-in form nobody can fetch is not a
        # sign-in form. The same call the daybook makes.
        self.assertIsNone(self.cookie)
        self.assertEqual(self.call("GET", "/insights")[0], 200)

    def test_the_entry_point_is_never_cached(self):
        _, _, headers = self.call("GET", "/insights")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_the_daybook_is_untouched(self):
        status, body, _ = self.call("GET", "/dashboard")
        self.assertEqual(status, 200)
        self.assertIn(b"Daybook", body)


class AssetRouteTests(AppRouteTestCase):
    def test_an_asset_is_served_by_its_exact_key(self):
        status, body, headers = self.call("GET", "/app/assets/index-DiwrgTda.js")
        self.assertEqual(status, 200)
        self.assertEqual(body, SCRIPT)
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], webapp_mod.IMMUTABLE)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_an_unhashed_asset_is_not_cached(self):
        _, body, headers = self.call("GET", "/app/assets/logo.svg")
        self.assertEqual(body, LOGO)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Content-Type"], "image/svg+xml")

    def test_a_file_the_map_does_not_hold_is_404(self):
        for path in ("/app/notes.txt", "/app/assets/missing.js", "/app/",
                     "/app/assets/", "/app/index.html/x"):
            self.assertEqual(self.call("GET", path)[0], 404, path)

    def test_a_traversal_cannot_reach_what_is_beside_the_app(self):
        # The route takes no file name, so there is nothing to traverse
        # *through* -- this asserts the absence stays absent. Every one of
        # these is a key that is not in the dict.
        for path in ("/app/../admin.token",
                     "/app/../../etc/insight/admin.token",
                     "/app/%2e%2e%2fadmin.token",
                     "/app/%2e%2e/%2e%2e/etc/passwd",
                     "/app/....//admin.token",
                     "/app//admin.token",
                     "/app/assets/../../admin.token",
                     "/app/..%5cadmin.token"):
            status, body = self.raw(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(ADMIN.encode(), body, path)

    def test_an_absolute_path_is_just_a_missing_key(self):
        for path in ("/app//etc/passwd", "/app/" + self.secret_file.lstrip("/")):
            status, body = self.raw(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(ADMIN.encode(), body, path)

    def test_a_query_string_changes_nothing(self):
        status, body, _ = self.call(
            "GET", "/app/assets/logo.svg?v=2&f=../admin.token")
        self.assertEqual(status, 200)
        self.assertEqual(body, LOGO)


class SnapshotRouteTests(AppRouteTestCase):
    def test_the_snapshot_needs_a_session(self):
        self.assertEqual(self.call("GET", "/insights/data")[0], 401)

    def test_a_session_gets_the_snapshot(self):
        self.open_it()
        status, body, headers = self.call("GET", "/insights/data")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), SNAPSHOT)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_the_cookie_reaches_this_path_at_all(self):
        # Scoped to /dashboard the cookie is simply not sent here, and that
        # looks exactly like a passcode that does not work.
        _, _, headers = self.call(
            "POST", "/dashboard/login", {"passcode": PASSCODE}, cookie=False)
        self.assertIn("Path=/;", headers.get("Set-Cookie"))

    def test_a_regenerated_snapshot_needs_no_restart(self):
        self.open_it()
        with open(self.snapshot_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(SNAPSHOT, people=[{"label": LINH}]),
                                    sort_keys=True))
        _, body, _ = self.call("GET", "/insights/data")
        self.assertEqual(json.loads(body)["people"], [{"label": LINH}])


class NoSnapshotTests(AppRouteTestCase):
    """The file has not been generated. Absent is never zero."""

    with_snapshot = False

    def test_it_is_a_503_that_says_what_is_missing(self):
        self.open_it()
        status, body, _ = self.call("GET", "/insights/data")
        self.assertEqual(status, 503)
        payload = json.loads(body)
        self.assertIn("has not been generated", payload["error"])
        self.assertIn("dashboard_data.py", payload["detail"])

    def test_it_is_not_an_empty_document_and_not_zeros(self):
        # The failure this exists to prevent: a page of noughts that reads as
        # a team who did nothing, when nothing has been measured at all.
        self.open_it()
        _, body, _ = self.call("GET", "/insights/data")
        payload = json.loads(body)
        self.assertNotEqual(payload, {})
        self.assertNotIn("people", payload)
        self.assertNotIn("metrics", payload)

    def test_it_still_needs_a_session(self):
        # A 503 that anybody can ask for would be a way to learn the endpoint
        # serves this at all. The cookie comes first.
        self.assertEqual(self.call("GET", "/insights/data")[0], 401)

    def test_the_screens_are_unaffected(self):
        self.assertEqual(self.call("GET", "/insights")[0], 200)


class NotBuiltTests(AppRouteTestCase):
    """No ``server/assets/app``: the frontend has not been built."""

    with_app = False

    def test_the_server_starts_and_the_daybook_still_works(self):
        self.assertEqual(self.call("GET", "/healthz")[0], 200)
        self.assertEqual(self.call("GET", "/dashboard")[0], 200)

    def test_the_screens_say_so_rather_than_hanging_or_500ing(self):
        for path in ("/insights", "/activities", "/app/index.html"):
            status, body, _ = self.call("GET", path)
            self.assertEqual(status, 404, path)
            self.assertIn("not built", json.loads(body)["error"], path)

    def test_the_snapshot_does_not_depend_on_the_frontend(self):
        # The numbers are produced by report/dashboard_data.py and are worth
        # fetching whether or not anybody has run a build.
        self.open_it()
        self.assertEqual(self.call("GET", "/insights/data")[0], 200)


class NoPasscodeTests(AppRouteTestCase):
    """No passcode configured: the routes do not exist. Never a 403."""

    passcode = ""

    def test_every_path_404s(self):
        for path in ("/insights", "/activities", "/insights/data",
                     "/app/index.html", "/app/assets/index-DiwrgTda.js"):
            self.assertEqual(self.call("GET", path)[0], 404, path)

    def test_the_rest_of_the_endpoint_is_untouched(self):
        self.assertEqual(self.call("GET", "/healthz")[0], 200)


class PublicListenerTests(AppRouteTestCase):
    """The listener that faces the internet. These screens are the whole team."""

    read_routes = False

    def test_none_of_it_is_on_the_public_listener(self):
        for path in ("/insights", "/activities", "/insights/data",
                     "/app/index.html", "/app/assets/index-DiwrgTda.js"):
            status, body, _ = self.call("GET", path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(INDEX, body, path)

    def test_not_even_with_the_admin_token(self):
        request = urllib.request.Request(
            self.base + "/insights",
            headers={"Authorization": "Bearer " + ADMIN})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 404)

    def test_uploads_still_work_here(self):
        self.assertEqual(self.call("GET", "/healthz")[0], 200)


if __name__ == "__main__":
    unittest.main()
