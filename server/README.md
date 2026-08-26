# The collection endpoint

Laptops `PUT` bundles here; this owns the S3 credentials so they never leave the server.

```bash
# Run it now, with no AWS account, and point a client at it:
INSIGHT_ADMIN_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))") \
python3 server/proxy.py --store file:///tmp/insight --allowed-file allowed.env
```

`allowed.env` is one line per engineer, exactly what `./insight whoami` prints.

## Why it exists at all

The client is standard-library only, and the reason is the attribute allow-list,
not convenience. Talking to S3 directly would mean hand-writing SigV4 in a
process that sits on a machine full of source code and secrets. With this in
front, the client is one `urllib` `PUT`.

It also takes a decision away from the client that the client should never have
had: **the object key is built here**, from the authenticated identity. A client
that names its own key can overwrite someone else's bundle.

## What it does not do

It does not check the attribute allow-list. That lives in `collector/main.py`
and is re-checked by `importers/bundle.py` at import, which is where a bundle a
person could have edited actually gets read. A second copy of the one rule
keeping prompts and source code out of the warehouse is precisely what
`ARCHITECTURE.md` refuses.

For the same reason this imports `cli/identity.py` rather than reimplementing
the whitelist. The client mints the secret and derives the fingerprint; this
checks it with the same function, so the two cannot disagree about what
`email:fp:fp` means.

## Configuration

| | |
|---|---|
| `INSIGHT_STORE` | `s3://bucket/prefix` or `file:///path` (or `--store`) |
| `INSIGHT_ADMIN_TOKEN` | **required** — guards the read routes |
| `INSIGHT_ADMIN_TOKEN_FILE` | the same token, in a file. Wins over the variable |
| `INSIGHT_ALLOWED` | `email:fp[:fp],...` |
| `INSIGHT_ALLOWED_FILE` | the same, one line per engineer — easier to edit |
| `INSIGHT_HOST` | default `127.0.0.1`; nginx is what faces the internet |
| `INSIGHT_PORT` | default `8479` — uploads *and* the read routes |
| `INSIGHT_UPLOAD_PORT` | optional second listener: uploads and `/healthz` only |
| `AWS_REGION`, credentials | standard boto3 resolution — prefer an instance role |

The admin token is required with no default. The read routes expose every
engineer at once and are never left open by accident.

### Reading from outside the office

The read routes are not published by default, because they expose every
engineer at once and `INSIGHT_ADMIN_TOKEN` would be the only thing in the way.
`server/compose.yaml` publishes the upload-only listener; the full one is
reached over SSH.

Where that is not workable — the weekly pull is done from a laptop that is
often not on the office network, and no identity proxy is available in front —
set `INSIGHT_PUBLISHED_PORT=8479` and restart. **This was chosen deliberately
for this deployment, 2026-08-26.** What it means, stated plainly so nobody has
to rediscover it:

- The admin token becomes the single credential protecting the whole team's
  telemetry and their work email addresses.
- It travels in shell history and in whatever `.env` the puller keeps. Treat it
  the way you would treat a production database password.
- Rotate it whenever it has been echoed, pasted or logged — `python3 -c "import
  secrets;print(secrets.token_urlsafe(32))" > /etc/insight/admin.token` and
  restart. Nothing else needs changing; upload secrets are unaffected.
- Reversible at any time: set it back to `8480` and restart.

The better answer, when it becomes available, is an identity proxy on the read
paths — Cloudflare Access or equivalent — which puts a real login in front and
leaves the admin token as a second layer rather than the only one.

`INSIGHT_UPLOAD_PORT` is for the case where the thing terminating TLS is on
**another machine**. It cannot reach a container network, so it has to be given
a host port — and its configuration, not yours, decides which paths it
forwards. If it forwards all of them, the route split becomes a token check
instead of a network boundary. Publish this port and the process enforces the
split itself: `/v1/bundle` and `/healthz` are served, everything else is `404`
regardless of credential.

Prefer `INSIGHT_ADMIN_TOKEN_FILE` in a container. `docker inspect` prints a
container's environment to anyone who can reach the Docker daemon, which on a
shared host is a wider audience than root; a mounted, root-owned file is not
visible that way. Same reasoning as `INSIGHT_ALLOWED_FILE` next to it.

## Deploying

Two supported shapes. Pick by what already owns port 443 on the host.

### A machine dedicated to this — systemd and nginx

```bash
sudo useradd --system insight
sudo git clone git@github.com:seta-canhta/usage-insight.git /opt/usage-insight
sudo python3 -m pip install -r /opt/usage-insight/server/requirements.txt

sudo install -d -m 700 -o root -g root /etc/insight
sudo tee /etc/insight/proxy.env >/dev/null <<'ENV'
INSIGHT_STORE=s3://aeris-insight
INSIGHT_ADMIN_TOKEN=<python3 -c "import secrets;print(secrets.token_urlsafe(32))">
INSIGHT_ALLOWED_FILE=/etc/insight/allowed.env
AWS_REGION=ap-southeast-1
ENV
sudo chmod 600 /etc/insight/proxy.env

sudo cp /opt/usage-insight/server/insight-proxy.service /etc/systemd/system/
sudo systemctl enable --now insight-proxy

sudo cp /opt/usage-insight/server/nginx.conf.example \
        /etc/nginx/sites-available/insight
sudo ln -s /etc/nginx/sites-available/insight /etc/nginx/sites-enabled/
sudo certbot --nginx -d aeris-insight.seta-international.com
sudo nginx -t && sudo systemctl reload nginx
```

### A host that already runs something — Docker and Traefik

Most hosts that are available are hosts that already do something, and on those
443 is taken and `sudo` may not be. `server/compose.yaml` needs neither: the
Docker group is enough, and the endpoint publishes **no host port at all** —
only the reverse proxy's container can reach it, which is the same guarantee
`INSIGHT_HOST=127.0.0.1` gives the systemd deployment.

```bash
mkdir -p ~/aeris-insight/{src,etc,data,state} && chmod 700 ~/aeris-insight
# put the repo in ./src -- git clone, or tar it across

cat > ~/aeris-insight/.env <<'ENV'
INSIGHT_SRC=./src
INSIGHT_CONF=./etc
INSIGHT_DATA=./data
INSIGHT_STATE=./state
INSIGHT_EDGE_NETWORK=<the reverse proxy's docker network>
INSIGHT_STORE=file:///var/lib/insight     # or s3://aeris-insight
AWS_REGION=ap-southeast-1
# Only if the TLS terminator is on another machine. LAN interface, not a bare
# port -- and it maps to the upload-only listener, not to 8479.
# INSIGHT_PUBLISH=192.168.x.y:8479
ENV

python3 -c "import secrets;print(secrets.token_urlsafe(32))" > ~/aeris-insight/etc/admin.token
# etc/allowed.env  -- one `./insight whoami` line per engineer
# etc/roster.txt   -- the same people, for the watchdog

# The image runs as uid 10001 and the config is mounted read-only, so it has to
# be readable by that id. No sudo needed to do it -- Docker is already root.
docker run --rm -v ~/aeris-insight/etc:/x -v ~/aeris-insight/data:/d \
          -v ~/aeris-insight/state:/s alpine:3 sh -c \
  'chgrp -R 10001 /x && chmod 640 /x/* && chown -R 10001:10001 /d /s'

cd ~/aeris-insight
docker compose -f src/server/compose.yaml \
               --project-directory ~/aeris-insight --env-file .env up -d --build
```

Then publish it. `server/traefik.insight.yml.example` is the Traefik equivalent
of `nginx.conf.example` and splits the same way — uploads and `/healthz` public,
the read routes restricted by source address:

```bash
cp server/traefik.insight.yml.example <traefik-dynamic-dir>/insight.yml
```

Traefik's file provider watches that directory, so there is nothing to restart.
Keep the copy in this repo authoritative: on a host where the dynamic directory
lives inside a CI checkout, a redeploy can remove it, and restoring it is one
`cp`.

Adding an engineer is one line appended to the whitelist and a restart
(`systemctl restart insight-proxy`, or `docker compose restart insight-proxy`).
Removing one is deleting that line.

### The watchdog

`server/compose.yaml` also runs `importers/watch.py` on an hourly loop, from
the same image with a different entrypoint. It belongs with the endpoint rather
than in a separate deployment: it is the piece that notices collection has
stopped, and deployed separately it is the piece most likely to be the one
nobody deployed. It reads the endpoint over the Docker network, so the read
routes it uses stay off the internet.

Working hours, the miss threshold and the ntfy topic are environment variables;
see `.env.example`. `--dry-run` prints what it would send.

## The bucket

```
bucket   aeris-insight
region   ap-southeast-1
arn      arn:aws:s3:::aeris-insight
store    s3://aeris-insight        ← no prefix; see below
```

**No prefix.** Object keys already begin with `bundles/`, so `s3://aeris-insight/bundles`
would file everything under `bundles/bundles/…`.

### IAM

Three actions, scoped to this bucket, and **no `s3:DeleteObject`** — retention is
a lifecycle rule and deletion is not a code path anywhere in this service:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "WriteAndReadBundles",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject"],
      "Resource": "arn:aws:s3:::aeris-insight/*"
    },
    {
      "Sid": "ListForPull",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::aeris-insight"
    }
  ]
}
```

`s3:ListBucket` is on the bucket ARN, the other two on `/*`. Getting that pair
the wrong way round is the usual reason a listing returns `AccessDenied` while
uploads work fine.

Prefer an **instance role** over keys. The whole point of the proxy is that no
long-lived S3 credential exists anywhere a laptop can reach.

### Hardening and retention

```bash
aws s3api put-public-access-block --bucket aeris-insight \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws s3api put-bucket-encryption --bucket aeris-insight \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

# Retention is here, not in code. Bundles are KBs, so this is about the
# promise made to engineers rather than about storage cost.
aws s3api put-bucket-lifecycle-configuration --bucket aeris-insight \
    --lifecycle-configuration '{"Rules":[
      {"ID":"expire-bundles","Status":"Enabled",
       "Filter":{"Prefix":"bundles/"},
       "Expiration":{"Days":400}}]}'
```

400 days keeps a full year of week-over-week comparison plus a margin. Change
the number to match what was promised at consent; do not change it to match
what is convenient later.

### Write-once

`put_object` uses `IfNoneMatch: "*"`, so an existing key is never overwritten.
On an S3-compatible store without conditional writes the service falls back to
check-then-write on **every** write and logs that it did — racy, but the key is
the content digest, so the worst case is a duplicate object rather than wrong
data.

### Verified against the real bucket, 2026-08-25

Run end to end against `aeris-insight` in `ap-southeast-1` with a real
credential, not a stub:

| | |
|---|---|
| `ListBucket`, `PutObject`, `GetObject` | pass |
| **write-once (`IfNoneMatch`)** | **refused conditionally** — real S3 honours it |
| byte-for-byte round trip | pass |
| public access block | all four settings on |
| two engineers ship → `201`, resend → `409` | pass |
| `pull.py` → `bundle.py` | 6 events, 0 rejected, 0 email addresses in the event path |

The conditional write was the one thing no stub could settle, and the one that
loses data silently if it is not honoured. It is honoured.

⚠️ **One advisory, and it is now the thing blocking S3 in production:** the
credential used for that run can delete objects. That is expected of a human
IAM user and is a defect in a service credential — see *What the endpoint is
still waiting for* below.

### Before trusting it

```bash
# from a laptop, where a broad human credential is expected:
AWS_REGION=ap-southeast-1 python3 server/verify_s3.py --store s3://aeris-insight

# on the host that serves the endpoint, where it is not:
AWS_REGION=ap-southeast-1 python3 server/verify_s3.py --store s3://aeris-insight --strict
```

`--strict` turns the advisory checks into failures. A credential that can
delete is expected on a laptop and is a defect on the endpoint's host, and
that difference is worth enforcing rather than remembering.

Everything in `server/tests` runs against a stub client — that proves this code
asks S3 for the right things, not that *this bucket, this region, this role*
answers the way the design needs. The preflight asserts the one that matters:
a second write to an existing key is **refused**. If conditional writes are not
honoured here, idempotency is gone and nothing downstream notices, because the
overwritten object still parses and still checksums.

It also reports, as advisory notes, whether the role can delete (it should not)
and whether public access is blocked. It leaves one small object,
`_preflight/probe.txt`, which says in its own body that it is safe to delete.

Run it when the credentials land, and again after any bucket policy change.

## Ports

The proxy binds loopback (or, in a container, publishes no host port). Whatever
terminates TLS on 443 publishes exactly one route, `PUT /v1/bundle`, plus
`/healthz`. The read routes are not public — reach them over SSH:

```bash
ssh -N -L 8479:<endpoint-container-ip>:8479 <host> &      # Docker deployment
ssh -N -L 8479:127.0.0.1:8479 <host> &                    # systemd deployment
export INSIGHT_ENDPOINT=http://127.0.0.1:8479
python3 importers/pull.py --week 2026-W34 --inbox inbox/ --roster roster.txt
```

`INSIGHT_ADMIN_TOKEN` still guards them; keeping them off the public interface
means a leaked admin token is not enough on its own. With Traefik the split is
an `ipAllowList` on the read router rather than a second server block — Traefik
matches the connecting address, not `X-Forwarded-For`, so a header cannot forge
its way past it. Behind a CDN every public request arrives from the CDN's
addresses, which is exactly what the allow-list excludes.

## Deployed, 2026-08-25

**Live at `https://aeris-insight.seta-international.com`.**

```
laptop → Cloudflare → office gateway (nginx, holds the LE cert)
                    → 192.168.90.127:8479  ← the upload-only listener
                    → insight-proxy
```

Running on the office box as `insight-proxy` and `insight-watch`. `docker
compose ... up -d` from `~/aeris-insight` is the whole operation; there is no
systemd unit and nothing was installed on the host.

Two reverse proxies reach it, and they get different things. The office gateway
is on another machine, so it is given a host port — mapped to the **upload-only**
listener, because that gateway forwards every path and the read routes must not
be internet-reachable behind a token alone. The Traefik already running on the
box reaches the full listener over the Docker network, which is how the read
routes are served on the LAN.

Exercised end to end over the public URL, no tunnel and no host-file trickery:

| | |
|---|---|
| `insight scan` → `pack` → `ship` | `201`, 7 real commit events |
| the same bundle again | `409`, one object on disk |
| `pull.py` → `bundle.py` | 7 events, 0 rejected, 0 email addresses |
| re-import | 7 duplicates skipped |
| coverage | named the one person on the roster who had not reported |
| an unknown secret, and a fingerprint used as one | `401` each |
| 1.2 MiB body | `413` at the edge, before the process buffers it |
| an engineer's own secret on a read route | `401` |
| a read route from the internet, **with the admin token** | `404` |
| the same read route from the LAN | `200` |
| the read route from an address outside Traefik's allow-list | `403` |
| uploads and `/healthz` from that same address | still served |
| the watchdog | ran, found one silent person, and **did not alert** — new to the roster |

The rows worth having run are the read routes and the watchdog. A `404` on a
listing *while holding the admin token* is the split being enforced by the
process rather than promised by a config file on someone else's machine — and
it was a `200` for about ten minutes before this listener existed, which is how
it got found. The watchdog's restraint on a first day is the false positive that
would otherwise page the whole team on day one.

### What the endpoint is still waiting for

One thing, outside this repo.

*(Resolved on the day: the `502` everything returned at first was the office
gateway's vhost for this hostname proxying to `192.168.90.127:8479` with nothing
listening there — the container published no host port. Cloudflare was relaying
the origin's own `502` faithfully. Also resolved: Cloudflare's browser integrity
check refuses `Python-urllib/*` zone-wide with `403 error code: 1010`, which the
client's own `User-Agent` now avoids. No Cloudflare rule was needed for either.)*

**A service credential for S3.** `INSIGHT_STORE` is `file:///var/lib/insight`
today, not `s3://aeris-insight`. That is a one-line change and a restart — and
deliberately not made yet, because the only credential in the account that can
write to the bucket is a *human* IAM user that can also delete objects in the
production application buckets, stop EC2 instances and push to ECR. This host
runs two self-hosted GitHub Actions runners as the same user that owns the
Docker socket, so a workflow on either repo could read anything placed here.
Putting that key on this box would be a larger problem than the one it solves.

What is needed is one IAM user with the policy above — `PutObject`, `GetObject`,
`ListBucket`, **no `DeleteObject`** — created by someone with `iam:CreateUser`.
Then:

```bash
# in ~/aeris-insight/.env
INSIGHT_STORE=s3://aeris-insight
# and the key where boto3 will find it, mounted into the container
docker compose ... up -d
python3 server/verify_s3.py --store s3://aeris-insight --strict   # must pass
```

Bundles already on disk are copied up with `aws s3 cp --recursive`; keys are
identical either way. Nothing downstream notices the switch: `importers/pull.py`
reads through this endpoint, never from the bucket.

## Tests

```bash
python3 -m pytest server/tests -q
```

Driven over real HTTP against a real store, because the status codes *are* the
contract `cli/ship.py` and `importers/pull.py` were written against. Notably
covered: the key comes from the authenticated identity and not from a header,
a stored bundle is never overwritten, a fingerprint is not a usable credential,
an unknown secret gets `401` (so a rotating client retries) rather than `403`,
and an engineer's own secret cannot read the team's data.

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
through chat. `insight setup --token` used to support it, for pre-provisioning a
joiner before their laptop existed. **It is gone as of 2026-08-26.** Removing it
removes the exception, and what is left is a stronger property than the
convenience was worth: every secret on every machine is one that has never
travelled anywhere.

The cost is stated rather than hidden. An admin can no longer set someone up
before their laptop is touched; every engineer runs `insight setup`, mints
locally, and sends the `email:fingerprint` line. That is one more step for the
one person joining, in exchange for there being no channel through which a live
upload credential has ever passed.

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

## Noticing when a machine goes quiet

```bash
python3 importers/watch.py --roster roster.txt
```

Run it once a day from cron. It lists recent weeks, works out who has not
reported, and posts to **ntfy.sh** when someone crosses the threshold.

**A miss is a working day, not an hour.** `auto` uploads nothing in an hour
where nothing changed, so three silent hours is what a normal afternoon of
non-AI work looks like -- alerting on it would train everyone to mute the
channel. A working day with no bundle at all is different: even an idle day
uploads one empty bundle, so silence for a whole day means the collection is
broken on that machine.

Nights and weekends are not misses, which is what the working hours are for. A
laptop shut at 19:00 on Friday is not a fault.

| `.env` | default | |
|---|---|---|
| `INSIGHT_WORK_START` | `09:00` | |
| `INSIGHT_WORK_END` | `19:00` | |
| `INSIGHT_WORK_DAYS` | `0,1,2,3,4` | Monday = 0 |
| `INSIGHT_TZ_OFFSET` | `+07:00` | fixed offset; Vietnam has no DST, so no tz database is needed |
| `INSIGHT_MISS_THRESHOLD` | `3` | consecutive working days |
| `INSIGHT_NTFY_URL` | `https://ntfy.sh/seta-insight` | |
| `INSIGHT_NTFY_TOKEN` | *(empty)* | only for an access-protected topic |

Two rules keep the channel worth listening to:

**One outage is one message.** The alert fires when the threshold is crossed and
again only if the streak grows. A fortnight-long outage is one problem, not ten
notifications, and a channel that repeats is a channel that gets muted -- which
is worse than having none, because everyone believes it is still watching.

**A new joiner is never the first thing it pages about.** Someone added to the
roster today has no uploads for the three working days before they existed here,
which looks exactly like a broken machine. First sighting is recorded, and the
clock starts then.

⚠️ **A public ntfy.sh topic is readable by anyone who guesses the name**, and
these messages carry work email addresses. Either use an access-protected topic
with `INSIGHT_NTFY_TOKEN`, or accept that the topic name is the only thing
keeping a list of your engineers private. `ntfy.sh/seta-insight` is guessable.

---
