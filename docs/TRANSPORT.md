# Transport — handing bundles over without handing them over

`ARCHITECTURE.md` deferred this deliberately: *"a transport with no bundles to
carry is a guess about a format that does not exist yet."* The format exists
now, so this is the transport.

Nothing about the bundle changes. `pack` still seals the same self-describing
NDJSON file, and `importers/bundle.py` still validates it on the way in. All
that changes is who carries the file: a person attaching it, or one `PUT`.

```
laptop                        your proxy                     S3            you
──────                        ──────────                     ──            ───
insight pack  → bundle
insight ship  ──PUT /v1/bundle──►  auth (optional)
                 X-Insight-*        ├ recompute the digest
                 body: .ndjson      ├ choose the key ──────►  PutObject
                                    └ 201 {key, sha256}       (if-none-match)
              ◄── 201 / 409                                        │
                                                                   │
                              GET /v1/bundles?week= ◄──────────────┤
                              Authorization: Bearer  importers/pull.py
                                                            │
                                                     inbox/ → bundle.py → report/
```

---

## Why a proxy and not S3 directly

The laptop client is **standard library only** — `ARCHITECTURE.md` commits to
that, and the reason is the allow-list, not convenience. Talking to S3 directly
would mean hand-writing SigV4 (the HMAC chain, the canonical request,
`x-amz-content-sha256`) in a process that sits on a machine full of source code
and secrets. That is the wrong place to be clever.

With a proxy in front, the client is one `urllib` `PUT`. No SDK, no signing, no
OAuth, no credential to rotate on fourteen laptops.

The proxy also takes a decision away from the client that the client should
never have had:

| | client picks the key | **proxy picks the key** |
|---|---|---|
| One laptop overwrites another's bundle | possible | no |
| One laptop reads the whole team's telemetry | needs `LIST` | never granted |
| Malformed upload reaches storage | yes | rejected at the door |
| Revoke one machine | rotate for everyone | one entry |

---

## Identity: a work email, a fingerprint, and one line of `.env`

At setup the engineer gives their work email. The client mints a random secret,
keeps it, and prints the **one line** to add to the server:

```bash
$ ./insight setup --email canh@seta-international.vn \
      --endpoint https://aeris-insight.seta-international.com

Send this line to whoever maintains the collection server, so they
can add you to INSIGHT_ALLOWED in its .env:

    canh@seta-international.vn:9f2ac41e8b...c3d1

It is a hash, not a secret -- the secret stays in ~/.seta-insight/config.json.
Uploading with `ship` will fail until that line is in place.
```

The server's `.env`, and nothing else:

```bash
INSIGHT_ALLOWED="canh@seta-international.vn:9f2ac41e8b...c3d1,\
                 minh@seta-international.vn:4d7be0a219...88fa"
INSIGHT_ADMIN_TOKEN="<for the read routes>"
```

No UI, no database, no token endpoint. Adding a person is a line; removing one
is deleting that line.

**The direction is the point.** The secret is generated on the laptop and never
transmitted to whoever keeps the whitelist — they only ever receive
`sha256(secret)`. So `INSIGHT_ALLOWED` is not a credential store: a copy of the
server's `.env` lets nobody upload anything, and it can be pasted into a ticket
or committed to a private ops repo without becoming an incident.

The reverse arrangement — admin mints, engineer pastes — puts a live secret
through chat. `insight setup --email X --token T` supports it anyway, because
pre-provisioning a joiner before their laptop exists occasionally needs it. It
is the worse of the two and should stay the exception.

### Rotation without coordination

```bash
$ ./insight rotate-token          # mints the next secret, keeps the old working
$ ./insight rotate-token --finish # after the new line is live, drop the old
```

`INSIGHT_ALLOWED` takes two fingerprints per person — `email:new:old` — and the
client tries the new secret first, falling back to the old on `401`. So the
engineer rotating and the admin editing `.env` never have to happen in the same
minute, and uploads keep working throughout.

That property is the whole reason rotation is worth building. A rotation that
requires scheduling is a rotation that happens once.

### What this does and does not buy

Worth being exact, because overstating it would be worse than not having it.

`ARCHITECTURE.md` declares this data **not an audit trail**: an engineer can
read and edit their own bundle before sending it. That was traded away
deliberately, for consent. Authentication does not take it back — a whitelisted
person can still upload a bundle they edited.

What it does buy:

- only known SETA addresses can write into the bucket
- every object carries **who** sent it, so coverage can name a person rather
  than a machine id
- one person can be removed without touching anyone else
- reading — which exposes the whole team at once — is a separate, admin-only
  credential

**The email never becomes telemetry.** `CONTRACT.md §1.1` forbids raw email
addresses in collected data, and nothing writes one into a bundle: it lives in
`config.json` and the `Authorization` header, both outside the event path.
Objects are filed under `sha256(email)[:12]`, and `pull.py` names people only in
its coverage report on stderr — never in the inbox that feeds `bundle.py`.

That key prefix is key hygiene, not a privacy control, and it is worth saying
so: the `.env` beside it holds every address in plaintext. Its job is to keep
raw addresses out of object keys, access logs, and anything pasted into a
ticket.

---

## The endpoint

`https://aeris-insight.seta-international.com`, stored as `endpoint` in
`~/.seta-insight/config.json` — defaulted by the client, so nothing has to be
typed. The paths below are appended to it.

### `PUT /v1/bundle` — upload

```http
PUT /v1/bundle HTTP/1.1
Content-Type: application/x-ndjson
Content-Length: 40213
X-Insight-Machine: a3f9c2b16d4e8f0a1b2c3d4e5f60718
X-Insight-Window: 2026-08-17/2026-08-23
X-Insight-Schema: 1.0.0
X-Insight-Format: seta-insight-bundle/1
X-Insight-Digest: sha256=1f3a...  ← of the ENTIRE body, all lines
X-Insight-Client: insight-ship/1
Authorization: Bearer <the engineer's secret>

{"_manifest":{...}}
{"event_id":"evt_...",...}
...
```

`X-Insight-Digest` is transport integrity — the whole file, manifest line
included. It is **not** the manifest's own `sha256`, which covers only the event
lines and is checked later by `importers/bundle.py`. Two checksums over two
different ranges, on purpose: one catches a truncated upload, the other catches
a truncated *bundle*, and they fail at different times for different reasons.

**The proxy must:**

1. Resolve `Authorization` against `INSIGHT_ALLOWED`: hash the bearer secret and
   look for that fingerprint. No match → `401`. Match to an address that has
   since been removed → `403`. The two are different on purpose — `ship` retries
   a `401` with an older secret, because that is what a rotation in flight looks
   like, and never retries a `403`.
2. Recompute SHA-256 over the received body. Mismatch with `X-Insight-Digest`
   → `400`, store nothing.
3. Reject a body over a sane cap (1 MiB is generous; see *Sizing*) → `413`.
4. Reject a `X-Insight-Schema` it does not know → `400`. A schema bump is a
   pipeline change, and silently storing the future is how you find out in
   three months.
5. Require `X-Insight-Window` to be two dates. The client refuses to send an
   undeclared window, but the proxy should not depend on the client being the
   one we shipped.
6. **Choose the object key itself**, from the *authenticated* identity and the
   headers — never from anything the client can name:

   ```
   bundles/<iso-week-of-window-start>/<sha256(email)[:12]>/<machine[:8]>-<digest>.ndjson
   e.g. bundles/2026-W34/8c1d40b9e2af/a3f9c2b1-1f3a9c....ndjson
   ```

   Filed under the person, not the machine, so someone with a laptop and a
   desktop is one line of coverage rather than two. The machine id stays in the
   file name, because two machines in one week is worth being able to see.

7. `PutObject` with `IfNoneMatch: "*"` so an existing key is never overwritten.

**Responses:**

| Status | Meaning | Body |
|---|---|---|
| `201` | stored | `{"key":"...","sha256":"...","bytes":40213}` |
| `409` | this exact bundle is already stored | same shape |
| `400` | digest mismatch, bad window, unknown schema | `{"error":"..."}` |
| `401` | secret not in `INSIGHT_ALLOWED` — **`ship` retries with its previous secret** | `{"error":"..."}` |
| `403` | known address, no longer allowed — `ship` stops | `{"error":"..."}` |
| `413` | too large | `{"error":"..."}` |

`409` is a **success** to the client, not a failure — `ship` reports it as
*"already handed over"*. Re-running `ship` must be safe, because someone will,
and a duplicate week is a duplicate week even with `event_id` dedupe behind it.

Because the key is the content digest and writes are `IfNoneMatch`, idempotency
falls out of the storage semantics. There is no dedupe logic to write and none
to get wrong.

### `GET /v1/bundles?week=YYYY-Www` — list a week

```http
GET /v1/bundles?week=2026-W34
Authorization: Bearer <admin secret>
```

```json
{"week": "2026-W34",
 "objects": [{"key": "bundles/2026-W34/8c1d40b9e2af/a3f9c2b1-1f3a9c....ndjson",
              "email": "canh@seta-international.vn",
              "machine": "a3f9c2b1",
              "bytes": 40213,
              "uploaded_at": "2026-08-24T09:12:44Z"}]}
```

`email` is resolved from `INSIGHT_ALLOWED` by reversing the key prefix, and is
returned only here — on the admin-authenticated route. It is what lets
`pull.py` report *missing: lan@seta-international.vn* instead of a machine id
nobody recognises. A proxy that omits it still works; coverage falls back to
machine ids.

### `GET /v1/bundle/<key>` — fetch one

```http
GET /v1/bundle/bundles/2026-W34/8c1d40b9e2af/a3f9c2b1-1f3a9c....ndjson
Authorization: Bearer <admin secret>
```

Returns the raw NDJSON. `importers/pull.py` writes it into `inbox/` and
`importers/bundle.py` takes it from there, unchanged.

Both `GET` routes **must** reject a missing or wrong bearer token with `401`.
That secret lives on your machine, never on a laptop.

---

## Where validation lives, and why not in the proxy

The proxy checks only what is cheap and structural: digest, size, schema
version, window shape. It does **not** check the attribute allow-list.

That is deliberate. The allow-list is `collector/main.py`'s
`ATTRIBUTE_ALLOWLIST`, and `importers/bundle.py` already re-checks every event
against it on import — deliberately, because *"a bundle is a file a person can
edit."* Reimplementing that check in the proxy's language would put two copies
of the one rule that keeps prompts and source code out of the warehouse in two
places, in two languages, drifting apart. `ARCHITECTURE.md` rejects a second
implementation of the contract for exactly this reason, and the argument does
not weaken because the second implementation is small.

So a bad bundle reaches S3 and is rejected at import, loudly and whole. That is
the same failure it would have had arriving by email, which is what the import
path was already built for.

---

## The thing automation makes worse

With manual handover, a missing week is visible: no email arrived. Automate it
and silence becomes ambiguous — did nobody work, or did nobody upload?

This is the failure mode `ARCHITECTURE.md` warns about most strongly, and
transport makes it *more* likely, not less. So `pull.py` takes a roster and
reports coverage against it:

```
$ python3 importers/pull.py --week 2026-W34 --inbox inbox/ --roster roster.txt
2026-W34: 11 of 14 machines
missing: 4c8d1104, 7b1e0092, a0f13bb7
```

Those three are **no data**, never `0`. `bundle.py`'s `coverage_report()`
already carries that distinction through to the report; the roster is what lets
it name a machine that has never sent anything at all, which coverage derived
from arrived bundles cannot do by construction.

Keep `roster.txt` in your central checkout, not in S3. It is the list of who is
expected to report, which is an organisational fact, not telemetry.

---

## Sizing, cost, retention

A contract event is roughly 300–600 bytes. A busy week for one engineer is
low thousands of events, so bundles run from a few KB to a couple of MB.
Fourteen engineers for a year is comfortably under 2 GB — a rounding error on
S3, and well inside a single-`PUT` object.

**Retention is an S3 lifecycle rule**, server-side, and the client is never
given `DeleteObject`. `insight purge` stays what it always was: a *local*
command. A bundle already handed over has already been handed over, and the
tool should not imply otherwise — that honesty is part of the consent model,
not a limitation of it.

---

## Deploying the proxy

Roughly eighty lines wherever you like — Lambda + Function URL, a container,
Express behind nginx. The shape:

```python
ALLOWED = parse_whitelist(env["INSIGHT_ALLOWED"])   # {email: [fp, fp_prev]}
ADMIN   = env["INSIGHT_ADMIN_TOKEN"]

on PUT /v1/bundle:
    email = identify(bearer_secret(request), ALLOWED)   # sha256 + constant-time
    if email is None:                                          → 401
    body   = read(limit = 1 MiB)                               → 413 if over
    digest = sha256(body)
    assert digest == header("X-Insight-Digest").removeprefix("sha256=")   → 400
    assert header("X-Insight-Schema") in KNOWN_SCHEMAS                    → 400
    start, end = header("X-Insight-Window").split("/")                    → 400

    key = "bundles/{}/{}/{}-{}.ndjson".format(
        iso_week(start), sha256(email)[:12],
        header("X-Insight-Machine")[:8], digest)

    try:    s3.put_object(Key=key, Body=body, IfNoneMatch="*")
    except PreconditionFailed:  return 409, {key, sha256: digest, bytes: len(body)}
    return 201, {key, sha256: digest, bytes: len(body)}

on GET /v1/bundles, GET /v1/bundle/*:
    assert constant_time_eq(bearer, ADMIN)                                → 401
    # reverse sha256(email)[:12] back to an address using ALLOWED, so the
    # listing can name people; the bucket itself never holds one.
```

`cli/identity.py` already implements `parse_whitelist`, `fingerprint`,
`identify` and `person_key`, with tests. If the proxy is Python, import it
rather than reimplementing — the whole argument in *Where validation lives*
applies here too, and a whitelist parser that disagrees with the client about
what `email:fp:fp` means fails in the least debuggable way available.

The IAM role wants `s3:PutObject` for the write path and `s3:GetObject` +
`s3:ListBucket` for the read path. Nothing wants `s3:DeleteObject` — retention
is a lifecycle rule, not a code path.

### Ports: one public, one on loopback

The proxy listens on **`127.0.0.1:8479`** and is never bound to a public
interface. Nginx or Caddy terminates TLS on **443** and is the only thing
exposed.

`8479` is above 1024 so the proxy needs no root, is unassigned in
`/etc/services`, collides with nothing in the observability stack this project
already touches (OTLP `4317`/`4318`, Prometheus `9090`/`9464`, Zipkin `9411`,
Jaeger `16686`), and sits well below Linux's ephemeral range of
**32768–60999** — binding inside that range is the mistake that produces a
service which fails to start perhaps one boot in fifty, when an outbound
connection got there first.

**Split the routes by exposure, not just by token.** Uploading has to be
reachable from fourteen laptops; reading exposes the whole team at once and is
done from one machine, by one person, who already has SSH.

```nginx
server {
    listen 443 ssl;
    server_name aeris-insight.seta-international.com;

    # The only route the internet can reach.
    location = /v1/bundle {
        proxy_pass http://127.0.0.1:8479;
        client_max_body_size 1m;          # matches the client's own cap
    }

    # Reading is not a public route at all.
    location /v1/ { return 404; }
}
```

So the read routes exist only on loopback, and `pull.py` reaches them through an
SSH tunnel:

```bash
ssh -N -L 8479:127.0.0.1:8479 aeris-insight.seta-international.com &

INSIGHT_ENDPOINT=http://127.0.0.1:8479 INSIGHT_ADMIN_TOKEN=... \
    python3 importers/pull.py --week 2026-W34 --inbox inbox/ --roster roster.txt
```

`INSIGHT_ADMIN_TOKEN` still guards them, but it becomes defence in depth rather
than the only thing between the internet and every engineer's telemetry. A
leaked admin token off a laptop is then not enough on its own — which is worth
having, because that token travels in a shell history and an `.env` on a
workstation, and the whitelist it protects cannot be rotated by the people it
describes.

That is what "one port" buys: **443 public, 22 for the person who runs the
pipeline, and nothing else.**

### Turning it on

Each engineer, once:

```bash
./insight setup --repo ~/work/repo-one --email canh@seta-international.vn
```

`https://aeris-insight.seta-international.com` is the built-in default, so
`--endpoint` is only for a staging proxy and `--no-endpoint` opts out into
handing bundles over by hand. An engineer who is never told a flag exists
collects diligently for a month and then finds out nothing ever arrived; a
default is the fix for that.

then sends the printed line for `INSIGHT_ALLOWED`. `./insight whoami` prints it
again whenever it scrolls away.

Thereafter the weekly ritual gains one line:

```bash
./insight otel && ./insight collect && ./insight scan
./insight pack --since 2026-08-17 --until 2026-08-23
./insight ship
```

And centrally, once a week:

```bash
ssh -N -L 8479:127.0.0.1:8479 aeris-insight.seta-international.com &
export INSIGHT_ENDPOINT=http://127.0.0.1:8479
export INSIGHT_ADMIN_TOKEN=...

python3 importers/pull.py --week 2026-W34 --inbox inbox/ --roster roster.txt
python3 importers/bundle.py --inbox inbox/ --out events.ndjson \
    --state state/bundles.json --coverage-out reports/coverage.json
```

`pull.py` prints who did not report. That line is the one to read before the
numbers.
