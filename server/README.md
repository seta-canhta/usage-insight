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
| `INSIGHT_ALLOWED` | `email:fp[:fp],...` |
| `INSIGHT_ALLOWED_FILE` | the same, one line per engineer — easier to edit |
| `INSIGHT_HOST` | default `127.0.0.1`; nginx is what faces the internet |
| `INSIGHT_PORT` | default `8479` |
| `AWS_REGION`, credentials | standard boto3 resolution — prefer an instance role |

The admin token is required with no default. The read routes expose every
engineer at once and are never left open by accident.

## Deploying

```bash
sudo useradd --system insight
sudo git clone git@github.com:seta-canhta/usage-insight.git /opt/usage-insight
sudo python3 -m pip install -r /opt/usage-insight/server/requirements.txt

sudo install -d -m 700 -o root -g root /etc/insight
sudo tee /etc/insight/proxy.env >/dev/null <<'ENV'
INSIGHT_STORE=s3://seta-insight/bundles
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

Adding an engineer is one line appended to `/etc/insight/allowed.env` and a
`systemctl restart insight-proxy`. Removing one is deleting that line.

## The bucket

```bash
aws s3api create-bucket --bucket seta-insight --region ap-southeast-1 \
    --create-bucket-configuration LocationConstraint=ap-southeast-1
aws s3api put-public-access-block --bucket seta-insight \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket seta-insight \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

The instance role wants `s3:PutObject`, `s3:GetObject` and `s3:ListBucket` on
that bucket, and **nothing else**. No `s3:DeleteObject`: retention is a lifecycle
rule, and deletion is not a code path anywhere in this service.

`put_object` uses `IfNoneMatch: "*"`, so an existing key is never overwritten. On
an S3-compatible store that does not support conditional writes the service
falls back to check-then-write and says so in the log — racy, but the key is the
content digest, so the worst case is a duplicate object rather than wrong data.

## Ports

The proxy binds loopback. Nginx terminates TLS on 443 and publishes exactly one
route, `PUT /v1/bundle`. The read routes are not public — reach them over SSH:

```bash
ssh -N -L 8479:127.0.0.1:8479 aeris-insight.seta-international.com &
export INSIGHT_ENDPOINT=http://127.0.0.1:8479
python3 importers/pull.py --week 2026-W34 --inbox inbox/ --roster roster.txt
```

`INSIGHT_ADMIN_TOKEN` still guards them; keeping them off the public interface
means a leaked admin token is not enough on its own.

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
