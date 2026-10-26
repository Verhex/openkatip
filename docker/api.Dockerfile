FROM docker:28-cli AS dockercli
FROM python:3.12-slim AS control-base

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin katip \
    && mkdir -p /data && chown katip:katip /data
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
COPY web ./web
COPY docker/worker-seccomp.json ./docker/worker-seccomp.json
RUN pip install --no-cache-dir --no-deps .
USER 1000:1000

FROM control-base AS manager
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
CMD ["python", "-m", "katip.workers.manager"]

FROM control-base AS parser
EXPOSE 8001
CMD ["python", "-m", "katip.documents.server"]

FROM control-base AS api
# Meeting browsers run in workers; the API still renders trusted PDF reports.
USER root
RUN playwright install --with-deps chromium
USER 1000:1000
EXPOSE 8000
CMD ["uvicorn", "katip.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--forwarded-allow-ips=127.0.0.1", "--ws-max-size=4194304"]
