FROM python:3.12-slim

LABEL org.opencontainers.image.title="DualRoute" \
      org.opencontainers.image.description="Administration de deux sorties Ethernet et de Tailscale pour ZimaOS" \
      org.opencontainers.image.source="https://github.com/mkdevtests/dualroute" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DUALROUTE_DATA_DIR=/var/lib/dualroute \
    DUALROUTE_PORT=9080

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        conntrack curl iproute2 iputils-ping nftables procps util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/dualroute
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN mkdir -p /var/lib/dualroute

EXPOSE 9080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${DUALROUTE_PORT}/api/health" || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${DUALROUTE_PORT}"]
