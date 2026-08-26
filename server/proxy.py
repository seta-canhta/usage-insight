#!/usr/bin/env python3
"""The collection endpoint. Deployment notes: ``docs/OPERATE.md``.

    python3 server/proxy.py --store file:///tmp/insight --allowed-file allowed.env
    python3 server/proxy.py --store s3://aeris-insight

Binds **127.0.0.1** by default. Nginx terminates TLS on 443 and exposes exactly
one route; the read routes stay on loopback and are reached over SSH. See
``server/nginx.conf.example`` and ``docs/OPERATE.md``.

Stdlib only, except boto3 for the S3 backend. Fourteen laptops uploading once an
hour is fourteen requests an hour, so ``ThreadingHTTPServer`` is not a compromise
here -- it is the right size, and it lets the process import ``cli/identity.py``
directly rather than reimplementing the whitelist rules.

That import is the point. A second implementation of
the contract on the grounds that the two drift, and the piece most worth not
drifting is the one deciding who may upload. The client mints the secret and
derives the fingerprint; this checks it with the same function.

**What this deliberately does not do:** check the attribute allow-list. That
lives in ``collector/main.py`` and is re-checked by ``importers/bundle.py`` on
import, which is where a bundle a person could have edited gets read. Copying it
here would put the one rule keeping prompts and source code out of the warehouse
in two places. A malformed bundle reaches storage and is rejected at import,
loudly and whole -- the same failure it had when bundles arrived by email.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (os.path.join(_ROOT, "cli"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import identity  # noqa: E402  -- the client's own whitelist rules, not a copy
import store as store_mod  # noqa: E402

#: Matches the client's own cap in ``cli/ship.py``. Both sides agreeing is what
#: makes a 413 mean the same thing in both places.
MAX_BYTES = 1 * 1024 * 1024

#: The one schema on the wire. Nothing emits an older one: `common.SCHEMA_VERSION`
#: is 1.1.0 and the client auto-updates, so a second entry here would be a
#: door left open for a version that no longer exists.
KNOWN_SCHEMAS = {"1.1.0"}

BUNDLE_FORMAT = "seta-insight-bundle/1"

#: How much of an over-cap body is read and thrown away so the 413 is
#: deliverable. Past this the connection is dropped instead.
DRAIN_LIMIT = 8 * 1024 * 1024

log = logging.getLogger("insight.proxy")


class Rejected(Exception):
    """A request that gets a 4xx. Carries what the client should be told."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------
# the rules
# --------------------------------------------------------------------------

def iso_week(day: str) -> str:
    try:
        year, week, _ = date.fromisoformat(day[:10]).isocalendar()
    except ValueError:
        raise Rejected(400, "window start {!r} is not a date".format(day))
    return "{}-W{:02d}".format(year, week)


def object_key(email: str, window_start: str, machine: str, digest: str) -> str:
    """Built here, from an authenticated identity -- never from client input.

    Filed under the person rather than the machine, so someone with a laptop and
    a desktop is one line of coverage instead of two. The machine id stays in the
    file name because two machines in one week is worth being able to see.
    """
    return "bundles/{}/{}/{}-{}.ndjson".format(
        iso_week(window_start), identity.person_key(email),
        (machine or "unknown")[:8], digest)


def check_upload(headers: Dict[str, str], body: bytes,
                 allowed: Dict[str, List[str]]) -> Tuple[str, str, str]:
    """Authenticate and validate. Returns ``(email, key, digest)`` or raises.

    Order matters: identity first, so an unknown caller is turned away before
    the process spends anything on their body.
    """
    secret = _bearer(headers)
    email = identity.identify(secret, allowed)
    if email is None:
        # 401, not 403. `ship` retries a 401 with its previous secret, because
        # that is exactly what a rotation in flight looks like -- the new
        # fingerprint has not reached this file yet. 403 means "known and not
        # allowed", and the client stops.
        raise Rejected(401, "not on the whitelist")

    if len(body) > MAX_BYTES:
        raise Rejected(413, "bundle is {} bytes, over the {} byte cap".format(
            len(body), MAX_BYTES))
    if not body:
        raise Rejected(400, "empty body")

    declared = (headers.get("x-insight-digest") or "").replace("sha256=", "").strip()
    actual = hashlib.sha256(body).hexdigest()
    if not declared:
        raise Rejected(400, "X-Insight-Digest is required")
    if declared.lower() != actual:
        raise Rejected(400, "digest mismatch -- the upload is truncated or altered")

    schema = (headers.get("x-insight-schema") or "").strip()
    if schema not in KNOWN_SCHEMAS:
        raise Rejected(400, "unknown schema_version {!r}".format(schema))

    fmt = (headers.get("x-insight-format") or "").strip()
    if fmt and fmt != BUNDLE_FORMAT:
        raise Rejected(400, "unknown bundle format {!r}".format(fmt))

    window = (headers.get("x-insight-window") or "").strip()
    if window.count("/") != 1:
        raise Rejected(400, "X-Insight-Window must be <start>/<end>")
    start, end = window.split("/")
    if not start or not end:
        raise Rejected(400, "X-Insight-Window declares no window")

    machine = (headers.get("x-insight-machine") or "").strip()
    return email, object_key(email, start, machine, actual), actual


def _bearer(headers: Dict[str, str]) -> str:
    value = headers.get("authorization") or ""
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value.strip()


def load_admin_token(raw: Optional[str], path: Optional[str]) -> str:
    """``INSIGHT_ADMIN_TOKEN``, or a file holding it.

    The file exists because of how this is actually deployed. In a container the
    environment is readable by anyone who can talk to the Docker daemon --
    ``docker inspect`` prints it -- and on a shared host that is a wider audience
    than root. A mounted, root-owned file is not visible that way, and it is the
    same shape as ``INSIGHT_ALLOWED_FILE`` next to it.

    Required either way. The read routes expose every engineer at once.
    """
    token = (raw or "").strip()
    if path:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                token = handle.read().strip()
        except OSError as exc:
            raise SystemExit("cannot read INSIGHT_ADMIN_TOKEN_FILE {}: {}".format(
                path, exc))
    if not token:
        raise SystemExit(
            "INSIGHT_ADMIN_TOKEN is required -- the read routes expose every "
            "engineer at once and are never left open")
    return token


def load_allowed(raw: Optional[str], path: Optional[str]) -> Dict[str, List[str]]:
    """``INSIGHT_ALLOWED`` from the environment, or a file holding the same.

    A file is offered because a whitelist grows a line per engineer, and
    fourteen of those in one environment variable is a line nobody can edit
    safely. Same format either way -- ``cli/identity.py`` parses both.
    """
    text = raw or ""
    if path:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    if not text.strip():
        raise SystemExit(
            "no whitelist -- set INSIGHT_ALLOWED or pass --allowed-file. "
            "Each engineer's `./insight whoami` prints the line to add.")
    return identity.parse_whitelist(text)


#: The install script is small; this is a sanity bound, not a policy. A file
#: that has grown past this is not the installer any more and serving it would
#: be serving whatever it has become.
MAX_INSTALL_SCRIPT = 64 * 1024


#: The three values ``install.sh`` carries, plus the schema the client of that
#: release emits. Read out of the script rather than out of a second file, and
#: that is the point -- see ``install_manifest``.
_INSTALL_FIELDS = {
    "version": re.compile(r'^VERSION="([^"]*)"$', re.M),
    "sha256": re.compile(r'^SHA256="([^"]*)"$', re.M),
    "url": re.compile(r'^URL="([^"]*)"$', re.M),
    "client_schema": re.compile(r'^SCHEMA="([^"]*)"$', re.M),
}

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def install_manifest(script: Optional[bytes]) -> Optional[bytes]:
    """``/install.json`` -- the same release facts, for a machine to read.

    **Derived from ``install.sh``, never configured separately.** A second file
    holding the version and digest is a second file that can disagree with the
    first, and the day it does, one of two things is true: laptops update to a
    version the installer does not serve, or they refuse an update that is
    genuinely available. Neither is discoverable from the outside. Parsing the
    script the endpoint is already serving costs a regex and removes the class.

    The coupling that buys is real and worth naming: this function knows the
    shape of four lines in ``server/install.sh.in``. It is checked once, at
    startup, and an unparseable script stops the process -- so the failure is
    an operator reading a log line at deploy time, not an engineer discovering
    six weeks later that nothing has updated.

    Refused rather than served on anything that does not look like a rendered
    template, which is also how an un-substituted ``@VERSION@`` gets caught.
    """
    if not script:
        return None
    text = script.decode("utf-8", "replace")
    found: Dict[str, str] = {}
    for name, pattern in _INSTALL_FIELDS.items():
        match = pattern.search(text)
        if not match:
            raise SystemExit(
                "the install script defines no {} -- it is not a rendered "
                "server/install.sh.in".format(name.upper()))
        found[name] = match.group(1)

    if not _SEMVER_RE.match(found["version"]):
        raise SystemExit(
            "the install script's VERSION is {!r}, which is not a version -- "
            "an unrendered template was deployed".format(found["version"]))
    if not _SHA256_RE.match(found["sha256"]):
        raise SystemExit(
            "the install script's SHA256 is not a sha256 -- an unrendered "
            "template was deployed")
    if not found["url"].lower().startswith("https://"):
        raise SystemExit("the install script's URL is not https")

    return json.dumps({
        "version": found["version"],
        "sha256": found["sha256"],
        "url": found["url"],
        "client_schema": found["client_schema"],
        # What this endpoint will actually accept. A client checks its
        # candidate against this and stays behind rather than upgrading itself
        # into 400-ing every upload -- see cli/update.py, `plan`.
        "schemas": sorted(KNOWN_SCHEMAS),
    }, sort_keys=True).encode("utf-8")


def load_install_script(path: Optional[str]) -> Optional[bytes]:
    """Read ``install.sh`` once, at startup, or return None to serve 404.

    Read once and held in memory, and that is the whole security property of
    the route it feeds -- see ``Handler.do_GET``. This is also the only place
    the file name is ever taken from anything but the command line.
    """
    if not path:
        return None
    try:
        with open(path, "rb") as handle:
            body = handle.read(MAX_INSTALL_SCRIPT + 1)
    except OSError as exc:
        raise SystemExit("cannot read --install-script {}: {}".format(path, exc))
    if not body.strip():
        raise SystemExit("--install-script {} is empty".format(path))
    if len(body) > MAX_INSTALL_SCRIPT:
        raise SystemExit(
            "--install-script {} is over {} bytes -- that is not the installer"
            .format(path, MAX_INSTALL_SCRIPT))
    return body


# --------------------------------------------------------------------------
# the server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "insight-proxy/1"
    protocol_version = "HTTP/1.1"

    # Injected by serve()
    store: Any = None
    allowed: Dict[str, List[str]] = {}
    admin_token: str = ""
    by_person_key: Dict[str, str] = {}
    #: False on a listener that faces the internet. See ``serve_upload_only``.
    read_routes: bool = True
    #: ``install.sh``, read at startup. None means the route 404s.
    install_script: Optional[bytes] = None
    #: ``/install.json``, derived from it at startup. Same fixed-bytes rule.
    install_manifest: Optional[bytes] = None

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        # Never the default stderr line: it carries the full path, and on the
        # read routes the path is an object key. Structured, and without secrets.
        log.info("%s", fmt % args)

    def _send(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str,
                    cache_control: Optional[str] = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cache_control:
            # Opt-in, never a default. The other caller here is a bundle: one
            # engineer's telemetry, admin-authenticated, and the last thing
            # that should acquire a cache header by inheritance.
            self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(body)

    def _headers(self) -> Dict[str, str]:
        return {k.lower(): v for k, v in self.headers.items()}

    def _read_body(self) -> bytes:
        """Read the body, bounded, and never more than one byte past the cap.

        The bound matters more than the early exit. Refusing an oversized body
        without draining it desyncs a keep-alive connection: the client is still
        writing while this end has moved on to the next request, and what it
        sees is a broken pipe rather than the ``413`` explaining why. So an
        oversized request is answered *and* the connection is closed, and the
        genuinely large ones never arrive at all because nginx caps the body at
        the same 1 MiB one hop earlier.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise Rejected(400, "Content-Length is not a number")
        if length < 0:
            raise Rejected(400, "negative Content-Length")
        if length > MAX_BYTES:
            self._drain(length)
            raise Rejected(413, "Content-Length {} is over the {} byte cap".format(
                length, MAX_BYTES))
        return self.rfile.read(length) if length else b""

    def _drain(self, length: int) -> None:
        """Read and discard a rejected body, so the 413 can actually be read.

        Answering before the client has finished writing resets the connection,
        and what the client sees is a broken pipe instead of the reason. Draining
        is bounded and discarded a chunk at a time, so nothing oversized is ever
        held in memory -- and beyond the ceiling the connection is simply closed,
        because at that size the sender is not something worth explaining to.

        Both hops in front of this already cap at 1 MiB: `cli/ship.py` refuses
        to send a larger bundle and nginx refuses to forward one. This path only
        runs for a client that is neither.
        """
        self.close_connection = True
        if length > DRAIN_LIMIT:
            return
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                return
            remaining -= len(chunk)

    def _require_admin(self) -> None:
        import secrets as _secrets
        given = _bearer(self._headers())
        if not self.admin_token or not _secrets.compare_digest(given, self.admin_token):
            # Reading exposes every engineer at once, which is why this route is
            # authenticated even though uploading is open to any whitelisted
            # address. The asymmetry is the design.
            raise Rejected(401, "admin token required")

    # -- routes -----------------------------------------------------------

    def do_PUT(self) -> None:      # noqa: N802 -- BaseHTTPRequestHandler's naming
        try:
            if urlparse(self.path).path != "/v1/bundle":
                raise Rejected(404, "no such route")
            body = self._read_body()
            email, key, digest = check_upload(self._headers(), body, self.allowed)

            result = {"key": key, "sha256": digest, "bytes": len(body)}
            try:
                self.store.put(key, body, metadata={
                    "machine": (self._headers().get("x-insight-machine") or "")[:8],
                    "window": self._headers().get("x-insight-window") or "",
                    "received-at": _now(),
                })
            except store_mod.Exists:
                # The key is the content digest, so this is the same bundle
                # arriving twice. The client reports it as already handed over.
                log.info(json.dumps({"event": "duplicate", "person": email,
                                     "key": key}, sort_keys=True))
                self._send(409, result)
                return
            log.info(json.dumps({"event": "stored", "person": email, "key": key,
                                 "bytes": len(body)}, sort_keys=True))
            self._send(201, result)
        except Rejected as exc:
            self._reject(exc)
        except store_mod.StoreError as exc:
            # 5xx, so `ship` retries. The bundle is still on the laptop.
            log.error(json.dumps({"event": "store_error", "detail": str(exc)}))
            self._send(503, {"error": "storage unavailable"})
        except Exception:                              # noqa: BLE001
            log.exception("unhandled error on PUT")
            self._send(500, {"error": "internal error"})

    def do_GET(self) -> None:      # noqa: N802
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                # `schemas` is here because a client deciding whether to update
                # itself needs to know what this endpoint accepts, and because
                # "what versions of the contract does this speak" is a fact
                # about a service that belongs in its health check. It is not a
                # disclosure: KNOWN_SCHEMAS is protocol, published in
                # docs/OPERATE.md, and worth nothing to anyone who cannot
                # already authenticate.
                self._send(200, {"ok": True, "people": len(self.allowed),
                                 "schemas": sorted(KNOWN_SCHEMAS)})
                return

            if parsed.path == "/install.json":
                # The machine-readable half of the same fixed byte string, and
                # subject to every word of the comment below: derived once at
                # startup, no path parameter, no filesystem access per request.
                # `cli/update.py` fetches this daily to decide whether to
                # upgrade itself, so the digest it will insist on arrives here,
                # from the endpoint over TLS, while the archive comes from
                # GitHub -- the same two origins the installer relies on.
                if not self.install_manifest:
                    raise Rejected(404, "no such route")
                self._send_bytes(self.install_manifest,
                                 "application/json; charset=utf-8",
                                 "public, max-age=300")
                return

            if parsed.path in ("/install", "/install.sh"):
                # The first unauthenticated public route that returns anything
                # an attacker might want, so it is worth writing down what they
                # get and what stops them getting more.
                #
                # What they get: the script's text. The endpoint hostname --
                # already public in DNS and hard-coded as SETA_ENDPOINT in
                # cli/insight.py. The release version, the artifact URL, the
                # expected sha256, and the paths under ~/.local this installs
                # to. No secret, and nothing that is not on the tin.
                #
                # What makes that safe is one property: the response is a fixed
                # byte string held in memory. There is no path parameter, no
                # filesystem access per request, and therefore no input on this
                # route that can influence which bytes come back. That is why
                # the file is read at startup rather than per request -- the
                # loading happens once, from a name given on the command line,
                # and the request never touches a name at all.
                #
                # The hazard it is guarding is concrete, not theoretical. This
                # container mounts /etc/insight read-only, and that directory
                # holds allowed.env -- every engineer's work address and
                # fingerprint -- next to admin.token. If this route ever grew a
                # path parameter, one traversal bug would hand the admin token
                # to the internet: exactly the outcome the loopback split and
                # `read_routes=False` exist to prevent. So it does not get one.
                #
                # Unset means 404, the same shape as the read-routes guard
                # below: a deployment that does not serve this does not admit
                # the route exists.
                if not self.install_script:
                    raise Rejected(404, "no such route")
                # Five minutes. Long enough that a team installing on the same
                # afternoon does not each pull it, short enough that a release
                # is live in the time it takes to walk to the next desk.
                self._send_bytes(self.install_script, "text/plain; charset=utf-8",
                                 "public, max-age=300")
                return

            if not self.read_routes:
                # This listener is the public one. The read routes list every
                # engineer at once, and "guarded by a token" is not the same
                # promise as "not reachable". 404 rather than 403: a listener
                # that does not serve these does not need to admit they exist.
                raise Rejected(404, "no such route")

            self._require_admin()

            if parsed.path == "/v1/bundles":
                week = (parse_qs(parsed.query).get("week") or [""])[0]
                if not _is_week(week):
                    raise Rejected(400, "week must look like 2026-W34")
                objects = self.store.list("bundles/{}/".format(week))
                for obj in objects:
                    obj["email"] = self._email_for(obj["key"])
                    obj["machine"] = _machine_from(obj["key"])
                self._send(200, {"week": week, "objects": objects})
                return

            if parsed.path.startswith("/v1/bundle/"):
                key = unquote(parsed.path[len("/v1/bundle/"):])
                if not _is_safe_key(key):
                    # The store refuses this too -- defence in depth -- but it
                    # raises StoreError, which would surface as 503 and tell the
                    # caller to retry something that can never work.
                    raise Rejected(400, "not a bundle key")
                try:
                    body = self.store.get(key)
                except KeyError:
                    raise Rejected(404, "no such key")
                self._send_bytes(body, "application/x-ndjson")
                return

            raise Rejected(404, "no such route")
        except Rejected as exc:
            self._reject(exc)
        except store_mod.StoreError as exc:
            log.error(json.dumps({"event": "store_error", "detail": str(exc)}))
            self._send(503, {"error": "storage unavailable"})
        except Exception:                              # noqa: BLE001
            log.exception("unhandled error on GET")
            self._send(500, {"error": "internal error"})

    def _reject(self, exc: Rejected) -> None:
        log.info(json.dumps({"event": "rejected", "status": exc.status,
                             "detail": exc.message}, sort_keys=True))
        try:
            self._send(exc.status, {"error": exc.message})
        except OSError:
            # The client gave up mid-write. It will retry; saying so in the log
            # beats a traceback that looks like a server fault.
            log.info(json.dumps({"event": "client_hung_up",
                                 "status": exc.status}))

    def _email_for(self, key: str) -> Optional[str]:
        """Reverse ``sha256(email)[:12]`` using the whitelist.

        The bucket holds no addresses; this route can name people because it is
        already admin-authenticated and the whitelist is right here. It is what
        lets `pull.py` say *missing: lan@...* instead of a hash nobody knows.
        """
        parts = key.split("/")
        return self.by_person_key.get(parts[2]) if len(parts) > 2 else None


def _machine_from(key: str) -> str:
    name = key.rsplit("/", 1)[-1]
    return name.split("-", 1)[0] if "-" in name else ""


def _is_safe_key(key: str) -> bool:
    """Only keys shaped like the ones this service writes.

    An allow-list rather than a search for ``..``: the store is also asked to
    resolve the path, and agreeing on what a key looks like is easier to be sure
    about than enumerating every way to spell "parent directory".
    """
    if not key or key.startswith("/") or "\\" in key:
        return False
    parts = key.split("/")
    if len(parts) != 4 or parts[0] != "bundles":
        return False
    if any(part in ("", ".", "..") for part in parts):
        return False
    return _is_week(parts[1]) and parts[3].endswith(".ndjson")


def _is_week(value: str) -> bool:
    if not value or len(value) != 8 or value[4:6] != "-W":
        return False
    return value[:4].isdigit() and value[6:].isdigit() and 1 <= int(value[6:]) <= 53


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------

def build_handler(store: Any, allowed: Dict[str, List[str]],
                  admin_token: str, read_routes: bool = True,
                  install_script: Optional[bytes] = None,
                  manifest: Optional[bytes] = None) -> type:
    return type("BoundHandler", (Handler,), {
        "store": store,
        "allowed": allowed,
        "admin_token": admin_token,
        "by_person_key": {identity.person_key(e): e for e in allowed},
        "read_routes": read_routes,
        "install_script": install_script,
        "install_manifest": manifest if manifest is not None
        else install_manifest(install_script),
    })


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collection endpoint for usage-insight bundles.")
    parser.add_argument("--store", default=os.environ.get("INSIGHT_STORE"),
                        help="s3://bucket[/prefix] or file:///path. Keys "
                             "already start with bundles/, so a `bundles` "
                             "prefix files everything under bundles/bundles/")
    parser.add_argument("--host", default=os.environ.get("INSIGHT_HOST", "127.0.0.1"),
                        help="default 127.0.0.1 -- nginx faces the internet, "
                             "not this")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("INSIGHT_PORT", "8479")))
    parser.add_argument("--upload-port", type=int,
                        default=int(os.environ.get("INSIGHT_UPLOAD_PORT") or 0),
                        help="a second listener serving uploads and /healthz "
                             "only. Publish this one when the reverse proxy is "
                             "on another machine and forwards every path")
    parser.add_argument("--allowed-file", default=os.environ.get("INSIGHT_ALLOWED_FILE"),
                        help="file of email:fingerprint lines")
    parser.add_argument("--install-script",
                        default=os.environ.get("INSIGHT_INSTALL_SCRIPT"),
                        help="the install.sh served at GET /install. Read once "
                             "at startup; unset means that route 404s")
    parser.add_argument("--admin-token-file",
                        default=os.environ.get("INSIGHT_ADMIN_TOKEN_FILE"),
                        help="file holding the admin token, instead of putting "
                             "it in the environment where `docker inspect` "
                             "shows it")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s")

    if not args.store:
        raise SystemExit("no --store -- pass s3://aeris-insight or file:///path")
    admin_token = load_admin_token(os.environ.get("INSIGHT_ADMIN_TOKEN"),
                                   args.admin_token_file)

    install_script = load_install_script(args.install_script)
    manifest = install_manifest(install_script)

    allowed = load_allowed(os.environ.get("INSIGHT_ALLOWED"), args.allowed_file)
    try:
        store = store_mod.open_store(args.store)
    except store_mod.StoreError as exc:
        raise SystemExit(str(exc))

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(json.dumps({
            "event": "public_bind", "host": args.host,
            "detail": "this process is not TLS and has no rate limiting; it is "
                      "designed to sit behind nginx on loopback"}))

    server = ThreadingHTTPServer((args.host, args.port), build_handler(
        store, allowed, admin_token, install_script=install_script,
        manifest=manifest))
    log.info(json.dumps({"event": "listening", "host": args.host,
                         "port": args.port, "store": args.store,
                         "people": len(allowed),
                         "install_script": bool(install_script),
                         "serving_version": json.loads(manifest)["version"]
                         if manifest else None},
                        sort_keys=True))

    # A second listener that serves uploads and nothing else.
    #
    # The routes split by exposure, not by token: uploading
    # has to be reachable from every laptop, and reading exposes the whole team
    # at once. Where a reverse proxy on this host enforces that split, this is
    # not needed. Where the gateway is on *another* machine -- so it cannot
    # reach a container network and must be given a host port -- the split has
    # to be enforced here instead, because that gateway's config may forward
    # every path, and then the admin token is the only thing left between the
    # internet and every engineer's telemetry.
    #
    # Publish this port; keep the one above on the private side.
    upload_server = None
    if args.upload_port:
        if args.upload_port == args.port:
            raise SystemExit(
                "--upload-port must differ from --port; the whole point is that "
                "one of them serves the read routes and the other does not")
        upload_server = ThreadingHTTPServer(
            (args.host, args.upload_port),
            build_handler(store, allowed, admin_token, read_routes=False,
                          install_script=install_script, manifest=manifest))
        threading.Thread(target=upload_server.serve_forever, daemon=True).start()
        log.info(json.dumps({"event": "listening_upload_only",
                             "host": args.host, "port": args.upload_port},
                            sort_keys=True))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info(json.dumps({"event": "stopping"}))
    finally:
        server.server_close()
        if upload_server is not None:
            upload_server.shutdown()
            upload_server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
