# Portainer Update Service

Keep your Portainer stacks up to date — automatically, on your schedule,
with a clean web UI.

Point it at your Portainer, and it checks all your stacks for newer images,
updates them for you, cleans up after itself, and reports what it did.
Runs on your own machine. No cloud, no accounts, no telemetry.

## Features

- **Dashboard** — update status, last run, next scheduled run at a glance
- **Update checks** — per-service badges against docker.io and ghcr.io
- **One-click updates** — full run or single stack, with live progress
- **Scheduled updates** — interval, daily, or weekly
- **Version pinning** — keep a service on a known-good version
- **Cleanup** — removes unused images and build cache, shows reclaimed space
- **History** — every run recorded with steps, duration, and full log
- **Backups** — Portainer datastore backup before every update run

## Requirements

- A running **Portainer** instance
- A Portainer **API key** with admin rights
  (*Portainer → Settings → API keys → Add API key*)

## Quick start

1. In Portainer: **Stacks → Add stack → Repository**
2. Point it at this repository (build method: **Dockerfile**,
   compose path: `docker-compose.yml`)
3. Add environment variables:

   | Variable | Example | Description |
   |---|---|---|
   | `PUS_LISTEN_HOST` | `0.0.0.0` | Required in a container |
   | `TZ` | `Europe/Berlin` | Timezone for the schedule |
   | `PUS_AUTH_TOKEN` | `change-me` | Recommended: protects the UI/API |

4. Deploy — the image is built on your host
5. Open the web UI and finish setup (Settings tab):
   1. Enter **Portainer URL** and **API key**
   2. Hit **Scan** → pick the endpoint that runs your stacks
   3. Hit **Test connection** → should answer with your Portainer version
   4. **Save** — the first update check starts automatically

All settings persist in the config volume and survive container rebuilds.

## Compose template

```yaml
services:
  update-service:
    build: https://github.com/<your-account>/Portainer-Update-Service.git
    container_name: update-service
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    ports:
      - "8090:8090"                     # change left side if 8090 is taken
    environment:
      PUS_LISTEN_HOST: "0.0.0.0"        # required in a container
      TZ: "Europe/Berlin"               # timezone for the schedule
      # optional - configure in the UI instead:
      # PUS_PORTAINER_URL: "https://portainer:9443"
      # PUS_PORTAINER_API_KEY: "ptr_xxx"
      # PUS_AUTH_TOKEN: "change-me"
      # PUS_NOTIFY_WEBHOOK: "https://ntfy.sh/your-topic"
    volumes:
      - /path/to/update-service/data:/app/data      # status/history/backups
      - /path/to/update-service/config:/app/config  # settings
    networks:
      - portainer-net   # join the network your Portainer is on

networks:
  portainer-net:
    external: true
    name: <the-network-your-portainer-runs-on>
```

The app talks exclusively to the Portainer API — no docker socket mount
needed for the base setup.

## Updating Portainer itself (optional)

Portainer itself is usually started outside Portainer (e.g. with plain
`docker compose`). To have this app update it as well, two things are
needed in this app's stack:

```yaml
    volumes:
      - /path/to/portainer/docker-compose:/host-portainer:ro  # Portainer's compose dir
      - /var/run/docker.sock:/var/run/docker.sock             # needs the daemon
```

Then in Settings, enable **Include Portainer updates** and hit **Check
compose mount** to verify everything is wired up. Every full update run
then finishes with a Portainer self-update
(`docker compose pull && docker compose up -d` in that directory).

## Environment variables (reference)

| Variable | Default | Description |
|---|---|---|
| `PUS_LISTEN_HOST` | `127.0.0.1` | Bind address — **must be `0.0.0.0` in containers** |
| `PUS_LISTEN_PORT` | `8090` | Web UI port inside the container |
| `TZ` | `UTC` | Timezone for the schedule (e.g. `Europe/Berlin`) |
| `PUS_PORTAINER_URL` | — | Portainer base URL (alternative: web UI setup) |
| `PUS_PORTAINER_API_KEY` | — | Portainer API key (alternative: web UI setup) |
| `PUS_PORTAINER_ENDPOINT_ID` | auto | Only if endpoint auto-detect picks wrong |
| `PUS_AUTH_TOKEN` | — | If set: mutating calls need `Authorization: Bearer <token>` |
| `PUS_NOTIFY_WEBHOOK` | — | POST target for failure notifications (ntfy-compatible) |
| `PUS_UPDATE_SCHEDULE_MODE` | interval | `interval` / `daily` / `weekly` |
| `PUS_UPDATE_INTERVAL_HOURS` | 168 | Interval mode only — every N hours |
| `PUS_UPDATE_SCHEDULE_TIME` | 03:30 | Run time (HH:MM, server-local) for daily/weekly |
| `PUS_UPDATE_SCHEDULE_DAY` | 0 | Weekday for weekly mode (0 = Monday) |
| `PUS_TLS_VERIFY` | false | Verify Portainer's TLS certificate |
| `PUS_MAX_PARALLEL_DEPLOYS` | 3 | Parallel stack redeploys |
| `PUS_DEPLOY_WAIT_TIME` | 300 | Seconds to wait for containers to become healthy |
| `PUS_KEEP_BACKUPS` | 5 | How many Portainer backups to keep |
| `PUS_CHECK_CACHE_MINUTES` | 30 | Registry result cache TTL |
| `PUS_INCLUDE_PORTAINER` | false | Also update Portainer itself |

Env vars override UI settings. Recommended: configure everything in the UI
and keep the env minimal.

## Using the app

**Dashboard** — update badges, reclaimable space, last/next run, server time.

**Stacks** — per-service update status. Open a stack to see its compose
file, pin a service to a specific tag, or redeploy it.

**Run update** performs the full sequence: backup Portainer → prune unused
images → redeploy all stacks (waits for each to become healthy) → verify →
repair networks → (optionally) update Portainer itself.

**History** — every run recorded with steps, duration, and full log.

## Configuration

Most settings live in the UI (**Settings** tab), grouped into Connection,
Schedule, Deployment, Repairs, and Clean Up. Everything is validated and
written to `config/config.yaml` in the mounted config volume.

See `config/config.example.yaml` for all keys and inline documentation.

## Security notes

- The UI has **no login** — set `auth_token` to protect every mutating
  endpoint, or put an authenticated reverse proxy in front.
- `tls_verify: false` accepts self-signed Portainer certificates (common on
  home networks). Set `true` for valid certificates.
- The API key is stored in `config.yaml` (never committed, never returned
  by the API) and needs **admin** rights (prune + backup are admin-only).
- No telemetry, no outbound calls except to your Portainer and the image
  registries you already use.

## API (for scripting)

| Method | Path | Description |
|---|---|---|
| GET  | `/api/status` | State, last run, current job, next run |
| GET  | `/api/stacks` | Stacks with per-service check results |
| GET  | `/api/stacks/<id>` | Stack detail + compose content |
| POST | `/api/stacks/<id>/image` | Pin version `{"service":"web","image":"nginx:1.27.3"}` |
| POST | `/api/stacks/<id>/redeploy` | Redeploy one stack |
| POST | `/api/check` | Force update check |
| POST | `/api/update` | Trigger full update run |
| POST | `/api/prune` | Prune images + build cache now |
| GET  | `/api/inventory` | Unused images/networks |
| GET  | `/api/runs/current` | Live progress of the active job |
| GET  | `/api/runs/<id>/log` | Run log (plain text) |
| GET  | `/api/versions?image=nginx` | Available tags |
| GET  | `/api/endpoints` | List Portainer endpoints |
| GET  | `/api/history` | Last 200 runs + 30d prune stats |
| GET/POST | `/api/settings` | View/change config |
| GET  | `/healthz` | Liveness + scheduler heartbeat |

Example:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" https://your-host:8090/api/update
```

## Project layout

```
├── app/                 Python package (API, engine, scheduler)
├── ui/index.html        the web UI (single file, no build step)
├── config/config.example.yaml
├── deploy/              Portainer stack templates
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── run.py               entry point
```

## License

MIT