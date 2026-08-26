#!/usr/bin/env python3
"""Hand a sealed bundle over the wire instead of by hand.

``pack`` seals a bundle; this carries it. Those stay two commands, and the
separation is load-bearing rather than tidy: the consent model in
``docs/ARCHITECTURE.md`` rests on the engineer being able to *read exactly what
was recorded before deciding to hand it over*. Uploading automatically at pack
time would delete that property without ever mentioning it.

One ``PUT`` to a proxy that owns the S3 credentials -- no SigV4, no SDK, no
OAuth, standard library only. The wire contract is ``docs/TRANSPORT.md``; this
is its client half.

Nothing here touches the bundle. A file that arrives is byte-for-byte the file
that was sealed, which is what lets ``importers/bundle.py`` verify a checksum
computed on another machine a week earlier.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_POLLERS = _ROOT
if _POLLERS not in sys.path:
    sys.path.insert(0, _POLLERS)

import common  # noqa: E402  -- the shared trust store; see common.ssl_context

CLIENT_VERSION = "insight-ship/1"

#: Sent as ``User-Agent``. Not cosmetic: urllib's default is
#: ``Python-urllib/3.x``, and Cloudflare's browser integrity check answers that
#: with ``403 error code: 1010`` -- before the request reaches the endpoint, so
#: no credential or header of ours can help. Found the first time a bundle was
#: shipped at the real hostname. Naming the client is also what makes an upload
#: identifiable in an access log.
USER_AGENT = "seta-insight/1 (+usage-insight)"

#: Bundles are KBs to a couple of MB (``docs/TRANSPORT.md``, *Sizing*). A file
#: an order of magnitude past that is a bug or a mistake, and the useful place
#: to find that out is here -- before spending an engineer's upstream on it --
#: rather than in a proxy's ``413``.
MAX_BYTES = 1 * 1024 * 1024

#: Only 5xx and transport errors are retried. A 4xx means the bundle is wrong,
#: and sending a wrong bundle three times makes it no less wrong.
RETRIES = 3


class ShipError(Exception):
    """The bundle was not stored. Never recorded as shipped."""


# --------------------------------------------------------------------------
# reading what pack sealed
# --------------------------------------------------------------------------

def read_manifest(path: str) -> Dict[str, Any]:
    """The manifest line, or raise.

    Read rather than trusted: ``ship`` is handed a path, and a path can point at
    anything. The window and machine id in the headers have to come from the
    file being sent, not from the config of the machine sending it, or a bundle
    copied between machines would be filed under the wrong one.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            first = handle.readline()
    except OSError as exc:
        raise ShipError("cannot read {}: {}".format(path, exc))

    if not first.strip():
        raise ShipError("{} is empty".format(os.path.basename(path)))
    try:
        manifest = json.loads(first).get("_manifest")
    except json.JSONDecodeError as exc:
        raise ShipError("{}: first line is not a manifest: {}".format(
            os.path.basename(path), exc))
    if not isinstance(manifest, dict):
        raise ShipError("{}: first line carries no _manifest object".format(
            os.path.basename(path)))
    return manifest


def window_of(manifest: Dict[str, Any]) -> str:
    """``YYYY-MM-DD/YYYY-MM-DD``, or raise if the bundle never declared one.

    An undeclared window is refused rather than guessed. The proxy files a
    bundle under the week its window starts, and downstream every aggregate is
    weighed by how many machine-weeks it actually covers -- so a bundle with no
    window is not a bundle with a slightly worse window, it is one that quietly
    lands in the wrong week and makes two weeks wrong at once.

    ``pack`` only leaves this empty when the buffer was empty *and* no
    ``--since``/``--until`` was given, which is precisely the case where only
    the person packing knows which week they meant.
    """
    start, end = manifest.get("window_start"), manifest.get("window_end")
    if not start or not end:
        raise ShipError(
            "this bundle declares no window, so there is no week to file it "
            "under -- re-pack it with `pack --since YYYY-MM-DD --until "
            "YYYY-MM-DD`, which records the week you meant even when it was "
            "quiet")
    return "{}/{}".format(str(start)[:10], str(end)[:10])


def digest_of(path: str) -> Tuple[bytes, str]:
    """The whole file and its SHA-256.

    Deliberately the entire file, manifest line included -- unlike the
    manifest's own ``sha256``, which covers the event lines only. One catches a
    truncated *upload*, the other a truncated *bundle*; they are checked by
    different processes at different times, and collapsing them into one number
    would lose the ability to tell those two failures apart.
    """
    with open(path, "rb") as handle:
        body = handle.read()
    return body, hashlib.sha256(body).hexdigest()


# --------------------------------------------------------------------------
# the wire
# --------------------------------------------------------------------------

def _post(url: str, body: bytes, headers: Dict[str, str],
          timeout: int) -> Tuple[int, Dict[str, Any]]:
    request = urllib.request.Request(url, data=body, headers=headers, method="PUT")
    context = common.ssl_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            payload = response.read().decode("utf-8", "replace")
            return response.status, _json_or_empty(payload)
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", "replace")
        return exc.code, _json_or_empty(payload)


def _json_or_empty(payload: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return {"body": payload[:200]}
    return parsed if isinstance(parsed, dict) else {"body": payload[:200]}


def ship_bundle(path: str, endpoint: str, token: Optional[str] = None,
                timeout: int = 60, retries: int = RETRIES,
                post=_post, previous_token: Optional[str] = None) -> Dict[str, Any]:
    """Send one bundle. Returns the receipt, or raises ``ShipError``.

    ``409`` is a success. The proxy keys objects by content digest and writes
    with ``IfNoneMatch``, so a second send of the same file is the storage layer
    telling us the week is already there -- which is the answer we wanted. Anyone
    who is unsure whether last week went through will run this again, and it
    must be safe when they do.

    ``previous_token`` covers a rotation in flight. After ``rotate-token`` the
    new fingerprint is not in the server's ``.env`` until a second person edits
    it, so the new secret is tried first and the old one is used if it comes
    back ``401``. Rotation therefore needs no coordination and no downtime,
    which is the only reason anyone rotates a credential twice.
    """
    manifest = read_manifest(path)
    window = window_of(manifest)
    body, digest = digest_of(path)

    if not body:
        raise ShipError("{} is empty".format(os.path.basename(path)))
    if len(body) > MAX_BYTES:
        raise ShipError(
            "{} is {:.1f} MB, over the {:.0f} MB cap -- a bundle this size is "
            "a fault worth reading before sending".format(
                os.path.basename(path), len(body) / 1048576.0,
                MAX_BYTES / 1048576.0))

    headers = {
        "Content-Type": "application/x-ndjson",
        "X-Insight-Machine": str(manifest.get("machine_id") or ""),
        "X-Insight-Window": window,
        "X-Insight-Schema": str(manifest.get("schema_version") or ""),
        "X-Insight-Format": str(manifest.get("format") or ""),
        "X-Insight-Digest": "sha256=" + digest,
        "X-Insight-Client": CLIENT_VERSION,
        "User-Agent": USER_AGENT,
    }
    url = endpoint.rstrip("/") + "/v1/bundle"

    # Newest first, and the old one only if the new one is not known yet. Each
    # gets its own header dict -- one shared dict would leave the caller holding
    # a record of a request that says it carried a credential it did not.
    candidates = [t for t in (token, previous_token) if t] or [None]
    for candidate in candidates:
        attempt_headers = dict(headers)
        if candidate:
            attempt_headers["Authorization"] = "Bearer " + candidate
        try:
            return _attempt(url, body, attempt_headers, timeout, retries, post,
                            manifest, window, digest, path)
        except _Unauthorised:
            continue
    raise ShipError(
        "{} rejected {} credential this machine holds. The address is probably "
        "not on the server whitelist yet, or a token was rotated and the new "
        "fingerprint has not been added -- `insight whoami` prints the line to "
        "send".format(url, "every" if len(candidates) > 1 else "the"))


class _Unauthorised(Exception):
    """A 401 with another credential still worth trying."""


def _attempt(url, body, headers, timeout, retries, post, manifest, window,
             digest, path) -> Dict[str, Any]:
    last = ""
    for attempt in range(1, retries + 1):
        try:
            status, payload = post(url, body, headers, timeout)
        except (urllib.error.URLError, OSError) as exc:
            if common.is_certificate_error(exc):
                # Permanent, like a 403: the certificate will not verify on the
                # next attempt either, and three tries only delay the message
                # that says what to do.
                raise ShipError(
                    "{} presented a certificate this machine cannot verify -- "
                    "{}. Python's trust store is empty, which is the default "
                    "on a python.org install for macOS: run "
                    "\"/Applications/Python 3.x/Install Certificates.command\" "
                    "or point SSL_CERT_FILE at a CA bundle. Nothing was "
                    "uploaded.".format(url, exc))
            last = "{}: {}".format(type(exc).__name__, exc)
            if attempt == retries:
                raise ShipError("{} unreachable after {} attempts -- {}".format(
                    url, retries, last))
            continue

        if status in (200, 201, 409):
            return {
                "file": os.path.basename(path),
                "status": status,
                "already_stored": status == 409,
                "key": payload.get("key"),
                "sha256": payload.get("sha256") or digest,
                # The manifest's own checksum, over the events only. Unlike the
                # transport digest it does not move when nothing but `packed_at`
                # changed, which is what lets an hourly run tell a genuinely
                # quiet hour from the same events packed again.
                "content_sha256": manifest.get("sha256"),
                "bytes": payload.get("bytes") or len(body),
                "machine_id": manifest.get("machine_id"),
                "window": window,
                "shipped_at": _now(),
            }

        detail = payload.get("error") or payload.get("body") or ""

        # Who actually answered? The endpoint replies in JSON with an "error"
        # key; a CDN or WAF in front of it replies with its own page. Told
        # apart because they mean opposite things: a 403 from the endpoint is
        # "you are not on the whitelist", and a 403 from Cloudflare is "the
        # bundle never reached the endpoint" -- and reading the second as the
        # first sends someone to edit a whitelist that was never wrong.
        # Found in deployment: Cloudflare answered `error code: 1010` to a
        # perfectly valid upload, and this said the address had been removed.
        answered_by_endpoint = "error" in payload

        # 401 is "I do not know this secret", so another one is worth trying.
        # 403 is "I know you and you may not upload" -- a second secret for the
        # same person cannot change that, and trying one looks like stuffing.
        if not answered_by_endpoint and 400 <= status < 500:
            raise ShipError(
                "{} did not reach the endpoint: HTTP {} came from something in "
                "front of it -- a CDN, WAF or reverse proxy{}. The bundle is "
                "untouched and nothing on this machine needs changing; the "
                "endpoint's own refusals arrive as JSON.".format(
                    url, status, " -- " + detail if detail else ""))
        if status == 401:
            raise _Unauthorised()
        if status == 403:
            raise ShipError(
                "{} knows this credential and refused it (HTTP 403){}. The "
                "address has been removed from the server whitelist rather "
                "than mistyped".format(url, " -- " + detail if detail else ""))
        if status == 400 and "schema_version" in (detail or ""):
            # The one 400 that is not about this machine. A client upgraded
            # ahead of the server emits a version the server has never been
            # taught, and the bare message reads as a corrupt bundle -- so
            # somebody re-packs, re-ships, and gets the same 400 again.
            #
            # Seen for real on 2026-08-26, the day the contract went to 1.1.0:
            # the deployed proxy still listed {"1.0.0"} and every upload from
            # an upgraded client failed for an hour before anybody looked. The
            # ordering rule this states is the fix: a server learns a version
            # before a client sends it, never the other way round.
            raise ShipError(
                "{} does not know this bundle's schema version{}.\n"
                "\n"
                "Nothing here is wrong and nothing was lost -- the bundle is "
                "still on disk and `ship` will send it once the server "
                "accepts. This client is newer than the collection server: "
                "whoever runs it needs to deploy a build that lists this "
                "version in KNOWN_SCHEMAS. Send them this line.".format(
                    url, " -- " + detail if detail else ""))
        if 400 <= status < 500:
            # The proxy read it and refused it. Retrying changes nothing, and
            # the message is the only thing that will.
            raise ShipError("{} rejected the bundle: HTTP {}{}".format(
                url, status, " -- " + detail if detail else ""))
        last = "HTTP {}{}".format(status, " -- " + detail if detail else "")
        if attempt == retries:
            raise ShipError("{} failed after {} attempts -- {}".format(
                url, retries, last))

    raise ShipError("unreachable")  # pragma: no cover


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# what has already gone
# --------------------------------------------------------------------------

def load_receipts(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError):
        # A damaged receipt file must not stop an engineer shipping. The worst
        # case is re-sending a bundle the proxy already has, which it answers
        # with 409 -- so the safe failure here is to forget, not to refuse.
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_receipts(path: str, receipts: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(receipts, handle, indent=2, sort_keys=True)
        handle.write("\n")


def unshipped(reports_dir: str, receipts: Dict[str, Any]) -> List[str]:
    """Bundles in ``.reports/`` with no receipt, oldest first.

    Keyed by file name rather than digest so this stays cheap -- naming a bundle
    is ``pack``'s job and it never reuses one. The digest is what the proxy
    keys on, and that check happens there.
    """
    if not os.path.isdir(reports_dir):
        return []
    return [os.path.join(reports_dir, name)
            for name in sorted(os.listdir(reports_dir))
            if name.endswith(".ndjson") and name not in receipts]
