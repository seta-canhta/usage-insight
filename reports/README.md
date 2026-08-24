# reports

Generated output. **Everything here is gitignored except this file.**

It carries live Atlassian account ids, issue keys, branch names and named
individuals — it is regenerated on demand and must never enter git history. The
code that produces it lives in `report/`, which is the part worth reviewing.

```
reports/
  <ISO-week>/
    weekly-<week>.md        the combined report
    ai-work-tracking.xlsx   per-person workbook
    exports/*.ndjson        the raw pull it was built from
```

Keeping the exports beside the report matters: without them a figure cannot be
re-derived later, and a number nobody can reproduce is a number nobody should
act on.

## Regenerating

```bash
python3 report/combined_weekly.py \
  --person "NAME=ACCOUNT_ID" --person "NAME=ACCOUNT_ID" \
  --input reports/<week>/exports/*.ndjson \
  --since YYYY-MM-DD --week YYYY-Www \
  --out reports/<week>/weekly-<week>.md
```

**Account ids are looked up, never inferred.** Taking one from an event's actor
field or from a "top assignee" list produces a plausible id belonging to
somebody else, and the report then renders that person's work as `no data`
rather than failing. Get them from Jira:

```
GET /rest/api/3/user/search?query=<name>
```
