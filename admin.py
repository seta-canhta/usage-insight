#!/usr/bin/env python3
"""insight-admin -- the pipeline side, in one command.

Two jobs, and nothing else belongs here:

    ./admin.py people ...          who is expected to report
    ./admin.py pull --week 2026-W35    everything for a period, into reports/

Both talk to the endpoint over HTTP. Nobody edits a file on the server and
nobody restarts a service: adding an address is a request, and the machine it
belongs to enrols itself the next time it collects.

The admin token is read from ``.admin.env`` beside this file -- one line,
``INSIGHT_ADMIN_TOKEN=...`` -- so it is never in a shell history or a process
list. That file is gitignored and must stay that way.

Stdlib only, like everything else that ships here.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "cli"))

import common  # noqa: E402

ADMIN_ENV = os.path.join(_ROOT, ".admin.env")
DEFAULT_ENDPOINT = "http://127.0.0.1:8479"

#: Sent as ``User-Agent``, for the same reason ``cli/ship.py`` sends one:
#: urllib's default is ``Python-urllib/3.x``, and Cloudflare's browser
#: integrity check answers that with a 403 before the request reaches the
#: endpoint, so no header or token of ours can help. Verified against the live
#: hostname -- the same call is 200 as `curl` and 403 as `Python-urllib`.
USER_AGENT = "seta-insight-admin/1 (+usage-insight)"


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def load_admin_env(path: str = ADMIN_ENV) -> Dict[str, str]:
    """``.admin.env``, if it is there. Real environment variables win.

    Kept separate from ``.env`` on purpose. ``.env`` holds the poller
    credentials and gets copied between machines; the admin token reads every
    engineer's bundle at once and should not travel with them.
    """
    values: Dict[str, str] = {}
    if os.path.exists(path):
        mode = os.stat(path).st_mode & 0o777
        if mode & 0o077:
            print("warning: {} is mode {:o} -- chmod 600 it".format(path, mode),
                  file=sys.stderr)
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.split("#", 1)[0].strip()
                if "=" in line:
                    key, _, value = line.partition("=")
                    values[key.strip()] = value.strip().strip('"').strip("'")
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


def admin_token() -> str:
    token = os.environ.get("INSIGHT_ADMIN_TOKEN")
    if not token:
        raise SystemExit(
            "no admin token. Put one line in {}:\n\n"
            "    INSIGHT_ADMIN_TOKEN=...\n\n"
            "then `chmod 600` it. It is the same token the endpoint runs with."
            .format(ADMIN_ENV))
    return token


def endpoint() -> str:
    return (os.environ.get("INSIGHT_ENDPOINT") or DEFAULT_ENDPOINT).rstrip("/")


def call(method: str, path: str, payload: Optional[Dict[str, Any]] = None,
         timeout: int = 30) -> Tuple[int, Any]:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        endpoint() + path, data=body, method=method,
        headers={"Authorization": "Bearer " + admin_token(),
                 "User-Agent": USER_AGENT,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=common.ssl_context()) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}
    except urllib.error.URLError as exc:
        raise SystemExit(
            "{} is not reachable ({}). The read routes are not public -- open "
            "the tunnel first:\n\n    ssh -N -L 8479:127.0.0.1:8479 <host> &"
            .format(endpoint(), exc.reason))


# --------------------------------------------------------------------------
# people
# --------------------------------------------------------------------------

def cmd_people(args: argparse.Namespace) -> int:
    status, body = call("GET", "/v1/people")
    if status != 200:
        raise SystemExit("{}: {}".format(status, body.get("error") or body))
    people = body.get("people") or []
    if args.json:
        print(json.dumps(body, indent=2, sort_keys=True))
        return 0
    if not people:
        print("Nobody on the roster yet. `./admin.py add name@seta-international.vn`")
        return 0
    width = max(len(p["email"]) for p in people)
    for person in people:
        if person["enrolled"]:
            state = "collecting"
        elif person["on_roster"]:
            state = "waiting -- their machine has not run setup yet"
        else:
            state = "enrolled but off the roster -- `remove` or `add`"
        print("{:<{w}}  {}".format(person["email"], state, w=width))
    print()
    print("{} expected, {} collecting".format(
        body.get("expected", 0), body.get("enrolled", 0)))
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    """Add addresses. Their machines enrol themselves; nothing is relayed."""
    status, body = call("POST", "/v1/people", {"emails": args.email})
    if status not in (200, 201):
        raise SystemExit("{}: {}".format(status, body.get("error") or body))
    for result in body.get("results", []):
        print("[{}] {}".format(result["outcome"], result["email"]))
    print()
    print("Their machines register on the next collection run. Nothing to send "
          "them.")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    for email in args.email:
        status, body = call("DELETE", "/v1/people?email=" + email)
        print("[{}] {}".format(body.get("outcome", status), email))
    print()
    print("Bundles already collected are not deleted. That is a retention "
          "decision, not this one.")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    """Forget a fingerprint so a replacement laptop can enrol itself."""
    for email in args.email:
        status, body = call("POST", "/v1/people/reset", {"email": email})
        print("[{}] {}".format(body.get("outcome", status), email))
    print()
    print("The address stays on the roster. The new machine registers itself.")
    return 0


# --------------------------------------------------------------------------
# pull -- one period, into reports/<name>/
# --------------------------------------------------------------------------

def week_bounds(week: str) -> Tuple[date, date]:
    year, _, number = week.partition("-W")
    try:
        monday = date.fromisocalendar(int(year), int(number), 1)
    except (ValueError, AttributeError):
        raise SystemExit("--week takes YYYY-Www, e.g. 2026-W35")
    return monday, monday + timedelta(days=6)


def month_bounds(month: str) -> Tuple[date, date]:
    try:
        first = datetime.strptime(month, "%Y-%m").date()
    except ValueError:
        raise SystemExit("--month takes YYYY-MM, e.g. 2026-08")
    if first.month == 12:
        following = date(first.year + 1, 1, 1)
    else:
        following = date(first.year, first.month + 1, 1)
    return first, following - timedelta(days=1)


def period(args: argparse.Namespace) -> Tuple[str, date, date]:
    """The one place a week or a month becomes a name and two dates."""
    if args.week and args.month:
        raise SystemExit("--week or --month, not both")
    if args.month:
        start, end = month_bounds(args.month)
        return args.month, start, end
    week = args.week
    if not week:
        # The most recent complete week, which is what somebody typing
        # `pull` on a Monday morning means.
        today = datetime.now(timezone.utc).date()
        monday = today - timedelta(days=today.weekday() + 7)
        week = "{}-W{:02d}".format(*monday.isocalendar()[:2])
    start, end = week_bounds(week)
    return week, start, end


def run(step: str, argv: List[str], out: str, log: List[Dict[str, Any]]) -> None:
    """One child process. A failure is recorded and the rest still run.

    A poller that 401s must not cost the three that would have worked -- the
    report renders what it has and names what is missing, and that is only
    true if the missing thing was allowed to be missing.
    """
    print("  {} ...".format(step), flush=True)
    result = subprocess.run(
        [sys.executable] + argv, capture_output=True, text=True,
        cwd=_ROOT)
    entry: Dict[str, Any] = {"step": step, "ok": result.returncode == 0,
                             "exit": result.returncode}
    if os.path.exists(out):
        with open(out, "r", encoding="utf-8") as handle:
            entry["events"] = sum(1 for _ in handle)
    tail = (result.stderr or result.stdout or "").strip().splitlines()
    if tail:
        entry["detail"] = tail[-1][:300]
    log.append(entry)


def cmd_pull(args: argparse.Namespace) -> int:
    name, start, end = period(args)
    since = start.isoformat()
    until = end.isoformat()
    folder = os.path.join(_ROOT, "reports", name)
    exports = os.path.join(folder, "exports")
    os.makedirs(exports, exist_ok=True)

    config = common.Config.from_env()
    projects = list(config.jira_project_keys or ())
    repos = [r.strip() for r in
             (os.environ.get("BITBUCKET_REPOS") or "").split(",") if r.strip()]
    aio_projects = [p.strip() for p in
                    (os.environ.get("AIO_PROJECTS") or "").split(",")
                    if p.strip()] or projects

    print("{}  {} -> {}".format(name, since, until))
    print("into {}".format(os.path.relpath(folder, _ROOT)))
    print()

    log: List[Dict[str, Any]] = []

    if not args.no_poll:
        for full in repos:
            workspace, _, repo = full.partition("/")
            if not repo:
                print("  skip bitbucket {}: expected workspace/repo".format(full))
                continue
            out = os.path.join(exports, "bitbucket-{}.ndjson".format(repo))
            run("bitbucket {}".format(full),
                ["pollers/poll_bitbucket.py", "--workspace", workspace,
                 "--repo", repo, "--since", since + "T00:00:00Z",
                 "--no-watermark", "--out", out], out, log)

        for project in projects:
            out = os.path.join(exports, "jira-{}.ndjson".format(project))
            run("jira {}".format(project),
                ["pollers/poll_jira.py", "--project", project,
                 "--since", since + "T00:00:00Z", "--no-watermark",
                 "--out", out], out, log)

        for project in aio_projects:
            out = os.path.join(exports, "aio-runs-{}.ndjson".format(project))
            run("aio runs {}".format(project),
                ["pollers/poll_aio.py", "--project", project,
                 "--since", since + "T00:00:00Z", "--no-watermark",
                 "--pace", "0.3", "--out", out], out, log)
            out = os.path.join(exports, "aio-coverage-{}.ndjson".format(project))
            run("aio coverage {}".format(project),
                ["pollers/poll_aio.py", "--project", project, "--coverage",
                 "--since", since + "T00:00:00Z", "--no-watermark",
                 "--pace", "0.3", "--out", out], out, log)

    if not args.no_laptops:
        inbox = os.path.join(folder, "inbox")
        os.makedirs(inbox, exist_ok=True)
        roster = os.path.join(folder, "roster.txt")
        status, body = call("GET", "/v1/people")
        if status == 200:
            with open(roster, "w", encoding="utf-8") as handle:
                for person in body.get("people") or []:
                    if person["on_roster"]:
                        handle.write(person["email"] + "\n")
        out = os.path.join(exports, "laptops.ndjson")
        # A week name is what the endpoint indexes by, so a month is pulled a
        # week at a time rather than not at all.
        weeks = sorted({"{}-W{:02d}".format(*(start + timedelta(days=n))
                                            .isocalendar()[:2])
                        for n in range((end - start).days + 1)})
        for week in weeks:
            run("laptops {}".format(week),
                ["importers/pull.py", "--week", week, "--inbox", inbox,
                 "--roster", roster], inbox, log)
        run("bundle", ["importers/bundle.py", "--inbox", inbox, "--out", out,
                       "--state", os.path.join(folder, "bundles.json")],
            out, log)

    print()
    for entry in log:
        print("[{}] {:<24} {}".format(
            "  ok  " if entry["ok"] else " FAIL ", entry["step"],
            "{:,} events".format(entry["events"]) if entry.get("events")
            else entry.get("detail", "")))

    daily = os.path.join(folder, "daily")
    os.makedirs(daily, exist_ok=True)
    print()
    print("Exports:      {}".format(os.path.relpath(exports, _ROOT)))
    print("Daily report: drop it in {}/".format(os.path.relpath(daily, _ROOT)))
    print()
    print("Then write the report:  /weekly-report {}".format(name))
    failed = [e["step"] for e in log if not e["ok"]]
    if failed:
        print()
        print("{} step(s) failed: {}".format(len(failed), ", ".join(failed)),
              file=sys.stderr)
        print("The report renders what arrived and names what did not.",
              file=sys.stderr)
    return 0


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="admin.py",
        description="The pipeline side: who reports, and pulling a period.")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("people", help="who is expected, and who has arrived")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_people)

    p = sub.add_parser("add", help="expect an address; its machine enrols itself")
    p.add_argument("email", nargs="+")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("remove", help="stop expecting an address, and revoke it")
    p.add_argument("email", nargs="+")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("reset", help="forget a fingerprint (replacement laptop)")
    p.add_argument("email", nargs="+")
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("pull", help="a week or a month, into reports/<name>/")
    p.add_argument("--week", help="YYYY-Www (default: the last complete week)")
    p.add_argument("--month", help="YYYY-MM")
    p.add_argument("--no-poll", action="store_true",
                   help="skip Jira, Bitbucket and AIO")
    p.add_argument("--no-laptops", action="store_true",
                   help="skip the bundles engineers uploaded")
    p.set_defaults(func=cmd_pull)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    load_admin_env()
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        build_parser().print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
