# Portainer Update Service - container image.
# Includes the docker CLI (mounted socket) so prune / Portainer backup /
# Portainer self-update work inside the container.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# docker CLI for prune/backup/self-update via the mounted docker.sock
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl docker.io \
    && rm -rf /var/lib/apt/lists/*
COPY app/ app/
COPY ui/ ui/
COPY run.py .
# persistent state (status, history, run logs, backups)
VOLUME ["/app/data"]
EXPOSE 8090
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8090/healthz || exit 1
CMD ["python", "run.py"]