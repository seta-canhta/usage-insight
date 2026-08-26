#!/usr/bin/env bash
# retain_metrics.sh -- stop deleting the metrics the agents already compute.
#
# Design reference: docs/spikes/ai-effectiveness-observability.md
#   Sec 14.1 item 4 -- "the highest value-per-hour action in this entire document"
#   Sec 3.2         -- inventory of the telemetry artifacts the agents produce today
#
# THE PROBLEM, IN ONE SENTENCE
#   The qualdev agents compute requirement coverage, automation coverage, scenario
#   counts, step reuse and a time-saved figure into .tmp/ -- and then
#   developer.implementer phase 'tmp_management: on_success: Clean all .tmp/' deletes
#   them. Every run produces real measurement and throws it away. Nothing downstream
#   in this design can be built until that stops.
#
# WHAT THIS DOES
#   Copies the surviving artifacts to a durable directory keyed by
#   {jira_key}/{UTC timestamp}, writes a small manifest, and appends one row to a
#   flat index. Run it at the END of a workflow, BEFORE any .tmp cleanup. From the
#   first run onwards a time series exists. That is the whole point: no pipeline, no
#   collector, no warehouse -- just stop deleting.
#
#   config.yaml:129 already reserves ".tmp/test-spec/08-metrics" for metrics output,
#   so per-agent metrics files there are collected too. They are still under .tmp/,
#   which is gitignored and cleaned; this script is what makes them survive.
#
# Copy, never move: the running workflow may still need the originals.
#
# Usage:
#   ./retain_metrics.sh --jira AUT-632
#   ./retain_metrics.sh                          # jira key auto-read from workflow-context.json
#   ./retain_metrics.sh --jira AUT-632 --source /path/to/project --dest ~/.aiep/metrics
#   ./retain_metrics.sh --dry-run
#   AIEP_METRICS_HOME=/mnt/share/aiep-metrics ./retain_metrics.sh
#
# Exit codes: 0 retained (or dry run), 3 nothing found to retain.
# Never returns non-zero for a partially missing artifact -- an agent must be able to
# call this unconditionally without risking its own run.

set -euo pipefail

SOURCE_ROOT="$(pwd)"
DEST_ROOT="${AIEP_METRICS_HOME:-$HOME/.aiep/metrics}"
JIRA_KEY=""
DRY_RUN=0
RUN_ID="${AIEP_RUN_ID:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --jira)    JIRA_KEY="${2:-}"; shift 2 ;;
    --source)  SOURCE_ROOT="${2:-}"; shift 2 ;;
    --dest)    DEST_ROOT="${2:-}"; shift 2 ;;
    --run-id)  RUN_ID="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

CTX="$SOURCE_ROOT/.tmp/test-spec/workflow-context.json"

# Jira key resolution, in the order the workflow itself resolves it:
#   explicit flag  ->  qd_jira_key  ->  jira_key  ->  UNKNOWN
# Deliberately no `jq` dependency: this must run on a laptop with nothing installed.
json_str() {  # $1 file, $2 key
  [ -f "$1" ] || return 0
  sed -n 's/.*"'"$2"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$1" | head -1
}

if [ -z "$JIRA_KEY" ] && [ -f "$CTX" ]; then
  JIRA_KEY="$(json_str "$CTX" qd_jira_key)"
  [ -z "$JIRA_KEY" ] && JIRA_KEY="$(json_str "$CTX" jira_key)"
fi
[ -z "$JIRA_KEY" ] && JIRA_KEY="UNKNOWN"
# Keep the key filesystem-safe without changing its meaning.
JIRA_KEY="$(printf '%s' "$JIRA_KEY" | tr -c 'A-Za-z0-9._-' '_')"

TS="$(date -u '+%Y%m%dT%H%M%SZ')"
TARGET="$DEST_ROOT/$JIRA_KEY/$TS"

# Artifacts named in design 14.1 item 4, plus the 08-metrics directory that
# config.yaml already reserves. Globs are intentional; missing entries are skipped.
ARTIFACTS="
.tmp/test-spec/04-specifications/spec-metrics.json
.tmp/test-spec/workflow-context.json
.tmp/test-spec/execution-context.json
.tmp/test-spec/intent-analysis.json
.tmp/test-spec/workflow-summary.md
.tmp/test-results/cucumber-report.json
.tmp/quality-gates-*.log
.tmp/test-spec/08-metrics/*.json
"

echo "retain_metrics"
echo "  source : $SOURCE_ROOT"
echo "  jira   : $JIRA_KEY"
echo "  target : $TARGET"
[ -n "$RUN_ID" ] && echo "  run_id : $RUN_ID"

FOUND=0
COPIED=0
PLAN=""

# `set -f`/`set +f` toggles globbing so the unexpanded patterns above survive the
# here-string and expand only where we want them to.
for pattern in $ARTIFACTS; do
  set +f
  for src in $SOURCE_ROOT/$pattern; do
    [ -f "$src" ] || continue
    FOUND=$((FOUND + 1))
    rel="${src#"$SOURCE_ROOT"/}"
    PLAN="${PLAN}${rel}
"
  done
  set -f
done
set +f

if [ "$FOUND" -eq 0 ]; then
  echo "  nothing to retain -- no matching artifacts under $SOURCE_ROOT/.tmp/"
  echo "  (this is not an error: run it after a workflow, before .tmp cleanup)"
  exit 3
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "  would retain $FOUND file(s):"
  printf '%s' "$PLAN" | sed 's/^/    /'
  exit 0
fi

mkdir -p "$TARGET"

printf '%s' "$PLAN" | while IFS= read -r rel; do
  [ -n "$rel" ] || continue
  dst="$TARGET/$rel"
  mkdir -p "$(dirname "$dst")"
  cp -p "$SOURCE_ROOT/$rel" "$dst" 2>/dev/null || continue
done
COPIED=$(find "$TARGET" -type f ! -name manifest.json | wc -l | tr -d ' ')

# Manifest: enough context to make the copied files interpretable a year from now.
# No content is summarised or transformed -- the files are kept verbatim.
GIT_BRANCH="$(git -C "$SOURCE_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"
GIT_SHA="$(git -C "$SOURCE_ROOT" rev-parse --short HEAD 2>/dev/null || echo '')"
GIT_REMOTE="$(git -C "$SOURCE_ROOT" remote get-url origin 2>/dev/null || echo '')"
TARGET_PROFILE="$(json_str "$CTX" target_profile)"

{
  printf '{\n'
  printf '  "retained_at": "%s",\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf '  "jira_key": "%s",\n' "$JIRA_KEY"
  printf '  "run_id": "%s",\n' "$RUN_ID"
  printf '  "target_profile": "%s",\n' "$TARGET_PROFILE"
  printf '  "source_root": "%s",\n' "$SOURCE_ROOT"
  printf '  "git_branch": "%s",\n' "$GIT_BRANCH"
  printf '  "git_sha": "%s",\n' "$GIT_SHA"
  printf '  "git_remote": "%s",\n' "$GIT_REMOTE"
  printf '  "host": "%s",\n' "$(hostname -s 2>/dev/null || echo unknown)"
  printf '  "file_count": %s,\n' "$COPIED"
  printf '  "files": [\n'
  first=1
  printf '%s' "$PLAN" | while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    [ -f "$TARGET/$rel" ] || continue
    [ "$first" -eq 1 ] && first=0 || printf ',\n'
    printf '    "%s"' "$rel"
  done
  printf '\n  ]\n}\n'
} > "$TARGET/manifest.json"

# Flat append-only index -- one line per retained run. This is the "time series"
# that starts accumulating today, readable by anything, including `sort` and awk.
INDEX="$DEST_ROOT/index.tsv"
if [ ! -f "$INDEX" ]; then
  printf 'retained_at\tjira_key\trun_id\tbranch\tgit_sha\tfile_count\tpath\n' > "$INDEX"
fi
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
  "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$JIRA_KEY" "${RUN_ID:-}" \
  "$GIT_BRANCH" "$GIT_SHA" "$COPIED" "$TARGET" >> "$INDEX"

echo "  retained $COPIED file(s)"
echo "  manifest : $TARGET/manifest.json"
echo "  index    : $INDEX"
echo
echo "Next: run this at the end of every workflow, BEFORE .tmp cleanup."
echo "Nothing else in the telemetry design has to exist for this to start paying off."
