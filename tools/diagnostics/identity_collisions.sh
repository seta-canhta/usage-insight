#!/usr/bin/env bash
# identity_collisions.sh — find git author identities that collide or fragment.
#
# Design reference: docs/spikes/ai-effectiveness-observability.md §5.2 (identifier
# catalogue), §9.4 (data quality), CONTRACT.md §2.1 (`person_id` is the canonical
# person key; `person_email_hash` is derived from the git author email).
#
# WHY THIS RUNS BEFORE ANYTHING ELSE
#   Every per-person metric in the design keys on a person, but git only records a
#   free-text (name, email) pair chosen by the developer's local config. One human
#   routinely appears as several identities and, occasionally, two humans share one.
#   Until an identity map exists, "distinct authors" is a count of git configs, not
#   of people, and every per-person rate is wrong by an unknown factor.
#
#   This script does not fix anything. It produces the worklist for building
#   core.dim_person, and emits a starter map you can hand-correct.
#
# Read-only. No network. No new infrastructure.
#
# Usage:
#   ./identity_collisions.sh                    # current repo
#   ./identity_collisions.sh /path/a /path/b    # union across repos
#   ./identity_collisions.sh --map              # also print a YAML identity-map stub
#   CORP_DOMAIN=example.com ./identity_collisions.sh

set -euo pipefail

CORP_DOMAIN="${CORP_DOMAIN:-example.com}"

EMIT_MAP=0
REPOS=()
for arg in "$@"; do
  case "$arg" in
    --map) EMIT_MAP=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) REPOS+=("$arg") ;;
  esac
done
[ ${#REPOS[@]} -eq 0 ] && REPOS=(".")

WORK="$(mktemp -d "${TMPDIR:-/tmp}/identity_collisions.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

RAW="$WORK/raw.tsv"     # count  name  email_lower  email_asgiven
: > "$WORK/pairs.tsv"

for repo in "${REPOS[@]}"; do
  if ! git -C "$repo" rev-parse --git-dir >/dev/null 2>&1; then
    echo "warning: not a git repository, skipping: $repo" >&2
    continue
  fi
  # Author identity, not committer: authorship is what attribution follows.
  git -C "$repo" log --all --pretty=format:'%an	%ae' 2>/dev/null >> "$WORK/pairs.tsv"
  printf '\n' >> "$WORK/pairs.tsv"
done

awk -F'\t' 'NF==2 && $1 != "" {c[$1 "\t" tolower($2) "\t" $2]++}
            END {for (k in c) print c[k] "\t" k}' "$WORK/pairs.tsv" \
  | sort -t'	' -k1,1nr > "$RAW"

if [ ! -s "$RAW" ]; then
  echo "No author identities found in: ${REPOS[*]}" >&2
  exit 1
fi

TOTAL_IDS=$(wc -l < "$RAW" | tr -d ' ')
TOTAL_EMAILS=$(awk -F'\t' '{print $3}' "$RAW" | sort -u | wc -l | tr -d ' ')

echo "Git author identity collision report"
echo "  repos            : ${REPOS[*]}"
echo "  corporate domain : ${CORP_DOMAIN}"
echo "  raw identities   : ${TOTAL_IDS}  (distinct name+email pairs)"
echo "  distinct emails  : ${TOTAL_EMAILS}"
echo "  ==> the gap between these two numbers is the fragmentation to resolve."

findings=0

# ---------------------------------------------------------------------------
# F1 — one email, several display names.  The commonest case, and always the
#      same person: safe to merge automatically on email.
# ---------------------------------------------------------------------------
printf '\n[F1] Same email, multiple display names  (SAFE TO MERGE ON EMAIL)\n'
# One record per email on a single line (names joined by ";"), sorted, then expanded —
# sorting multi-line awk output directly would scramble headers away from their lists.
awk -F'\t' '{n[$3] = n[$3] ";" $2 "  (" $1 " commits)"; c[$3]++}
    END {for (e in c) if (c[e] > 1) print e "\t" substr(n[e], 2)}' "$RAW" \
  | sort \
  | awk -F'\t' '{ printf "  %s\n", $1
                  k = split($2, p, ";")
                  for (i = 1; i <= k; i++) printf "      - %s\n", p[i] }'
n1=$(awk -F'\t' '{c[$3]++} END {k=0; for (e in c) if (c[e]>1) k++; print k}' "$RAW")
[ "$n1" -eq 0 ] && printf '  (none)\n'
findings=$((findings + n1))

# ---------------------------------------------------------------------------
# F2 — one person, several emails.  Detected by normalising the display name
#      (lowercase, strip punctuation/spaces). NOT safe to merge blind: two
#      different people can normalise to the same short name. Human review.
# ---------------------------------------------------------------------------
printf '\n[F2] Same normalised name, multiple emails  (REVIEW BEFORE MERGING)\n'
awk -F'\t' '{
      k = tolower($2); gsub(/[^a-z0-9]/, "", k)
      if (k == "") next
      key = k SUBSEP $3
      if (!(key in seen)) { seen[key]=1; cnt[k]++; lst[k] = lst[k] ";" $2 " <" $4 ">  (" $1 " commits)" }
    }
    END {for (k in cnt) if (cnt[k] > 1) print k "\t" substr(lst[k], 2)}' "$RAW" \
  | sort \
  | awk -F'\t' '{ printf "  %s\n", $1
                  k = split($2, p, ";")
                  for (i = 1; i <= k; i++) printf "      - %s\n", p[i] }'
n2=$(awk -F'\t' '{k=tolower($2); gsub(/[^a-z0-9]/,"",k); if(k=="")next; key=k SUBSEP $3;
      if(!(key in seen)){seen[key]=1; c[k]++}} END {n=0; for (k in c) if (c[k]>1) n++; print n}' "$RAW")
[ "$n2" -eq 0 ] && printf '  (none)\n'
findings=$((findings + n2))

# ---------------------------------------------------------------------------
# F3 — near-duplicate names across DIFFERENT emails, by Levenshtein distance <= 2.
#      Catches transposition typos ("Rathore" / "Rtahore") that F1 misses when the
#      typo also came with a different email. O(n^2) over distinct names, which is
#      trivial at repository scale.
# ---------------------------------------------------------------------------
printf '\n[F3] Near-duplicate names across different emails (edit distance <= 2)  (REVIEW)\n'
awk -F'\t' '
  function lev(a, b,   la, lb, i, j, cost, prev, cur, tmp) {
    la = length(a); lb = length(b)
    if (la == 0) return lb
    if (lb == 0) return la
    for (j = 0; j <= lb; j++) prev[j] = j
    for (i = 1; i <= la; i++) {
      cur[0] = i
      for (j = 1; j <= lb; j++) {
        cost = (substr(a, i, 1) == substr(b, j, 1)) ? 0 : 1
        cur[j] = prev[j] + 1
        if (cur[j-1] + 1 < cur[j]) cur[j] = cur[j-1] + 1
        if (prev[j-1] + cost < cur[j]) cur[j] = prev[j-1] + cost
      }
      for (j = 0; j <= lb; j++) prev[j] = cur[j]
    }
    return prev[lb]
  }
  {
    k = tolower($2); gsub(/[^a-z0-9]/, "", k)
    if (k == "" || length(k) < 4) next
    if (!(k in email)) { name[++n] = k; email[k] = $3; disp[k] = $2 " <" $4 ">" }
    else if (index(email[k], $3) == 0) { email[k] = email[k] "," $3; disp[k] = disp[k] "; " $2 " <" $4 ">" }
  }
  END {
    hits = 0
    for (i = 1; i <= n; i++) for (j = i+1; j <= n; j++) {
      a = name[i]; b = name[j]
      if (a == b) continue
      # skip pairs that already share an email — F1 covers those
      split(email[a], ea, ","); split(email[b], eb, ",")
      shared = 0
      for (x in ea) for (y in eb) if (ea[x] == eb[y]) shared = 1
      if (shared) continue
      d = lev(a, b)
      if (d <= 2) { printf "  distance %d:\n      - %s\n      - %s\n", d, disp[a], disp[b]; hits++ }
    }
    if (hits == 0) print "  (none)"
    print hits > "/dev/stderr"
  }' "$RAW" 2> "$WORK/n3"
n3=$(cat "$WORK/n3" 2>/dev/null || echo 0)
findings=$((findings + ${n3:-0}))

# ---------------------------------------------------------------------------
# F4 — unusable email: no "@", or an obviously synthetic/placeholder address.
#      These cannot be hashed into a stable person_email_hash (CONTRACT §2.1) and
#      must be mapped by hand or excluded from person-level metrics entirely.
# ---------------------------------------------------------------------------
printf '\n[F4] Missing or unusable email  (CANNOT BE HASHED — map by hand or exclude)\n'
awk -F'\t' '$3 !~ /@/ || $3 ~ /^(git|root|unknown|none)@/ || $3 ~ /@(localhost|stash|example\.com)$/ {
      printf "  %-28s <%s>  (%s commits)\n", $2, $4, $1 }' "$RAW"
n4=$(awk -F'\t' '$3 !~ /@/ || $3 ~ /^(git|root|unknown|none)@/ || $3 ~ /@(localhost|stash|example\.com)$/' "$RAW" | wc -l | tr -d ' ')
[ "$n4" -eq 0 ] && printf '  (none)\n'
findings=$((findings + n4))

# ---------------------------------------------------------------------------
# F5 — non-corporate domains. Not necessarily wrong (contractors, forge-generated
#      noreply addresses), but they will not join to a corporate directory, so
#      team_id and person_id stay null for them.
# ---------------------------------------------------------------------------
printf '\n[F5] Non-corporate email domain  (will not join to the directory)\n'
awk -F'\t' -v d="@${CORP_DOMAIN}" '$3 ~ /@/ && index($3, d) == 0 {
      printf "  %-28s <%s>  (%s commits)\n", $2, $4, $1 }' "$RAW"
n5=$(awk -F'\t' -v d="@${CORP_DOMAIN}" '$3 ~ /@/ && index($3, d) == 0' "$RAW" | wc -l | tr -d ' ')
[ "$n5" -eq 0 ] && printf '  (none)\n'

# ---------------------------------------------------------------------------
# F6 — email recorded in more than one letter case. Hashing is case-sensitive, so
#      an un-normalised hash would split one person in two. CONTRACT §2.1 already
#      mandates lower(email) before hashing; this confirms it matters here.
# ---------------------------------------------------------------------------
printf '\n[F6] Same email in multiple letter cases  (lowercase before hashing)\n'
awk -F'\t' '{if (!($3 SUBSEP $4 in s)) {s[$3 SUBSEP $4]=1; c[$3]++; l[$3]=l[$3] " " $4}}
    END {for (e in c) if (c[e] > 1) printf "  %s ->%s\n", e, l[e]}' "$RAW" | sort
n6=$(awk -F'\t' '{if(!($3 SUBSEP $4 in s)){s[$3 SUBSEP $4]=1; c[$3]++}} END {n=0; for(e in c) if(c[e]>1) n++; print n}' "$RAW")
[ "$n6" -eq 0 ] && printf '  (none)\n'

printf '\n----------------------------------------------------------------\n'
printf 'Summary\n'
printf '  F1 emails with multiple names   : %s\n' "$n1"
printf '  F2 names with multiple emails   : %s\n' "$n2"
printf '  F3 near-duplicate name pairs    : %s\n' "${n3:-0}"
printf '  F4 unusable emails              : %s\n' "$n4"
printf '  F5 non-corporate domains        : %s\n' "$n5"
printf '  F6 case-variant emails          : %s\n' "$n6"
printf '  raw identities -> people (est.) : %s -> %s\n' "$TOTAL_IDS" "$TOTAL_EMAILS"
printf '\nAction: resolve F1 automatically on lowercased email; review F2/F3 with the\n'
printf 'people concerned; decide per case for F4/F5. The result is core.dim_person.\n'

if [ "$EMIT_MAP" -eq 1 ]; then
  printf '\n# ---- identity-map stub (hand-correct, then load into core.dim_person) ----\n'
  printf '# person_id must come from the Atlassian directory (CONTRACT.md 2.1);\n'
  printf '# TODO markers below are deliberate — do not load with TODOs still present.\n'
  printf 'people:\n'
  awk -F'\t' '{ if (!($3 in seen)) { seen[$3]=1; order[++n]=$3; primary[$3]=$2 }
                aka[$3] = aka[$3] "\n      - \"" $2 "\"" ; cnt[$3] += $1 }
      END { for (i = 1; i <= n; i++) { e = order[i]
              printf "  - person_id: TODO_ATLASSIAN_ACCOUNT_ID\n"
              printf "    display_name: \"%s\"\n", primary[e]
              printf "    emails: [\"%s\"]\n", e
              printf "    commits: %d\n", cnt[e]
              printf "    aka:%s\n", aka[e] } }' "$RAW"
fi

exit 0
