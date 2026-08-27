#!/usr/bin/env python3
"""Fetch a week's bundles from the collection endpoint into ``inbox/``.

    python3 importers/pull.py --week 2026-W34 --inbox inbox/ --roster roster.txt
    python3 importers/bundle.py --inbox inbox/ --out events.ndjson

The download half of the endpoint. It stops at ``inbox/`` on purpose --
``bundle.py`` already parses, checksums, re-checks the allow-list and dedupes,
and a bundle that arrived over HTTP has earned none of those exemptions. This
just replaces the person who used to save attachments into a folder.

**The roster is the point.** When bundles arrived by email, a missing week was
visible: no email came. Automate the transport and silence turns ambiguous --
nobody worked, or nobody uploaded? An unmeasured quantity and a measured zero
must never render the same, and automation makes that failure *more* likely
rather than less. So this reports coverage against a declared roster, and names
who sent nothing.

``bundle.py``'s own coverage report cannot do that, by construction: it is
derived from the bundles that arrived, so someone who has never sent anything is
invisible to it. Only a roster knows who was expected.

The roster is work emails, because the proxy authenticated the upload and knows
who sent it. ``missing: minh@seta-international.vn`` is something a person acts
on; ``missing: 4c8d1104`` is something a person ignores. Emails stay on this
side of the pipeline -- ``CONTRACT.md 1.1`` keeps them out of the events
themselves, and nothing here writes one into the inbox.

If the roster is omitted this still falls back to comparing machine ids, so a
deployment with no whitelist yet is degraded rather than broken.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "importers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import common  # noqa: E402  -- the shared trust store; see common.ssl_context

USER_AGENT = "insight-pull/1"


class PullError(Exception):
    """Nothing was written. A partial week is worse than an absent one."""


# --------------------------------------------------------------------------
# the wire
# --------------------------------------------------------------------------

def _get(url: str, token: str, timeout: int) -> Tuple[int, bytes]:
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(
                request, timeout=timeout, context=common.ssl_context()) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        if common.is_certificate_error(exc):
            raise PullError("{}: {}".format(url, common.CERT_ADVICE))
        raise PullError("{} unreachable: {}".format(url, exc))


def _check(status: int, body: bytes, url: str) -> bytes:
    if status == 401:
        raise PullError(
            "{} rejected the admin token. Reading exposes every machine at "
            "once, so this route is authenticated even though uploading is "
            "not -- see docs/OPERATE.md".format(url))
    if status != 200:
        detail = body.decode("utf-8", "replace")[:200]
        raise PullError("{} returned HTTP {}{}".format(
            url, status, " -- " + detail if detail else ""))
    return body


def list_week(endpoint: str, week: str, token: str, timeout: int = 60,
              get=_get) -> List[Dict[str, Any]]:
    url = "{}/v1/bundles?{}".format(
        endpoint.rstrip("/"), urllib.parse.urlencode({"week": week}))
    body = _check(*get(url, token, timeout), url=url)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PullError("{} did not return JSON: {}".format(url, exc))
    objects = payload.get("objects")
    if not isinstance(objects, list):
        raise PullError("{} returned no objects list".format(url))
    return objects


def fetch(endpoint: str, key: str, token: str, timeout: int = 60,
          get=_get) -> bytes:
    url = "{}/v1/bundle/{}".format(endpoint.rstrip("/"), urllib.parse.quote(key))
    return _check(*get(url, token, timeout), url=url)


# --------------------------------------------------------------------------
# roster
# --------------------------------------------------------------------------

def read_roster(path: str) -> List[str]:
    """One work email per line; ``#`` comments and blanks ignored.

    The same file that seeds the server's ``INSIGHT_ALLOWED``, minus the
    fingerprints -- so a person added to one is obviously missing from the
    other. A display name after the address is allowed and kept out of the
    comparison.
    """
    people: List[str] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            # Tolerates a pasted INSIGHT_ALLOWED entry, fingerprints and all.
            people.append(line.split()[0].split(":")[0].strip().lower())
    return [p for p in people if p]


def _who(obj: Dict[str, Any]) -> str:
    """Who an object came from -- the authenticated email where there is one.

    Falls back to the machine id so a proxy that predates the whitelist still
    produces a coverage report, rather than one that silently says nobody
    reported.
    """
    return str(obj.get("email") or obj.get("machine") or "").strip().lower()


def bundle_measured(path: str) -> bool:
    """Whether a bundle on disk came from a machine that could measure anything.

    Shares the rule with ``bundle.py`` rather than restating it -- the two
    disagreeing about what a zero means is exactly the confusion this is here
    to remove. An unreadable file is left to `bundle.py`, which rejects it
    loudly and whole; inventing a second opinion here would only add noise.
    """
    import bundle as bundle_mod                                  # noqa: PLC0415

    try:
        with open(path, "r", encoding="utf-8") as handle:
            manifest = json.loads(handle.readline()).get("_manifest")
    except (OSError, ValueError, AttributeError):
        return True
    return bundle_mod.measured(manifest) if isinstance(manifest, dict) else True


def coverage(objects: List[Dict[str, Any]], roster: List[str],
             measured: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
    """Who reported this week, and who was expected and did not."""
    arrived = sorted({_who(o) for o in objects if _who(o)})
    expected = sorted({str(p).strip().lower() for p in roster if str(p).strip()})
    measured = measured or {}
    return {
        "expected": len(expected),
        "arrived": len(arrived),
        "reported": arrived,
        # Reported, and every bundle came from a machine with nothing
        # configured to read. Worse than missing: missing is visible in the
        # line above, while these arrive looking like a week of zeros and get
        # averaged in as though somebody had measured them.
        "reported_but_measuring_nothing": [
            p for p in arrived if not measured.get(p, True)],
        # Absent, not zero. Named so a reader can weigh the week honestly
        # rather than reading a smaller total as a quieter team.
        "missing": [p for p in expected if p not in arrived],
        # Someone who sent but is on nobody's roster is worth surfacing:
        # usually a new joiner nobody added, occasionally a stale entry.
        "unexpected": [p for p in arrived if p not in expected],
    }


# --------------------------------------------------------------------------

def read_identities(path: Optional[str]) -> Dict[str, str]:
    """``email accountId`` per line. Comments and blanks ignored.

    An operator file, like the roster beside it, and for the same reason: a
    laptop cannot know its own Atlassian accountId -- that is a Jira fact, not
    a machine one -- so the address it authenticated with has to be turned into
    one somewhere, and this is the last place the address exists.

    Without it every laptop event keeps `person_id: null` and joins to nothing,
    which is what was measured on 2026-08-26: 935 laptop events sharing not one
    id with AIO or Bitbucket, while those two joined to each other perfectly.
    """
    found: Dict[str, str] = {}
    if not path:
        return found
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                found[parts[0].strip().lower()] = parts[1].strip()
    return found


def pull_week(endpoint: str, week: str, token: str, inbox: str,
              roster: Optional[List[str]] = None, timeout: int = 60,
              get=_get, identities: Optional[Dict[str, str]] = None
              ) -> Dict[str, Any]:
    objects = list_week(endpoint, week, token, timeout, get)
    os.makedirs(inbox, exist_ok=True)
    identities = identities or {}
    #: bundle file name -> accountId. Written beside the bundles for
    #: `bundle.py`, and deliberately keyed by file name rather than by address:
    #: the inbox is the start of the event path and CONTRACT.md §1.1 keeps raw
    #: addresses out of it.
    stamped: Dict[str, str] = {}
    unmapped: set = set()

    written, skipped = [], []
    measured: Dict[str, bool] = {}
    for obj in objects:
        key = obj.get("key")
        if not key:
            continue
        # Flattened, because the key's slashes are the proxy's filing scheme and
        # `bundle.py` reads one flat directory. The machine and digest stay in
        # the name so a file on disk is still traceable to its object.
        #
        # Named by machine, not by email: the inbox feeds bundle.py, which is
        # the start of the event path, and CONTRACT.md 1.1 keeps raw addresses
        # out of it. The email stays in the coverage report on stderr.
        machine = str(obj.get("machine") or "")[:8]
        name = os.path.basename(key)
        if machine and not name.startswith(machine):
            # Only when the proxy files under some other scheme. The contract's
            # key already ends in `<machine>-<digest>.ndjson`, and prefixing it
            # again produces `57833cd4-57833cd4-...`.
            name = "{}-{}".format(machine, name)
        path = os.path.join(inbox, name)
        who = identities.get(_who(obj))
        if who:
            stamped[name] = who
        elif _who(obj):
            unmapped.add(_who(obj))
        if os.path.exists(path):
            # Keyed by content digest upstream, so the same name is the same
            # bytes. Re-running a pull is cheap and safe, which matters because
            # a week gets pulled again whenever someone ships late.
            skipped.append(name)
        else:
            body = fetch(endpoint, key, token, timeout, get)
            with open(path, "wb") as handle:
                handle.write(body)
            written.append(name)

        # One measured bundle in the week is enough: somebody who configured
        # the client on Wednesday has a Monday of empty bundles that are not
        # evidence of anything, and is still reporting properly now.
        person = _who(obj)
        if person:
            measured[person] = measured.get(person, False) or bundle_measured(path)

    # Written even when empty, so `bundle.py` can tell "nobody is mapped" from
    # "this pull predates identities" -- the same measured-zero rule the
    # manifests follow.
    #
    # **Merged, not replaced.** One inbox is meant to hold several weeks --
    # `docs/OPERATE.md` and the weekly-report skill both say to pull a span
    # into one and let the generator filter, because a backfill is filed under
    # the week it *started*. Rewriting this file per week left only the last
    # week's names in it, so `bundle.py` mapped that week and left every
    # earlier one at `person_id: null`. Measured 2026-08-27: a five-week pull
    # produced a map covering 24 of 58 bundles, and 1,081 of 1,390 imported
    # events were unattributed -- the exact failure `--identities` exists to
    # prevent, arriving silently and looking like missing data.
    if identities:
        path = os.path.join(inbox, "_identities.json")
        merged: Dict[str, str] = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    found = json.load(handle)
                if isinstance(found, dict):
                    merged = {str(k): str(v) for k, v in found.items()
                              if isinstance(v, str)}
            except (OSError, ValueError):
                # A corrupt map is replaced rather than allowed to block the
                # pull; the names it held are all rederivable from the listing.
                merged = {}
        merged.update(stamped)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, indent=2, sort_keys=True)

    result = {
        "week": week,
        "objects_listed": len(objects),
        "files_written": written,
        "files_already_present": skipped,
        "inbox": inbox,
        "identified": len(stamped),
    }
    if unmapped:
        # Named, not counted. An unmapped person is one whose AI usage will not
        # join to their test runs, and the fix is one line in the identities
        # file -- which nobody does for a number.
        result["unmapped"] = sorted(unmapped)
    if roster is not None:
        result["coverage"] = coverage(objects, roster, measured)
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch a week's bundles from the collection endpoint.")
    parser.add_argument("--week", required=True, help="ISO week, e.g. 2026-W34")
    parser.add_argument("--identities",
                        help="file of `email accountId` lines. Turns the "
                             "address the endpoint authenticated into the "
                             "Atlassian accountId CONTRACT.md 2.1 calls the "
                             "canonical person key, so laptop events join to "
                             "AIO runs and pull requests. Without it they "
                             "join to nothing")
    parser.add_argument("--inbox", required=True,
                        help="directory to write bundles into")
    parser.add_argument("--endpoint", help="collection endpoint "
                                           "(default: $INSIGHT_ENDPOINT)")
    parser.add_argument("--token", help="admin bearer token "
                                        "(default: $INSIGHT_ADMIN_TOKEN)")
    parser.add_argument("--roster",
                        help="file of work emails expected to report -- the "
                             "same people as the server's INSIGHT_ALLOWED")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args(argv)

    endpoint = args.endpoint or os.environ.get("INSIGHT_ENDPOINT")
    token = args.token or os.environ.get("INSIGHT_ADMIN_TOKEN")
    if not endpoint:
        raise SystemExit("no endpoint -- pass --endpoint or set INSIGHT_ENDPOINT")
    if not token:
        raise SystemExit(
            "no admin token -- pass --token or set INSIGHT_ADMIN_TOKEN")

    roster = read_roster(args.roster) if args.roster else None
    try:
        result = pull_week(endpoint, args.week, token, args.inbox, roster,
                           args.timeout,
                           identities=read_identities(args.identities))
    except PullError as exc:
        raise SystemExit(str(exc))

    print(json.dumps(result, indent=2, sort_keys=True))

    report = result.get("coverage")
    if report:
        print("\n{}: {} of {} reported".format(
            args.week, report["arrived"], report["expected"]), file=sys.stderr)
        if report["missing"]:
            print("missing: {}".format(", ".join(report["missing"])),
                  file=sys.stderr)
            print("These are absent, not zero. Every aggregate over this week "
                  "carries how many machine-weeks it actually covers.",
                  file=sys.stderr)
        if report.get("reported_but_measuring_nothing"):
            print("measuring nothing: {}".format(
                ", ".join(report["reported_but_measuring_nothing"])),
                file=sys.stderr)
            print("These uploaded, and their machines have no repository, no "
                  "Copilot exporter and no agent emitter configured. Their "
                  "zeros are not measured zeros -- chase the setup rather than "
                  "reading them as a quiet week.", file=sys.stderr)
        if report["unexpected"]:
            print("not on the roster: {}".format(
                ", ".join(report["unexpected"])), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
