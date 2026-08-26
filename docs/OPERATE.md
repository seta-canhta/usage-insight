# Operating the pipeline

Four stages: laptops collect, the endpoint receives, importers pull a week, the
report and the warehouse read it.

## 0. The admin CLI

Everything below is reachable through one command. Put the endpoint's admin
token in `.admin.env` beside the repo (`chmod 600`; it is gitignored):

```
INSIGHT_ADMIN_TOKEN=...
INSIGHT_ENDPOINT=http://127.0.0.1:8479
```

```bash
./admin.py people                        # who is expected, who has arrived
./admin.py add ngoc@seta-international.vn
./admin.py reset ngoc@...                # replacement laptop
./admin.py remove ngoc@...               # revoke
./admin.py pull --week 2026-W35          # or --month 2026-08
```

**Nobody relays a fingerprint and nobody restarts the service.** `add` puts an
address on the roster; that person's machine registers itself on its next
collection run and can upload immediately. The whitelist and roster are still
files, and a hand edit to either is picked up without a restart.

`pull` writes `reports/<name>/exports/*.ndjson`, a `roster.txt` of who was
expected, and an empty `reports/<name>/daily/` to drop the daily report into.
Then write the report with the `weekly-report` skill.

The read routes are not public, so open the tunnel first:

```bash
ssh -N -L 8479:127.0.0.1:8479 <host> &
```

## 1. The endpoint

```bash
INSIGHT_ADMIN_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))") \
python3 server/proxy.py --store file:///tmp/insight --allowed-file allowed.env
```

`allowed.env` is one line per engineer, exactly what their `insight whoami`
prints.

| Variable | |
|---|---|
| `INSIGHT_STORE` | `s3://bucket/prefix` or `file:///path` |
| `INSIGHT_ADMIN_TOKEN` | required, no default; guards the read routes |
| `INSIGHT_ALLOWED_FILE` | the whitelist, one line per engineer. Written by the endpoint on enrolment, so it must be writable |
| `INSIGHT_ROSTER_FILE` | one work email per line: who is expected. A machine may enrol itself against an address on this list. Without it, enrolment is off and the whitelist is whatever the file says |
| `INSIGHT_HOST` / `INSIGHT_PORT` | default `127.0.0.1:8479` |
| `INSIGHT_UPLOAD_PORT` | optional second listener: uploads and `/healthz` only |
| `AWS_REGION` and credentials | standard boto3 resolution; prefer an instance role |

The server owns the S3 credentials so they never reach a laptop, and it builds
the object key from the authenticated identity so no client can name a key over
someone else's bundle.

Only `PUT /v1/bundle` and `/healthz` are published. The read routes expose every
engineer at once, so they are reached over SSH:

```bash
ssh -N -L 8479:127.0.0.1:8479 <host> &
export INSIGHT_ENDPOINT=http://127.0.0.1:8479
```

Deployment files: `server/compose.yaml`, `server/insight-proxy.service`,
`server/nginx.conf.example`, `server/traefik.insight.yml.example`.

Live at `https://aeris-insight.seta-international.com`, behind the office
gateway.

## 2. Pollers

Credentials come from the environment; a `.env` in any parent directory is a
fallback, and real environment variables win.

| Variable | For |
|---|---|
| `BITBUCKET_USERNAME`, `BITBUCKET_ACCESS_TOKEN` | Bitbucket, CI |
| `JIRA_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN` | Jira |
| `AIO_API_TOKEN` | AIO. **Not the Jira token**, and AIO uses `AioAuth`, not Basic |
| `JIRA_PROJECT_KEYS` | the key allow-list. Without it, keys get invented |
| `AIEP_TELEMETRY_SALT` | without it no `person_email_hash` is emitted at all |

Auth is HTTP Basic for Jira and Bitbucket. Bearer returns 401. Do not
"modernise" it.

```bash
python3 pollers/poll_bitbucket.py --workspace aeriscom --repo wt-playwrite-taf \
  --out bb.ndjson
python3 pollers/poll_jira.py --project IML --out jira.ndjson
python3 pollers/poll_aio.py --project IML --coverage --since 2026-08-01T00:00:00Z \
  --priority High --priority Medium --pace 0.3 --out aio.ndjson
python3 pollers/poll_ci.py --workspace aeriscom --repo wt-playwrite-taf --probe
```

Output is NDJSON, one event per line. Diagnostics go to stderr as single-line
JSON.

| Exit | |
|---|---|
| 0 | success, watermark advanced |
| 2 | configuration error, nothing ran |
| 3 | API error; no watermark advance, no partial file. Safe to re-run |
| 4 | `poll_ci` only: CI not available. An answer, not a crash |

`--pace` matters on AIO, which 429s readily. `--max-cycles` bounds what is kept,
not what is fetched.

Schedule: Bitbucket hourly, Jira and AIO nightly.

## 3. Importing a week

```bash
export INSIGHT_ADMIN_TOKEN=...
python3 importers/pull.py --week 2026-W34 --inbox inbox/ --roster roster.txt
python3 importers/bundle.py --inbox inbox/ --out events.ndjson \
  --state state/bundles.json
python3 importers/watch.py --roster roster.txt      # hourly, next to the endpoint
```

**Always pass `--roster`.** Coverage derived from bundles that arrived cannot
see someone who never sent one. Only a roster knows who was expected. `pull`
names who did not report; read that line before the numbers.

`pull` stops at `inbox/`. `bundle.py` is what parses, checksums, re-checks the
allow-list and dedupes.

## 4. Report

```bash
python3 report/weekly_report.py --input events.ndjson --week 2026-W34
python3 report/weekly_report.py --input events.ndjson --format html --out r.html
```

Stdlib only, no network. `report/people_workbook.py` needs `openpyxl`; without
it that one module and its tests are skipped.

## 5. Warehouse

BigQuery. Every file uses `${PROJECT_ID}` as a placeholder. Dataset location is
`EU` in the `CREATE SCHEMA` statements and is immutable once created — change it
before the first apply.

```bash
export PROJECT_ID="your-gcp-project"
for f in sql/01_raw.sql sql/02_dims.sql sql/03_core_fct.sql sql/08_metrics.sql; do
  sed "s/\${PROJECT_ID}/${PROJECT_ID}/g" "$f" | bq query --use_legacy_sql=false
done
```

Then, scheduled:

```
hourly   04_transform_run.sql     raw → core.fct_ai_run
hourly   07_dq_checks.sql         → dq.dq_findings
nightly  05_transform_output.sql  → core.fct_ai_output, then 06_marts.sql
```

Files 4–7 are idempotent. `02_dims.sql` is not: its seed inserts duplicate
pricing rows on a second run. `09_set_model_price.sql` is run on demand, never
in sequence.

Three things that will otherwise produce a wrong number:

- **Pricing is seeded with placeholders at NULL rates.** `cost_usd` resolves to
  NULL and `DQ-6` fires until real rates are loaded. Close the placeholder rows
  and insert new ones; never `UPDATE` a rate in place, which restates history.
- **`premium_requests` is measured, `cost_usd` is modelled.** They sit side by
  side and are never summed. `premium_requests` is NUMERIC, not INT64 — a
  premium request can cost 0.33, and INT64 truncates that to zero.
- **The grain cutover on 2026-08-26.** Runs before it take usage from OTel spans
  at per-call grain; runs after take it from the Copilot journal at
  per-session-model grain. The boundary is carried in the data
  (`fct_ai_run.usage_grain`, `marts.dim_grain_cutover`), not in a date, because
  the transforms rebuild trailing windows. `DQ-GRAIN` reports rows on the wrong
  branch.

## Tests

```bash
for s in pollers report collector cli importers server; do
  python3 -m unittest discover -s $s/tests
done
python3 -m unittest discover -s tests
```
