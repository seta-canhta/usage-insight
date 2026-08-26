# =============================================================================
# usage-insight collection endpoint — container image
#
# Contract: docs/TRANSPORT.md
#
# The service is stdlib-only apart from boto3, which is only reached by the
# s3:// backend. With --store file:// it needs nothing at all, which is how it
# is tested and how it can be run before any AWS credential exists.
#
# No bucket, region, token, domain or whitelist appears here. Everything that
# varies is an environment variable bound at deploy time.
# =============================================================================

FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so the layer caches independently of the source.
COPY server/requirements.txt /app/server/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/server/requirements.txt

# The service, and the client module it shares rather than reimplements.
# cli/identity.py is the whitelist contract; a second copy of it in this image
# is exactly the drift ARCHITECTURE.md refuses. tests/ is deliberately absent.
COPY server/proxy.py server/store.py /app/server/
COPY cli/identity.py /app/cli/identity.py

# The watchdog, which runs from this same image with a different entrypoint
# (see server/compose.yaml). It belongs here because it is the piece that
# notices the endpoint has stopped receiving -- deployed separately, it is the
# piece most likely to be the one nobody deployed.
COPY importers/watch.py importers/pull.py /app/importers/
# The shared library. It moved out of `pollers/` to the repository root when
# it became clear that cli/, importers/, report/ and collector/ all depend on
# it -- see CLAUDE.md. `importers/watch.py` and `pull.py` add /app to sys.path
# and `import common`, so the package has to sit at /app/common.
COPY common/__init__.py /app/common/__init__.py

RUN groupadd --system --gid 10001 insight \
 && useradd --system --uid 10001 --gid insight --no-create-home insight \
 && mkdir -p /var/lib/insight \
 && chown -R insight:insight /var/lib/insight

USER 10001:10001

# Loopback by default even in a container: whatever fronts this is what should
# face the network. Override INSIGHT_HOST=0.0.0.0 only behind a proxy.
ENV INSIGHT_HOST=127.0.0.1 \
    INSIGHT_PORT=8479 \
    LOG_LEVEL=INFO

EXPOSE 8479

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('INSIGHT_PORT','8479')+'/healthz', timeout=2).status==200 else 1)"

ENTRYPOINT ["python", "/app/server/proxy.py"]
