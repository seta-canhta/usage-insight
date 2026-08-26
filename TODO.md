# TODO

Working checklist, not documentation. Delete a line when it is done.

## Deploy v0.5.0

Released and tagged 2026-08-26. `main` is `cb6ca6d`. Both CI workflows green.
The endpoint still serves **0.4.0** — `/install.json` says so.

Blocked on network: `ssh future` (192.168.90.127) was unreachable from
192.168.100.x. The endpoint itself is healthy, `/healthz` 200.

Order matters. Step 2 before step 1 returns 404: the 0.4.0 image has no
`/v1/projects` route.

- [ ] **1. Redeploy the endpoint.** Copy the v0.5.0 `install.sh` into
      `/etc/insight/`, add `INSIGHT_PROJECTS_FILE` (env + mount, already in
      `server/compose.yaml`), then `docker compose up -d --build`.
- [ ] **2. Define the project.**
      ```bash
      ./admin.py project WatchtowerQD \
        --boards IML,APR,AERLABS \
        --members ngoc.nguyen@aeris.net,linh.hoang@aeris.net
      ```
- [ ] **3. Verify.** Enrol a throwaway probe address, confirm the response
      carries `jira_projects`, then remove the probe and its bundles.
- [ ] **4. Confirm the fleet self-repairs.** `insight auto` re-enrols hourly
      while it holds no board list. Check `./admin.py people`, then check a
      later bundle carries real keys or none — never `AUG-25`.

## Re-collect the laptop data

0.4.0 sets no `jira_projects`, so every reader ran with no allow-list and
invented keys. Measured 2026-08-26: `fix/AUG-25` became ticket `AUG-25` on 28
of 28 of Linh's events; a Bitbucket export held 45 fabricated keys against 9
real ones.

- [ ] After the deploy lands and clients are on 0.5.0, have Ngoc and Linh run
      `insight backfill --since 2026-08-01`.
- [ ] Re-pull W34 and regenerate the report; the current
      `reports/2026-W34/exports/` was repaired by hand (originals in
      `superseded/`).

## Open questions, not yet decided

- [ ] `ATTRIBUTE_ALLOWLIST["scm.pr.created"]` names 3 attributes; the poller
      emits ~29, including everything metrics 3 and 4 need. They survive only
      because poller output does not pass through `importers/bundle.py`, where
      the allow-list is enforced. Widening it is a schema decision.
- [ ] `post_review_change_ratio` is defined in `schema/CONTRACT.md` §5 and
      drives the accepted/reworked state machine, but nothing emits
      `lines_changed_after_first_review`. Needs per-commit diffstat.
- [ ] `repo_of()` reads the branch with `git rev-parse HEAD` at collection
      time, so a backfill stamps today's branch on three weeks of sessions.
      Value may be right by luck; the method is not, and `link.confidence` is
      0.9 either way.
- [ ] Thao Nguyen is on the roster and has never enrolled.

## Lost

- [ ] `NOTE.md` had ~20 uncommitted lines at the start of 2026-08-26 that
      vanished during a background agent run. Not recoverable — git does not
      keep uncommitted work. Retype if they were wanted.
