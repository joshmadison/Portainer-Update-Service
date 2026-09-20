# Portainer Update Service - container image.
# Includes the docker CLI (mounted socket) so prune / Portainer backup /
# Portainer self-update work inside the container.
FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# docker CLI for prune/backup/self-update via the mounted docker.sock.
# Installed from Docker's official apt repo (docker-ce-cli): the plain
# "docker.io" Debian package is huge (ships the daemon too) and unreliable
# across debian point releases. Fallback: if both apt sources fail, the app
# degrades gracefully (prune/backup skipped with a warning) - see updater.
# docker-compose-plugin: needed for Portainer self-update when Portainer
# runs as a plain compose project (compose_dir mode) - `docker compose pull/up`.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
        -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
        https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/* \
    && docker --version && docker compose version

COPY app/ app/
COPY ui/ ui/
COPY run.py .

# persistent state (status, history, run logs, backups)
VOLUME ["/app/data"]
EXPOSE 8090
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8090/healthz || exit 1
CMD ["python", "run.py"]