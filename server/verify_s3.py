#!/usr/bin/env python3
"""Prove the bucket behaves the way the endpoint assumes, before trusting it.

    python3 server/verify_s3.py --store s3://aeris-insight

Everything in ``server/tests`` runs against a stub client. That checks this code
asks S3 for the right things; it cannot check that S3 -- this bucket, this
region, this IAM role -- answers the way the design needs. Those are different
questions, and only one of them can be answered from a laptop with no
credentials.

The one that matters is **write-once**. ``docs/TRANSPORT.md`` gets idempotency
for free from ``IfNoneMatch: "*"``: object keys are content digests, so a
re-upload is refused by the storage layer and the client reports it as *already
handed over*. If conditional writes are not honoured here, that guarantee is
gone and nothing else notices -- the object still parses, still checksums, and is
simply the wrong copy. So this asserts the refusal rather than assuming it.

Run it once when the credentials land, and again after any bucket policy change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import store as store_mod  # noqa: E402

#: A fixed key, deliberately. A per-run key would leave one object behind on
#: every check -- and the role is not granted DeleteObject, so nothing could
#: clean them up. One object, written once, reused by every later run.
PROBE_KEY = "_preflight/probe.txt"
PROBE_BODY = (
    b"usage-insight preflight probe.\n"
    b"Written by server/verify_s3.py to prove conditional writes work.\n"
    b"Safe to delete. Not telemetry, and not counted by any report.\n"
)


class Check:
    def __init__(self, name: str, detail: str = "", ok: bool = True,
                 advisory: bool = False):
        self.name, self.detail, self.ok, self.advisory = name, detail, ok, advisory

    def line(self) -> str:
        mark = "  ok  " if self.ok else ("  --  " if self.advisory else " FAIL ")
        return "[{}] {:<34} {}".format(mark, self.name, self.detail)


def verify(store: Any, bucket: str) -> List[Check]:
    checks: List[Check] = []

    # 1. Can we see the bucket at all? Distinguishes bad credentials from a bad
    #    bucket name, which otherwise look identical at the next step.
    try:
        store.list("_preflight/")
        checks.append(Check("ListBucket", "the role can list " + bucket))
    except store_mod.StoreError as exc:
        checks.append(Check("ListBucket", str(exc), ok=False))
        return checks          # nothing after this can mean anything

    # 2. The probe exists, or gets written. Both are a pass: the first run
    #    writes it and every later run finds it.
    try:
        store.put(PROBE_KEY, PROBE_BODY)
        checks.append(Check("PutObject", "probe written"))
    except store_mod.Exists:
        checks.append(Check("PutObject", "probe already present from an earlier run"))
    except store_mod.StoreError as exc:
        checks.append(Check("PutObject", str(exc), ok=False))
        return checks

    # 3. THE check. A second write of the same key must be refused.
    try:
        store.put(PROBE_KEY, b"this must never land")
        checks.append(Check(
            "write-once (IfNoneMatch)",
            "OVERWROTE an existing key -- conditional writes are not being "
            "honoured, and re-uploads will silently replace bundles", ok=False))
    except store_mod.Exists:
        how = "conditionally" if getattr(store, "_conditional", True) else \
              "via check-then-write fallback (racy; store lacks IfNoneMatch)"
        checks.append(Check("write-once (IfNoneMatch)", "refused " + how))
    except store_mod.StoreError as exc:
        checks.append(Check("write-once (IfNoneMatch)", str(exc), ok=False))

    # 4. What came back is what went in. A bundle's checksum was computed on a
    #    laptop a week earlier; one byte changed here breaks it at import.
    try:
        got = store.get(PROBE_KEY)
        if got == PROBE_BODY:
            checks.append(Check("GetObject", "byte-for-byte"))
        else:
            checks.append(Check(
                "GetObject",
                "content differs: wrote {} bytes, read {}".format(
                    len(PROBE_BODY), len(got)), ok=False))
    except (KeyError, store_mod.StoreError) as exc:
        checks.append(Check("GetObject", "{}: {}".format(type(exc).__name__, exc),
                            ok=False))

    # 5. The listing is what pull.py walks. A key that lands but does not list
    #    is a bundle nobody ever fetches.
    try:
        listed = [o["key"] for o in store.list("_preflight/")]
        if PROBE_KEY in listed:
            checks.append(Check("listing round-trip", "the probe lists under its key"))
        else:
            checks.append(Check(
                "listing round-trip",
                "wrote {} but the listing shows {}".format(PROBE_KEY, listed or "nothing"),
                ok=False))
    except store_mod.StoreError as exc:
        checks.append(Check("listing round-trip", str(exc), ok=False))

    return checks


def check_least_privilege(store: Any, bucket: str) -> List[Check]:
    """Advisory: the role should not be able to delete, and the bucket should
    not be public. Neither is required for the endpoint to work, which is why
    a failure here does not fail the run -- but both are worth knowing."""
    checks: List[Check] = []
    client = getattr(store, "client", None)
    if client is None:
        return checks

    try:
        from botocore.exceptions import ClientError
    except ImportError:                                # pragma: no cover
        return checks

    # DeleteObject must be refused. Retention is a lifecycle rule; deletion is
    # not a code path anywhere in this service, so the role should not have it.
    try:
        client.delete_object(Bucket=bucket, Key="_preflight/never-created")
        checks.append(Check(
            "DeleteObject is not granted",
            "the role CAN delete. Retention is a lifecycle rule and no code "
            "here deletes -- consider removing s3:DeleteObject",
            ok=False, advisory=True))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("AccessDenied", "AllAccessDisabled"):
            checks.append(Check("DeleteObject is not granted", "refused, as intended"))
        else:
            checks.append(Check("DeleteObject is not granted",
                                "inconclusive ({})".format(code), advisory=True))

    try:
        block = client.get_public_access_block(Bucket=bucket)
        config = block["PublicAccessBlockConfiguration"]
        if all(config.get(k) for k in ("BlockPublicAcls", "IgnorePublicAcls",
                                       "BlockPublicPolicy", "RestrictPublicBuckets")):
            checks.append(Check("public access is blocked", "all four settings on"))
        else:
            off = [k for k in ("BlockPublicAcls", "IgnorePublicAcls",
                               "BlockPublicPolicy", "RestrictPublicBuckets")
                   if not config.get(k)]
            checks.append(Check("public access is blocked",
                                "NOT fully blocked: " + ", ".join(off),
                                ok=False, advisory=True))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        checks.append(Check("public access is blocked",
                            "could not check ({})".format(code), advisory=True))
    return checks


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prove the bucket honours what the endpoint assumes.")
    parser.add_argument("--store", default=os.environ.get("INSIGHT_STORE"),
                        help="s3://bucket (default: $INSIGHT_STORE)")
    parser.add_argument("--json", action="store_true", help="machine-readable")
    args = parser.parse_args(argv)

    if not args.store:
        raise SystemExit("no --store -- pass s3://aeris-insight or set INSIGHT_STORE")

    try:
        store = store_mod.open_store(args.store)
    except store_mod.StoreError as exc:
        raise SystemExit(str(exc))

    bucket = getattr(store, "bucket", args.store)
    checks = verify(store, bucket)
    if checks and checks[-1].ok:
        checks += check_least_privilege(store, bucket)

    failed = [c for c in checks if not c.ok and not c.advisory]
    warned = [c for c in checks if not c.ok and c.advisory]

    if args.json:
        print(json.dumps({
            "store": args.store, "bucket": bucket,
            "checks": [{"name": c.name, "ok": c.ok, "advisory": c.advisory,
                        "detail": c.detail} for c in checks],
            "passed": not failed,
        }, indent=2, sort_keys=True))
    else:
        print()
        for check in checks:
            print(check.line())
        print()
        if failed:
            print("{} check(s) failed. Do not point the endpoint at this bucket "
                  "yet.".format(len(failed)))
        elif warned:
            print("Usable. {} advisory note(s) above -- neither blocks the "
                  "endpoint.".format(len(warned)))
        else:
            print("Bucket is ready. Set INSIGHT_STORE={} and start the "
                  "endpoint.".format(args.store))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
