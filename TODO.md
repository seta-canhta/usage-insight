# TODO

Working checklist, not documentation. Delete a line when it is done.

## Deploy v0.5.0 — done 2026-08-27, except the wait

The endpoint serves **0.5.0**. `/install.json` reports version 0.5.0 and the
digest of `insight-0.5.0.pyz`; `/healthz` 200; `insight-proxy` healthy;
`insight-watch` completed a cycle after the restart.

What was done, on `future` (`/home/ubuntu/aeris-insight`, compose project
`aeris-insight`):

- [x] **1. Redeployed.** `git archive v0.5.0` into `src/` (old tree kept as
      `src.old-20260827-0221`), the release `install.sh` into `etc/`
      (sha256 `708c955a…`, matching the release asset; old one kept beside it),
      then `docker compose -p aeris-insight --project-directory . -f
      src/server/compose.yaml --env-file .env up -d --build`.
      `INSIGHT_PROJECTS_FILE` needed no `.env` edit — it is in `compose.yaml`.
- [x] **2. Project defined.** `WatchtowerQD` → `AERLABS, APR, IML` →
      `linh.hoang@aeris.net, ngoc.nguyen@aeris.net`. `registry/projects.env`
      holds one line and both addresses were already on the roster.
- [x] **3. Verified both directions** with a throwaway `probe.deploy@aeris.net`:
      rostered but in no project returned `"jira_projects": []`; added to the
      project and re-enrolled after a `reset` it returned
      `["AERLABS","APR","IML"]`. Probe removed, membership restored, no probe
      bundles on the store.
- [ ] **4. Confirm the fleet self-repairs.** Nothing more to push — a laptop
      takes no inbound commands. Expect up to **~25 hours**, not one: the
      self-update check in `cli/update.py` is throttled to
      `CHECK_INTERVAL = 24 * 3600`, and the swapped archive only takes effect
      on the *next* `insight auto`, which is hourly. So: update check → up to
      24 h; upgrade lands → next hour; that run re-enrols, because `cmd_auto`
      re-enrols while `config["jira_projects"]` is empty.
      Check by watching `docker logs insight-proxy` for `"event": "enrolled"`
      from the two real addresses, then check a later bundle carries real keys
      or none — never `AUG-25`.

## Re-collect the laptop data

0.4.0 sets no `jira_projects`, so every reader ran with no allow-list and
invented keys. Measured 2026-08-26: `fix/AUG-25` became ticket `AUG-25` on 28
of 28 of Linh's events; a Bitbucket export held 45 fabricated keys against 9
real ones.

- [ ] After step 4 above shows both laptops on 0.5.0, have Ngoc and Linh run
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

## Housekeeping on the host

- [ ] `src.old-20260826-1021`, `src.old-20260827-0221` and
      `etc/install.sh.old-20260827-0221` are rollback copies. Delete them once
      step 4 confirms the fleet is healthy on 0.5.0.

## Lost

- [ ] `NOTE.md` had ~20 uncommitted lines at the start of 2026-08-26 that
      vanished during a background agent run. Not recoverable — git does not
      keep uncommitted work. Retype if they were wanted.
