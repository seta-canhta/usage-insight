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
./admin.py project                       # list projects
./admin.py project WatchtowerQD \
  --boards IML,APR,AERLABS \
  --members ngoc.nguyen@aeris.net,linh.hoang@aeris.net
./admin.py pull --week 2026-W35          # or --month 2026-08
```

**Define a project before anyone installs.** A project is a team and the Jira
boards its work lives on, and it is the only way a laptop learns which project
keys are real. A client that has not been told runs `extract_jira_key` with no
allow-list and invents keys from anything key-shaped -- measured 2026-08-26,
`fix/AUG-25` became ticket "AUG-25" on 28 of 28 events from one machine, and a
Bitbucket export carried 45 fabricated keys against 9 real ones.

The boards ride back on the enrolment response and the client writes them down.
`project` also puts its members on the roster, because being on a project and
being expected to report are the same statement. A machine already enrolled
without a board list re-enrols hourly until it has one, so an existing fleet
repairs itself; nobody reinstalls.

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
| `INSIGHT_PROJECTS_FILE` | `<project>:<BOARD,BOARD>:<member,member>` per line. The boards an enrolling laptop is told are real. Written by the endpoint when an admin defines a project, so it lives in the same writable mount as the roster -- never beside the admin token. Unset means no laptop is told anything, which is safe but leaves every reader emitting no key |
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

### Pushing a release to the fleet

Redeploy the endpoint with the new `install.sh` in `etc/` and the fleet follows
on its own: `insight auto` runs hourly, checks `/install.json`, verifies the
digest and swaps the archive. Two caveats worth holding.

**Count in working hours.** The team works 09:00–19:00 ICT, so a laptop is
awake about ten hours a day. The swapped archive also only takes effect on the
*next* invocation. A release therefore reaches a used machine within about two
hours of use, and a machine nobody opens not at all.

**There is no way to push.** A laptop takes no inbound commands and that is
deliberate. To move faster than the hourly run, ask the person to paste:

```bash
curl -fsSL https://aeris-insight.seta-international.com/update | sh
```

That is the installer under the name that says what it does — same bytes, same
route, and re-running it *is* the upgrade: the archive is versioned, the swap
is atomic, it rolls back if the new one fails its own smoke test, and the
config, machine id, salt and upload secret are untouched.

## 1b. The daybook

A browser page on the endpoint's **read** listener: today's attendance for
every person, and one contribution calendar per person for the AI activity
already in the bucket. Opt-in — with no passcode set the routes do not exist,
so an endpoint nobody has asked this of is untouched by a redeploy.

**It is not one of the ten metrics.** Attendance is typed in by hand, so it is
self-reported and must never be counted into a figure (`CLAUDE.md`). What it is
for is the denominator: telemetry cannot tell a day off from a day somebody
worked without AI, and the page puts the two side by side so the difference is
visible.

Turn it on:

```bash
printf '981022\n' | sudo tee /etc/insight/daybook.passcode
sudo chmod 600 /etc/insight/daybook.passcode
# in the compose .env, beside INSIGHT_INSTALL_SCRIPT:
INSIGHT_DASHBOARD_PASSWORD_FILE=/etc/insight/daybook.passcode
docker compose up -d
```

`INSIGHT_ATTENDANCE_FILE` defaults to `/var/lib/insight-registry/attendance.tsv`
in `compose.yaml` — the **writable** mount, beside `roster.txt` and never beside
the admin token, because the page writes it. A passcode with no attendance file
stops the process at startup rather than serving a page that refuses every
entry.

**Know where it is reachable from before turning it on.** The page is served on
the read listener (8479) and never on the upload listener (8480), so it is not
internet-reachable whatever the gateway forwards. Everything beyond that is a
property of the deployment, not of the code:

| if the read listener is | `/dashboard` is reachable from |
|---|---|
| unpublished (`INSIGHT_PUBLISH=127.0.0.1:8479`) | an SSH tunnel only |
| published to the LAN (`INSIGHT_PUBLISHED_PORT=8479`) | every machine on the office network |

**The production deployment is the second row.** `.env` on the host sets
`INSIGHT_PUBLISH=192.168.90.127:8479`, and the gateway's `insight-read` router
allows `127.0.0.1/32` plus all of RFC1918 — so the daybook is an office-LAN
page behind a passcode, not a tunnel-only one. That was chosen deliberately;
it is written down here so nobody re-derives the tunnel assumption from the
`pull` instructions above.

Either way:

```bash
ssh -L 8479:127.0.0.1:8479 <host>        # always works
open http://127.0.0.1:8479/dashboard
```

A passcode is a weaker credential than `INSIGHT_ADMIN_TOKEN`, and this page
lists every engineer on one screen — which is the whole reason it never goes on
8480. Eight wrong passcodes from one address locks that address out for a
minute; sessions are signed with a key minted per process, so a restart signs
everyone out.

The attendance file is tab separated and meant to be edited by hand:

```
ngoc.nguyen@aeris.net	2026-08-27	out	09:12	18:40
linh.hoang@aeris.net	2026-08-27	off
```

It is re-read when it changes, so an edit needs no restart. `in` is arrived and
still here, `out` is arrived and left, `off` is not working — and **no line at
all** is the fourth state, "nobody has said", which the page draws as a hole
rather than as a zero. Clearing a day removes the line; it does not write `off`.

The calendars read bundles straight out of the store and cache what they parse
by object key, which never needs invalidating because keys are content digests
and the store is write-once. A cold process reads up to 96 bundles per request
and the page says how many are still queued rather than quietly serving a short
answer.

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

### Daily, unattended

`importers/daily_pull.py` runs all four pulls — Bitbucket, Jira, AIO runs, AIO
coverage — into `reports/cache/<YYYY-MM-DD>/`: one NDJSON per source, plus a
`_status.json` naming what arrived and what did not, plus a `latest` symlink.
File names match `admin.py pull`, so a dated directory drops straight in
wherever `workbook-input/` is read from.

```bash
python3 importers/daily_pull.py                          # today, month-to-date
python3 importers/daily_pull.py --date 2026-08-26 --force
```

The window is the first of the day's month, not a rolling 30 days: the workbook
is monthly, and a rolling window moves its denominator by a day every morning.
`--since` and `--days` override it.

A source that fails writes **no file**. An empty NDJSON where the pull should
have been reads downstream as a real day with nothing on it, and nobody is
watching when a scheduled job fails. Re-running is cheap and is the normal fix
— a source already cached for that day is left alone, so recovering one failure
costs one API budget rather than four.

| Exit | |
|---|---|
| 0 | every source is on disk, fetched or already cached |
| 1 | one or more blocked (no credential) or failed. The rest are still there |
| 2 | nothing planned: `JIRA_PROJECT_KEYS`/`BITBUCKET_REPOS`/`AIO_PROJECTS` are empty, and keys are never guessed (AR-1) |

#### Scheduling it — macOS, launchd

launchd and not cron, because the machine is a laptop that is shut every
evening: cron does not run a job it slept through, launchd runs a missed
`StartCalendarInterval` as soon as the machine wakes. 09:15, inside the hours
the laptop is actually on.

`tools/launchd/vn.seta.insight.dailypull.plist.in` is a template — a committed
absolute path carries a username. Its header comment carries the same lines and
the reasoning:

```bash
REPO="$(git rev-parse --show-toplevel)"
mkdir -p "$REPO/reports" ~/Library/LaunchAgents
sed -e "s|@REPO@|$REPO|g" -e "s|@PYTHON@|$(command -v python3)|g" \
  "$REPO/tools/launchd/vn.seta.insight.dailypull.plist.in" \
  > ~/Library/LaunchAgents/vn.seta.insight.dailypull.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/vn.seta.insight.dailypull.plist
launchctl kickstart -p gui/$UID/vn.seta.insight.dailypull   # run it now
```

Off again: `launchctl bootout gui/$UID/vn.seta.insight.dailypull`, then delete
the plist. It logs to `reports/daily-pull.log`. The label is deliberately not
the client's `vn.seta.insight` (§1) — sharing one would mean
`insight schedule --off` silently removing this job too.

**The laptop only.** This job holds the credentials, and `future` runs
untrusted workflow code — it must never have one. Nothing goes in the plist
either: `~/Library/LaunchAgents` is world-readable, and `common.Config` finds
the repo-root `.env` on its own, whatever the environment or the working
directory. A copy of this landing on `future` reports every source blocked and
exits 1, rather than writing a day of zeros.

## 3. Importing a week

```bash
export INSIGHT_ADMIN_TOKEN=...
python3 importers/pull.py --week 2026-W34 --inbox inbox/ --roster roster.txt \
  --identities identities.txt
python3 importers/bundle.py --inbox inbox/ --out events.ndjson \
  --state state/bundles.json
python3 importers/watch.py --roster roster.txt      # hourly, next to the endpoint
```

**Always pass `--roster`.** Coverage derived from bundles that arrived cannot
see someone who never sent one. Only a roster knows who was expected. `pull`
names who did not report; read that line before the numbers.

`pull` stops at `inbox/`. `bundle.py` is what parses, checksums, re-checks the
allow-list and dedupes.

### `--identities` — without it, laptop events join to nothing

`identities.txt` is one `email accountId` line per person, `#` for comments:

```
ngoc.nguyen@aeris.net   712020:198a0913-d658-4d93-9e9d-1b9747429f1b
linh.hoang@aeris.net    712020:28cc987e-5263-4564-83c6-7f76fa32574e
```

A laptop cannot know its own Atlassian accountId — that is a Jira fact, not a
machine one — so it emits `person_id: null` and a `person_email_hash` salted
with a `uuid4()` generated at `init`, which is deliberately unlinkable and
therefore useless as a join key. CONTRACT.md §2.1 makes the accountId the
canonical person key, and the endpoint is the only party that knows who
uploaded a bundle, so `pull.py` is where the address becomes an id.

The address itself never enters the event path (§1.1). `pull.py` writes
`inbox/_identities.json`, which maps *file name* to accountId, and
`bundle.py` stamps `actor.person_id` from it — filling a null, never
overwriting a value the client supplied.

Measured 2026-08-26 without it: 935 laptop events, `person_id` null on every
one, while AIO runs, AIO cases and Bitbucket all keyed on the same accountIds
and joined to each other perfectly. No AI usage could be attributed to any test
run, any pull request or anyone. `pull.py` now names the addresses it could not
map in its `unmapped` output — named rather than counted, because the fix is
one line in this file and nobody adds a line for a number.

Get an accountId from Jira: profile URL, or the admin user list. Where a person
has no entry, their events stay NULL rather than being guessed.

The accountIds are already in the exports; what is missing is which is whose.
`tools/diagnostics/who_is_who.py` ranks them by what they actually did — test
runs executed, cycles touched, pull requests opened and reviewed — so the
question narrows from "what are the accountIds" to "which of these is Ngoc":

```bash
python3 tools/diagnostics/who_is_who.py reports/2026-W34/exports/*.ndjson
```

It cannot tell you who is who and does not pretend to: this pipeline stores no
names by design. Confirm each in Jira before writing the line. A wrong entry
attributes one person's AI use to another, which is worse than the null it
replaces.

## 4. Report

```bash
python3 report/weekly_report.py --input events.ndjson --week 2026-W34
python3 report/weekly_report.py --input events.ndjson --format html --out r.html
```

Stdlib only, no network. `report/people_workbook.py` needs `openpyxl`; without
it that one module and its tests are skipped.

## 4b. The insights screens

Two more pages on the same read listener and behind the same passcode:
`/insights` (the ten metrics, with charts and tables) and `/activities` (the
work itself, grouped by the job being done). Both filter by member.

They read a **snapshot**, not the sources. Three of the four sources need
credentials that must never exist on the box serving the page — it runs
untrusted workflow code — so the figures are derived where the credentials are
and travel with the deploy. The snapshot carries counts and no secrets: no
tokens, no emails, no paths, no prompt or response text.

Generate it from the same pull the workbook uses:

```bash
python3 report/dashboard_data.py \
  --person "Ngoc Nguyen=5bee6a1ec03ef4570f0a78e3" \
  --person "Linh Hoang=712020:28cc987e-5263-4564-83c6-7f76fa32574e" \
  --pronouns "Ngoc Nguyen=she" --pronouns "Linh Hoang=he" \
  --role "Ngoc Nguyen=runs the tests and raises the bugs" \
  --role "Linh Hoang=builds the automated tests" \
  --input reports/cache/latest \
  --weeks 2026-W31..2026-W35 --full-weeks 2026-W32..2026-W34 \
  --price "claude-sonnet-4.6=3.0/15.0" \
  --partial "2026-W35=week not finished" \
  --out server/assets/insights.json
```

`--full-weeks` is not optional in spirit: volume is only compared across weeks
that finished, and leaving a part week in a trend once turned a +74% into a
-17%. `--pronouns` is never inferred from a name; anyone unnamed stays
they/them. The snapshot is **gitignored** for the same reason `/reports/*` is —
it names individuals and carries issue keys.

Build the pages (Node, on a developer machine — the endpoint has no toolchain,
so the bundle is committed and ships with the code):

```bash
npm install
npm run typecheck:web && npm run build:web   # -> server/assets/app/
```

The server reads `server/assets/app/` once at startup and serves it by exact
key, so no request path ever reaches the filesystem. Override either location
with `--insights-app` / `--insights-snapshot`. A missing bundle is not an
error: those two routes 404 and the daybook is untouched. A missing snapshot
returns 503 with a reason — never an empty object, and never zeros.

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
