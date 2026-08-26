#!/usr/bin/env python3
"""Notice when a machine stops reporting, and say so once.

    python3 importers/watch.py --roster roster.txt

Automating the upload removed the thing that used to make a gap visible: a
weekly email that did not arrive. `pull.py` reports coverage, but only when
somebody runs it and reads the output. This is the part that speaks first.

**What counts as a miss is a working day, not an hour.** `insight auto` uploads
nothing in an hour where nothing changed, so three silent hours is what a normal
afternoon of non-AI work looks like -- alerting on it would train everyone to
ignore the alert. A *day* is different: even a completely idle day packs one
empty bundle with a declared window, because a measured zero and missing data
must not look the same. So a working day with no bundle at all means the
collection is broken on that machine, not that the person was quiet.

Nights and weekends are not misses either, which is what the working-hours
configuration is for. A laptop shut at 19:00 on Friday is not a fault, and an
alert that fires at 02:00 on Sunday is one nobody will act on and everybody will
mute.

Config comes from ``.env`` (see ``.env.example``); the defaults are SETA's.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "pollers"), os.path.join(_ROOT, "importers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import common  # noqa: E402
import pull as pull_mod  # noqa: E402

DEFAULT_NTFY = "https://ntfy.sh/seta-insight"
DEFAULT_WORK_START = "09:00"
DEFAULT_WORK_END = "19:00"
#: Mon-Fri. `date.weekday()` numbering, Monday = 0.
DEFAULT_WORK_DAYS = "0,1,2,3,4"
DEFAULT_THRESHOLD = 3

#: Vietnam is UTC+7 and has no daylight saving, so a fixed offset is exact and
#: needs no tz database. `zoneinfo` on Python 3.9 depends on system tzdata being
#: present, which is one more thing to be missing on one machine at 3am.
DEFAULT_TZ_OFFSET = "+07:00"


class WatchError(Exception):
    """Nothing was checked and nothing was sent."""


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def parse_offset(value: str) -> timezone:
    text = (value or DEFAULT_TZ_OFFSET).strip()
    sign = -1 if text.startswith("-") else 1
    text = text.lstrip("+-")
    try:
        hours, _, minutes = text.partition(":")
        delta = timedelta(hours=int(hours), minutes=int(minutes or 0))
    except ValueError:
        raise WatchError(
            "INSIGHT_TZ_OFFSET must look like +07:00, got {!r}".format(value))
    if abs(delta) > timedelta(hours=14):
        raise WatchError("INSIGHT_TZ_OFFSET out of range: {!r}".format(value))
    return timezone(sign * delta)


def parse_hhmm(value: str, fallback: str) -> Tuple[int, int]:
    text = (value or fallback).strip()
    try:
        hours, _, minutes = text.partition(":")
        hour, minute = int(hours), int(minutes or 0)
    except ValueError:
        raise WatchError("expected HH:MM, got {!r}".format(value))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise WatchError("not a time of day: {!r}".format(value))
    return hour, minute


def parse_work_days(value: str) -> Set[int]:
    days = set()
    for part in (value or DEFAULT_WORK_DAYS).replace(" ", "").split(","):
        if not part:
            continue
        try:
            day = int(part)
        except ValueError:
            raise WatchError(
                "INSIGHT_WORK_DAYS is Monday=0..Sunday=6, got {!r}".format(part))
        if not 0 <= day <= 6:
            raise WatchError("INSIGHT_WORK_DAYS out of range: {!r}".format(part))
        days.add(day)
    if not days:
        raise WatchError("INSIGHT_WORK_DAYS names no days at all")
    return days


def _positive_int(value: Optional[str], fallback: int) -> int:
    """A threshold of 0 would alert on everyone, every run, forever."""
    if not value:
        return fallback
    try:
        number = int(value)
    except ValueError:
        raise WatchError(
            "INSIGHT_MISS_THRESHOLD must be a whole number, got {!r}".format(value))
    if number < 1:
        raise WatchError(
            "INSIGHT_MISS_THRESHOLD must be at least 1, got {!r}".format(value))
    return number


def load_config(env: Optional[Dict[str, str]] = None,
                use_dotenv: bool = True) -> Dict[str, Any]:
    """Read the schedule and alerting settings.

    Same precedence the pollers use -- the real environment wins over ``.env`` --
    so a one-off run can override a setting without editing the file everyone
    else depends on.
    """
    env = dict(os.environ if env is None else env)
    if use_dotenv:
        path = common._find_dotenv()
        if path:
            for key, value in common.load_dotenv_values(path).items():
                env.setdefault(key, value)
    return {
        "tz": parse_offset(env.get("INSIGHT_TZ_OFFSET") or DEFAULT_TZ_OFFSET),
        "work_start": parse_hhmm(env.get("INSIGHT_WORK_START"), DEFAULT_WORK_START),
        "work_end": parse_hhmm(env.get("INSIGHT_WORK_END"), DEFAULT_WORK_END),
        "work_days": parse_work_days(env.get("INSIGHT_WORK_DAYS")),
        "threshold": _positive_int(env.get("INSIGHT_MISS_THRESHOLD"),
                                   DEFAULT_THRESHOLD),
        "ntfy_url": env.get("INSIGHT_NTFY_URL") or DEFAULT_NTFY,
        "ntfy_token": env.get("INSIGHT_NTFY_TOKEN") or "",
    }


# --------------------------------------------------------------------------
# which days should have reported
# --------------------------------------------------------------------------

def working_days_before(today: date, work_days: Set[int],
                        count: int) -> List[date]:
    """The ``count`` working days ending yesterday, most recent first.

    Today is deliberately excluded. A day is only a miss once it is over --
    judging it at 10:00 would flag everyone who starts at 10:30.
    """
    days: List[date] = []
    cursor = today - timedelta(days=1)
    guard = 0
    while len(days) < count and guard < 400:
        if cursor.weekday() in work_days:
            days.append(cursor)
        cursor -= timedelta(days=1)
        guard += 1
    return days


def days_reported(objects: Sequence[Dict[str, Any]], tz: timezone) -> Dict[str, Set[date]]:
    """Which local days each person has an upload for.

    Keyed on ``uploaded_at`` -- when the bundle arrived -- rather than the window
    it covers. The question here is "is this machine still reporting", and a
    machine catching up on last week's window is answering yes.
    """
    seen: Dict[str, Set[date]] = {}
    for obj in objects:
        who = pull_mod._who(obj)
        stamp = common.parse_ts(obj.get("uploaded_at"))
        if not who or stamp is None:
            continue
        seen.setdefault(who, set()).add(stamp.astimezone(tz).date())
    return seen


def missed_streak(reported: Set[date], expected: Sequence[date]) -> int:
    """How many working days in a row, ending yesterday, had no upload."""
    streak = 0
    for day in expected:            # already most recent first
        if day in reported:
            break
        streak += 1
    return streak


# --------------------------------------------------------------------------
# notifying
# --------------------------------------------------------------------------

def _post(url: str, body: bytes, headers: Dict[str, str], timeout: int) -> int:
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=common.ssl_context()) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError) as exc:
        if common.is_certificate_error(exc):
            # An alert that cannot be delivered is the failure this whole file
            # exists to prevent, so it says why rather than "unreachable".
            raise WatchError("{}: {}".format(url, common.CERT_ADVICE))
        raise WatchError("{} unreachable: {}".format(url, exc))


def notify(config: Dict[str, Any], title: str, message: str,
           post=_post, timeout: int = 20) -> int:
    headers = {
        "Title": title,
        "Priority": "high",
        "Tags": "warning",
        "Content-Type": "text/plain; charset=utf-8",
    }
    if config.get("ntfy_token"):
        headers["Authorization"] = "Bearer " + config["ntfy_token"]
    return post(config["ntfy_url"], message.encode("utf-8"), headers, timeout)


# --------------------------------------------------------------------------
# state -- so one outage is one message
# --------------------------------------------------------------------------

def load_state(path: Optional[str]) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_state(path: Optional[str], state: Dict[str, Any]) -> None:
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")


def should_alert(person: str, streak: int, threshold: int,
                 state: Dict[str, Any], settled: bool = True) -> bool:
    """Alert on crossing the threshold, and again only if it gets worse.

    An outage that lasts a fortnight is one problem, not ten. Repeating the
    same message every run is how a channel gets muted, and a muted channel is
    worse than no channel because everyone believes it is still watching.

    ``settled`` is the new-joiner guard. Someone added to the roster today has
    no uploads for the three working days before they existed here, which looks
    identical to a machine that broke -- and the very first thing the watchdog
    would do to a new colleague is page the team about them.
    """
    if streak < threshold or not settled:
        return False
    already = (state.get(person) or {}).get("alerted_streak") or 0
    return streak > already


def _is_date(value: Any) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


def first_evidence(person: str, seen: Set[date],
                   state: Dict[str, Any]) -> Optional[date]:
    """The earliest day this person is known to have existed here.

    Their first upload, or the day they were added to the roster, whichever is
    older. Anything before that is not silence -- it is a period they were not
    being measured in.
    """
    candidates = set(seen)
    since = (state.get(person) or {}).get("roster_since")
    if since:
        try:
            candidates.add(date.fromisoformat(since))
        except (TypeError, ValueError):
            pass        # an unreadable stamp must not silence a real outage
    return min(candidates) if candidates else None


def is_settled(person: str, seen: Set[date], state: Dict[str, Any],
               today: date, work_days: Set[int], threshold: int) -> bool:
    """Has this person existed here long enough to be judged silent?

    The subtle case, and the one that pages the whole team on day one: someone
    whose only upload is *today*. Today is excluded from the window on purpose,
    so their streak counts back across working days that predate the system --
    and an upload an hour ago is the strongest possible evidence that nothing is
    broken. The clock starts at first evidence, not at the start of the window.
    """
    start = first_evidence(person, seen, state)
    if start is None:
        return False
    elapsed = sum(1 for day in working_days_before(today, work_days, 400)
                  if day >= start)
    return elapsed >= threshold


# --------------------------------------------------------------------------

def check(objects: Sequence[Dict[str, Any]], roster: Sequence[str],
          config: Dict[str, Any], today: date,
          state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Who has gone quiet, and who is newly quiet enough to say so about."""
    state = state or {}
    tz = config["tz"]
    expected = working_days_before(today, config["work_days"], config["threshold"] + 7)
    reported = days_reported(objects, tz)

    people = []
    for person in sorted({str(p).strip().lower() for p in roster if str(p).strip()}):
        seen = reported.get(person, set())
        streak = missed_streak(seen, expected)
        settled = is_settled(person, seen, state, today,
                             config["work_days"], config["threshold"])
        people.append({
            "person": person,
            "missed_working_days": streak,
            "last_reported": max(seen).isoformat() if seen else None,
            "new_to_the_roster": not settled,
            "alerting": should_alert(person, streak, config["threshold"],
                                     state, settled),
        })
    return {
        "checked_on": today.isoformat(),
        "threshold": config["threshold"],
        "working_days_considered": [d.isoformat() for d in expected[:config["threshold"]]],
        "people": people,
        "silent": [p for p in people if p["missed_working_days"] >= config["threshold"]],
        "to_alert": [p for p in people if p["alerting"]],
    }


def message_for(entry: Dict[str, Any]) -> str:
    last = entry["last_reported"] or "never"
    return (
        "{person} has not reported for {n} working days.\n"
        "Last bundle: {last}\n\n"
        "An idle day still uploads one empty bundle, so this means collection "
        "is broken on that machine rather than that the week was quiet.\n"
        "Ask them for `./insight schedule --status` and "
        "`./insight status`."
    ).format(person=entry["person"], n=entry["missed_working_days"], last=last)


def recent_weeks(today: date, count: int = 3) -> List[str]:
    """ISO weeks to list, newest first. A streak spans a week boundary."""
    weeks, seen = [], set()
    for back in range(0, count * 7):
        day = today - timedelta(days=back)
        year, week, _ = day.isocalendar()
        label = "{}-W{:02d}".format(year, week)
        if label not in seen:
            seen.add(label)
            weeks.append(label)
    return weeks


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Notify when a machine stops reporting.")
    parser.add_argument("--roster", required=True,
                        help="work emails expected to report")
    parser.add_argument("--endpoint", help="default: $INSIGHT_ENDPOINT")
    parser.add_argument("--token", help="default: $INSIGHT_ADMIN_TOKEN")
    parser.add_argument("--state", default="state/watch.json",
                        help="remembers what has already been alerted")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be sent, and send nothing")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args(argv)

    env = dict(os.environ)
    path = common._find_dotenv()
    if path:
        for key, value in common.load_dotenv_values(path).items():
            env.setdefault(key, value)

    endpoint = args.endpoint or env.get("INSIGHT_ENDPOINT")
    token = args.token or env.get("INSIGHT_ADMIN_TOKEN")
    if not endpoint:
        raise SystemExit("no endpoint -- pass --endpoint or set INSIGHT_ENDPOINT")
    if not token:
        raise SystemExit("no admin token -- pass --token or set INSIGHT_ADMIN_TOKEN")

    try:
        config = load_config(env, use_dotenv=False)
    except (WatchError, ValueError) as exc:
        raise SystemExit(str(exc))

    roster = pull_mod.read_roster(args.roster)
    today = datetime.now(config["tz"]).date()

    objects: List[Dict[str, Any]] = []
    try:
        for week in recent_weeks(today):
            objects.extend(pull_mod.list_week(endpoint, week, token, args.timeout))
    except pull_mod.PullError as exc:
        # The endpoint being unreachable is itself worth knowing about, and it
        # is exactly the failure a silent watchdog would hide.
        raise SystemExit("cannot reach the collection endpoint: {}".format(exc))

    state = load_state(args.state)
    report = check(objects, roster, config, today, state)
    print(json.dumps(report, indent=2, sort_keys=True))

    # Record first sighting before anything else, so the grace period starts
    # from the day someone joined the roster rather than the day they broke.
    for entry in report["people"]:
        record = state.setdefault(entry["person"], {})
        if not _is_date(record.get("roster_since")):
            # Rewritten rather than kept. `setdefault` would leave a corrupt
            # stamp in place, and a stamp that never parses is a person who is
            # never judged -- silence that lasts as long as the file does.
            record["roster_since"] = today.isoformat()

    sent = 0
    for entry in report["to_alert"]:
        if args.dry_run:
            print("WOULD NOTIFY {}: {} working days".format(
                entry["person"], entry["missed_working_days"]), file=sys.stderr)
            continue
        try:
            status = notify(
                config,
                "usage-insight: {} has stopped reporting".format(entry["person"]),
                message_for(entry), timeout=args.timeout)
        except WatchError as exc:
            print("NOTIFY FAILED {}: {}".format(entry["person"], exc),
                  file=sys.stderr)
            continue
        if status >= 400:
            print("NOTIFY FAILED {}: HTTP {}".format(entry["person"], status),
                  file=sys.stderr)
            continue
        sent += 1
        state.setdefault(entry["person"], {})["alerted_streak"] = \
            entry["missed_working_days"]
        state[entry["person"]]["alerted_at"] = datetime.now(
            timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Reporting again clears the record, so the next outage alerts from scratch.
    for entry in report["people"]:
        if entry["missed_working_days"] == 0 and entry["person"] in state:
            state[entry["person"]].pop("alerted_streak", None)
            state[entry["person"]].pop("alerted_at", None)

    if not args.dry_run:
        save_state(args.state, state)

    print(json.dumps({"msg": "watch_complete", "silent": len(report["silent"]),
                      "notified": sent}, sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
