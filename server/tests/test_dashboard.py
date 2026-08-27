"""Tests for the daybook: attendance, the activity index, and the routes.

    python3 -m unittest discover -s server/tests

The route tests go over real HTTP against a real filesystem store, like
``test_proxy.py``, because what is worth asserting is the contract the page was
written against -- which paths exist on which listener, what a cookie is worth,
and what a wrong passcode gets.

Two properties get the most attention here, because both have already been got
wrong somewhere in this project:

* **null is not 0.** A day inside a bundle's window with no events is a
  measured zero; a day no bundle covers is unmeasured. If those ever collapse
  into one value the page cannot draw them differently, and the difference is
  the reason the page exists.
* **The routes are not on the public listener.** ``read_routes=False`` is the
  listener that faces the internet, and the daybook lists every engineer.
"""

import http.cookies
import json
import os
import shutil
import sys
import tempfile
import threading
import time
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

ADMIN = "admin-token-for-tests"
PASSCODE = "981022"
NGOC = "ngoc.nguyen@aeris.net"
LINH = "linh.hoang@aeris.net"


def bundle(events, window_start, window_end, machine="a3f9c2b1"):
    """An ndjson bundle: a manifest line, then one line per event."""
    body = "".join(json.dumps(e, sort_keys=True) + "\n" for e in events)
    manifest = {
        "format": "seta-insight-bundle/1", "schema_version": "1.1.0",
        "machine_id": machine, "packed_at": window_end,
        "window_start": window_start, "window_end": window_end,
        "event_count": len(events),
    }
    return (json.dumps({"_manifest": manifest}, sort_keys=True) + "\n" + body).encode()


def event(when, kind="model.call"):
    return {"event_id": "evt_" + when.replace(":", ""), "event_time": when,
            "event_type": kind}


# --------------------------------------------------------------------------
# attendance
# --------------------------------------------------------------------------

class AttendanceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="daybook-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "attendance.tsv")
        self.book = dashboard_mod.Attendance(self.path)

    def test_a_day_survives_a_reread(self):
        self.book.set(NGOC, "2026-08-25", "out", "09:12", "18:40")
        again = dashboard_mod.Attendance(self.path)
        row = again.between("2026-08-25", "2026-08-25")[NGOC]["2026-08-25"]
        self.assertEqual(row, {"state": "out", "in": "09:12", "out": "18:40"})

    def test_clearing_is_not_the_same_as_off(self):
        # "not working" and "nobody has said" are different facts, and the page
        # draws them differently. Clearing has to remove the line, not write
        # one that says off.
        self.book.set(NGOC, "2026-08-25", "off")
        self.assertEqual(self.book.count(), 1)
        self.book.set(NGOC, "2026-08-25", None)
        self.assertEqual(self.book.count(), 0)
        self.assertEqual(self.book.between("2026-08-01", "2026-08-31"), {})

    def test_a_day_off_carries_no_clock(self):
        # A row that says both "not working" and "arrived at 09:12" is a row
        # somebody will eventually believe.
        row = self.book.set(NGOC, "2026-08-25", "off", "09:12", "18:40")
        self.assertEqual(row, {"state": "off", "in": "", "out": ""})

    def test_out_before_in_is_refused(self):
        with self.assertRaises(dashboard_mod.DashboardError):
            self.book.set(NGOC, "2026-08-25", "out", "18:40", "09:12")

    def test_junk_is_refused_rather_than_guessed_at(self):
        for bad in [
            (NGOC, "25-08-2026", "in", "", ""),
            (NGOC, "2026-08-25", "maybe", "", ""),
            (NGOC, "2026-08-25", "in", "9am", ""),
            ("", "2026-08-25", "in", "", ""),
        ]:
            with self.assertRaises(dashboard_mod.DashboardError):
                self.book.set(*bad)

    def test_a_hand_edit_is_picked_up_without_a_restart(self):
        self.book.set(NGOC, "2026-08-25", "in")
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write("{}\t2026-08-26\toff\t\t\n".format(LINH))
        found = self.book.between("2026-08-01", "2026-08-31")
        self.assertEqual(found[LINH]["2026-08-26"]["state"], "off")

    def test_an_unreadable_line_is_dropped_not_guessed(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("# comment\n\n")
            handle.write("nobody\tnot-a-date\tin\t\t\n")
            handle.write("{}\t2026-08-25\tin\t09:00\t\n".format(NGOC))
        book = dashboard_mod.Attendance(self.path)
        self.assertEqual(book.count(), 1)

    def test_the_range_is_bounded(self):
        with self.assertRaises(dashboard_mod.DashboardError):
            dashboard_mod.day_range("2020-01-01", "2026-01-01")
        with self.assertRaises(dashboard_mod.DashboardError):
            dashboard_mod.day_range("2026-08-10", "2026-08-01")


# --------------------------------------------------------------------------
# activity
# --------------------------------------------------------------------------

class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="daybook-store-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = store_mod.FileStore(self.dir)
        self.tz = dashboard_mod.parse_offset("+07:00")
        self.key = "bundles/2026-W35/{}/a3f9c2b1-deadbeef.ndjson".format(
            identity.person_key(NGOC))

    def index(self):
        activity = dashboard_mod.ActivityIndex(self.store, self.tz)
        activity.refresh()
        return activity

    def test_a_covered_day_with_no_events_is_zero_not_absent(self):
        self.store.put(self.key, bundle(
            [event("2026-08-24T03:00:00Z")],
            "2026-08-24T00:00:00Z", "2026-08-26T23:59:59Z"))
        found = self.index().by_person("2026-08-24", "2026-08-28")
        slot = found[identity.person_key(NGOC)]
        self.assertIn("2026-08-25", slot["covered"])     # measured
        self.assertNotIn("2026-08-25", slot["days"])     # and it was zero
        self.assertNotIn("2026-08-27", slot["covered"])  # nobody was looking

    def test_events_are_bucketed_into_local_days(self):
        # 2026-08-24T18:30Z is 01:30 on the 25th in Hanoi. Bucketing on the UTC
        # date would file an evening's work under the wrong day.
        self.store.put(self.key, bundle(
            [event("2026-08-24T18:30:00Z")],
            "2026-08-24T00:00:00Z", "2026-08-25T23:59:59Z"))
        slot = self.index().by_person("2026-08-24", "2026-08-26")[
            identity.person_key(NGOC)]
        self.assertEqual(sorted(slot["days"]), ["2026-08-25"])

    def test_only_times_and_type_names_are_read(self):
        # Rule 1: never store content. Nothing here may keep an attribute, a
        # prompt, a repo name or a path.
        self.store.put(self.key, bundle([{
            "event_id": "evt_1",
            "event_time": "2026-08-25T03:00:00Z",
            "event_type": "tool.call",
            "attributes": {"prompt": "SECRET", "repo_full_name": "acme/thing"},
            "context": {"branch_name": "fix/AUG-25"},
        }], "2026-08-25T00:00:00Z", "2026-08-25T23:59:59Z"))
        kept = json.dumps(self.index().by_person("2026-08-25", "2026-08-25"),
                          default=sorted)
        self.assertNotIn("SECRET", kept)
        self.assertNotIn("acme/thing", kept)
        self.assertNotIn("AUG-25", kept)
        self.assertIn("tool.call", kept)

    def test_a_corrupt_window_paints_no_coverage(self):
        # A manifest claiming years of window would draw months of false
        # coverage across one person's row.
        self.store.put(self.key, bundle(
            [], "2019-01-01T00:00:00Z", "2026-08-25T23:59:59Z"))
        found = self.index().by_person("2026-08-01", "2026-08-25")
        self.assertEqual(found[identity.person_key(NGOC)]["covered"], set())

    def test_a_second_pass_reads_nothing_twice(self):
        self.store.put(self.key, bundle(
            [event("2026-08-25T03:00:00Z")],
            "2026-08-25T00:00:00Z", "2026-08-25T23:59:59Z"))
        activity = dashboard_mod.ActivityIndex(self.store, self.tz)
        self.assertEqual(activity.refresh(), 0)
        reads = []
        original = self.store.get
        self.store.get = lambda key: (reads.append(key), original(key))[1]
        activity._listed_at = 0
        activity.refresh()
        self.assertEqual(reads, [])   # keys are digests; nothing can change

    def test_the_scan_is_budgeted_and_says_what_is_left(self):
        for n in range(5):
            self.store.put("bundles/2026-W35/{}/a3f9c2b1-{:064x}.ndjson".format(
                identity.person_key(NGOC), n), bundle(
                [], "2026-08-25T00:00:00Z", "2026-08-25T23:59:59Z"))
        activity = dashboard_mod.ActivityIndex(self.store, self.tz)
        self.assertEqual(activity.refresh(budget=2), 3)
        self.assertEqual(activity.refresh(budget=2), 1)
        self.assertEqual(activity.refresh(budget=2), 0)


# --------------------------------------------------------------------------
# the routes
# --------------------------------------------------------------------------

class RouteTestCase(unittest.TestCase):
    read_routes = True
    passcode = PASSCODE

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="daybook-routes-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        allowed = os.path.join(self.tmp, "allowed.env")
        roster = os.path.join(self.tmp, "roster.txt")
        with open(allowed, "w", encoding="utf-8") as handle:
            handle.write("{}:{}\n".format(NGOC, identity.fingerprint(
                identity.mint_secret())))
        with open(roster, "w", encoding="utf-8") as handle:
            handle.write("{}\n{}\n".format(NGOC, LINH))

        self.store = store_mod.FileStore(os.path.join(self.tmp, "store"))
        self.store.put("bundles/2026-W35/{}/a3f9c2b1-cafe.ndjson".format(
            identity.person_key(NGOC)), bundle(
            [event("2026-08-25T03:00:00Z"), event("2026-08-25T04:00:00Z")],
            "2026-08-24T00:00:00Z", "2026-08-26T23:59:59Z"))

        self.attendance = os.path.join(self.tmp, "attendance.tsv")
        people = registry_mod.Registry(allowed_path=allowed, roster_path=roster)
        daybook = None
        if self.passcode:
            daybook = {
                "sessions": dashboard_mod.Sessions(self.passcode),
                "attendance": dashboard_mod.Attendance(self.attendance),
                "activity": dashboard_mod.ActivityIndex(
                    self.store, dashboard_mod.parse_offset("+07:00")),
                "daybook_page": dashboard_mod.load_page(),
                "tz_label": "+07:00",
            }
        handler = proxy_mod.build_handler(
            self.store, people, ADMIN, read_routes=self.read_routes, daybook=daybook)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = "http://127.0.0.1:{}".format(self.httpd.server_address[1])
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

    def open_it(self, passcode=PASSCODE):
        status, _, headers = self.call(
            "POST", "/dashboard/login", {"passcode": passcode}, cookie=False)
        raw = headers.get("Set-Cookie")
        if raw:
            jar = http.cookies.SimpleCookie()
            jar.load(raw)
            name = dashboard_mod.COOKIE
            self.cookie = "{}={}".format(name, jar[name].value)
        return status


class GateTests(RouteTestCase):
    def test_the_page_is_served_without_a_session(self):
        # It carries no data -- everything worth protecting is behind the
        # cookie -- and a login form nobody can fetch is not a login form.
        status, body, _ = self.call("GET", "/dashboard")
        self.assertEqual(status, 200)
        self.assertIn(b"Daybook", body)

    def test_data_needs_a_session(self):
        status, _, _ = self.call(
            "GET", "/dashboard/data?from=2026-08-24&to=2026-08-26")
        self.assertEqual(status, 401)

    def test_a_wrong_passcode_gets_nothing(self):
        self.assertEqual(self.open_it("000000"), 401)
        self.assertIsNone(self.cookie)

    def test_the_cookie_is_httponly_and_samesite(self):
        _, _, headers = self.call(
            "POST", "/dashboard/login", {"passcode": PASSCODE}, cookie=False)
        raw = headers.get("Set-Cookie")
        self.assertIn("HttpOnly", raw)
        self.assertIn("SameSite=Strict", raw)

    def test_guessing_is_throttled(self):
        for _ in range(dashboard_mod.MAX_ATTEMPTS):
            self.call("POST", "/dashboard/login", {"passcode": "000000"},
                      cookie=False)
        status, body, _ = self.call(
            "POST", "/dashboard/login", {"passcode": "000000"}, cookie=False)
        self.assertEqual(status, 429)
        self.assertIn("retry_after", json.loads(body))

    def test_signing_out_expires_the_cookie(self):
        self.open_it()
        _, _, headers = self.call("POST", "/dashboard/logout", {})
        self.assertIn("Max-Age=0", headers.get("Set-Cookie"))


class PayloadTests(RouteTestCase):
    def payload(self, start="2026-08-24", end="2026-08-28"):
        self.open_it()
        status, body, _ = self.call(
            "GET", "/dashboard/data?from={}&to={}".format(start, end))
        self.assertEqual(status, 200)
        return json.loads(body)

    def rows(self, payload):
        return {row["email"]: row for row in payload["people"]}

    def test_zero_and_unmeasured_are_different_values(self):
        payload = self.payload()
        row = self.rows(payload)[NGOC]
        by_day = dict(zip(payload["days"], row["activity"]))
        self.assertEqual(by_day["2026-08-25"], 2)      # measured, and busy
        self.assertEqual(by_day["2026-08-24"], 0)      # measured, and zero
        self.assertIsNone(by_day["2026-08-27"])        # nobody was looking

    def test_everyone_on_either_list_gets_a_row(self):
        rows = self.rows(self.payload())
        self.assertTrue(rows[LINH]["on_roster"])
        self.assertFalse(rows[LINH]["enrolled"])
        self.assertEqual(rows[LINH]["bundles"], 0)

    def test_a_bad_range_is_a_400(self):
        self.open_it()
        for query in ("from=nonsense&to=2026-08-26", "from=2026-08-26",
                      "from=2020-01-01&to=2026-01-01"):
            status, _, _ = self.call("GET", "/dashboard/data?" + query)
            self.assertEqual(status, 400, query)

    def test_a_day_is_recorded_and_comes_back_in_the_payload(self):
        self.open_it()
        status, _, _ = self.call("POST", "/dashboard/day", {
            "email": NGOC, "date": "2026-08-25", "state": "out",
            "in": "09:12", "out": "18:40"})
        self.assertEqual(status, 200)
        payload = self.payload()
        row = self.rows(payload)[NGOC]
        at = dict(zip(payload["days"], row["attendance"]))["2026-08-25"]
        self.assertEqual(at, {"state": "out", "in": "09:12", "out": "18:40"})

    def test_only_people_the_endpoint_knows_may_be_recorded(self):
        # Otherwise the daybook becomes a second, quieter roster that nothing
        # else in the system reads.
        self.open_it()
        status, _, _ = self.call("POST", "/dashboard/day", {
            "email": "somebody@example.com", "date": "2026-08-25", "state": "in"})
        self.assertEqual(status, 400)

    def test_recording_needs_a_session(self):
        status, _, _ = self.call("POST", "/dashboard/day", {
            "email": NGOC, "date": "2026-08-25", "state": "in"})
        self.assertEqual(status, 401)


class NotServedTests(RouteTestCase):
    """No passcode configured: the routes do not exist."""

    passcode = ""

    def test_every_path_404s(self):
        for path in ("/dashboard", "/dashboard/session",
                     "/dashboard/data?from=2026-08-01&to=2026-08-02"):
            status, _, _ = self.call("GET", path)
            self.assertEqual(status, 404, path)
        status, _, _ = self.call("POST", "/dashboard/login", {"passcode": ""})
        self.assertEqual(status, 404)

    def test_the_rest_of_the_endpoint_is_untouched(self):
        status, _, _ = self.call("GET", "/healthz")
        self.assertEqual(status, 200)


class PublicListenerTests(RouteTestCase):
    """The listener that faces the internet. The daybook lists every engineer."""

    read_routes = False

    def test_the_daybook_is_not_on_it(self):
        for path in ("/dashboard", "/dashboard/session"):
            status, _, _ = self.call("GET", path)
            self.assertEqual(status, 404, path)

    def test_not_even_the_login(self):
        status, _, _ = self.call(
            "POST", "/dashboard/login", {"passcode": PASSCODE}, cookie=False)
        self.assertEqual(status, 404)

    def test_uploads_still_work_here(self):
        status, _, _ = self.call("GET", "/healthz")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()

class SessionRevocationTests(unittest.TestCase):
    """Signing out has to end the session, not just the browser's copy.

    The token was `expiry.signature(expiry)` and nothing else, so it carried no
    identity: two people signing in during the same second got the same cookie,
    and `/dashboard/logout` could only ask the browser to forget one. The token
    stayed valid for its full life.
    """

    def sessions(self):
        return dashboard_mod.Sessions("123456", ttl=60)

    def test_two_sessions_are_not_the_same_token(self):
        s = self.sessions()
        self.assertNotEqual(s.issue(), s.issue())

    def test_a_revoked_token_stops_working(self):
        s = self.sessions()
        token = s.issue()
        self.assertTrue(s.valid(token))
        self.assertTrue(s.revoke(token))
        self.assertFalse(s.valid(token))

    def test_revoking_one_session_leaves_the_others_alone(self):
        s = self.sessions()
        mine, theirs = s.issue(), s.issue()
        s.revoke(mine)
        self.assertFalse(s.valid(mine))
        self.assertTrue(s.valid(theirs),
                        "signing out on one machine must not sign out the rest")

    def test_revoking_rubbish_is_not_an_error_and_changes_nothing(self):
        s = self.sessions()
        live = s.issue()
        for junk in (None, "", "nonsense", "1.2", "9999999999.x.y"):
            self.assertFalse(s.revoke(junk))
        self.assertTrue(s.valid(live))

    def test_a_second_revoke_reports_nothing_left_to_end(self):
        s = self.sessions()
        token = s.issue()
        self.assertTrue(s.revoke(token))
        self.assertFalse(s.revoke(token))

    def test_the_revoked_set_does_not_grow_without_bound(self):
        # Entries are dropped once their own expiry has passed -- by then the
        # signature has stopped verifying anyway, so keeping them buys nothing.
        s = dashboard_mod.Sessions("123456", ttl=1)
        stale = s.issue()
        s.revoke(stale)
        time.sleep(1.1)
        live = dashboard_mod.Sessions("123456", ttl=60)
        live._key = s._key
        live._revoked = dict(s._revoked)
        live.revoke(live.issue())
        self.assertNotIn(stale.split(".")[1], live._revoked)

