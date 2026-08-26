#!/usr/bin/env python3
"""Where bundles land: S3, or a directory.

Two backends behind one interface, chosen by URL:

    s3://bucket/prefix      production
    file:///var/lib/insight local, and the whole proxy before AWS exists

The filesystem backend is not a toy. It is how the proxy gets exercised
end-to-end -- every route, every status code, the write-once rule -- without a
bucket, a role, or a credential. A server nobody can run until the credentials
arrive is a server nobody has tested.

**Write-once is the contract, not a convention.** ``put`` refuses to overwrite,
and both backends enforce it: S3 with a conditional write, the filesystem with
``O_EXCL``. Object keys are content digests (``server/README.md``), so a
refusal means "this exact bundle is already here" -- which is the answer the
client wants and reports as *already handed over*. Nothing in this module can
delete; retention is a lifecycle rule and deletion is not a code path.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


class StoreError(Exception):
    """The object was not written, and no caller should report that it was."""


class Exists(Exception):
    """The key is taken. Not an error -- the reason the client sees ``409``."""


# --------------------------------------------------------------------------

class FileStore:
    """A directory. Used for local runs and for the whole test suite."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def _path(self, key: str) -> str:
        # Keys are built by the proxy from an authenticated identity and a
        # digest, never from client input -- but this module cannot see that, and
        # a path traversal here would write anywhere the process can reach.
        full = os.path.abspath(os.path.join(self.root, key))
        if full != self.root and not full.startswith(self.root + os.sep):
            raise StoreError("key escapes the store root: {!r}".format(key))
        return full

    def put(self, key: str, body: bytes, metadata: Optional[Dict[str, str]] = None) -> None:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
        except FileExistsError:
            raise Exists(key)
        except OSError as exc:
            raise StoreError("cannot write {}: {}".format(key, exc))
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(body)
        except OSError as exc:
            # A half-written bundle would fail its checksum at import, which is
            # the right outcome but a confusing one. Remove it instead.
            try:
                os.remove(path)
            except OSError:
                pass
            raise StoreError("cannot write {}: {}".format(key, exc))
        if metadata:
            try:
                with open(path + ".meta", "w", encoding="utf-8") as meta:
                    for name, value in sorted(metadata.items()):
                        meta.write("{}={}\n".format(name, value))
            except OSError:
                pass  # metadata is a convenience; the bundle is the record

    def get(self, key: str) -> bytes:
        try:
            with open(self._path(key), "rb") as handle:
                return handle.read()
        except FileNotFoundError:
            raise KeyError(key)
        except OSError as exc:
            raise StoreError("cannot read {}: {}".format(key, exc))

    def list(self, prefix: str) -> List[Dict[str, Any]]:
        root = self._path(prefix)
        found: List[Dict[str, Any]] = []
        for base, _, names in os.walk(root):
            for name in sorted(names):
                if name.endswith(".meta"):
                    continue
                path = os.path.join(base, name)
                stat = os.stat(path)
                found.append({
                    "key": os.path.relpath(path, self.root).replace(os.sep, "/"),
                    "bytes": stat.st_size,
                    "uploaded_at": _rfc3339(stat.st_mtime),
                })
        return sorted(found, key=lambda o: o["key"])


def _boto_errors() -> tuple:
    """Every way botocore fails, as one tuple.

    ``ClientError`` is what S3 *answers*; ``BotoCoreError`` is what happens
    before it is ever asked -- no credentials, no region, DNS, connect timeout.
    They do not share a base class, and catching only the first turns a missing
    credential into an unhandled traceback: a 500 from the endpoint, where 503
    is the truth and the one that makes `ship` retry instead of giving up.
    """
    from botocore.exceptions import BotoCoreError, ClientError  # noqa: PLC0415
    return (ClientError, BotoCoreError)


class S3Store:
    """A bucket. boto3 is the one wheel this service needs."""

    def __init__(self, bucket: str, prefix: str = "", client: Any = None):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is not None:
            self.client = client
        else:
            try:
                import boto3  # noqa: PLC0415 -- optional, and only for S3
            except ImportError:
                raise StoreError(
                    "s3:// needs boto3 -- `pip install -r server/requirements.txt`, "
                    "or run against file:// until the bucket exists")
            try:
                self.client = boto3.client("s3")
            except Exception as exc:      # noqa: BLE001 -- e.g. NoRegionError
                raise StoreError(
                    "cannot create an S3 client: {} -- set AWS_REGION and make "
                    "credentials available (an instance role is preferred)"
                    .format(exc))
        #: Conditional writes are recent, and S3-compatible stores vary. Probed
        #: once on first refusal rather than assumed, so the write-once
        #: guarantee degrades loudly instead of silently.
        self._conditional = True

    def _full(self, key: str) -> str:
        return "{}/{}".format(self.prefix, key) if self.prefix else key

    def put(self, key: str, body: bytes, metadata: Optional[Dict[str, str]] = None) -> None:
        errors = _boto_errors()

        args: Dict[str, Any] = {
            "Bucket": self.bucket, "Key": self._full(key), "Body": body,
            "ContentType": "application/x-ndjson",
        }
        if metadata:
            args["Metadata"] = metadata
        if self._conditional:
            args["IfNoneMatch"] = "*"
        elif self.exists(key):
            # Check-then-write, on every write and not only on the one that
            # discovered the store lacks conditional writes. Skipping it here
            # would not make write-once racy, it would switch it off.
            raise Exists(key)

        try:
            self.client.put_object(**args)
            return
        except errors as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in ("PreconditionFailed", "ConditionalRequestConflict", "412"):
                raise Exists(key)
            if code in ("NotImplemented", "InvalidArgument") and self._conditional:
                # An S3-compatible store without conditional writes. Fall back to
                # a check-then-write, and say so: it is racy, and two uploads of
                # the same bundle in the same instant could both be written. They
                # would be byte-identical -- the key is their digest -- so the cost
                # is a duplicate object, not wrong data.
                self._conditional = False
                if self.exists(key):
                    raise Exists(key)
                args.pop("IfNoneMatch", None)
                try:
                    self.client.put_object(**args)
                    return
                except errors as retry:
                    raise StoreError("cannot write {}: {}".format(key, retry))
            raise StoreError("cannot write {}: {}".format(key, exc))

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._full(key))
            return True
        except _boto_errors():
            return False

    def get(self, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._full(key))
            return response["Body"].read()
        except _boto_errors() as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NotFound"):
                raise KeyError(key)
            raise StoreError("cannot read {}: {}".format(key, exc))

    def list(self, prefix: str) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []
        token = None
        try:
            while True:
                kwargs: Dict[str, Any] = {
                    "Bucket": self.bucket, "Prefix": self._full(prefix)}
                if token:
                    kwargs["ContinuationToken"] = token
                page = self.client.list_objects_v2(**kwargs)
                for item in page.get("Contents") or []:
                    key = item["Key"]
                    if self.prefix and key.startswith(self.prefix + "/"):
                        key = key[len(self.prefix) + 1:]
                    found.append({
                        "key": key,
                        "bytes": item.get("Size", 0),
                        "uploaded_at": _stamp(item.get("LastModified")),
                    })
                if not page.get("IsTruncated"):
                    break
                token = page.get("NextContinuationToken")
        except _boto_errors() as exc:
            raise StoreError("cannot list {}: {}".format(prefix, exc))
        return sorted(found, key=lambda o: o["key"])


# --------------------------------------------------------------------------

def open_store(url: str, client: Any = None) -> Any:
    """``s3://bucket/prefix`` or ``file:///path`` (or a bare path)."""
    parsed = urlparse(url)
    if parsed.scheme == "s3":
        if not parsed.netloc:
            raise StoreError("s3:// needs a bucket: {!r}".format(url))
        return S3Store(parsed.netloc, parsed.path, client=client)
    if parsed.scheme == "file":
        return FileStore(parsed.path or "/")
    if not parsed.scheme:
        return FileStore(url)
    raise StoreError("unsupported store {!r} -- use s3:// or file://".format(url))


def _rfc3339(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp(value: Any) -> str:
    if value is None:
        return ""
    try:
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    except AttributeError:
        return str(value)

