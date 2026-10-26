FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    HOME=/tmp \
    KATIP_OPS_CONFIG=/opt/katip/ops/config \
    KATIP_CONFIG_DIR=/opt/katip/ops/config

WORKDIR /app
COPY LICENSE /app/LICENSE
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --requirement /app/requirements.lock \
    && groupadd --gid 10001 ops \
    && useradd --uid 10001 --gid 10001 --no-create-home ops
COPY --chown=10001:10001 src /app/src
USER 10001:10001
ENTRYPOINT ["python", "-m", "katip_ops"]
CMD ["serve"]
