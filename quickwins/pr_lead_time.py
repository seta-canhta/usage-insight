#!/usr/bin/env python3
"""pr_lead_time.py -- merge lead time for merged Bitbucket PRs, split by AI marker.

Design reference: docs/spikes/ai-effectiveness-observability.md
  Sec 14.1 item 2  (measure immediately, no new infrastructure)
  Sec 8.14         (PR review and merge lead time)
  Sec 2.3          (the "[Authored By Copilot]" PR-title marker)

Answers one question: "how long do merged PRs take from open to merge, and does that
differ for PRs whose title carries the Copilot marker?"

STRICTLY READ-ONLY. Issues only HTTP GET. Never creates, approves, merges, comments
on, or declines anything. Credentials come from the environment and are never printed,
logged, or written to any output file.

Credentials (same variables the bitbucket-ops skill already uses):
    BITBUCKET_USERNAME
    BITBUCKET_ACCESS_TOKEN

Usage:
    export BITBUCKET_USERNAME=... BITBUCKET_ACCESS_TOKEN=...
    ./pr_lead_time.py --repo myworkspace/my-repo
    ./pr_lead_time.py --repo ws/a --repo ws/b --since 2026-01-01 --json out.json
    ./pr_lead_time.py --repo ws/a --no-diffstat        # skip per-PR diffstat calls
    ./pr_lead_time.py --repo ws/a --exact-merge        # merged_at from /activity

Stdlib only -- no pip install, so it runs on a laptop with nothing set up.

READ THE CAVEATS AT THE BOTTOM OF THE OUTPUT BEFORE QUOTING ANY NUMBER.
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.bitbucket.org/2.0"
AI_MARKER = "[Authored By Copilot]"
USER_AGENT = "aiep-quickwins-pr-lead-time/1.0 (read-only)"


# --------------------------------------------------------------------------- io

def _fail(msg, code=1):
    print("error: %s" % msg, file=sys.stderr)
    sys.exit(code)


def _auth_header():
    user = os.environ.get("BITBUCKET_USERNAME", "").strip()
    token = os.environ.get("BITBUCKET_ACCESS_TOKEN", "").strip()
    if not user or not token:
        _fail(
            "BITBUCKET_USERNAME and BITBUCKET_ACCESS_TOKEN must both be set.\n"
            "       This script reads them from the environment and never prints them."
        )
    raw = ("%s:%s" % (user, token)).encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _get(url, auth, timeout=30):
    """One authenticated GET. Returns parsed JSON, or None on a handled failure.

    Error text is deliberately reduced to a status code: a Bitbucket error body can
    echo back request context, and this tool must never risk emitting a credential.
    """
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", auth)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        hint = {
            401: "bad or expired credentials",
            403: "token lacks repository read scope",
            404: "workspace/repo not found, or no access",
            429: "rate limited -- retry later",
        }.get(e.code, "")
        print("  warn: HTTP %d %s for %s" % (e.code, hint, _redact(url)), file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print("  warn: network error (%s) for %s" % (e.reason, _redact(url)), file=sys.stderr)
        return None


def _redact(url):
    """Strip any query string before a URL reaches stderr."""
    return url.split("?", 1)[0]


def _paged(url, auth, cap=1000):
    """Follow Bitbucket's `next` cursor, yielding values, up to `cap` records."""
    seen = 0
    while url and seen < cap:
        page = _get(url, auth)
        if not page:
            return
        for v in page.get("values", []):
            yield v
            seen += 1
            if seen >= cap:
                return
        url = page.get("next")


# ------------------------------------------------------------------------ stats

def _parse_ts(s):
    if not s:
        return None
    s = s.strip()
    # Bitbucket emits e.g. 2026-04-11T09:22:13.918233+00:00
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _percentile(sorted_vals, pct):
    """Linear-interpolated percentile. `pct` in 0..1. Empty list -> None."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _fmt_hours(h):
    if h is None:
        return "     n/a"
    if h < 48:
        return "%6.1f h" % h
    return "%6.1f d" % (h / 24.0)


def _summarise(rows):
    """rows: list of dicts with lead_hours and changed_lines."""
    lead = sorted(r["lead_hours"] for r in rows)
    out = {
        "count": len(rows),
        "median_hours": _percentile(lead, 0.50),
        "p85_hours": _percentile(lead, 0.85),
    }
    sized = sorted(
        (r["lead_hours"] / (r["changed_lines"] / 100.0))
        for r in rows
        if r.get("changed_lines")
    )
    out["sized_count"] = len(sized)
    out["median_hours_per_100_lines"] = _percentile(sized, 0.50)
    out["p85_hours_per_100_lines"] = _percentile(sized, 0.85)
    lines = sorted(r["changed_lines"] for r in rows if r.get("changed_lines"))
    out["median_changed_lines"] = _percentile(lines, 0.50)
    return out


# ------------------------------------------------------------------------- main

def collect_repo(repo, auth, since, cap, want_diffstat, exact_merge):
    ws_repo = repo.strip("/")
    if ws_repo.count("/") != 1:
        _fail("--repo must be workspace/repo_slug, got: %s" % repo)

    q = "state=\"MERGED\""
    if since:
        q += ' AND updated_on >= %s' % json.dumps(since)
    url = "%s/repositories/%s/pullrequests?%s" % (
        API,
        ws_repo,
        urllib.parse.urlencode({"q": q, "pagelen": 50, "state": "MERGED"}),
    )

    rows = []
    for pr in _paged(url, auth, cap=cap):
        created = _parse_ts(pr.get("created_on"))
        merged = _parse_ts(pr.get("updated_on"))
        merged_src = "updated_on (proxy)"

        if exact_merge:
            act_url = "%s/repositories/%s/pullrequests/%s/activity" % (API, ws_repo, pr["id"])
            for act in _paged(act_url, auth, cap=200):
                if "update" in act and act["update"].get("state") == "MERGED":
                    t = _parse_ts(act["update"].get("date"))
                    if t:
                        merged, merged_src = t, "activity"
                    break

        if not created or not merged or merged < created:
            continue

        changed = None
        if want_diffstat:
            ds_url = "%s/repositories/%s/pullrequests/%s/diffstat?pagelen=100" % (
                API, ws_repo, pr["id"])
            total = 0
            got = False
            for f in _paged(ds_url, auth, cap=500):
                total += int(f.get("lines_added") or 0) + int(f.get("lines_removed") or 0)
                got = True
            changed = total if got else None

        title = pr.get("title") or ""
        rows.append({
            "repo": ws_repo,
            "id": pr.get("id"),
            "ai": AI_MARKER.lower() in title.lower(),
            "lead_hours": (merged - created).total_seconds() / 3600.0,
            "changed_lines": changed,
            "created_on": created.isoformat(),
            "merged_at": merged.isoformat(),
            "merged_at_source": merged_src,
        })
    return rows


def main():
    ap = argparse.ArgumentParser(
        description="Merged-PR lead time, split by the [Authored By Copilot] title marker. Read-only.")
    ap.add_argument("--repo", action="append", required=True,
                    help="workspace/repo_slug (repeatable)")
    ap.add_argument("--since", default=None,
                    help="ISO date, e.g. 2026-01-01 -- filters on updated_on")
    ap.add_argument("--max-prs", type=int, default=500,
                    help="cap per repo (default 500); each PR costs 1-2 extra API calls")
    ap.add_argument("--no-diffstat", action="store_true",
                    help="skip per-PR diffstat; disables the per-100-lines normalisation")
    ap.add_argument("--exact-merge", action="store_true",
                    help="derive merged_at from /activity instead of updated_on (slower, exact)")
    ap.add_argument("--json", metavar="PATH", default=None,
                    help="also write the per-PR rows and summary to a JSON file")
    args = ap.parse_args()

    auth = _auth_header()

    rows = []
    for repo in args.repo:
        print("scanning %s ..." % repo, file=sys.stderr)
        rows.extend(collect_repo(repo, auth, args.since, args.max_prs,
                                 not args.no_diffstat, args.exact_merge))

    if not rows:
        print("No merged PRs found (or no access). Nothing to report.")
        return 2

    ai = [r for r in rows if r["ai"]]
    human = [r for r in rows if not r["ai"]]
    groups = [("AI-marked  [Authored By Copilot]", ai),
              ("Not marked", human),
              ("ALL", rows)]

    print()
    print("Merged PR lead time  (created_on -> merged)")
    print("  repos : %s" % ", ".join(args.repo))
    print("  since : %s" % (args.since or "all history"))
    print("  merged_at source : %s" % ("activity" if args.exact_merge else "updated_on (proxy)"))
    print()
    print("  %-34s %5s %11s %11s %11s %11s" %
          ("group", "n", "median", "p85", "med/100ln", "p85/100ln"))
    summaries = {}
    for label, g in groups:
        if not g:
            print("  %-34s %5d %11s %11s %11s %11s" % (label, 0, "-", "-", "-", "-"))
            continue
        s = _summarise(g)
        summaries[label] = s
        print("  %-34s %5d %11s %11s %11s %11s" % (
            label, s["count"],
            _fmt_hours(s["median_hours"]), _fmt_hours(s["p85_hours"]),
            _fmt_hours(s["median_hours_per_100_lines"]),
            _fmt_hours(s["p85_hours_per_100_lines"]),
        ))

    if not args.no_diffstat:
        sized = sum(1 for r in rows if r.get("changed_lines"))
        print()
        print("  PRs with a usable diffstat: %d / %d" % (sized, len(rows)))
        for label, g in groups[:2]:
            gs = [r["changed_lines"] for r in g if r.get("changed_lines")]
            if gs:
                print("    %-32s median changed lines: %d" % (
                    label, int(_percentile(sorted(gs), 0.50))))

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
                       "repos": args.repo, "since": args.since,
                       "summaries": summaries, "pull_requests": rows}, fh, indent=2)
        print("\n  wrote %s" % args.json)

    print("""
CAVEATS -- read before quoting any of the above
  1. n is small. This repository has 3 PRs carrying the marker (design sec 2.3).
     Do not report a difference between the two groups until n >= 20 per arm
     (design sec 9.1 "Guard"). Below that this table is descriptive, not evidence.
  2. This is NOT an AI-vs-human comparison. There is no non-AI control group
     (design sec 9.1 Decision 2): unmarked PRs are mostly AI-assisted work where the
     marker was simply not applied. The split is "marker present" vs "marker absent",
     nothing more.
  3. Lead time is confounded by PR size, reviewer availability, weekends, holidays,
     and release freezes. The per-100-changed-lines column controls for size only.
  4. Without --exact-merge, merged_at is approximated by updated_on, which moves on
     any late edit to the PR. Use --exact-merge for a figure anyone will act on.
  5. Merged-only. Declined and still-open PRs are invisible here, so this is a
     survivor-biased view of the process.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
