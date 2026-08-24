# Daily report — column spec

The team already writes a daily Excel. That makes it the cheapest trail available
(`TRAIL-GAPS.md` G5) — but only if the columns are fixed. **Every day spent writing
free text is a day that cannot be parsed later, because nobody goes back to restructure
old spreadsheets.**

This spec is a proposal. Adapt it to whatever the team writes today and keep as many of
their existing columns as possible; a format people already use is worth more than a
tidier one they abandon.

---

## One row = one piece of work

| Column | Required | Format | Purpose |
|---|:--:|---|---|
| `date` | ✓ | `YYYY-MM-DD` text | not an Excel date cell — locale turns those into garbage |
| `person` | ✓ | company email | joins to the Jira account id |
| `jira_key` | ✓ | `PRJ-1234` | must match the pattern, not prose |
| `test_case_key` | | `PRJ-TC-567` | **the column that repairs the AIO ↔ Jira gap** |
| `activity` | ✓ | enum below | bounded, so it can be a dimension |
| `ai_used` | ✓ | `yes` / `no` | |
| `ai_agent` | | agent name | which platform agent — answers the question today, without waiting for the client |
| `hours` | | number | |
| `note` | | free text | everything narrative goes here and is **never counted** |

`activity` enum: `design_case` · `automate` · `execute` · `fix_defect` · `review` ·
`other`

---

## Three rules that decide whether this works

1. **One row per piece of work.** Three tickets in a day means three rows. Never
   `PRJ-1, PRJ-2, PRJ-3` in one cell.
2. **Key columns hold keys only.** Explanation goes in `note`.
3. **Do not merge cells, rename headers, reorder columns, or use colour to mean
   something.** The importer reads by header name; a renamed header fails silently,
   and a colour carries no data at all.

---

## Why `ai_agent` is worth a column

It answers *"which platform agent are they using"* **this week**, while `emit.py` wiring
and the npx client are still being built.

Once the client ships, this column becomes something better: **ground truth to validate
the emitter against.** If the Excel says `test-script-generator` ran and the emitter
recorded no such run, the emitter is broken — and without the column there would be
nothing to notice that with.

---

## What the importer does

```
read → validate → hash emails → emit contract events → reconcile
```

- **Reject loudly.** A row that does not parse is reported as unparseable with its row
  number. It is never dropped, and never coerced into a guess.
- **Hash emails** per `CONTRACT.md §11.3`. The raw email never reaches storage.
- **Source files stay out of git** — they land in `inbox/`, which is gitignored, and
  they contain personal data.

---

## Reconciliation — the actual point

```
Excel:  person X worked on PRJ-6384 and PRJ-TC-2891 on 2026-08-12
Jira:   PRJ-6384 transitioned on 2026-08-12       ✓
AIO:    PRJ-TC-2891 has a run on 2026-08-12       ✓
     →  mapping PRJ-6384 ↔ PRJ-TC-2891, high confidence
```

Agreement across three sources builds the AIO ↔ Jira mapping without editing 4,248
cases by hand. Each mapping row carries its confidence and which sources agreed.

Disagreement is a finding too — either work is not reaching Jira, or the report
overstates. Surface both; smoothing them over discards the most useful signal in the
file.

---

## The limit, restated because it will be forgotten

**This file builds mappings and cross-checks other sources. It does not count output.**

Counting from self-report ranks people by how carefully they write reports. Someone who
works hard and logs little looks worse than someone who works less and logs everything,
and nothing in the output reveals that this happened. Output counts come from Jira,
Bitbucket, AIO and the client — systems that record work as a side effect of doing it.
