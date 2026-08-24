#!/usr/bin/env bash
# scan_ai_commits.sh — AI-authored commit share, from git history alone.
#
# Design reference: docs/spikes/ai-effectiveness-observability.md §2.3, §14.1 item 1.
#
# Answers exactly one question: "of every commit reachable in this repository, what
# share carries an AI provenance marker, by whom, and since when?"
#
# No new infrastructure. Read-only. Works offline against an already-cloned repo.
#
# Usage:
#   ./scan_ai_commits.sh                       # scan the current repo
#   ./scan_ai_commits.sh /path/a /path/b       # scan several repos, plus a grand total
#   MARKERS='AUTH_BY_COPILOT|GEN_BY_COPILOT' ./scan_ai_commits.sh
#   ./scan_ai_commits.sh --tsv                 # machine-readable monthly rows on stdout
#
# ---------------------------------------------------------------------------
# WHY THE DETECTION PATTERN IS DELIBERATELY NARROW
# ---------------------------------------------------------------------------
# The `aiep-impact-report-generator` skill on origin/feature/ATSP-22288 detects AI
# commits with a keyword regex that ends:
#
#     …|^feat[\(:]|^feat\b|^fix[\(:]|^fix\b|^chore[\(:]|^chore\b)
#
# .github/copilot-instructions.md §5 MANDATES Conventional Commits repo-wide, so
# those three alternations match essentially every human commit in the repository
# as well. Any AI share computed that way is an over-estimate of unknown magnitude
# — here it would sweep in a large part of the 300+ non-marked commits and report
# something near 90% instead of 12%.
#
# This script therefore matches ONLY the explicit provenance markers that the
# agents actually write (skills/git-commits, developer.implementer phase_2/phase_4).
# It never infers AI authorship from commit *style*. A false negative (an agent that
# skipped the marker) is an acceptable, bounded error; a false positive silently
# destroys the credibility of every downstream number.
#
# Marker placement in this repo's real history is inconsistent, so all three
# observed placements must match (design §2.3):
#   prefix  "[AUTH_BY_COPILOT] feat(x): …"
#   infix   "[AUTH_BY_COPILOT] : refactor(x): …"   and   "AMS-1856: docs: [AUTH_BY_COPILOT] …"
#   suffix  "feat(x): … [AUTH_BY_COPILOT] [TICKET-123]"
# A plain substring match on the marker token covers all three; anchoring would not.
# ---------------------------------------------------------------------------

set -euo pipefail

MARKERS="${MARKERS:-AUTH_BY_COPILOT|GEN_BY_COPILOT}"

# Bracketed form is the convention; unbracketed occurrences are counted but also
# reported separately because a subject may merely *mention* the marker in prose.
# Double backslashes: these strings are consumed by awk as *dynamic* regexes, so awk
# strips one level of escaping before compiling. "\[" would degrade to a character
# class; "\\[" is what reaches the regex engine as a literal bracket.
MARKER_RE="(${MARKERS})"
MARKER_RE_STRICT='\\['"(${MARKERS})"'\\]'

US=$'\037'   # unit separator — safe field delimiter; commit subjects contain | and ,

TSV=0
REPOS=()
for arg in "$@"; do
  case "$arg" in
    --tsv) TSV=1 ;;
    -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
    *) REPOS+=("$arg") ;;
  esac
done
[ ${#REPOS[@]} -eq 0 ] && REPOS=(".")

WORK="$(mktemp -d "${TMPDIR:-/tmp}/scan_ai_commits.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

ALL="$WORK/all.tsv"          # repo  sha  month  date  author_name  author_email  subject
: > "$ALL"

for repo in "${REPOS[@]}"; do
  if ! git -C "$repo" rev-parse --git-dir >/dev/null 2>&1; then
    echo "warning: not a git repository, skipping: $repo" >&2
    continue
  fi
  name="$(basename "$(cd "$repo" && pwd)")"
  # --all covers every local and remote-tracking ref; git log already deduplicates a
  # commit reachable from several refs, so no extra dedupe pass is needed.
  git -C "$repo" log --all --no-decorate \
      --pretty=format:"%H${US}%ad${US}%an${US}%ae${US}%s" --date=short 2>/dev/null \
    | awk -v FS="$US" -v OFS='\t' -v r="$name" '
        NF>=5 {
          sub_ = $5
          for (i=6; i<=NF; i++) sub_ = sub_ $i          # subject containing US, defensive
          gsub(/\t/, " ", sub_)
          print r, $1, substr($2,1,7), $2, $3, tolower($4), sub_
        }' >> "$ALL"
done

if [ ! -s "$ALL" ]; then
  echo "No commits found in: ${REPOS[*]}" >&2
  exit 1
fi

report_one() {  # $1 = label, $2 = tsv file scoped to that label
  local label="$1" f="$2"
  local total marked strict loose_only first last authors

  total=$(wc -l < "$f" | tr -d ' ')
  marked=$(awk -F'\t' -v re="$MARKER_RE" '$7 ~ re' "$f" | wc -l | tr -d ' ')
  strict=$(awk -F'\t' -v re="$MARKER_RE_STRICT" '$7 ~ re' "$f" | wc -l | tr -d ' ')
  loose_only=$((marked - strict))

  printf '\n=== %s ===\n' "$label"
  printf '  Total commits (all refs)          : %s\n' "$total"
  printf '  AI-marked commits                 : %s\n' "$marked"
  if [ "$total" -gt 0 ]; then
    printf '  AI share                          : %s%%\n' \
      "$(awk -v m="$marked" -v t="$total" 'BEGIN{printf "%.1f", (t?100*m/t:0)}')"
  fi
  printf '  ... of which bracketed [MARKER]   : %s\n' "$strict"
  printf '  ... unbracketed (verify by hand)  : %s\n' "$loose_only"

  if [ "$marked" -gt 0 ]; then
    first=$(awk -F'\t' -v re="$MARKER_RE" '$7 ~ re {print $4}' "$f" | sort | head -1)
    last=$(awk  -F'\t' -v re="$MARKER_RE" '$7 ~ re {print $4}' "$f" | sort | tail -1)
    authors=$(awk -F'\t' -v re="$MARKER_RE" '$7 ~ re {print $5"|"$6}' "$f" | sort -u | wc -l | tr -d ' ')
    printf '  Date range of AI-marked commits   : %s -> %s\n' "$first" "$last"
    printf '  Distinct author identities (AI)   : %s\n' "$authors"
    printf '  Distinct author identities (all)  : %s\n' \
      "$(awk -F'\t' '{print $5"|"$6}' "$f" | sort -u | wc -l | tr -d ' ')"
    printf '  NOTE: "identities" are raw git name|email pairs, not people.\n'
    printf '        Run identity_collisions.sh before treating these as person counts.\n'

    printf '\n  Authors of AI-marked commits (identity, count):\n'
    awk -F'\t' -v re="$MARKER_RE" '$7 ~ re {print $5" <"$6">"}' "$f" \
      | sort | uniq -c | sort -rn | awk '{printf "    %5d  ", $1; $1=""; sub(/^ /,""); print}'

    if [ "$loose_only" -gt 0 ]; then
      printf '\n  Unbracketed marker mentions (likely prose, review these):\n'
      awk -F'\t' -v re="$MARKER_RE" -v sre="$MARKER_RE_STRICT" \
        '$7 ~ re && $7 !~ sre {printf "    %s  %s\n", substr($2,1,8), $7}' "$f"
    fi
  fi

  printf '\n  Monthly breakdown:\n'
  printf '    %-9s %8s %8s %8s\n' "month" "commits" "ai" "share"
  awk -F'\t' -v re="$MARKER_RE" '
      { t[$3]++ } $7 ~ re { a[$3]++ }
      END { for (m in t) printf "%s\t%d\t%d\n", m, t[m], (m in a ? a[m] : 0) }
    ' "$f" | sort | awk -F'\t' '{
      printf "    %-9s %8d %8d %7.1f%%\n", $1, $2, $3, ($2 ? 100*$3/$2 : 0)
    }'
}

if [ "$TSV" -eq 1 ]; then
  printf 'repo\tmonth\tcommits\tai_commits\n'
  awk -F'\t' -v re="$MARKER_RE" '
      { t[$1 FS $3]++ } $7 ~ re { a[$1 FS $3]++ }
      END { for (k in t) { split(k, p, FS); printf "%s\t%s\t%d\t%d\n", p[1], p[2], t[k], (k in a ? a[k] : 0) } }
    ' "$ALL" | sort
  exit 0
fi

echo "AI-authored commit scan"
echo "  markers : ${MARKERS}"
echo "  repos   : ${REPOS[*]}"
echo "  scanned : $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

NREPOS=$(awk -F'\t' '{print $1}' "$ALL" | sort -u | wc -l | tr -d ' ')
for r in $(awk -F'\t' '{print $1}' "$ALL" | sort -u); do
  awk -F'\t' -v r="$r" '$1 == r' "$ALL" > "$WORK/one.tsv"
  report_one "$r" "$WORK/one.tsv"
done

if [ "$NREPOS" -gt 1 ]; then
  report_one "ALL REPOS" "$ALL"
fi

cat <<'EOF'

What this number does and does not mean
  DOES   : the share of commits an agent explicitly claimed authorship of.
  DOES NOT: how much of the code AI wrote (a marked commit may be one line);
            whether that code was reviewed, merged, reworked, or reverted;
            what it cost. The marker is applied by policy, not enforced by a
            hook, so opt-out is silent and this is a LOWER BOUND.
EOF
