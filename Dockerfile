# SourceHub -- container image for Unraid / any Docker host.
#
# Built on Microsoft's Playwright image rather than python:slim. Four of the working
# adapters (Banggood, eBay, AliExpress detail, Chinavasion) need a real Chromium to
# run the page's JavaScript, and Chromium pulls in ~90 system libraries. Installing
# those onto a slim base by hand is a long, brittle apt incantation that breaks on
# every Debian point release; this image is maintained by the Playwright team and
# already matches the browser build the Python package expects.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

LABEL org.opencontainers.image.title="SourceHub" \
      org.opencontainers.image.description="Cross-marketplace product aggregation and price comparison" \
      org.opencontainers.image.source="https://github.com/yourname/sourcehub"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Everything mutable lives under /config, which is the volume Unraid maps to
    # appdata. Nothing the app writes should land inside the image.
    SOURCEHUB_CONFIG_DIR=/config \
    SOURCEHUB_DB_URL=sqlite:////config/db/sourcehub.db \
    SOURCEHUB_MEDIA_DIR=/config/media \
    SOURCEHUB_BROWSER_PROFILE=/config/browser_profile \
    SOURCEHUB_HEADLESS=true \
    SOURCEHUB_MODE=serve \
    SOURCEHUB_PORT=8000 \
    PUID=99 \
    PGID=100 \
    UMASK=022 \
    TZ=Etc/UTC

WORKDIR /app

# Dependencies first so a source edit does not invalidate the pip layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sourcehub/ ./sourcehub/
COPY config.yaml providers.yaml duty.yaml freight.yaml ./
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# The defaults are copied into /config on first start (see entrypoint), so keep a
# pristine copy inside the image to seed from and to diff against after upgrades.
RUN mkdir -p /defaults \
    && cp config.yaml providers.yaml duty.yaml freight.yaml /defaults/

EXPOSE 8000
VOLUME ["/config"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('SOURCEHUB_PORT','8000')+'/healthz', timeout=8).status==200 else 1)"

ENTRYPOINT ["/entrypoint.sh"]
