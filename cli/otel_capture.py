#!/usr/bin/env python3
"""Capture what Copilot's OTel exporter actually sends. Verification only.

    python3 cli/otel_capture.py --port 4418 --out ~/.seta-insight/otel-raw.ndjson

Copilot's exporter is configured but nothing has ever listened on the endpoint,
so every span it emitted went nowhere. This listens, writes each payload down
verbatim, and prints a one-line summary as it arrives.

**This is not part of the collector.** `./insight` has no daemon by design. This
exists to answer one question -- what does the exporter actually send, in what
shape -- because the alternative is writing a parser against a guessed format,
and a parser written against a guess reads nothing while appearing to work.
Once the shape is known, `pack` reads it as a file and this can be deleted.

It binds to loopback only and speaks no protocol of its own: it accepts any
POST, records the body, and answers 200. That is deliberate. A strict OTLP
implementation would reject a payload we do not yet understand, which is exactly
the payload worth seeing.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"count": 0, "out": None, "quiet": False}


def summarise(payload):
    """Pull out the few things worth seeing at a glance, tolerating any shape."""
    spans, names, attrs = 0, [], set()
    try:
        for resource in payload.get("resourceSpans", []):
            for scope in resource.get("scopeSpans", []):
                for span in scope.get("spans", []):
                    spans += 1
                    if span.get("name") and span["name"] not in names:
                        names.append(span["name"])
                    for attribute in span.get("attributes", []):
                        attrs.add(attribute.get("key"))
    except AttributeError:
        return {"spans": 0, "names": [], "attribute_keys": []}
    return {"spans": spans, "names": names[:6],
            "attribute_keys": sorted(k for k in attrs if k)}


class Handler(BaseHTTPRequestHandler):

    def read_body(self):
        """Read the body whether it is length-delimited or chunked.

        Reading only Content-Length loses the whole payload when the sender
        streams it -- there is no header, so it looks like a zero-byte POST and
        reads as "the exporter sent nothing". That is a much more misleading
        answer than an error would have been.
        """
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            chunks = []
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    break
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline()  # trailing CRLF
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()  # CRLF after each chunk
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
        body = self.read_body()
        if self.headers.get("Content-Encoding") == "gzip":
            try:
                body = gzip.decompress(body)
            except OSError:
                pass

        record = {
            "received_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "path": self.path,
            "content_type": self.headers.get("Content-Type"),
            # Every header, because this exists to find out what the exporter
            # actually does and the interesting one is always the one nobody
            # thought to record.
            "headers": {k: v for k, v in self.headers.items()},
            "bytes": len(body),
        }
        if not body:
            # An empty POST is the exporter reaching out with nothing to send --
            # a heartbeat, not a failure. Labelling it "binary" (which the
            # decode path below would do) reads like a protocol we cannot parse
            # and sends the next person hunting for a protobuf decoder.
            record["encoding"] = "empty"
        else:
            try:
                record["payload"] = json.loads(body.decode("utf-8"))
                record["encoding"] = "json"
            except (UnicodeDecodeError, ValueError):
                # Protobuf, most likely. Record its size and the first bytes
                # rather than dropping it -- "it arrived and was binary" is
                # itself the answer to a question we are asking.
                record["encoding"] = "binary"
                record["head_hex"] = body[:64].hex()

        STATE["count"] += 1
        with open(STATE["out"], "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

        if not STATE["quiet"]:
            line = {"n": STATE["count"], "path": self.path,
                    "bytes": len(body), "encoding": record["encoding"]}
            if record["encoding"] == "json":
                line.update(summarise(record["payload"]))
            print(json.dumps(line, sort_keys=True), flush=True)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"otel-capture listening\n")

    def log_message(self, *args):
        pass  # the summary line above is the log


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Record what Copilot's OTel exporter sends. Verification only.")
    parser.add_argument("--port", type=int, default=4418)
    parser.add_argument("--out", default=os.path.join(
        os.path.expanduser("~"), ".seta-insight", "otel-raw.ndjson"))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    STATE["out"] = os.path.abspath(os.path.expanduser(args.out))
    STATE["quiet"] = args.quiet
    os.makedirs(os.path.dirname(STATE["out"]), exist_ok=True)

    # Loopback only. This accepts anything posted to it and writes it to disk;
    # that is fine from the machine's own browser and not fine from the network.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(json.dumps({"msg": "listening", "port": args.port,
                      "out": STATE["out"]}, sort_keys=True), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(json.dumps({"msg": "stopped", "payloads": STATE["count"]}),
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
