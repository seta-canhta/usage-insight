# =============================================================================
# AI telemetry ingestion collector — container image
#
# Design: docs/spikes/ai-effectiveness-observability.md §11.1 (Cloud Run collector)
#
# The service is stdlib-only at runtime (see the header of main.py for why).
# google-cloud-pubsub is the single optional wheel; when it is absent, or when
# AIEP_PUBSUB_PROJECT is unset, the collector spills to a local NDJSON file
# instead of losing events.
#
# No project id, region, endpoint or credential appears here. Everything that
# varies is an environment variable bound at deploy time.
# =============================================================================

FROM python:3.13-slim AS base

# --- build/runtime hardening -------------------------------------------------
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so the layer caches independently of the source.
COPY deploy/requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

# Application source. tests/ is deliberately NOT copied into the image.
COPY main.py /app/main.py

# --- non-root ---------------------------------------------------------------
# The spill directory must be writable by the runtime user; Cloud Run's
# filesystem is an in-memory overlay, so the spill is a crash-window buffer,
# not durable storage. Durability comes from Pub/Sub; see deploy.sh notes.
RUN groupadd --system --gid 10001 aiep \
 && useradd --system --uid 10001 --gid aiep --no-create-home aiep \
 && mkdir -p /var/tmp/aiep \
 && chown -R aiep:aiep /var/tmp/aiep

USER 10001:10001

ENV PORT=8080 \
    AIEP_PUBSUB_TOPIC=ai-run-events \
    AIEP_FALLBACK_PATH=/var/tmp/aiep/ai-run-events.ndjson \
    AIEP_FINDINGS_PATH=/var/tmp/aiep/dq_findings.ndjson \
    AIEP_DEDUP_TTL_SECONDS=900 \
    LOG_LEVEL=INFO

EXPOSE 8080

# Cloud Run has its own health probing; this HEALTHCHECK is for local/docker use.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/healthz', timeout=2).status==200 else 1)"

ENTRYPOINT ["python", "/app/main.py"]
