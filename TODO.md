# TODO

Working checklist, not documentation. Delete a line when it is done.

## Deploy — done 2026-08-27

Endpoint serves **0.6.0**. `/install.json` carries the digest of
`insight-0.6.0.pyz`, which matches the local build byte for byte; `/healthz`
200; `/update` returns the same bytes as `/install`; `insight-proxy` healthy.

Project **WatchtowerQD** = `AERLABS, APR, IML, IOTA3` →
`linh.hoang@aeris.net`, `ngoc.nguyen@aeris.net`. Verified against production
with a throwaway address: a member enrolling receives all four boards, a
rostered non-member receives `[]`. Probe removed, no residue.

Verified live on a sandbox install from `/update`: 0.6.0 installs on stock
macOS python 3.9.6, and `pack --since 2026-08-19 --until 2026-09-01` produced
three bundles filed under W34, W35 and W36. The same range as one bundle would
have gone to W31.

- [ ] **IOTA3 is unverified.** It was added on the user's word, which is the
      intended way to fill an allow-list — AR-1 is about not *deriving* keys
      from strings. But the Atlassian account reachable from here is
      `all-it.atlassian.net`, which holds neither IOTA3 nor IML, so it is not
      the pilot's Jira and nothing was checked against the real site. Worth one
      look by somebody who can see it.
- [ ] **Wait for the fleet.** Both laptops were on 0.4.0. The update check is
      now hourly rather than daily, so a machine that gets used should be on
      0.6.0 within about two hours of use — but nothing can be pushed, and a
      machine nobody opens stays behind. Watch `docker logs insight-proxy` for
      `"event": "enrolled"` from the two real addresses.

## Attribution

0.8.0 (2026-08-27) closed the code half of this.

- [x] **`AI-Run-Id` on 0 of 121 PRs** — two bugs, neither a missing feature.
      The hook read only `~/.aiep/telemetry`, the platform agent's buffer,
      which does not exist on the pilot machines; their runs come from the
      Copilot CLI journal into insight's own buffer, which it never looked at.
      And `MAX_AGE_SECONDS` was declared, commented and never read, so a run
      left open on Friday would stamp Monday's commit. Both fixed, both tested.
      The test harness had been rewriting the hook's source to point at a test
      buffer; that rewrite silently stopped matching, so it was testing the
      unpatched hook. It now runs the real file via `SETA_INSIGHT_HOME`.
- [x] **Test keys from script file names** — the only route from a repo to an
      AIO case that needs nobody to type one. `test_case_keys` on the terminal
      PR events, keys only, paths still dropped (§11.3).
- [x] **`who_is_who.py`** ranks the accountIds in an export by what they did,
      so `identities.txt` can be filled without guessing.

- [ ] **Write `identities.txt`.** The one step nobody else can do:
      `python3 tools/diagnostics/who_is_who.py reports/2026-W34/exports/*.ndjson`
      lists six candidates with their activity. Confirm each in Jira, write
      `email accountId` lines, pass `--identities` on every pull. Until then
      laptop events keep `person_id: null` and join to nothing.
- [ ] **Ask the QA team to name spec files after their cases**, or to fill
      `automation_key` in AIO. Without one of those, case-level attribution
      stays out of reach — 0 of 82 branches name a case, 1 prompt in 5,036
      names any ticket, and `has_automation_key` is false on all 4,512 cases
      including the 4,165 marked "Automated". Person-and-week attribution works
      now; case-level does not, and no parser will change that.

**Structurally unreachable, recorded so nobody hunts for the bug again:** VS
Code Copilot Chat has no run concept — `vscode_read` emits `run_id: null`
deliberately, because inventing one would manufacture a join key (AR-1). So
chat-only use produces no `AI-Run-Id` however well the hook works, and
`explicit` linkage — and with it the cost metrics — is out of reach for it.
That is a property of the surface. See `CONTRACT.md` §9.

## Re-collect the laptop data

0.4.0 sets no `jira_projects`, so every reader ran with no allow-list and
invented keys. Measured 2026-08-26: `fix/AUG-25` became ticket `AUG-25` on 28
of 28 of Linh's events; a Bitbucket export held 45 fabricated keys against 9
real ones.

- [ ] Once both laptops report 0.7.0, have Ngoc and Linh run
      `insight backfill --since 2026-08-01`. It now produces one bundle per
      ISO week, so W31-W35 each land in their own folder.
- [ ] Re-pull W34 and regenerate the report; the current
      `reports/2026-W34/exports/` was repaired by hand (originals in
      `superseded/`).

## Open questions — three of four closed 2026-08-27

- [x] `ATTRIBUTE_ALLOWLIST` was describing the pollers wrongly, not protecting
      them. Measured: `scm.pr.merged` emitted 38 attributes against 3 listed,
      `scm.pr.declined` 37 against 3, `ci.pipeline.completed` 19 against 8
      (plus 5 more on the Jenkins path, which is the one production uses),
      `jira.transition` 10 against 6. Widened to what is emitted, `CONTRACT.md`
      §3 rows 16–20 corrected to match, and a drift test now runs all four
      pollers and fails on the next name that appears without being listed.
- [x] `post_review_change_ratio` has an emitter. `lines_changed_after_first_review`
      and `lines_changed_pre_review` are measured per commit against its first
      parent, split at `first_review_at`. Both NULL when the PR was never
      reviewed — the boundary that defines them does not exist — and both NULL
      if any per-commit request fails, because an undercount of rework is wrong
      in the flattering direction.
- [x] `repo_of()` no longer stamps today's branch on old sessions. The branch
      now comes from HEAD's reflog at the session's own timestamp, and a
      session older than the reflog gets NULL rather than an answer.
- [ ] Thao Nguyen is on the roster and has never enrolled. Not a code fix.

## Backfill

- [x] `pack` emits one bundle per ISO week. A bundle is filed by the endpoint
      under `iso_week(window_start)` alone and the pipeline pulls a week at a
      time, so `backfill --since 2026-08-01` filed four weeks under 2026-W31
      and W32–W34 read as weeks nobody sent.

## Housekeeping on the host

- [ ] `src.old-20260826-1021`, `src.old-20260827-0221` and
      `etc/install.sh.old-20260827-0221` are rollback copies. Delete them once
      step 4 confirms the fleet is healthy on 0.5.0.

## Lost

- [ ] `NOTE.md` had ~20 uncommitted lines at the start of 2026-08-26 that
      vanished during a background agent run. Not recoverable — git does not
      keep uncommitted work. Retype if they were wanted.
