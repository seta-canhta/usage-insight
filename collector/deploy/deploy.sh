#!/usr/bin/env bash
# =============================================================================
# Deploy the AI telemetry ingestion collector to Cloud Run.
#
# Design : docs/spikes/ai-effectiveness-observability.md §11.1 (pipeline),
#          §11.4 (access control — "the collector service account can write
#          only to Pub/Sub")
# Contract: tools/ai-telemetry/schema/CONTRACT.md §1 (fail open, append only)
#
# ---------------------------------------------------------------------------
# THIS SCRIPT DOES NOT DEPLOY BY DEFAULT.
# No GCP project has been confirmed for this work. DRY_RUN defaults to 1, which
# prints every command instead of running it. Set DRY_RUN=0 deliberately, and
# only against a project you own.
# ---------------------------------------------------------------------------
#
# Required environment (no defaults, no hardcoded ids anywhere in this repo):
#   PROJECT_ID               target GCP project
#   REGION                   e.g. europe-west1
#
# Optional (defaults shown):
#   COLLECTOR_SA=aiep-collector
#   PUBSUB_TOPIC=ai-run-events
#   OTEL_TOPIC=ai-otel-spans
#   BQ_DATASET_RAW=raw
#   AR_REPO=aiep
#   IMAGE_TAG=$(git rev-parse --short HEAD)
#   COLLECTOR_INGRESS=internal-and-cloud-load-balancing
#   COLLECTOR_TOKEN_SECRET=aiep-collector-token
#   LOG_LEVEL=INFO
#   DRY_RUN=1
# =============================================================================

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COLLECTOR_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
: "${PROJECT_ID:?PROJECT_ID must be set — this script hardcodes no project id}"
: "${REGION:?REGION must be set}"

COLLECTOR_SA="${COLLECTOR_SA:-aiep-collector}"
PUBSUB_TOPIC="${PUBSUB_TOPIC:-ai-run-events}"
OTEL_TOPIC="${OTEL_TOPIC:-ai-otel-spans}"
BQ_DATASET_RAW="${BQ_DATASET_RAW:-raw}"
AR_REPO="${AR_REPO:-aiep}"
IMAGE_TAG="${IMAGE_TAG:-$(git -C "${COLLECTOR_DIR}" rev-parse --short HEAD 2>/dev/null || echo dev)}"
COLLECTOR_INGRESS="${COLLECTOR_INGRESS:-internal-and-cloud-load-balancing}"
COLLECTOR_TOKEN_SECRET="${COLLECTOR_TOKEN_SECRET:-aiep-collector-token}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"
DRY_RUN="${DRY_RUN:-1}"

SA_EMAIL="${COLLECTOR_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/aiep-telemetry-collector:${IMAGE_TAG}"

export PROJECT_ID REGION COLLECTOR_SA PUBSUB_TOPIC COLLECTOR_INGRESS \
       COLLECTOR_TOKEN_SECRET LOG_LEVEL IMAGE_URI

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }

run() {
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '    [dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

require_tool() {
  command -v "$1" >/dev/null 2>&1 || { warn "missing required tool: $1"; exit 2; }
}

for tool in gcloud docker envsubst; do require_tool "${tool}"; done

if [[ "${DRY_RUN}" == "1" ]]; then
  warn "DRY_RUN=1 — printing the plan, changing nothing. Set DRY_RUN=0 to apply."
fi

# ===========================================================================
# IAM — THE EXACT ROLES REQUIRED, AND NOTHING MORE
# ===========================================================================
#
# A. COLLECTOR RUNTIME SERVICE ACCOUNT  (${SA_EMAIL})
#    This identity runs the ingest service. Design §11.4: "the collector service
#    account can write only to Pub/Sub." Both data-plane bindings below are
#    RESOURCE-SCOPED to a single topic — never granted at project level.
#
#      roles/pubsub.publisher
#          on  projects/${PROJECT_ID}/topics/${PUBSUB_TOPIC}
#          why publish validated envelopes (the ONLY data-plane write it has)
#
#      roles/secretmanager.secretAccessor
#          on  projects/${PROJECT_ID}/secrets/${COLLECTOR_TOKEN_SECRET}
#          why read the shared bearer token at container start. Scoped to that
#              one secret. Omit this ONLY if the token is injected another way.
#
#      roles/logging.logWriter
#          on  the project
#          why emit the dq_payload_rejected findings and operational logs.
#              Project-scoped because Cloud Logging has no per-log-name grant.
#              It is write-only: it confers no read access to any log.
#
#    DELIBERATELY NOT GRANTED to the collector — each would breach §11.4:
#      roles/pubsub.subscriber      (it must never read the stream back)
#      roles/pubsub.viewer/admin    (no topic administration at runtime)
#      roles/bigquery.*             (it never touches the warehouse)
#      roles/storage.*              (no object storage of any kind)
#      roles/iam.serviceAccountTokenCreator (no identity impersonation)
#      any role on dim_person or any identity dataset
#
# B. OTEL COLLECTOR SERVICE ACCOUNT (if otel_config.yaml is deployed separately)
#      roles/pubsub.publisher  on projects/${PROJECT_ID}/topics/${OTEL_TOPIC}
#    ...and nothing else. Same reasoning.
#
# C. PUB/SUB SERVICE AGENT  (service-<PROJECT_NUMBER>@gcp-sa-pubsub.iam...)
#    Google-managed; needed for the BigQuery subscriptions that land the rows.
#      roles/bigquery.dataEditor    on dataset ${BQ_DATASET_RAW}
#      roles/bigquery.metadataViewer on dataset ${BQ_DATASET_RAW}
#    This is NOT the collector's identity and gives the collector nothing.
#
# D. CALLER / INVOKER
#    Requests carry the shared bearer token that main.py checks with
#    hmac.compare_digest. Choose ONE of:
#      * roles/run.invoker granted to the emitters' identity (preferred: two
#        independent factors — Google IAM plus the bearer token);
#      * roles/run.invoker on allUsers ONLY if laptops cannot obtain a Google
#        identity, in which case the bearer token is the sole factor and the
#        service MUST sit behind the corporate load balancer / Cloud Armor.
#    The default COLLECTOR_INGRESS above assumes the load-balancer path.
#
# E. DEPLOYER (a human or CI, NOT the collector)
#      roles/run.admin, roles/artifactregistry.writer,
#      roles/iam.serviceAccountUser (to act as ${SA_EMAIL}),
#      roles/pubsub.editor (topic creation), roles/secretmanager.admin
#      (secret creation). None of these are ever attached to the service.
# ===========================================================================

log "Enabling required APIs"
run gcloud services enable \
  run.googleapis.com \
  pubsub.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  --project "${PROJECT_ID}"

log "Creating the least-privilege runtime service account (idempotent)"
run gcloud iam service-accounts create "${COLLECTOR_SA}" \
  --project "${PROJECT_ID}" \
  --display-name "AI telemetry collector (Pub/Sub publish only)" || true

log "Creating Pub/Sub topics (idempotent)"
run gcloud pubsub topics create "${PUBSUB_TOPIC}" --project "${PROJECT_ID}" || true
run gcloud pubsub topics create "${OTEL_TOPIC}"   --project "${PROJECT_ID}" || true

log "Binding IAM — topic-scoped publisher only"
run gcloud pubsub topics add-iam-policy-binding "${PUBSUB_TOPIC}" \
  --project "${PROJECT_ID}" \
  --member "serviceAccount:${SA_EMAIL}" \
  --role "roles/pubsub.publisher"

log "Binding IAM — secret-scoped accessor for the bearer token"
run gcloud secrets add-iam-policy-binding "${COLLECTOR_TOKEN_SECRET}" \
  --project "${PROJECT_ID}" \
  --member "serviceAccount:${SA_EMAIL}" \
  --role "roles/secretmanager.secretAccessor"

log "Binding IAM — project-scoped log writer (write-only)"
run gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member "serviceAccount:${SA_EMAIL}" \
  --role "roles/logging.logWriter" \
  --condition=None

# ---------------------------------------------------------------------------
# Build and push
# ---------------------------------------------------------------------------
log "Building image ${IMAGE_URI}"
run docker build \
  --file "${SCRIPT_DIR}/Dockerfile" \
  --tag "${IMAGE_URI}" \
  "${COLLECTOR_DIR}"

log "Pushing image"
run gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
run docker push "${IMAGE_URI}"

# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------
log "Rendering cloudrun.yaml"
RENDERED="$(mktemp -t aiep-cloudrun.XXXXXX.yaml)"
trap 'rm -f "${RENDERED}"' EXIT
envsubst \
  '${PROJECT_ID} ${COLLECTOR_SA} ${IMAGE_URI} ${PUBSUB_TOPIC} ${COLLECTOR_INGRESS} ${COLLECTOR_TOKEN_SECRET} ${LOG_LEVEL}' \
  < "${SCRIPT_DIR}/cloudrun.yaml" > "${RENDERED}"
log "Rendered manifest at ${RENDERED}"
if [[ "${DRY_RUN}" == "1" ]]; then
  sed -n '1,40p' "${RENDERED}"
fi

log "Deploying to Cloud Run"
run gcloud run services replace "${RENDERED}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}"

# ---------------------------------------------------------------------------
# Post-deploy notes (printed, never executed automatically)
# ---------------------------------------------------------------------------
cat <<EOF

-----------------------------------------------------------------------------
NEXT STEPS — deliberately manual
-----------------------------------------------------------------------------
1. Create the BigQuery subscription that lands raw.ai_run_event:

     gcloud pubsub subscriptions create ${PUBSUB_TOPIC}-to-bq \\
       --project "${PROJECT_ID}" \\
       --topic "${PUBSUB_TOPIC}" \\
       --bigquery-table "${PROJECT_ID}:${BQ_DATASET_RAW}.ai_run_event" \\
       --use-table-schema

   ...and the equivalent for ${OTEL_TOPIC} -> ${BQ_DATASET_RAW}.otel_span.
   Grant the Pub/Sub service agent bigquery.dataEditor + bigquery.metadataViewer
   on the ${BQ_DATASET_RAW} dataset first (IAM note C above).

2. Verify the collector rejects what it must:

     curl -s -o /dev/null -w '%{http_code}\\n' "\${COLLECTOR_URL}/healthz"        # 200
     curl -s -o /dev/null -w '%{http_code}\\n' -XPOST "\${COLLECTOR_URL}/v1/events" \\
          -d '[]'                                                                # 401

3. Confirm the runtime service account holds NOTHING beyond the three roles in
   IAM note A:

     gcloud projects get-iam-policy "${PROJECT_ID}" \\
       --flatten='bindings[].members' \\
       --filter="bindings.members:${SA_EMAIL}" \\
       --format='table(bindings.role)'

   Expected output: roles/logging.logWriter, and nothing else at project level.

4. The spill file in the container is a crash-window buffer only (Cloud Run's
   filesystem is in-memory). If the Pub/Sub outage lasts longer than one
   revision's lifetime, events in the spill are lost with the instance. The
   durable offline buffer is the emitter's ~/.aiep/telemetry/pending/ queue
   (CONTRACT.md §8) — that is the layer designed to survive, not this one.
-----------------------------------------------------------------------------
EOF
