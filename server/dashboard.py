#!/usr/bin/env python3
"""The daybook: presence and AI activity, one row per person.

Two things sit side by side here because neither answers the question alone.
The bundles say what a laptop did; they cannot say whether anybody was at it.
A day with no events is a day somebody took off, or a day somebody worked
without Copilot, or a day nothing was collected -- three different facts that
the telemetry renders identically. Attendance is the missing half, and it has
to be typed in by a person because nothing in this system observes a door.

**This is not one of the ten metrics.** ``CLAUDE.md`` is explicit that the ten
are the point and everything else is supporting evidence, and attendance is
supporting evidence: it is the denominator you need before "five silent days"
means anything. It must never be reported as a measure of how anybody worked.

Three rules from the rest of the project are load-bearing here:

* **Absent is never zero.** A day inside a bundle's window with no events is a
  measured zero. A day no bundle covers is unmeasured. They are ``0`` and
  ``null`` in the payload and a floor and a hole on the page, and the whole
  visual design turns on the difference.
* **Never store content.** This reads event *times* and *type names* out of a
  bundle and keeps counts. Nothing else in the line is looked at.
* **Never count from self-reported data.** Attendance *is* self-reported, so it
  is never counted into a metric. It is a mask over the activity graph and a
  label on a row, and the page says so.

Authentication is a passcode, not the admin token, because a browser cannot
send a bearer header and this is a page a person opens. That is a weaker
credential than the token beside it, which is why these routes are on the read
listener only -- loopback, reached over an SSH tunnel. Reaching the passcode
prompt at all already requires an account on the host. See ``docs/OPERATE.md``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "cli"))

import identity  # noqa: E402

#: Vietnam is UTC+7 with no daylight saving, so a fixed offset is exact and
#: needs no tz database -- the same reasoning, and the same variable, as
#: ``importers/watch.py``. Event times are UTC; a working day is not.
DEFAULT_TZ_OFFSET = "+07:00"

#: The three things a day can be told. ``in`` is arrived and still here, ``out``
#: is arrived and left, ``off`` is not working. A fourth state -- "no record" --
#: is the absence of a line, never a value, for the same reason NULL is not 0.
STATES = ("in", "out", "off")

#: How many bundles a single request will read that it has not read before.
#: Bundle keys are content digests and the store is write-once, so a parsed
#: bundle is cached forever; only a cold process pays. The cap keeps that first
#: request from hanging on a year of S3, and the payload says how many are left
#: rather than quietly serving a short answer.
SCAN_BUDGET = 96

#: A window wider than this is not a week's telemetry, it is a corrupt manifest.
#: Believing it would paint months of false coverage across one person's row.
MAX_WINDOW_DAYS = 400

#: Longest range the page may ask for at once.
MAX_RANGE_DAYS = 400

#: Failed passcode attempts, per client address, before the prompt stops
#: answering. A six-digit passcode is 10^6, which is not many if something can
#: try them at HTTP speed; this is what makes the arithmetic uninteresting.
MAX_ATTEMPTS = 8
LOCKOUT_SECONDS = 60

SESSION_SECONDS = 12 * 3600

COOKIE = "insight_daybook"


class DashboardError(Exception):
    """Bad input from the page. Surfaces as a 400."""


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------

def parse_offset(value: Optional[str]) -> timezone:
    text = (value or DEFAULT_TZ_OFFSET).strip()
    sign = -1 if text.startswith("-") else 1
    text = text.lstrip("+-")
    try:
        hours, _, minutes = text.partition(":")
        delta = timedelta(hours=int(hours), minutes=int(minutes or 0))
    except ValueError:
        raise DashboardError(
            "INSIGHT_TZ_OFFSET must look like +07:00, got {!r}".format(value))
    if abs(delta) > timedelta(hours=14):
        raise DashboardError("INSIGHT_TZ_OFFSET out of range: {!r}".format(value))
    return timezone(sign * delta)


def _parse_stamp(value: str) -> Optional[datetime]:
    """RFC3339 as the bundles write it, tolerant of the shapes they use."""
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def local_day(value: str, tz: timezone) -> Optional[str]:
    moment = _parse_stamp(value)
    return moment.astimezone(tz).date().isoformat() if moment else None


def _date_part(value: str) -> Optional[str]:
    """``2026-08-25T00:00:00Z`` -> ``2026-08-25``, or None if it is not a date."""
    text = (value or "").strip()[:10]
    return text if _is_day(text) else None


def day_range(start: str, end: str) -> List[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first:
        raise DashboardError("the range ends before it starts")
    if (last - first).days + 1 > MAX_RANGE_DAYS:
        raise DashboardError(
            "at most {} days at a time".format(MAX_RANGE_DAYS))
    out, cursor = [], first
    while cursor <= last:
        out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


def _is_day(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except (ValueError, TypeError):
        return False
    return len(value) == 10


def _is_clock(value: str) -> bool:
    if not value:
        return True
    if len(value) != 5 or value[2] != ":":
        return False
    hours, minutes = value[:2], value[3:]
    if not (hours.isdigit() and minutes.isdigit()):
        return False
    return 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59


# --------------------------------------------------------------------------
# attendance -- the half a machine cannot see
# --------------------------------------------------------------------------

def _atomic_write(path: str, text: str) -> None:
    """Replace a file without ever leaving a half-written one behind.

    The same shape as ``registry._atomic_write``, and for the same reason: this
    file is re-read on mtime, so a torn write is a window where the daybook
    reads short.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".attendance-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise


class Attendance:
    """``email <tab> date <tab> state <tab> in <tab> out``, one line per day.

    A tab-separated file rather than a database for the same reason the
    whitelist and the roster are files: an operator can read it, diff it, fix it
    with an editor and put it in a backup, and this service is fourteen people
    who between them generate about fourteen lines a day.

    Re-read when it changes, so a hand edit needs no restart. Kept sorted, so
    the diff between two days is the change and not a reshuffle.
    """

    HEADER = (
        "# The attendance daybook. One line per person per day.\n"
        "#\n"
        "# email\\tdate\\tstate\\tin\\tout   --   state is in | out | off\n"
        "#\n"
        "# Self-reported, therefore never counted into a metric: it is the mask\n"
        "# that says whether a quiet day was a day off or a day nobody used AI.\n"
        "# Safe to edit by hand; it is re-read when it changes.\n"
    )

    def __init__(self, path: Optional[str]) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._rows: Dict[Tuple[str, str], Dict[str, str]] = {}
        self._stamp: object = "unset"
        self._reload(force=True)

    # -- state ------------------------------------------------------------

    def _mtime(self) -> object:
        if not self.path:
            return None
        try:
            stat = os.stat(self.path)
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _reload(self, force: bool = False) -> None:
        if not self.path:
            return
        stamp = self._mtime()
        if not force and stamp == self._stamp:
            return
        self._stamp = stamp
        self._rows = {}
        if stamp is None:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            return
        for line in text.splitlines():
            line = line.strip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = (line.split("\t") + ["", "", "", ""])[:5]
            email, day, state, at_in, at_out = [p.strip() for p in parts]
            if not email or not _is_day(day) or state not in STATES:
                # A line nobody can act on is dropped rather than guessed at.
                continue
            self._rows[(email.lower(), day)] = {
                "state": state,
                "in": at_in if _is_clock(at_in) else "",
                "out": at_out if _is_clock(at_out) else "",
            }

    def _persist(self) -> None:
        if not self.path:
            raise DashboardError(
                "no attendance file configured -- set INSIGHT_ATTENDANCE_FILE")
        lines = [self.HEADER]
        for (email, day) in sorted(self._rows):
            row = self._rows[(email, day)]
            lines.append("\t".join(
                [email, day, row["state"], row.get("in", ""), row.get("out", "")]
            ).rstrip("\t"))
        _atomic_write(self.path, "\n".join(lines) + "\n")
        self._stamp = self._mtime()

    # -- reads ------------------------------------------------------------

    def between(self, start: str, end: str) -> Dict[str, Dict[str, Dict[str, str]]]:
        with self._lock:
            self._reload()
            found: Dict[str, Dict[str, Dict[str, str]]] = {}
            for (email, day), row in self._rows.items():
                if start <= day <= end:
                    found.setdefault(email, {})[day] = dict(row)
            return found

    def count(self) -> int:
        with self._lock:
            self._reload()
            return len(self._rows)

    # -- writes -----------------------------------------------------------

    def set(self, email: str, day: str, state: Optional[str],
            at_in: str = "", at_out: str = "") -> Optional[Dict[str, str]]:
        """Write one day. ``state=None`` removes the line.

        Removing is a real operation and not the same as writing ``off``: one
        says this person was not working, the other says nobody has said. The
        page offers both because the daybook is only useful if the difference
        survives a mis-click.
        """
        email = (email or "").strip().lower()
        if not email:
            raise DashboardError("email is required")
        if not _is_day(day):
            raise DashboardError("date must look like 2026-08-27")
        if state is not None and state not in STATES:
            raise DashboardError(
                "state must be one of {}".format(", ".join(STATES)))
        at_in, at_out = (at_in or "").strip(), (at_out or "").strip()
        for clock in (at_in, at_out):
            if not _is_clock(clock):
                raise DashboardError("times must look like 09:30, got {!r}".format(clock))
        if state == "off":
            # A day off has no clock on it. Keeping the times would leave a row
            # that says both "not working" and "arrived at 09:12".
            at_in = at_out = ""
        if at_in and at_out and at_out < at_in:
            raise DashboardError("out is before in")

        with self._lock:
            self._reload()
            if state is None:
                self._rows.pop((email, day), None)
                self._persist()
                return None
            row = {"state": state, "in": at_in, "out": at_out}
            self._rows[(email, day)] = row
            self._persist()
            return dict(row)


# --------------------------------------------------------------------------
# activity -- what the bundles already know
# --------------------------------------------------------------------------

class ActivityIndex:
    """Per person, per local day: how many events, of what kinds, and whether
    anybody was looking.

    Reads bundles out of the same store the endpoint writes them to, and caches
    what it finds by object key. That cache never needs invalidating: keys are
    content digests and ``store.put`` refuses to overwrite, so a key that has
    been read once cannot come back different. Only the *list* is refetched.

    The two facts it keeps are different in kind and must not be conflated:

    ``counts``    events on a day. Present only where events were seen.
    ``covered``   days a bundle's window spans, whether or not it held events.

    A day in ``covered`` with no entry in ``counts`` is a measured zero. A day
    in neither is unmeasured. The page draws those two differently and that is
    the whole point of keeping them apart here.
    """

    def __init__(self, store: Any, tz: timezone) -> None:
        self.store = store
        self.tz = tz
        self._lock = threading.Lock()
        #: key -> {"person": str, "days": {day: {type: n}}, "covered": [days],
        #:         "events": int, "window": (start, end)}
        self._parsed: Dict[str, Dict[str, Any]] = {}
        self._keys: List[Dict[str, Any]] = []
        self._listed_at = 0.0

    # -- reading ----------------------------------------------------------

    def _list(self, max_age: float = 30.0) -> List[Dict[str, Any]]:
        now = time.time()
        if self._keys and now - self._listed_at < max_age:
            return self._keys
        self._keys = [o for o in self.store.list("bundles/")
                      if o["key"].endswith(".ndjson")]
        self._listed_at = now
        return self._keys

    def _parse(self, key: str, body: bytes) -> Dict[str, Any]:
        """Times and type names out; everything else stays in the bundle.

        The allow-list this observes is the one in ``CLAUDE.md``: nothing here
        reads an attribute, a prompt, a path or a repo name. It reads
        ``event_time`` and ``event_type`` and counts them.
        """
        parts = key.split("/")
        person = parts[2] if len(parts) > 2 else ""
        days: Dict[str, Dict[str, int]] = {}
        window: Tuple[Optional[str], Optional[str]] = (None, None)
        total = 0

        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            manifest = record.get("_manifest")
            if isinstance(manifest, dict):
                # The date part, not the instant converted to local time. `pack`
                # writes the window as `<day>T00:00:00Z` / `<day>T23:59:59Z` --
                # a *date range* wearing dummy times, over days it has already
                # bucketed locally. Converting 23:59:59Z to UTC+7 lands at
                # 06:59 the next morning and paints a day of coverage nobody
                # measured, which is the one mistake this whole module exists
                # to avoid. Event times are real instants and are still
                # converted; see `local_day` below.
                window = (_date_part(manifest.get("window_start") or ""),
                          _date_part(manifest.get("window_end") or ""))
                continue
            day = local_day(record.get("event_time") or "", self.tz)
            if not day:
                continue
            kind = str(record.get("event_type") or "unknown")
            days.setdefault(day, {})
            days[day][kind] = days[day].get(kind, 0) + 1
            total += 1

        return {"person": person, "days": days, "events": total,
                "covered": _window_days(window), "window": window}

    def refresh(self, budget: int = SCAN_BUDGET) -> int:
        """Read up to ``budget`` bundles not yet seen. Returns how many remain.

        Never silently short: the count it returns is what the page renders as
        "still reading", because a graph that is missing a week and does not say
        so is worse than one that admits it.
        """
        with self._lock:
            keys = self._list()
            pending = [o["key"] for o in keys if o["key"] not in self._parsed]
            for key in pending[:budget]:
                try:
                    body = self.store.get(key)
                except Exception:                    # noqa: BLE001
                    # A key that cannot be read is recorded as read-and-empty,
                    # or every request retries it forever. It contributes no
                    # coverage, so the days it held stay holes -- which is true.
                    self._parsed[key] = {"person": key.split("/")[2]
                                         if len(key.split("/")) > 2 else "",
                                         "days": {}, "events": 0,
                                         "covered": [], "window": (None, None)}
                    continue
                self._parsed[key] = self._parse(key, body)
            return max(0, len(pending) - budget)

    # -- queries ----------------------------------------------------------

    def by_person(self, start: str, end: str) -> Dict[str, Dict[str, Any]]:
        """``{person_key: {"days": {day: {...}}, "covered": set, "bundles": n,
        "first": day, "events": n}}`` clipped to the range."""
        with self._lock:
            found: Dict[str, Dict[str, Any]] = {}
            for key, parsed in self._parsed.items():
                person = parsed["person"]
                slot = found.setdefault(person, {
                    "days": {}, "covered": set(), "bundles": 0,
                    "first": None, "events": 0})
                slot["bundles"] += 1
                window_start = parsed["window"][0]
                if window_start and (slot["first"] is None
                                     or window_start < slot["first"]):
                    slot["first"] = window_start
                for day in parsed["covered"]:
                    if start <= day <= end:
                        slot["covered"].add(day)
                for day, kinds in parsed["days"].items():
                    if not (start <= day <= end):
                        continue
                    # An event outside its own bundle's declared window still
                    # happened; count it and let the day count as covered.
                    slot["covered"].add(day)
                    bucket = slot["days"].setdefault(day, {})
                    for kind, number in kinds.items():
                        bucket[kind] = bucket.get(kind, 0) + number
                        slot["events"] += number
            return found


def _window_days(window: Tuple[Optional[str], Optional[str]]) -> List[str]:
    start, end = window
    if not start:
        return []
    if not end or end < start:
        end = start
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        return []
    if (last - first).days > MAX_WINDOW_DAYS:
        return []
    out, cursor = [], first
    while cursor <= last:
        out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


# --------------------------------------------------------------------------
# the passcode
# --------------------------------------------------------------------------

class Sessions:
    """A passcode in, a signed cookie back.

    The signing key is minted per process and never written down, so every
    restart signs everyone out. That is the right trade for a page whose whole
    audience is one or two admins: there is no session store to keep, nothing
    on disk to steal, and the failure mode is signing in again.

    Every session carries its own random id, and that is not decoration. The
    token used to be `expiry.signature(expiry)` and nothing else, which had two
    consequences worth stating: two people signing in during the same second
    were handed the identical cookie, and there was nothing to revoke -- so
    signing out only cleared the browser's copy while the token itself stayed
    valid for its full twelve hours. Anyone still holding it, including a copy
    taken from a shared machine, stayed signed in.
    """

    def __init__(self, passcode: str, ttl: int = SESSION_SECONDS) -> None:
        self.passcode = passcode or ""
        self.ttl = ttl
        self._key = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._failures: Dict[str, List[float]] = {}
        #: session id -> the expiry it was issued with. Revoked ids only; a
        #: live session is not tracked, so the normal case stores nothing.
        #: Bounded because an entry is dropped once its own expiry has passed
        #: -- by then the signature has stopped verifying anyway.
        self._revoked: Dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.passcode)

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def issue(self) -> str:
        expiry = str(int(time.time()) + self.ttl)
        sid = secrets.token_urlsafe(12)
        payload = "{}.{}".format(expiry, sid)
        return "{}.{}".format(payload, self._sign(payload))

    def _parse(self, token: Optional[str]) -> Optional[Tuple[str, str]]:
        """`(expiry, sid)` for a token this process signed, else None."""
        if not token:
            return None
        parts = token.split(".")
        if len(parts) != 3:
            return None
        expiry, sid, signature = parts
        if not hmac.compare_digest(signature,
                                   self._sign("{}.{}".format(expiry, sid))):
            return None
        return expiry, sid

    def valid(self, token: Optional[str]) -> bool:
        parsed = self._parse(token)
        if parsed is None:
            return False
        expiry, sid = parsed
        try:
            if int(expiry) <= time.time():
                return False
        except ValueError:
            return False
        with self._lock:
            return sid not in self._revoked

    def revoke(self, token: Optional[str]) -> bool:
        """Signing out ends the session, not just the browser's copy of it.

        Returns whether there was a live session to end, which is only used for
        the log line -- the response says the same either way, because a caller
        should not learn from a sign-out whether the token it sent was real.
        """
        parsed = self._parse(token)
        if parsed is None:
            return False
        expiry, sid = parsed
        try:
            when = int(expiry)
        except ValueError:
            return False
        now = time.time()
        with self._lock:
            # Drop anything whose signature has already stopped verifying, so
            # this cannot grow without bound on a long-lived process.
            self._revoked = {k: v for k, v in self._revoked.items() if v > now}
            if when <= now:
                return False
            fresh = sid not in self._revoked
            self._revoked[sid] = when
            return fresh

    # -- throttle ---------------------------------------------------------

    def locked_for(self, who: str) -> int:
        """Seconds this address must wait, or 0."""
        with self._lock:
            recent = [t for t in self._failures.get(who, [])
                      if time.time() - t < LOCKOUT_SECONDS]
            self._failures[who] = recent
            if len(recent) < MAX_ATTEMPTS:
                return 0
            return max(1, int(LOCKOUT_SECONDS - (time.time() - recent[0])))

    def attempt(self, given: str, who: str) -> bool:
        if not self.enabled:
            return False
        ok = hmac.compare_digest((given or "").strip(), self.passcode)
        with self._lock:
            if ok:
                self._failures.pop(who, None)
            else:
                self._failures.setdefault(who, []).append(time.time())
        return ok


def read_passcode(raw: Optional[str], path: Optional[str]) -> str:
    """``INSIGHT_DASHBOARD_PASSWORD``, or a file holding it.

    A file for the same reason the admin token has one: ``docker inspect``
    prints the environment to anybody who can reach the Docker daemon. Empty
    means the daybook is not served at all, which is how every deployment that
    has not asked for it stays exactly as it was.
    """
    passcode = (raw or "").strip()
    if path:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                passcode = handle.read().strip()
        except OSError as exc:
            raise SystemExit(
                "cannot read INSIGHT_DASHBOARD_PASSWORD_FILE {}: {}".format(path, exc))
    return passcode


def load_page(path: Optional[str] = None) -> Optional[bytes]:
    """The page, read once at startup and held as fixed bytes.

    Same property as ``/install``: no path parameter reaches the filesystem on
    a request, so no request can influence which bytes come back. The page is
    one file -- markup, style and script together -- because three files would
    be three routes and three chances to get that wrong.
    """
    target = path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "assets", "dashboard.html")
    try:
        with open(target, "rb") as handle:
            body = handle.read()
    except OSError as exc:
        raise SystemExit("cannot read the daybook page {}: {}".format(target, exc))
    if not body.strip():
        raise SystemExit("the daybook page {} is empty".format(target))
    return body


# --------------------------------------------------------------------------
# the payload
# --------------------------------------------------------------------------

def build_payload(people: Any, attendance: Attendance, activity: ActivityIndex,
                  start: str, end: str, tz_label: str) -> Dict[str, Any]:
    """Everything the page draws, in one response.

    ``activity`` and ``attendance`` are arrays aligned to ``days`` rather than
    objects keyed by date: fourteen people over a year is five thousand cells,
    and the difference between an array of nulls and five thousand keys is the
    difference between a payload that loads and one that does not.

    **null is not 0 in either array**, and that distinction is the reason this
    function exists rather than the page joining two simpler endpoints.
    """
    days = day_range(start, end)
    pending = activity.refresh()
    measured = activity.by_person(start, end)
    marks = attendance.between(start, end)

    roster = people.people()
    known = {row["email"]: row for row in roster}

    # Anybody with bundles but no line in the registry still gets a row: their
    # telemetry is in the bucket and hiding it would make the daybook lie by
    # omission. They show as a person_key, which is what the bucket knows.
    rows: List[Dict[str, Any]] = []
    seen_keys: Set[str] = set()
    for email in sorted(known):
        # ``identity.person_key`` and not a second sha256 here: the bucket
        # layout is that function's answer, and two implementations of it is
        # exactly the drift this project refuses (``server/proxy.py`` imports
        # it for the same reason).
        key = identity.person_key(email)
        seen_keys.add(key)
        rows.append(_row(email, key, known[email], measured.get(key),
                         marks.get(email, {}), days))
    for key in sorted(measured):
        if key in seen_keys or key in ("", None):
            continue
        rows.append(_row(None, key, None, measured[key], {}, days))

    return {
        "from": start,
        "to": end,
        "days": days,
        "tz": tz_label,
        "pending_bundles": pending,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "people": rows,
        "states": list(STATES),
    }


def _row(email: Optional[str], key: Optional[str], registry_row: Optional[Dict[str, Any]],
         measured: Optional[Dict[str, Any]], marks: Dict[str, Dict[str, str]],
         days: List[str]) -> Dict[str, Any]:
    covered = (measured or {}).get("covered") or set()
    counts = (measured or {}).get("days") or {}

    activity: List[Optional[int]] = []
    kinds: List[Optional[Dict[str, int]]] = []
    for day in days:
        if day in counts:
            activity.append(sum(counts[day].values()))
            kinds.append(counts[day])
        elif day in covered:
            activity.append(0)          # measured, and it was zero
            kinds.append({})
        else:
            activity.append(None)       # nobody was looking
            kinds.append(None)

    attendance: List[Optional[Dict[str, str]]] = [
        marks.get(day) or None for day in days]

    return {
        "email": email,
        "person_key": key,
        "label": email or ("unknown:" + (key or "?")),
        "on_roster": bool(registry_row and registry_row.get("on_roster")),
        "enrolled": bool(registry_row and registry_row.get("enrolled")),
        "bundles": (measured or {}).get("bundles", 0),
        "first_bundle": (measured or {}).get("first"),
        "events": (measured or {}).get("events", 0),
        "activity": activity,
        "kinds": kinds,
        "attendance": attendance,
    }
