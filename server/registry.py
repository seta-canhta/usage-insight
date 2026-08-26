#!/usr/bin/env python3
"""Who may upload, and who is expected to.

Two lists, both files an operator can still read and edit:

``allowed.env``   ``email:fingerprint[:fingerprint]`` per line -- who may upload.
``roster.txt``    one work email per line -- who is *expected* to.

The roster used to exist only so the weekly pull could name people who sent
nothing. It does a second job here: it is the list a laptop is allowed to
enrol itself against. That removes the step this system used to require --
engineer runs setup, reads a fingerprint off their screen, sends it to an
admin over chat, admin edits a file, admin restarts the service, and until all
five happen every upload is a 401 nobody can explain. The admin now adds an
email to the roster once, which they already had to do for coverage.

**Trust on first use.** An email on the roster with no fingerprint yet accepts
the first one offered. After that the entry is fixed: a second fingerprint for
the same person is refused, so knowing a colleague's address is not enough to
upload as them. A replacement laptop needs ``reset``, which is an admin route.
Every enrolment is logged with the address and the outcome.

What this deliberately does not defend against: someone inside the roster
window who reaches the endpoint before the real person does. The payload is
upload-only -- reading is a separate route behind the admin token -- so the
worst case is telemetry attributed to the wrong person, which the enrolment
log makes visible after the fact. The alternative, a shared enrolment secret,
puts a credential in a chat message to buy that, and this system already
refuses that trade for the upload secret itself.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from typing import Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "cli"))

import identity  # noqa: E402


class RegistryError(Exception):
    """A change that would leave the two files disagreeing, or an unknown person."""


def _atomic_write(path: str, text: str) -> None:
    """Replace a file without ever leaving a half-written one behind.

    The proxy reloads on mtime, so a torn write is a window where the whitelist
    is short and real uploads 401.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".registry-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise


def read_roster(path: Optional[str]) -> List[str]:
    """One address per line, ``#`` comments, blank lines ignored."""
    if not path or not os.path.exists(path):
        return []
    people = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip().lower()
            if line:
                people.append(line)
    return sorted(set(people))


class Registry:
    """The whitelist and the roster, reloaded when either file changes.

    Reloading on mtime rather than only at startup is what makes both halves
    work: a laptop that enrols is live immediately, and an operator who edits
    the file by hand does not have to restart the service to be obeyed. Every
    read takes the lock, which costs nothing at this rate and means an upload
    never sees a dict mid-rewrite.
    """

    def __init__(self, allowed_path: Optional[str] = None,
                 roster_path: Optional[str] = None,
                 allowed: Optional[Dict[str, List[str]]] = None) -> None:
        self.allowed_path = allowed_path
        self.roster_path = roster_path
        self._lock = threading.Lock()
        self._allowed: Dict[str, List[str]] = dict(allowed or {})
        self._roster: List[str] = []
        self._stamps: Dict[str, object] = {}
        self._reload(force=True)

    # -- state ------------------------------------------------------------

    def _mtime(self, path: Optional[str]) -> object:
        """A change stamp, not a timestamp.

        ``mtime`` alone is not enough. A file created after this object was
        built starts from the same "missing" value, and two writes inside one
        filesystem tick share an mtime -- both cases leave a hand edit
        unnoticed until the next restart, which is the failure this whole
        reload exists to avoid. Size and nanosecond mtime together settle it.
        """
        if not path:
            return None
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _reload(self, force: bool = False) -> None:
        for kind, path in (("allowed", self.allowed_path),
                           ("roster", self.roster_path)):
            if not path:
                continue
            stamp = self._mtime(path)
            if not force and stamp == self._stamps.get(kind, "unset"):
                continue
            self._stamps[kind] = stamp
            if kind == "allowed" and os.path.exists(path):
                with open(path, "r", encoding="utf-8") as handle:
                    self._allowed = identity.parse_whitelist(handle.read())
            elif kind == "roster":
                self._roster = read_roster(path)

    def _persist(self) -> None:
        if not self.allowed_path:
            return
        lines = ["# One line per engineer. Written by the endpoint on enrolment,",
                 "# and safe to edit by hand -- it is re-read when it changes.",
                 ""]
        for email in sorted(self._allowed):
            lines.append("{}:{}".format(email, ":".join(self._allowed[email])))
        _atomic_write(self.allowed_path, "\n".join(lines) + "\n")
        self._stamps["allowed"] = self._mtime(self.allowed_path)

    def _persist_roster(self) -> None:
        if not self.roster_path:
            raise RegistryError(
                "no roster file configured -- set INSIGHT_ROSTER_FILE")
        _atomic_write(self.roster_path, "\n".join(self._roster) + "\n")
        self._stamps["roster"] = self._mtime(self.roster_path)

    # -- reads ------------------------------------------------------------

    def identify(self, secret: str) -> Optional[str]:
        with self._lock:
            self._reload()
            return identity.identify(secret, self._allowed)

    def email_for_person_key(self, person_key: str) -> Optional[str]:
        with self._lock:
            self._reload()
            for email in self._allowed:
                if identity.person_key(email) == person_key:
                    return email
        return None

    def count(self) -> int:
        with self._lock:
            self._reload()
            return len(self._allowed)

    def people(self) -> List[Dict[str, object]]:
        """Everyone on either list, and which of the two they are missing from.

        An address on the roster that has never enrolled is the interesting
        row: it is a machine that is expected to report and cannot. That used
        to be invisible until the weekly pull noticed the silence.
        """
        with self._lock:
            self._reload()
            names = sorted(set(self._roster) | set(self._allowed))
            return [{
                "email": email,
                "on_roster": email in self._roster,
                "enrolled": email in self._allowed,
                "fingerprints": len(self._allowed.get(email, [])),
            } for email in names]

    # -- writes -----------------------------------------------------------

    def enroll(self, email: str, fingerprint: str) -> str:
        """Trust on first use, against the roster. Returns what happened.

        ``created``   first fingerprint for someone expected -- now live
        ``known``     the same fingerprint again; setup is safe to re-run
        ``not_rostered``  nobody is expecting this address
        ``taken``     already enrolled with a different fingerprint
        """
        email = identity.normalise_email(email)
        fingerprint = (fingerprint or "").strip().lower()
        if len(fingerprint) != 64 or any(
                c not in "0123456789abcdef" for c in fingerprint):
            raise RegistryError("fingerprint is not a sha256 hex digest")
        with self._lock:
            self._reload()
            if email not in self._roster:
                return "not_rostered"
            current = self._allowed.get(email) or []
            if fingerprint in [f.lower() for f in current]:
                return "known"
            if current:
                return "taken"
            self._allowed[email] = [fingerprint]
            self._persist()
            return "created"

    def rotate(self, email: str, fingerprint: str) -> str:
        """Add a fingerprint to an identity that already proved it holds one.

        Called by a laptop that authenticated with its current secret, so this
        needs no roster check and no first-use rule -- the caller has already
        demonstrated they are this person. Both are kept, which is what lets a
        rotation finish without the two sides changing in the same minute.
        """
        email = identity.normalise_email(email)
        fingerprint = (fingerprint or "").strip().lower()
        with self._lock:
            self._reload()
            current = [f.lower() for f in (self._allowed.get(email) or [])]
            if not current:
                raise RegistryError("{} is not enrolled".format(email))
            if fingerprint in current:
                return "known"
            self._allowed[email] = [fingerprint] + current[:1]
            self._persist()
            return "rotated"

    def add_person(self, email: str) -> str:
        email = identity.normalise_email(email)
        with self._lock:
            self._reload()
            if email in self._roster:
                return "known"
            self._roster = sorted(set(self._roster) | {email})
            self._persist_roster()
            return "added"

    def remove_person(self, email: str) -> str:
        """Off both lists. Uploads stop at the next attempt.

        What is already stored stays stored -- deleting collected bundles is a
        retention decision, not an access-control one, and doing it silently
        here would hide it.
        """
        email = identity.normalise_email(email)
        with self._lock:
            self._reload()
            found = False
            if email in self._roster:
                self._roster = [p for p in self._roster if p != email]
                self._persist_roster()
                found = True
            if email in self._allowed:
                del self._allowed[email]
                self._persist()
                found = True
            return "removed" if found else "unknown"

    def reset(self, email: str) -> str:
        """Forget the fingerprint, keep the person. For a replacement laptop.

        The address stays on the roster, so the new machine enrols itself the
        next time it runs -- the admin does not have to see a fingerprint.
        """
        email = identity.normalise_email(email)
        with self._lock:
            self._reload()
            if email not in self._allowed:
                return "unknown"
            del self._allowed[email]
            self._persist()
            return "reset"
