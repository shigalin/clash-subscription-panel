FROM python:3.13-slim-trixie@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1
ARG VERSION=dev
LABEL org.opencontainers.image.source="https://github.com/shigalin/clash-subscription-panel"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 APP_VERSION=$VERSION
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./
USER 10001:10001
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=3 CMD ["python", "/app/healthcheck.py"]
CMD ["python", "/app/panel.py"]
