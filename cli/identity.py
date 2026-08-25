#!/usr/bin/env python3
"""Who is uploading, and the secret that proves it.

The engineer types their work email once at setup. This mints a random secret
that stays on the laptop and derives a **fingerprint** -- ``sha256`` of the
secret -- which is what goes into the server's ``.env`` whitelist. One line per
person, no UI, no database.

    canh@seta-international.vn:9f2a...c41

The direction matters. The secret is generated here and never transmitted to
whoever maintains the whitelist; they only ever hold a hash of it. So the
whitelist is not a credential store, and a copy of the server's ``.env`` lets
nobody upload anything. Distributing tokens the other way -- admin mints, engineer
pastes -- puts the live secret through Slack, and is supported only because
pre-provisioning a new joiner sometimes needs it.

**The email is transport identity and never becomes telemetry.**
``CONTRACT.md 1.1`` forbids raw email addresses in collected data, and nothing
here writes one into a bundle: it lives in ``config.json`` and in the
``Authorization`` header, both outside the event path. What it buys is a
coverage report that can say *minh@seta-international.vn did not report this
week* instead of *4c8d1104 did not report this week*, which is the difference
between a number and something someone can act on.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Dict, List, Optional, Tuple

#: Deliberately loose. The server's whitelist decides who may upload; this only
#: catches a typo before it becomes a 403 nobody can explain.
EMAIL_RE = re.compile(r"^[^@\s,:;]+@[^@\s,:;]+\.[^@\s,:;]+$")

#: 32 bytes from the OS CSPRNG, base64url. Long enough that the fingerprint in
#: a leaked .env is not worth attacking.
SECRET_BYTES = 32


class IdentityError(Exception):
    """A setup that would produce an unusable or ambiguous identity."""


def normalise_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise IdentityError(
            "{!r} does not look like a work email address".format(email))
    return email


def mint_secret() -> str:
    return secrets.token_urlsafe(SECRET_BYTES)


def fingerprint(secret: str) -> str:
    """What the server stores. Never the secret itself."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def person_key(email: str) -> str:
    """The stable, opaque directory a person's bundles are filed under.

    An obfuscation for key hygiene, not a privacy control, and worth saying so
    plainly: the whitelist sitting beside it in the same ``.env`` holds every
    address in plaintext, so anyone who can read the bucket layout can already
    read the names. Its actual job is to keep raw addresses out of object keys,
    log lines and anything that gets pasted into a ticket.
    """
    return hashlib.sha256(normalise_email(email).encode("utf-8")).hexdigest()[:12]


def whitelist_line(email: str, secret: str) -> str:
    """The one line to hand to whoever maintains the server's ``.env``."""
    return "{}:{}".format(normalise_email(email), fingerprint(secret))


def parse_whitelist(raw: str) -> Dict[str, List[str]]:
    """Read ``INSIGHT_ALLOWED`` -- the server half, kept here so both sides agree.

    ``email:fp[:fp2],email:fp,...``

    A second fingerprint is a rotation in progress. Both are accepted at once so
    a rotation never needs the engineer and the ``.env`` to change in the same
    minute, which is the coordination that makes people avoid rotating at all.
    """
    allowed: Dict[str, List[str]] = {}
    for entry in (raw or "").replace("\n", ",").split(","):
        entry = entry.strip()
        if not entry or entry.startswith("#"):
            continue
        parts = [p.strip() for p in entry.split(":") if p.strip()]
        if len(parts) < 2:
            raise IdentityError(
                "whitelist entry {!r} is not email:fingerprint".format(entry))
        allowed[parts[0].lower()] = parts[1:]
    return allowed


def identify(secret: str, allowed: Dict[str, List[str]]) -> Optional[str]:
    """Which whitelisted person holds this secret, if any.

    Compared with ``compare_digest`` over hex of a fixed length, so a wrong
    secret takes the same time as a right one.
    """
    if not secret:
        return None
    given = fingerprint(secret)
    for email, fingerprints in allowed.items():
        for known in fingerprints:
            if secrets.compare_digest(given, known.lower()):
                return email
    return None


def rotate(config: Dict[str, object]) -> Tuple[str, str]:
    """Mint the next secret, keeping the current one usable.

    Returns ``(new_secret, previous_secret)``. The old secret is retained rather
    than replaced because the ``.env`` it is checked against is edited by a
    different person at a different time. ``ship`` tries the new one first and
    falls back, so uploading keeps working throughout the window and nobody has
    to be told to stop.
    """
    previous = str(config.get("endpoint_token") or "")
    return mint_secret(), previous
