#!/usr/bin/env python3
"""Refresh the poller cache once a day, without anybody typing anything.

    python3 importers/daily_pull.py                    # today, month-to-date
    python3 importers/daily_pull.py --date 2026-08-26  # a specific day
    python3 importers/daily_pull.py --force            # re-fetch what is cached

Four kinds of pull -- Jira, Bitbucket, AIO runs, AIO coverage -- into
``reports/cache/<YYYY-MM-DD>/``, one NDJSON file per source, plus a
``_status.json`` naming what arrived and what did not. ``admin.py pull`` runs
the same four pollers for a named week or month; this is the unattended
version, and the two agree on file names on purpose so a dated cache directory
can be dropped in wherever ``workbook-input/`` is read from.

**Why this exists.** The analysis cache was refreshed by a person remembering
to run four commands, which means it is current on the days somebody
remembered. A stale cache does not announce itself: it renders as a quiet week.

Three properties this job has to have, and each one cost something to learn.

*Absent is never zero.* A source that fails writes **no file at all**. The
tempting alternative -- an empty NDJSON where the pull should have been -- is
indistinguishable downstream from a real week with no pull requests, and the
whole point of a scheduled job is that nobody is watching when it fails. The
manifest names the failure, and the process exits non-zero.

*One failure costs one source.* AIO 429s readily and Jira tokens expire; if
either took the other three down, the daily cache would be all-or-nothing on
the least reliable source in the set. Each poller is a child process for that
reason -- its exit code is its contract, and a poller that dies outright still
only loses its own file.

*Idempotent for the day.* Re-running is the normal case, not the edge one:
something failed at 08:45 and the fix is to run it again at 11:00. A source
whose file is already there is left alone, so the re-run costs one API budget
rather than four. ``--force`` is the way to actually re-fetch.

**AR-1: projects are read, never invented.** ``--project`` comes from
``JIRA_PROJECT_KEYS`` and ``AIO_PROJECTS`` and from nowhere else. With neither
set there is no safe default -- polling "whatever Jira will return" is how a
key space stops being an allow-list -- so those sources are reported blocked
rather than guessed at.

**This runs on the laptop that holds the credentials, and only there.** Not on
``future``, which executes untrusted workflow code from pull requests and must
never hold one. Nothing here needs a special case for that: the credentials
come from the repository-root ``.env``, which does not exist on that box, so a
copy of this job landing there reports four blocked sources and exits non-zero
instead of quietly producing a day of zeros.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import common  # noqa: E402  -- Config, and the .env walk-up that finds it

#: Where a day lands. Under ``reports/``, which is gitignored: these files
#: carry live Atlassian account ids, issue keys and branch names, and a
#: scheduled job writing into a tracked directory would put them in front of
#: `git add -A` every morning.
DEFAULT_CACHE = os.path.join("reports", "cache")

#: AIO rate-limits far harder than Jira or Bitbucket, and an unattended job has
#: nobody to notice the retries. Same value ``admin.py pull`` uses.
AIO_PACE = "0.3"


class Source(NamedTuple):
    """One poller invocation and the file it is responsible for.

    ``needs`` names the credential set rather than the variables, so the check
    stays in ``common.Config`` where the poller itself does it -- two places
    disagreeing about what "configured" means is how a source ends up skipped
    with a green exit code.
    """

    name: str
    filename: str
    argv: List[str]
    needs: str


# --------------------------------------------------------------------------
# what to pull
# --------------------------------------------------------------------------

def window_start(day: date, since: Optional[str], days: Optional[int]) -> str:
    """The ISO8601 instant every poller is given as ``--since``.

    Default is the **first of the day's month**, not a rolling 30 days. The
    consumer is a monthly workbook, so a month-to-date pull drops straight into
    ``reports/<YYYY-MM>/workbook-input/``; a rolling window straddles two
    months and moves the denominator of every rate in the report by one day
    each morning, which is the kind of drift nobody attributes to the puller.
    """
    if since and days:
        raise SystemExit("--since or --days, not both")
    if since:
        start = date.fromisoformat(since)
    elif days:
        start = day - timedelta(days=days)
    else:
        start = day.replace(day=1)
    return start.isoformat() + "T00:00:00Z"


def plan(config: "common.Config", env: Dict[str, str], since: str) -> List[Source]:
    """The four kinds of pull, expanded over the configured projects and repos.

    Order is deliberate: Bitbucket and Jira first because they are quick and
    rarely throttled, AIO last because a paced inventory pass takes minutes. A
    run that is killed part-way has then finished the cheap sources rather than
    none of them.
    """
    projects = list(config.jira_project_keys or ())
    repos = [r.strip() for r in (env.get("BITBUCKET_REPOS") or "").split(",")
             if r.strip()]
    # AIO has its own project space and would otherwise inherit every Jira key
    # in the allow-list. The fallback matches `admin.py pull` so the two
    # produce the same files, and `daily_pull_plan` logs which route was taken
    # -- inheriting four keys into a source that has one is worth seeing.
    aio_projects = [p.strip() for p in (env.get("AIO_PROJECTS") or "").split(",")
                    if p.strip()]
    aio_inherited = not aio_projects and bool(projects)
    aio_projects = aio_projects or projects

    sources: List[Source] = []
    for full in repos:
        workspace, _, repo = full.partition("/")
        if not repo:
            # Named, not skipped silently: `aeriscom` on its own is a typo in
            # BITBUCKET_REPOS, and a typo that removes a source is exactly the
            # failure this job is supposed to make loud.
            common.log("daily_pull_bad_repo", value=full,
                       hint="BITBUCKET_REPOS entries are workspace/repo")
            continue
        sources.append(Source(
            name="bitbucket {}".format(full),
            filename="bitbucket-{}.ndjson".format(repo),
            needs="bitbucket",
            argv=["pollers/poll_bitbucket.py", "--workspace", workspace,
                  "--repo", repo, "--since", since, "--no-watermark"]))

    for project in projects:
        sources.append(Source(
            name="jira {}".format(project),
            filename="jira-{}.ndjson".format(project),
            needs="jira",
            argv=["pollers/poll_jira.py", "--project", project,
                  "--since", since, "--no-watermark"]))

    for project in aio_projects:
        sources.append(Source(
            name="aio runs {}".format(project),
            filename="aio-runs-{}.ndjson".format(project),
            needs="aio",
            argv=["pollers/poll_aio.py", "--project", project,
                  "--since", since, "--no-watermark", "--pace", AIO_PACE]))
        # Cycles scope, which is the default and is left implicit rather than
        # spelled out, because `--coverage-scope project` is a different
        # question. CLAUDE.md: metric 2 is measured per test cycle and never
        # over the case estate -- the estate reading of the same data puts P3
        # at 22.8% against 93.1%, and the difference is 3,695 cases whose
        # automation status nobody has filled in. A daily job must not be the
        # thing that quietly ships the misleading one.
        sources.append(Source(
            name="aio coverage {}".format(project),
            filename="aio-coverage-{}.ndjson".format(project),
            needs="aio",
            argv=["pollers/poll_aio.py", "--project", project, "--coverage",
                  "--since", since, "--no-watermark", "--pace", AIO_PACE]))

    common.log("daily_pull_plan", since=since, sources=len(sources),
               jira_projects=projects or None, bitbucket_repos=repos or None,
               aio_projects=aio_projects or None,
               aio_inherited_jira_keys=aio_inherited or None)
    return sources


# --------------------------------------------------------------------------
# running one
# --------------------------------------------------------------------------

#: ``(argv) -> (exit code, last diagnostic line)``. Injected by the tests, which
#: must never reach a real endpoint.
Runner = Callable[[List[str]], Tuple[int, str]]


def _subprocess_runner(argv: List[str]) -> Tuple[int, str]:
    """One poller, as its own process, from the repository root.

    A child process rather than an import for two reasons that are the same
    reason: the pollers' documented interface is an exit code (2 config, 3 API,
    130 interrupt), and an import shares this process's fate with theirs. AIO
    exhausting its retries must cost AIO.
    """
    result = subprocess.run([sys.executable] + argv, capture_output=True,
                            text=True, cwd=_ROOT)
    # stderr is single-line JSON diagnostics; the pollers never print event
    # content or credentials, so the last line is safe to keep. Truncated at
    # the same 300 characters `admin.py` uses.
    tail = (result.stderr or result.stdout or "").strip().splitlines()
    return result.returncode, (tail[-1][:300] if tail else "")


def _line_count(path: str) -> int:
    with open(path, "r", encoding="utf-8") as handle:
        return sum(1 for _ in handle)


def run_source(source: Source, directory: str, runner: Runner,
               force: bool = False,
               blocked: Optional[str] = None) -> Dict[str, Any]:
    """Fetch one source into ``directory``, and report honestly what happened.

    Four outcomes, and they are kept distinct because they need different
    people to fix them: ``ok`` (fetched), ``cached`` (already there this day),
    ``blocked`` (no credential -- an operator), ``failed`` (the API said no --
    usually a token or a rate limit).

    The file only appears on success. The poller writes to a ``.part`` which is
    renamed once it exits zero, so the presence of ``jira-IML.ndjson`` in a
    dated directory means a completed pull and nothing else. Renaming rather
    than deleting on failure also protects the earlier good file when a
    ``--force`` re-run is the thing that fails.
    """
    final = os.path.join(directory, source.filename)
    entry: Dict[str, Any] = {"source": source.name, "file": source.filename}

    if blocked:
        # Reported before the process is spawned so the missing variable can be
        # named. The poller would exit 2 on its own, but "blocked" and "failed"
        # are different sentences and only one of them means "ring the API
        # owner".
        entry.update(status="blocked", detail=blocked)
        return entry

    if os.path.exists(final) and not force:
        entry.update(status="cached", events=_line_count(final))
        return entry

    part = final + ".part"
    if os.path.exists(part):
        # Left by a run that was killed mid-fetch. It is a prefix of a window,
        # not a window, so it is never promoted.
        os.unlink(part)
    code, detail = runner(list(source.argv) + ["--out", part])

    if code == 0 and os.path.exists(part):
        os.replace(part, final)
        entry.update(status="ok", events=_line_count(final))
        return entry

    if os.path.exists(part):
        os.unlink(part)
    if code == 0:
        # Exited clean and wrote nothing at all. Not a measured zero: a
        # measured zero is an empty file the poller committed. Treated as a
        # failure so it cannot pass for a quiet day.
        entry.update(status="failed", exit=0,
                     detail=detail or "poller exited 0 without writing a file")
        return entry
    entry.update(status="failed", exit=code, detail=detail)
    return entry


# --------------------------------------------------------------------------
# the day
# --------------------------------------------------------------------------

def _blocked_reasons(config: "common.Config") -> Dict[str, Optional[str]]:
    """Which credential sets are unusable, and which variables are missing.

    ``ConfigError`` never carries a value, only names -- see its docstring --
    so this is safe to put in a manifest and a log line.
    """
    reasons: Dict[str, Optional[str]] = {}
    for needs, require in (("bitbucket", config.require_bitbucket),
                           ("jira", config.require_jira),
                           ("aio", config.require_aio)):
        try:
            require()
            reasons[needs] = None
        except common.ConfigError as exc:
            reasons[needs] = str(exc)
    return reasons


def _relative(path: str) -> str:
    """Repo-relative, always. An absolute path here carries the username, and
    the manifest is the file a reader is most likely to paste somewhere."""
    return os.path.relpath(path, _ROOT)


def daily_pull(day: date, cache_root: str, since: str,
               config: Optional["common.Config"] = None,
               env: Optional[Dict[str, str]] = None,
               runner: Optional[Runner] = None,
               force: bool = False) -> Dict[str, Any]:
    """Every source for one day, into ``<cache_root>/<YYYY-MM-DD>/``.

    ``config`` and ``runner`` are injectable for the same reason the pollers
    take a ``client``: the test-suite must never reach a real endpoint. They
    default at call time rather than in the signature -- a default bound at
    import time cannot be replaced, and the first version of this defaulted to
    the real subprocess runner so hard that the tests spawned live pollers.
    """
    config = config or common.Config.from_env()
    env = dict(os.environ if env is None else env)
    runner = runner or _subprocess_runner

    directory = os.path.join(cache_root, day.isoformat())
    os.makedirs(directory, exist_ok=True)

    sources = plan(config, env, since)
    reasons = _blocked_reasons(config)

    results: List[Dict[str, Any]] = []
    for source in sources:
        entry = run_source(source, directory, runner, force,
                           blocked=reasons.get(source.needs))
        results.append(entry)
        # One JSON line per source, on stderr, as it finishes. A scheduled job
        # is read from a log file after the fact, and a summary printed at the
        # end is the one thing a killed run never gets to print.
        common.log("daily_pull_source", **entry)

    manifest = {
        "date": day.isoformat(),
        "since": since,
        "generated_at": datetime.now(timezone.utc)
                                .replace(microsecond=0).isoformat()
                                .replace("+00:00", "Z"),
        "directory": _relative(directory),
        "sources": results,
        "ok": bool(sources) and all(
            r["status"] in ("ok", "cached") for r in results),
    }
    # Written whatever happened, including when nothing was planned. A cache
    # directory with no manifest and a cache directory with a manifest full of
    # failures say different things, and only the second one is readable by
    # somebody who was asleep when it ran.
    with open(os.path.join(directory, "_status.json"), "w",
              encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)

    _point_latest(cache_root, day)
    return manifest


def _point_latest(cache_root: str, day: date) -> None:
    """``<cache_root>/latest`` -> today's directory.

    A relative symlink, so the tree survives being moved or copied to another
    machine, and so nothing in it spells out a home directory. Best effort: a
    filesystem that cannot do symlinks costs a convenience, not the pull.
    """
    link = os.path.join(cache_root, "latest")
    try:
        if os.path.islink(link) or os.path.exists(link):
            os.unlink(link)
        os.symlink(day.isoformat(), link)
    except OSError as exc:
        common.log("daily_pull_latest_unlinked", error=str(exc))


# --------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None,
         runner: Optional[Runner] = None,
         config: Optional["common.Config"] = None) -> int:
    """``runner``/``config`` are injectable so the tests never touch a source."""
    parser = argparse.ArgumentParser(
        prog="daily_pull.py",
        description="Refresh the Jira/Bitbucket/AIO cache for one day.")
    parser.add_argument("--date", help="day to pull (default: today, UTC). "
                                       "Names the cache directory")
    parser.add_argument("--cache", default=DEFAULT_CACHE,
                        help="cache root, repo-relative "
                             "(default: {})".format(DEFAULT_CACHE))
    parser.add_argument("--since", help="window start YYYY-MM-DD "
                                        "(default: the first of --date's month)")
    parser.add_argument("--days", type=int,
                        help="window start as --date minus N days. Prefer the "
                             "month default: the workbook is monthly and a "
                             "rolling window moves its denominator daily")
    parser.add_argument("--force", action="store_true",
                        help="re-fetch sources already cached for this day")
    args = parser.parse_args(argv)

    day = (date.fromisoformat(args.date) if args.date
           else datetime.now(timezone.utc).date())
    since = window_start(day, args.since, args.days)
    cache_root = args.cache if os.path.isabs(args.cache) \
        else os.path.join(_ROOT, args.cache)

    manifest = daily_pull(day, cache_root, since, config=config,
                          runner=runner, force=args.force)
    print(json.dumps(manifest, indent=2, sort_keys=True))

    if not manifest["sources"]:
        # Nothing was even attempted. Distinct from a failed pull, and it is
        # always the same fix: JIRA_PROJECT_KEYS / BITBUCKET_REPOS / AIO_PROJECTS
        # are empty, so AR-1 leaves nothing safe to poll.
        common.log("daily_pull_nothing_planned",
                   hint="set JIRA_PROJECT_KEYS, BITBUCKET_REPOS, AIO_PROJECTS "
                        "in the repository-root .env. Keys are never guessed")
        return 2

    bad = [r for r in manifest["sources"] if r["status"] not in ("ok", "cached")]
    if bad:
        # Non-zero so launchd's log, and anybody reading `$?`, sees a bad day as
        # a bad day. The sources that worked are still on disk and still
        # readable -- the exit code says the cache is incomplete, not empty.
        common.log("daily_pull_incomplete", failed=len(bad),
                   sources=[r["source"] for r in bad],
                   hint="those files are ABSENT, not zero. Every rate computed "
                        "over this day is missing their denominator")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
