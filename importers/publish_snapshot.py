#!/usr/bin/env python3
"""Rebuild the /insights snapshot from a dated cache and put it on the endpoint.

    python3 importers/publish_snapshot.py                       # latest cache
    python3 importers/publish_snapshot.py --cache reports/cache/2026-08-26
    python3 importers/publish_snapshot.py --dry-run             # build, no push

Two screens read one file, and until now that file moved only when somebody
remembered to type the `report/dashboard_data.py` invocation out of
`docs/OPERATE.md` and then copy the result up. A page that is a fortnight stale
does not say so -- it draws a real week of work as a flat line, which is worse
than an empty page, because an empty page sends somebody to find out why.
`importers/daily_pull.py --publish` calls this after a complete pull, so the
screens follow the data and nobody runs a command.

**Nothing here is a person's name.** The member list, their account ids and the
destination host live in `reports/dashboard.json`, which is gitignored for the
same reason `reports/identities.txt` is. `reports/dashboard.json.example` is
the tracked copy and its names are invented. A source file that carries the
team's names commits them to git history the first time anybody edits it.

**Nothing here is a credential, either.** The snapshot is derived counts: no
tokens, no emails, no paths, no prompt or response text (`schema/CONTRACT.md`
§1.1, and `report/dashboard_data.py`'s own header). The destination box runs
untrusted workflow code from pull requests and must never hold a secret, so
this pushes a data file to it and nothing else. The config file carries no
secret for the same reason -- ssh's own key and `~/.ssh/config` do that job,
outside this repository.

**The window is derived, never written down.** A `--weeks` span pasted into a
config is correct on the day it is pasted and silently wrong every Monday
after. This reads the pull's own manifest instead: the weeks it asks for end
with the day being published, and the two weeks that cannot be compared for
volume -- the one still running, and any week that starts before the pull
window does -- are marked partial and kept out of `--full-weeks`. Leaving a
part week in a trend once turned a +74% into a -17%.

**Written to a `.tmp` beside the target and renamed.** The endpoint re-stats
the file rather than caching it for the process lifetime, so it will read
whatever is there the moment the mtime moves. Uploading over the live path
gives it a window in which to parse half a document. `mv` in the same directory
is a rename(2) and has no such window.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shlex
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import common  # noqa: E402

#: Names individuals and carries their account ids, so it is gitignored data
#: and not source. `reports/dashboard.json.example` is the tracked shape.
DEFAULT_CONFIG = os.path.join("reports", "dashboard.json")

#: The symlink `importers/daily_pull.py` repoints at the end of every run.
DEFAULT_CACHE = os.path.join("reports", "cache", "latest")

#: How many ISO weeks the screens show, ending with the week being published.
#: Five is what fits the charts and is what `docs/OPERATE.md` documented by
#: hand; a config may say otherwise, and nothing here is load-bearing on it.
DEFAULT_WEEKS_BACK = 5

#: What a member entry may say. Anything else is a typo -- `pronoun` for
#: `pronouns` would otherwise be dropped in silence, and the page would render
#: a they/them nobody chose.
MEMBER_KEYS = frozenset({"name", "account_id", "short", "role", "pronouns"})

#: `(argv) -> (exit code, last diagnostic line)`. Injected by the tests, which
#: must never build a real snapshot or reach a real host.
Runner = Callable[[List[str]], Tuple[int, str]]


class ConfigError(Exception):
    """Names the key that is missing or malformed, never its value.

    The message goes to a log and into launchd's file, and this config holds
    account ids and a hostname. Same rule as `common.ConfigError`.
    """


# --------------------------------------------------------------------------
# the config file
# --------------------------------------------------------------------------

def _require(mapping: Dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError("{}: missing key {!r}".format(where, key))
    return mapping[key]


def _check_destination(value: Any) -> str:
    """`host:/absolute/path/insights.json`, and both halves are checked.

    A relative remote path is the failure worth catching here: scp resolves it
    against the ssh login's home directory, so the push reports success and the
    endpoint keeps serving the snapshot from a month ago. Nothing downstream
    would ever say so.
    """
    if not isinstance(value, str) or ":" not in value:
        raise ConfigError(
            "destination: wants host:/path/to/insights.json")
    host, _, path = value.partition(":")
    if not host.strip():
        raise ConfigError("destination: names no host before the ':'")
    if not path.startswith("/"):
        raise ConfigError(
            "destination: remote path must be absolute, or scp resolves it "
            "against the login home directory and the push succeeds nowhere "
            "useful")
    return value


def _check_prices(value: Any) -> Dict[str, str]:
    """`{"model": "IN/OUT"}`, per 1M tokens, exactly `--price`'s form.

    An empty object is allowed and is a real answer: a model with no price is
    counted and left unpriced. A *malformed* price is not -- it would be
    rejected by `report/dashboard_data.py` several minutes into a build, in a
    subprocess whose failure this can only report second-hand.
    """
    if not isinstance(value, dict):
        raise ConfigError("prices: wants an object of model -> \"IN/OUT\"")
    for model, rate in value.items():
        inp, _, out = str(rate).partition("/")
        try:
            float(inp), float(out)
        except ValueError:
            raise ConfigError(
                "prices[{!r}]: wants \"IN/OUT\" per 1M tokens, e.g. "
                "\"3.0/15.0\"".format(model))
    return {str(k): str(v) for k, v in value.items()}


def _check_members(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ConfigError("members: wants a non-empty list of people")
    members: List[Dict[str, str]] = []
    for i, entry in enumerate(value):
        where = "members[{}]".format(i)
        if not isinstance(entry, dict):
            raise ConfigError("{}: wants an object".format(where))
        unknown = sorted(set(entry) - MEMBER_KEYS)
        if unknown:
            raise ConfigError("{}: unknown key(s) {} -- allowed: {}".format(
                where, ", ".join(repr(k) for k in unknown),
                ", ".join(sorted(MEMBER_KEYS))))
        member = {}
        for key in ("name", "account_id"):
            got = _require(entry, key, where)
            if not str(got).strip():
                raise ConfigError("{}: {!r} is empty".format(where, key))
            member[key] = str(got).strip()
        for key in ("short", "role", "pronouns"):
            if entry.get(key) is not None and str(entry[key]).strip():
                member[key] = str(entry[key]).strip()
        members.append(member)
    return members


def load_config(path: str) -> Dict[str, Any]:
    """Read and check the whole file before anything is built or pushed.

    All of it up front, because the expensive half of this job is the build and
    the irreversible half is the push. A destination typo that surfaces after
    four minutes of collecting is a typo found by the person who has already
    stopped watching.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        raise ConfigError(
            "{}: no such file. Copy reports/dashboard.json.example and fill it "
            "in -- it names individuals, so it is data and not source".format(
                _relative(path)))
    except ValueError as exc:
        raise ConfigError("{}: not JSON ({})".format(_relative(path), exc))
    if not isinstance(raw, dict):
        raise ConfigError("{}: wants a JSON object".format(_relative(path)))

    where = _relative(path)
    weeks_back = raw.get("weeks_back", DEFAULT_WEEKS_BACK)
    if not isinstance(weeks_back, int) or isinstance(weeks_back, bool) \
            or weeks_back < 2:
        # Two is the floor rather than one: the week being published is always
        # partial, so a single-week window has nothing complete in it and
        # `--full-weeks` would have nothing to name.
        raise ConfigError("weeks_back: wants a whole number of weeks, 2 or more")

    return {
        "destination": _check_destination(_require(raw, "destination", where)),
        "members": _check_members(_require(raw, "members", where)),
        "prices": _check_prices(_require(raw, "prices", where)),
        "weeks_back": weeks_back,
    }


# --------------------------------------------------------------------------
# which weeks
# --------------------------------------------------------------------------

def _week(day: datetime.date) -> str:
    year, num, _ = day.isocalendar()
    return "%d-W%02d" % (year, num)


def window(day: datetime.date, weeks_back: int,
           since: Optional[str] = None) -> Dict[str, Any]:
    """The `--weeks`, `--full-weeks` and `--partial` the build is given.

    Two kinds of week may not be compared for volume, and both are named rather
    than dropped: the one still running, and any week that begins before the
    pull's own window does, whose days are missing because nobody fetched them.
    Dropping a week instead would leave the page showing four weeks where a
    reader counted five, with nothing to say why.
    """
    monday = day - datetime.timedelta(days=day.isocalendar()[2] - 1)
    firsts = [monday - datetime.timedelta(weeks=n)
              for n in range(weeks_back - 1, -1, -1)]
    weeks = [_week(d) for d in firsts]

    partial: Dict[str, str] = {}
    start = datetime.date.fromisoformat(since[:10]) if since else None
    for first in firsts:
        if start and first < start:
            partial[_week(first)] = "pull window starts " + start.isoformat()
    partial[_week(monday)] = "week not finished"

    full = [w for w in weeks if w not in partial]
    if not full:
        raise ConfigError(
            "no complete week in the last {}: every one of {} is partial. "
            "Raise weeks_back, or pull a longer window -- volume may only be "
            "compared across weeks that finished".format(
                weeks_back, ", ".join(weeks)))
    return {"weeks": weeks, "full": full, "partial": partial}


def read_manifest(cache_dir: str) -> Optional[Dict[str, Any]]:
    """`_status.json`, if this is a directory `daily_pull` wrote.

    Optional on purpose: `--cache reports/2026-08/exports` is a legitimate
    thing to build from, and it has no manifest. Its absence costs the window
    clamp above and nothing else, and is logged so the wider window is not a
    surprise.
    """
    try:
        with open(os.path.join(cache_dir, "_status.json"), "r",
                  encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# building
# --------------------------------------------------------------------------

def build_argv(config: Dict[str, Any], cache_dir: str, out: str,
               weeks: Dict[str, Any]) -> List[str]:
    """The `report/dashboard_data.py` invocation `docs/OPERATE.md` documents.

    Assembled rather than reimplemented: that module is the one source of the
    figures, and a second builder here would be a second answer to every
    question the workbook already answers.
    """
    argv = [sys.executable, os.path.join("report", "dashboard_data.py")]
    for member in config["members"]:
        argv += ["--person", "{}={}".format(member["name"],
                                            member["account_id"])]
        for flag, key in (("--short", "short"), ("--role", "role"),
                          ("--pronouns", "pronouns")):
            if key in member:
                argv += [flag, "{}={}".format(member["name"], member[key])]
    for model, rate in sorted(config["prices"].items()):
        argv += ["--price", "{}={}".format(model, rate)]
    for week in weeks["weeks"]:
        if week in weeks["partial"]:
            argv += ["--partial", "{}={}".format(week, weeks["partial"][week])]
    argv += ["--input", cache_dir,
             "--weeks", "{}..{}".format(weeks["weeks"][0], weeks["weeks"][-1]),
             "--full-weeks", "{}..{}".format(weeks["full"][0],
                                             weeks["full"][-1]),
             "--out", out]
    return argv


def _readable_snapshot(path: str) -> Dict[str, Any]:
    """Parse what was built before it is allowed near the endpoint.

    The endpoint refuses a snapshot it cannot parse and serves a 503 with a
    sentence, which is the right behaviour there and a bad way to find out
    here: by then the good file has already been replaced. Cheap to check on
    the machine that made it.
    """
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    for key in ("schema", "people", "metrics", "weeks"):
        if key not in payload:
            raise ConfigError(
                "built snapshot has no {!r} -- not publishing it".format(key))
    return payload


# --------------------------------------------------------------------------
# pushing
# --------------------------------------------------------------------------

def _subprocess_runner(argv: List[str]) -> Tuple[int, str]:
    """One child process, from the repository root.

    Same contract as `importers/daily_pull.py`'s runner and for the same
    reason: an exit code is the interface, and neither `scp` nor a several
    minute build belongs in this process's own fate. stderr is truncated to the
    300 characters `admin.py` uses -- ssh's diagnostics name a host, never a
    key.
    """
    result = subprocess.run(argv, capture_output=True, text=True, cwd=_ROOT)
    tail = (result.stderr or result.stdout or "").strip().splitlines()
    return result.returncode, (tail[-1][:300] if tail else "")


def push_argv(local: str, destination: str) -> List[List[str]]:
    """Copy to a `.tmp` beside the target, then rename onto it.

    Two commands and not one. The endpoint re-stats this file and re-reads it
    the moment the mtime moves, so a copy straight onto the live path gives it
    a document that is half there. `mv` in the same directory is rename(2),
    which has no such moment.

    `BatchMode=yes` on both: this runs from launchd with no terminal, and ssh's
    default on a missing key is to prompt -- which from a scheduled job is not
    a prompt, it is a process that never exits.
    """
    host, _, remote = destination.partition(":")
    tmp = remote + ".tmp"
    return [
        ["scp", "-q", "-o", "BatchMode=yes", local,
         "{}:{}".format(host, shlex.quote(tmp))],
        ["ssh", "-o", "BatchMode=yes", host,
         "mv -- {} {}".format(shlex.quote(tmp), shlex.quote(remote))],
    ]


def push(local: str, destination: str, runner: Runner) -> None:
    """Both halves, and the rename is never attempted after a failed copy."""
    for argv in push_argv(local, destination):
        code, detail = runner(list(argv))
        if code != 0:
            raise ConfigError(
                "{} exited {}{}".format(argv[0], code,
                                        ": " + detail if detail else ""))


# --------------------------------------------------------------------------
# the job
# --------------------------------------------------------------------------

def _relative(path: str) -> str:
    """Repo-relative, always: an absolute path here carries the username.

    A path outside the checkout is reduced to its last component rather than
    walked back to with `..`, which would spell the home directory out just as
    plainly as the absolute form did.
    """
    try:
        relative = os.path.relpath(path, _ROOT)
    except ValueError:
        return os.path.basename(path)
    return os.path.basename(path) if relative.startswith("..") else relative


def publish(config: Dict[str, Any], cache_dir: str, day: datetime.date,
            out: str, runner: Optional[Runner] = None,
            dry_run: bool = False) -> Dict[str, Any]:
    """Build the snapshot for `day` out of `cache_dir`, then put it in place.

    `out` is where the built file lands locally and is kept: the deploy is a
    tar copy, so a machine that has to redeploy the endpoint by hand still
    wants the current snapshot on disk. The push is a separate step over the
    top of it.
    """
    runner = runner or _subprocess_runner

    manifest = read_manifest(cache_dir)
    if manifest is None:
        common.log("publish_snapshot_no_manifest", cache=_relative(cache_dir),
                   hint="not a daily_pull directory; the week window is not "
                        "clamped to a pull window")
    weeks = window(day, config["weeks_back"],
                   since=(manifest or {}).get("since"))

    argv = build_argv(config, cache_dir, out, weeks)
    common.log("publish_snapshot_build", weeks=weeks["weeks"],
               full_weeks=weeks["full"], people=len(config["members"]),
               cache=_relative(cache_dir))
    code, detail = runner(argv)
    if code != 0:
        raise ConfigError(
            "report/dashboard_data.py exited {}{}".format(
                code, ": " + detail if detail else ""))

    payload = _readable_snapshot(out)
    result = {
        "out": _relative(out),
        "bytes": os.path.getsize(out),
        "generated_at": payload.get("generated_at"),
        "weeks": weeks["weeks"],
        "full_weeks": weeks["full"],
    }

    if dry_run:
        common.log("publish_snapshot_not_pushed", reason="--dry-run", **result)
        return dict(result, pushed=False)

    push(out, config["destination"], runner)
    common.log("publish_snapshot_pushed",
               destination=config["destination"], **result)
    return dict(result, pushed=True)


# --------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None,
         runner: Optional[Runner] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="publish_snapshot.py",
        description="Rebuild the /insights snapshot and put it on the "
                    "endpoint.")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="the member list, prices and destination, "
                             "repo-relative (default: {}). Gitignored: it "
                             "names individuals".format(DEFAULT_CONFIG))
    parser.add_argument("--cache", default=DEFAULT_CACHE,
                        help="the pull to build from, repo-relative "
                             "(default: {})".format(DEFAULT_CACHE))
    parser.add_argument("--date", help="the day being published, which is the "
                                       "last week shown (default: today, UTC)")
    parser.add_argument("--out", default=os.path.join("server", "assets",
                                                      "insights.json"),
                        help="where the built snapshot is kept locally "
                             "(default: server/assets/insights.json, which is "
                             "what a hand redeploy tars up)")
    parser.add_argument("--dry-run", action="store_true",
                        help="build it and stop. Nothing is copied anywhere")
    args = parser.parse_args(argv)

    day = (datetime.date.fromisoformat(args.date) if args.date
           else datetime.datetime.now(datetime.timezone.utc).date())

    def absolute(path: str) -> str:
        return path if os.path.isabs(path) else os.path.join(_ROOT, path)

    out = absolute(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        config = load_config(absolute(args.config))
        result = publish(config, absolute(args.cache), day, out,
                         runner=runner, dry_run=args.dry_run)
    except (ConfigError, OSError, ValueError) as exc:
        # 2 is what every poller here uses for "the configuration or the input
        # was wrong, nothing shipped". A push that failed is the same sentence
        # to whoever reads the log: the endpoint is still serving the snapshot
        # it had, which is old but is not half a document.
        #
        # `ValueError` and `OSError` are here because a snapshot that does not
        # parse, or that never appeared, arrive that way -- and a traceback out
        # of a launchd job is a stack for a fault that already has a sentence.
        common.log("publish_snapshot_failed", error=str(exc))
        return 2
    # The two are not the same sentence and the log is read after the fact: a
    # dry run that printed "published" is how somebody concludes the endpoint
    # has today's figures when nothing left this machine.
    print(json.dumps(dict(result, msg="snapshot_published" if result["pushed"]
                          else "snapshot_built"), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
