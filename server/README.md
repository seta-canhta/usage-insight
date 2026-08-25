# The collection endpoint

Implements [`docs/TRANSPORT.md`](../docs/TRANSPORT.md). Laptops `PUT` bundles
here; this owns the S3 credentials so they never leave the server.

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
| `INSIGHT_PORT` | default `8479` |
| `AWS_REGION`, credentials | standard boto3 resolution — prefer an instance role |

The admin token is required with no default. The read routes expose every
engineer at once and are never left open by accident.

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

Running on the office box behind the Traefik that already owns 443 there, as
`insight-proxy` and `insight-watch` on its Docker network. `docker compose ...
up -d` from `~/aeris-insight` is the whole operation; there is no systemd unit
and nothing was installed on the host.

Exercised end to end at `https://aeris-insight.seta-international.com` with the
real hostname and SNI (resolved to the origin, since the DNS record is not
finished — see below):

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
| the read route from an address outside the allow-list | `403` |
| uploads and `/healthz` from that same address | still served |
| the watchdog | ran, found one silent person, and **did not alert** — new to the roster |

The last two rows are the ones worth having run: the source restriction was
checked by narrowing it to an address this machine does not have and confirming
the read route closed while uploads stayed open, and the watchdog's restraint on
a first day is the false positive that would otherwise page the whole team on
day one.

### What the endpoint is still waiting for

Two things, both outside this repo:

**1. The Cloudflare record.** `aeris-insight.seta-international.com` resolves to
Cloudflare and Cloudflare answers `502`: its origin for that hostname is not the
box. Sibling hostnames on the same zone work, and their requests arrive at the
same Traefik, so the fix is to make this record match one of those — a proxied
`A` record to the office's public address. Nothing on the box changes.

Cloudflare's browser integrity check also refuses `Python-urllib/*` with
`403 error code: 1010`, zone-wide, before the request reaches any origin. The
client now sends its own `User-Agent`, which that check accepts; no Cloudflare
rule is needed for it.

**2. A service credential for S3.** `INSIGHT_STORE` is `file:///var/lib/insight`
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
