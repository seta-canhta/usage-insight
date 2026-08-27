#!/usr/bin/env python3
"""Keep the installed client current, without ever surprising its owner.

    insight update --status     what version, when it last looked, is it on
    insight update --check      look now, report, change nothing
    insight update --now        look now and install if there is something
    insight update --on/--off   turn the automatic check on or off
    insight update --pin 0.3.0  stay on this version
    insight update --unpin

**The uncomfortable part, said first.** Code that replaces itself on somebody
else's laptop is a different act from an installer they typed, and this file
exists inside a project whose every other decision is "never do a thing the
person was not shown". So the automatic check is a question ``insight setup``
asks, its answer is recorded in ``config.json`` beside the consent record, and
the hourly consent text says it out loud. It is not on until somebody says so,
and ``insight update --off`` ends it. The same shape as ``schedule --hourly``,
which is a larger act than this one and is nonetheless asked rather than
assumed.

Three further limits, each of which is the answer to a specific way this could
go wrong:

*Same major only.* ``0.3.0 -> 0.4.1`` installs itself. ``0.x -> 1.0.0`` never
does; it prints a nudge and waits. A major bump is where what gets collected
could change, and a change to what is collected must not arrive silently on a
machine whose owner agreed to the old answer. That is the whole consent model
in one line, and it is cheap to enforce.

*Never past what the endpoint accepts.* ``server/proxy.py`` refuses a
``schema_version`` it has not been taught. A client that upgraded itself into
400-ing every upload is worse than one that stayed behind a release, because it
fails in the direction nobody is watching. The manifest carries both the schema
the new client emits and the set this endpoint accepts, and a mismatch holds.

*Never in a checkout.* If this is running from a git clone, ``git pull`` is the
update mechanism and rewriting somebody's working tree would be appalling. The
test for "installed rather than cloned" is exact: the archive has to be reached
through ``<data-dir>/current.pyz``, which is a thing only ``install.sh`` makes.

**Trust.** Identical to the installer's, and deliberately so. The expected
digest arrives from the endpoint over TLS; the bytes arrive from GitHub. Two
origins, so compromising GitHub Releases alone cannot put code on a laptop --
the digest is not theirs to change. Verification is mandatory, there is no flag
that skips it, and a mismatch is a refusal rather than a warning.

Standard library only, like everything else on the client side.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_POLLERS = _ROOT
if _POLLERS not in sys.path:
    sys.path.insert(0, _POLLERS)

import common  # noqa: E402  -- the shared trust store; see common.ssl_context

#: Sent as ``User-Agent``, same value and same reason as ``cli/ship.py``:
#: urllib's default draws a Cloudflare ``403 error code: 1010`` before the
#: request ever reaches the endpoint.
USER_AGENT = "seta-insight/1 (+usage-insight)"

#: Once an hour, with the collection run that is already making network calls.
#:
#: This was once a day until 2026-08-27, on the argument that hourly is 24
#: pointless requests a day for a file that changes a few times a year. Two
#: things were wrong with it. The arithmetic assumed a 24-hour day: the team
#: works 09:00-19:00 ICT and the laptops are shut for the other fourteen hours,
#: so "once a day" is really once per working day -- and because the swapped
#: archive only takes effect on the *next* invocation, a release reached the
#: fleet in up to two working days.
#:
#: The second is what that delay cost. v0.5.0 was the release that tells a
#: laptop which Jira project keys are real; until it lands, every reader runs
#: permissive and invents them (AR-1). A fleet two working days behind is two
#: working days of fabricated join keys, and no amount of "a release is not
#: urgent" survives an example of one that was.
#:
#: The cost of the new value is one conditional GET of a few hundred bytes per
#: laptop per hour, on a run that is already uploading. `check` is written
#: never to raise, so the laptop on a plane is unaffected either way.
CHECK_INTERVAL = 3600

#: Where the manifest lives, relative to the configured endpoint. Derived by
#: ``server/proxy.py`` from the very ``install.sh`` it serves at ``/install``,
#: so the two can never disagree about which version is current.
MANIFEST_PATH = "/install.json"

#: A manifest is a few hundred bytes and the archive is under a megabyte. Both
#: bounds are sanity, not policy -- but an unbounded read into memory on an
#: hourly job is how a laptop gets taken down by a misconfigured proxy.
MAX_MANIFEST_BYTES = 16 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024

#: Short. This runs inside the hourly collection; a hung endpoint must cost
#: seconds, not the whole run.
TIMEOUT = 20

STATE_NAME = "update.json"

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class UpdateError(Exception):
    """Nothing was changed. Never raised past the hourly run -- see ``check``."""


# --------------------------------------------------------------------------
# state -- machine state, not a decision
# --------------------------------------------------------------------------
#
# The *decision* ("may this machine update itself") lives in config.json beside
# the consent record, because that is what it is. The timestamps and the last
# result live here, because they are bookkeeping and nobody should have to read
# past them to check what they agreed to.

def state_path(home: str) -> str:
    return os.path.join(home, STATE_NAME)


def load_state(home: str) -> Dict[str, Any]:
    try:
        with open(state_path(home), "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(home: str, state: Dict[str, Any]) -> None:
    try:
        os.makedirs(home, exist_ok=True)
        tmp = state_path(home) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
        os.replace(tmp, state_path(home))
    except OSError:
        pass  # a full disk must not turn a collection run into a crash loop


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# versions
# --------------------------------------------------------------------------

def parse_version(value: str) -> Optional[Tuple[int, int, int]]:
    match = _SEMVER.match((value or "").strip())
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))) \
        if match else None


def is_newer(current: str, candidate: str) -> bool:
    a, b = parse_version(current), parse_version(candidate)
    return bool(a and b and b > a)


# --------------------------------------------------------------------------
# where we are installed
# --------------------------------------------------------------------------

def installation(archive: Optional[str]) -> Optional[Dict[str, str]]:
    """The layout to update in place, or None if this is not one.

    Deliberately narrow. ``install.sh`` puts the archive at
    ``<data>/insight-<v>.pyz`` and points ``<data>/current.pyz`` at it, and the
    launcher execs the symlink -- so a running client that came from the
    installer sees ``sys.argv[0]`` ending in ``current.pyz``. Anything else is
    a git checkout, or a ``.pyz`` somebody downloaded into ~/Downloads and ran
    once, and neither is a thing this may start rewriting.
    """
    if not archive:
        return None
    # Realpath, not abspath. On macOS $TMPDIR and /var are themselves symlinks,
    # so a directory reached one way and an archive resolved the other way are
    # the same file under two spellings -- and the prune below, which compares
    # paths as strings, would then delete the very archive a rollback needs.
    directory = os.path.realpath(os.path.dirname(os.path.abspath(archive)))
    current = os.path.join(directory, "current.pyz")
    if not os.path.islink(current):
        return None
    if not os.access(directory, os.W_OK):
        return None
    return {"dir": directory, "current": current,
            "resolved": os.path.realpath(current)}


# --------------------------------------------------------------------------
# the manifest
# --------------------------------------------------------------------------

def manifest_url(endpoint: str) -> str:
    return endpoint.rstrip("/") + MANIFEST_PATH


def fetch_manifest(endpoint: str, timeout: int = TIMEOUT,
                   opener=urllib.request.urlopen) -> Dict[str, Any]:
    """Read ``<endpoint>/install.json``. Raises ``UpdateError`` on anything odd.

    Same endpoint the bundles go to, over the same TLS, using the same trust
    store. No new host to reach, no new certificate to trust, and nothing here
    that a laptop behind a corporate proxy has to be told about separately.
    """
    url = manifest_url(endpoint)
    if not url.lower().startswith("https://"):
        # http:// would put the expected digest on the wire in clear, which is
        # the one value in this exchange that has to be trustworthy.
        raise UpdateError("endpoint {} is not https".format(endpoint))
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with opener(request, timeout=timeout,
                    context=common.ssl_context()) as response:
            raw = response.read(MAX_MANIFEST_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UpdateError("{}: {}".format(url, exc))
    if len(raw) > MAX_MANIFEST_BYTES:
        raise UpdateError("{} returned more than a manifest".format(url))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("{} is not JSON: {}".format(url, exc))
    if not isinstance(data, dict):
        raise UpdateError("{} is not a manifest".format(url))
    return data


def validate_manifest(data: Dict[str, Any]) -> Dict[str, Any]:
    """Check the three fields anything is done with. Raises rather than guesses.

    A manifest missing its digest must never degrade into an unverified
    install -- so this refuses the whole document rather than defaulting the
    field, and the caller has no branch that could proceed without one.
    """
    version = str(data.get("version") or "")
    digest = str(data.get("sha256") or "").lower()
    url = str(data.get("url") or "")
    if not parse_version(version):
        raise UpdateError("manifest version {!r} is not a semver".format(version))
    if not _SHA256.match(digest):
        raise UpdateError("manifest sha256 is not a sha256")
    if not url.lower().startswith("https://"):
        # The digest makes the bytes trustworthy wherever they come from, but a
        # scheme other than https is a sign the manifest is not what we think,
        # and it is not this code's job to be clever about that.
        raise UpdateError("manifest url is not https")
    schemas = data.get("schemas")
    return {
        "version": version,
        "sha256": digest,
        "url": url,
        "client_schema": str(data.get("client_schema") or "") or None,
        "schemas": [str(s) for s in schemas] if isinstance(schemas, list) else None,
    }


# --------------------------------------------------------------------------
# the decision -- pure, so it can be tested without a network or a filesystem
# --------------------------------------------------------------------------

def plan(current_version: str, manifest: Dict[str, Any],
         pinned: Optional[str] = None) -> Tuple[str, str]:
    """``(action, why)``. Actions: install, current, pinned, held, refused."""
    candidate = manifest["version"]

    if pinned:
        # A pin holds; it does not travel. The manifest describes exactly one
        # release -- the current one -- so there is no digest and no URL for an
        # arbitrary older version, and pretending otherwise would mean either
        # guessing a URL or installing something unverified. Pinning therefore
        # means "stop here", and getting off a pin is `--unpin` followed by a
        # normal update, or the installer, both of which are explicit acts.
        if pinned == candidate:
            return ("current" if pinned == current_version else "install",
                    "pinned to {}, which is the current release".format(pinned))
        return "pinned", "pinned to {}; {} is available (insight update --unpin)".format(
            pinned, candidate)

    if not is_newer(current_version, candidate):
        return "current", "{} is current".format(current_version)

    here, there = parse_version(current_version), parse_version(candidate)
    if here and there and here[0] != there[0]:
        # Not installed automatically, at all, ever. A major bump is where what
        # is collected could change, and a machine whose owner consented to the
        # old answer must not quietly start doing the new one. Said out loud in
        # `status` and in auto.log until somebody acts on it.
        return "held", (
            "{} is available -- a major version, so it is not installed "
            "automatically. Read what changed, then run: insight update --now"
            .format(candidate))

    schemas = manifest.get("schemas")
    schema = manifest.get("client_schema")
    if schemas is not None and schema and schema not in schemas:
        # An endpoint that has not been taught the new schema 400s every upload
        # from a client that speaks it. Staying a release behind is the strictly
        # better failure: collection keeps working and somebody upgrades the
        # server. `schemas` absent means an endpoint too old to say, and
        # refusing forever on silence would mean never updating at all.
        return "refused", (
            "{} emits schema {}, which {} does not accept ({}). Staying on {}."
            .format(candidate, schema, "the endpoint",
                    ", ".join(schemas) or "none advertised", current_version))

    return "install", "{} -> {}".format(current_version, candidate)


# --------------------------------------------------------------------------
# doing it
# --------------------------------------------------------------------------

def _download(url: str, dest: str, timeout: int = TIMEOUT,
              opener=urllib.request.urlopen) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener(request, timeout=timeout,
                    context=common.ssl_context()) as response:
            body = response.read(MAX_ARCHIVE_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UpdateError("downloading {}: {}".format(url, exc))
    if not body:
        raise UpdateError("{} returned nothing".format(url))
    if len(body) > MAX_ARCHIVE_BYTES:
        raise UpdateError("{} is larger than any release has ever been".format(url))
    with open(dest, "wb") as handle:
        handle.write(body)


def _verify(path: str, expected: str) -> None:
    """Mandatory. There is no argument to this function that skips it.

    Not a flag, not an environment variable, not a private one. A way to skip
    verification is a line somebody pastes into Slack the week a corporate
    proxy breaks a download, and from then on nobody verifies anything.
    """
    import hashlib                                             # noqa: PLC0415
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    got = digest.hexdigest()
    if got != expected:
        os.remove(path)
        raise UpdateError(
            "checksum mismatch -- nothing was changed. expected {} (from the "
            "endpoint over TLS), got {} (from the release artifact)"
            .format(expected, got))


def _self_test(python: str, archive: str, expected_version: str) -> None:
    """Run the new archive before anything points at it.

    Ordering is the whole point: verified bytes still cannot run under a
    Python that lost a module, on a filesystem mounted noexec, or after a
    truncation that somehow matched. Finding that out *after* repointing the
    symlink means the hourly job is now running the thing that failed, once an
    hour, into a log nobody opens.
    """
    try:
        done = subprocess.run([python, archive, "--version"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError("the new archive did not run: {}".format(exc))
    out = done.stdout.decode("utf-8", "replace").strip()
    if done.returncode != 0:
        raise UpdateError("the new archive exited {}: {}".format(
            done.returncode, out))
    if "insight {}".format(expected_version) not in out:
        raise UpdateError(
            "the new archive reports {!r}, expected version {}".format(
                out, expected_version))


def install(manifest: Dict[str, Any], place: Dict[str, str],
            python: Optional[str] = None, download=_download) -> Dict[str, Any]:
    """Download, verify, self-test, then repoint. In that order, always.

    The running process is never swapped underneath itself. The new archive
    goes to a new name, and only the symlink moves -- the interpreter already
    has the old file open by inode, and the launcher resolves ``current.pyz``
    at exec time, so the swap takes effect on the *next* invocation. Which is
    exactly the moment it should.
    """
    python = python or sys.executable
    version = manifest["version"]
    target = os.path.join(place["dir"], "insight-{}.pyz".format(version))
    previous = place["resolved"]

    handle, staged = tempfile.mkstemp(prefix=".update-", suffix=".pyz",
                                      dir=place["dir"])
    os.close(handle)
    try:
        download(manifest["url"], staged)
        _verify(staged, manifest["sha256"])
        os.chmod(staged, 0o755)
        _self_test(python, staged, version)
        # Same filesystem by construction -- staged in the destination
        # directory -- so this rename is atomic and cannot half-happen.
        os.replace(staged, target)
    except BaseException:
        if os.path.exists(staged):
            os.remove(staged)
        raise

    tmp_link = os.path.join(place["dir"], ".current.pyz.new")
    if os.path.lexists(tmp_link):
        os.remove(tmp_link)
    os.symlink(target, tmp_link)
    os.replace(tmp_link, place["current"])

    # The previous archive stays. Rollback is then a symlink flip and nothing
    # else, and -- more immediately -- the process running this call still has
    # that file open.
    removed = []
    for name in sorted(os.listdir(place["dir"])):
        path = os.path.join(place["dir"], name)
        if not name.startswith("insight-") or not name.endswith(".pyz"):
            continue
        if os.path.realpath(path) in (os.path.realpath(target),
                                      os.path.realpath(previous)):
            continue
        try:
            os.remove(path)
            removed.append(name)
        except OSError:
            pass

    return {"installed": version, "archive": target,
            "previous_archive": previous, "pruned": removed}


def rollback(place: Dict[str, str], archive: str) -> Dict[str, Any]:
    """Point ``current.pyz`` back at an archive that is still on disk."""
    if not os.path.isfile(archive):
        raise UpdateError("{} is gone -- reinstall instead".format(archive))
    tmp_link = os.path.join(place["dir"], ".current.pyz.new")
    if os.path.lexists(tmp_link):
        os.remove(tmp_link)
    os.symlink(archive, tmp_link)
    os.replace(tmp_link, place["current"])
    return {"rolled_back_to": archive}


# --------------------------------------------------------------------------
# the hourly entry point
# --------------------------------------------------------------------------

def due(state: Dict[str, Any], interval: int = CHECK_INTERVAL,
        clock=time.time) -> bool:
    last = state.get("last_check_epoch")
    if not isinstance(last, (int, float)):
        return True
    # A clock that went backwards (a laptop resuming, a timezone fix) must not
    # mean "never check again".
    return not (0 <= clock() - last < interval)


def check(home: str, endpoint: Optional[str], current_version: str,
          archive: Optional[str], enabled: bool, apply_it: bool = True,
          force: bool = False, clock=time.time,
          fetch=None, installer=None) -> Dict[str, Any]:
    """Look, maybe install, and never raise. Returns a record for the log.

    Every failure here is a no-op that leaves the collection run alone. A
    laptop on a plane, an endpoint that is down, a manifest that is nonsense --
    none of those is a reason to skip reading Copilot's journals, and an update
    check that can break collection is a worse feature than no update check.
    """
    fetch = fetch or fetch_manifest
    installer = installer or install
    record: Dict[str, Any] = {"event": "update_check"}

    if not enabled and not force:
        return {"event": "update_check", "action": "off"}
    if not endpoint:
        return {"event": "update_check", "action": "unavailable",
                "detail": "no endpoint configured"}

    place = installation(archive)
    if place is None:
        return {"event": "update_check", "action": "unavailable",
                "detail": "not an installed archive -- update with `git pull` "
                          "in the checkout, or reinstall from the endpoint"}

    state = load_state(home)
    if not force and not due(state, clock=clock):
        return {"event": "update_check", "action": "not_due",
                "last_check_at": state.get("last_check_at")}

    state["last_check_at"] = _now()
    state["last_check_epoch"] = int(clock())

    try:
        manifest = validate_manifest(fetch(endpoint))
    except UpdateError as exc:
        state["last_result"] = "failed"
        state["last_detail"] = str(exc)
        save_state(home, state)
        # Logged, not raised, and not counted as a problem in the run. An
        # endpoint being unreachable for a day is not a collection failure.
        return {"event": "update_check", "action": "failed", "detail": str(exc)}

    action, why = plan(current_version, manifest, state.get("pinned"))
    record["action"] = action
    record["detail"] = why
    record["available"] = manifest["version"]
    state["last_result"] = action
    state["last_detail"] = why
    state["available"] = manifest["version"]

    if action != "install" or not apply_it:
        save_state(home, state)
        return record

    try:
        done = installer(manifest, place)
    except UpdateError as exc:
        state["last_result"] = "failed"
        state["last_detail"] = str(exc)
        save_state(home, state)
        return {"event": "update_check", "action": "failed", "detail": str(exc),
                "available": manifest["version"]}
    except OSError as exc:
        state["last_result"] = "failed"
        state["last_detail"] = str(exc)
        save_state(home, state)
        return {"event": "update_check", "action": "failed",
                "detail": "{}: {}".format(type(exc).__name__, exc)}

    history = state.get("history")
    history = history if isinstance(history, list) else []
    history.insert(0, {"at": state["last_check_at"], "from": current_version,
                       "to": manifest["version"]})
    state["history"] = history[:5]
    state["last_result"] = "updated"
    state["last_detail"] = "{} -> {}".format(current_version, manifest["version"])
    state["installed_version"] = manifest["version"]
    save_state(home, state)

    record.update(done)
    record["action"] = "updated"
    record["from"] = current_version
    record["to"] = manifest["version"]
    # Said in the log with both numbers, because "updated" alone is the line
    # somebody reads three weeks later while working out when behaviour changed.
    return record


def status(home: str, current_version: str, archive: Optional[str],
           enabled: bool) -> Dict[str, Any]:
    """What `insight status` folds in, and what `update --status` prints."""
    state = load_state(home)
    place = installation(archive)
    return {
        "version": current_version,
        "auto_update": bool(enabled),
        "supported": place is not None,
        "pinned": state.get("pinned"),
        "last_check_at": state.get("last_check_at"),
        "last_result": state.get("last_result"),
        "last_detail": state.get("last_detail"),
        "available": state.get("available"),
        "history": state.get("history", []),
    }
