#!/usr/bin/env python3
"""The insights app: a built single-page bundle, and the snapshot it draws.

Two screens -- ``/insights`` and ``/activities`` -- are one React bundle built
to static files under ``server/assets/app``. The routing between them happens
in the browser, so both URLs return the same bytes and neither is a directory
on this host.

**The whole security property of this module is that no path out of a request
ever reaches the filesystem.** ``server/dashboard.py`` gets that for free by
holding one fixed file in memory; a bundle of forty files cannot, so the same
property is bought a different way: every file is read *once at startup* into a
dict keyed by its exact relative name, and a request path is looked up in that
dict as a key. It is never joined to a directory, never resolved, never
normalised. ``../``, an absolute path, and any encoding of either are keys that
are not in the dict, which is why they get a 404 rather than a decision. See
the comment on ``/install`` in ``server/proxy.py`` for what that is guarding:
this container mounts ``/etc/insight`` -- every engineer's address and
fingerprint, next to the admin token -- read-only beside the code.

The snapshot is the other half. It is produced elsewhere, by
``report/dashboard_data.py``, and is read lazily and re-stat'ed so a redeployed
file is picked up without a restart. When it is absent the route says so and
fails: **absent is never zero** (``CLAUDE.md``). An empty object here would
render as a team that measured nothing and did nothing, and those are not the
same fact.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Dict, List, NamedTuple, Optional

#: Extension -> Content-Type. An allow-list, not ``mimetypes.guess_type``: a
#: file the build did not mean to ship should not become servable because the
#: host happens to know a media type for it. Anything else in the directory is
#: not loaded at all, so it cannot be reached under any name.
MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
}

#: The entry point, and the only name this module needs to know. Both screens
#: return it; the build decides everything else.
ENTRY = "index.html"

#: Per-file and whole-bundle sanity bounds. Not a policy -- the bundle is held
#: in memory for the life of the process, and a directory that has grown past
#: this is not the frontend build any more.
MAX_ASSET_BYTES = 8 * 1024 * 1024
MAX_ASSET_FILES = 512

#: The snapshot is one JSON document for a fourteen-person team; the real one
#: is fifteen kilobytes. This is the bound that stops a runaway generator from
#: being loaded into the process.
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024

#: A cached asset that turns out to be stale is a page that will not come back
#: without a hard refresh, so caching is opted into by name and never by
#: default: only a file whose name carries a content hash is cached, because
#: only that name changes when the bytes do.
IMMUTABLE = "public, max-age=31536000, immutable"

#: ``index-DiwrgTda.js``, ``app.a1b2c3d4.css`` -- a separator, then a token of
#: at least eight name characters, then the extension. The token must also
#: carry a digit or mixed case, which is what keeps ``my-component.js`` out:
#: a false positive here caches a file for a year under a name that will be
#: reused, and that failure is unrecoverable from the browser's side.
_HASHED = re.compile(r"[-.]([A-Za-z0-9_-]{8,})\.[A-Za-z0-9]+$")


class Asset(NamedTuple):
    body: bytes
    content_type: str
    cache_control: str


class AppBundle:
    """Every file of the built app, keyed by its exact relative name.

    ``get`` is a dict lookup and nothing else. There is deliberately no method
    here that takes a name and touches the filesystem -- adding one would undo
    the reason this class exists.
    """

    def __init__(self, root: str, files: Dict[str, Asset],
                 skipped: Optional[List[str]] = None) -> None:
        self.root = root
        self.files = files
        self.skipped = skipped or []

    @property
    def built(self) -> bool:
        """Whether there is an entry point to serve.

        A directory with no ``index.html`` is a build that has not run or has
        half-run, and is reported the same way as no directory at all: the
        screens do not exist yet.
        """
        return ENTRY in self.files

    @property
    def index(self) -> Optional[Asset]:
        return self.files.get(ENTRY)

    def get(self, key: str) -> Optional[Asset]:
        return self.files.get(key)


def cache_control_for(name: str) -> str:
    """``no-store`` unless the name carries a content hash.

    HTML is never cached whatever it is called: the entry point names the
    hashed bundles, so a cached one pins the browser to a deploy that is gone.
    """
    if name.endswith(".html"):
        return "no-store"
    match = _HASHED.search(os.path.basename(name))
    if not match:
        return "no-store"
    token = match.group(1)
    if any(c.isdigit() for c in token) or (token.lower() != token
                                           and token.upper() != token):
        return IMMUTABLE
    return "no-store"


def load_app(path: Optional[str] = None) -> AppBundle:
    """Read the built app once, at startup. A missing directory is not fatal.

    ``dashboard.load_page`` raises ``SystemExit`` when its file is missing, and
    that is right *there*: a passcode was configured, so somebody asked for
    that page, and a deployment serving a page it cannot read is a broken
    deployment worth stopping at the door. It is wrong *here*. This directory
    is written by a separate frontend build that is not part of a checkout and
    is not in the image until it has been run, so its absence is the ordinary
    state of the tree rather than a misconfiguration. Refusing to start would
    take the upload endpoint -- the collection path for all ten metrics -- down
    for the sake of two screens nobody has built yet. So the bundle comes back
    empty, ``built`` is False, and those two routes say so.

    The name is taken from the command line, once. No request supplies one.
    """
    root = path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "assets", "app")
    files: Dict[str, Asset] = {}
    skipped: List[str] = []
    if not os.path.isdir(root):
        return AppBundle(root, files, skipped)

    for folder, _dirs, names in os.walk(root):
        for name in sorted(names):
            if name.startswith("."):
                continue
            full = os.path.join(folder, name)
            key = os.path.relpath(full, root).replace(os.sep, "/")
            media = MEDIA_TYPES.get(os.path.splitext(name)[1].lower())
            if media is None:
                skipped.append(key)
                continue
            if len(files) >= MAX_ASSET_FILES:
                skipped.append(key)
                continue
            try:
                with open(full, "rb") as handle:
                    body = handle.read(MAX_ASSET_BYTES + 1)
            except OSError:
                # Unreadable at startup means unservable for the life of the
                # process, which is honest: a chunk that 404s is a page that
                # visibly fails, not one that quietly renders half a truth.
                skipped.append(key)
                continue
            if len(body) > MAX_ASSET_BYTES:
                skipped.append(key)
                continue
            files[key] = Asset(body, media, cache_control_for(key))
    return AppBundle(root, files, skipped)


# --------------------------------------------------------------------------
# the snapshot
# --------------------------------------------------------------------------

class SnapshotMissing(Exception):
    """There is no snapshot to serve. Surfaces as a 503, never as ``{}``."""


class Snapshot:
    """The JSON the app draws, written by ``report/dashboard_data.py``.

    Read lazily and re-read when the file changes -- the same ``(mtime, size)``
    stamp ``dashboard.Attendance`` uses -- so a snapshot regenerated on the
    host is live without a restart. The path is settled once, from the command
    line; a request never names it, and there is no method here that takes one.

    Parsed on load and refused if it does not parse. A half-written file is not
    a smaller measurement, it is not a measurement, and serving its bytes would
    put a truncated document in front of somebody who would read it as data.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "assets",
            "insights.json")
        self._lock = threading.Lock()
        self._body: Optional[bytes] = None
        self._stamp: object = "unset"

    def _mtime(self) -> object:
        try:
            stat = os.stat(self.path)
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def body(self) -> bytes:
        """The snapshot's bytes, or ``SnapshotMissing`` with a sentence.

        The sentence is written to be shown to a person: whoever opens the page
        and finds nothing on it is the one who has to decide whether to run the
        generator, and "no data" would not tell them that.
        """
        with self._lock:
            stamp = self._mtime()
            if stamp is None:
                self._body, self._stamp = None, "unset"
                raise SnapshotMissing(
                    "the insights snapshot has not been generated")
            if stamp == self._stamp and self._body is not None:
                return self._body

            self._stamp = "unset"
            try:
                with open(self.path, "rb") as handle:
                    body = handle.read(MAX_SNAPSHOT_BYTES + 1)
            except OSError:
                raise SnapshotMissing("the insights snapshot cannot be read")
            if len(body) > MAX_SNAPSHOT_BYTES:
                raise SnapshotMissing(
                    "the insights snapshot is over the {} byte cap -- that is "
                    "not a snapshot".format(MAX_SNAPSHOT_BYTES))
            try:
                json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise SnapshotMissing(
                    "the insights snapshot is not JSON -- it may be half "
                    "written; the next run replaces it whole")
            self._body, self._stamp = body, stamp
            return body

    def state(self) -> Dict[str, Any]:
        """What the startup log says about it. Never opens the file."""
        return {"path": self.path, "present": self._mtime() is not None}
