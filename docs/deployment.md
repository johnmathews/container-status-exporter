# 1. Deployment

## 1.1 Production topology

The exporter runs on the **infra host**, defined in `/srv/infra/docker-compose.yml`.

| What | Value |
|------|-------|
| Compose **service** name | `container-health-exporter` |
| Container **name** | `container-status-exporter` |
| Image | `ghcr.io/johnmathews/container-status-exporter:latest` |
| Prometheus job | `container-status` (scrapes `192.168.2.106:8081`) |

> **Naming split (historical):** the compose *service* is called `container-health-exporter` while the *container* (and this repo, and the image) are called `container-status-exporter`. This is a historical inconsistency, not an error in this document. `docker compose` commands take the **service** name; `docker logs`/`docker exec` take the **container** name.

Downstream consumers (Grafana dashboard, alert rules, label-rename behavior) are listed in [CLAUDE.md](../CLAUDE.md).

## 1.2 Deploying a new version

CI (`.github/workflows/build-and-push.yml`) builds and pushes `:latest` to ghcr.io on every push to `main`, and can also be triggered manually via `workflow_dispatch` (the "Run workflow" button in the Actions tab).

The infra compose setup never pulls implicitly (`pull_policy` is effectively `never`), so `docker compose up` alone will **not** fetch a new image. On the infra host, run:

```bash
docker pull ghcr.io/johnmathews/container-status-exporter:latest
docker compose -f /srv/infra/docker-compose.yml up -d --force-recreate container-health-exporter
```

Rollback: `:latest` is the only moving tag the deploy uses (CI also pushes a `:main` branch tag and per-commit sha tags), so roll back by re-pushing a previous commit to `main` (CI rebuilds) or by pinning the previous image digest in the compose file.

## 1.3 Configuration

All configuration is via environment variables (set in `/srv/infra/.env` in production):

| Variable | Default | Description |
|----------|---------|-------------|
| `PORTAINER_URL` | `http://localhost:9000` | Portainer API base URL |
| `PORTAINER_TOKEN` | (required) | Portainer API token |
| `SCRAPE_INTERVAL` | `30` | Seconds between metric collections |
| `LISTEN_PORT` | `8081` | HTTP server port |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `FRESHNESS_ENABLED` | `true` | Enable image freshness checks (registry digest comparison) |
| `REGISTRY_CHECK_INTERVAL` | `21600` | Seconds between registry freshness cycles (6h) |
| `REGISTRY_TIMEOUT` | `10` | Per-request timeout for registry calls (seconds) |
| `REGISTRY_PLATFORM` | `linux/amd64` | Platform used to resolve multi-arch manifests |

### 1.3.1 The `.env` token gotcha

Portainer API tokens can end in `=` padding. When extracting the token from `/srv/infra/.env`, split on the **first** `=` only:

```bash
# Correct — keeps '=' padding in the value:
grep '^PORTAINER_TOKEN=' /srv/infra/.env | cut -d= -f2-

# WRONG — silently truncates a token containing '=':
grep '^PORTAINER_TOKEN=' /srv/infra/.env | cut -d= -f2
```

## 1.4 Outbound registry traffic

With `FRESHNESS_ENABLED=true` (the default), the exporter makes **anonymous HTTPS requests to public image registries** every `REGISTRY_CHECK_INTERVAL` seconds: `registry-1.docker.io` and `auth.docker.io` (Docker Hub), `ghcr.io`, `quay.io`, `gcr.io`, and `lscr.io` — whichever registries the running images come from. If the exporter must not talk to the internet, set `FRESHNESS_ENABLED=false` — that is the kill switch; container state/health metrics are unaffected.

Rate-limit budget: Docker Hub allows anonymous pulls of 100 per 6 hours per IPv4 address, but **manifest HEAD requests are free** (exempt from the pull count). The exporter's steady state is HEAD-only — metadata GETs happen only when an image's digest actually changes (results are cached in memory by digest) — so a normal cycle spends none of the pull budget. A cold start with many changed images spends some, shared with everything else on the same IP.

## 1.5 Running it elsewhere

Prerequisites: a running Portainer instance and an API token (Portainer UI: Account Settings > API tokens).

This repo's `docker-compose.yml` runs the exporter standalone (service `exporter`, built locally, Portainer at `http://host.docker.internal:9000` for Docker Desktop):

```bash
echo 'PORTAINER_TOKEN=your_api_token_here' > .env
docker compose up -d
```

Or add it to an existing stack:

```yaml
  container-health-exporter:
    image: ghcr.io/johnmathews/container-status-exporter:latest
    container_name: container-status-exporter
    ports:
      - "8081:8081"
    environment:
      PORTAINER_URL: "http://your-portainer-host:9000"
      PORTAINER_TOKEN: "${PORTAINER_TOKEN}"
    restart: unless-stopped
```

Manual image build, if ever needed (CI normally does this):

```bash
docker build -t ghcr.io/johnmathews/container-status-exporter:latest .
docker push ghcr.io/johnmathews/container-status-exporter:latest
```

## 1.6 Prometheus scrape config

The production job already exists. For a new environment, add to `prometheus.yml`:

```yaml
  - job_name: 'container-status'
    scrape_interval: 30s
    static_configs:
      - targets: ['<exporter-host>:8081']
```

Note: Prometheus does not set `honor_labels` for this job, so the exporter's `hostname` label arrives downstream as `exported_hostname` (see [CLAUDE.md](../CLAUDE.md)).

## 1.7 Verification

```bash
curl http://192.168.2.106:8081/metrics   # metrics endpoint (or localhost on the host)
curl http://192.168.2.106:8081/health    # health endpoint (JSON, includes last_error)
docker logs container-status-exporter    # container logs (container name, not service name)
```

## 1.8 Troubleshooting

**"PORTAINER_TOKEN environment variable is required"**
Set `PORTAINER_TOKEN`. If it was extracted from `.env`, check the `=`-padding gotcha (section 1.3.1).

**`portainer_exporter_up 0`**
Portainer itself is unreachable (the endpoints fetch failed). The exporter keeps the previous container series and reports truthfully; check `PORTAINER_URL`, token validity, and network reachability. The `/health` endpoint echoes the last error.

**Offline Portainer endpoints (Docker hosts)**
Silently skipped by design — `portainer_endpoint_status{hostname="..."} 0` is emitted, no error is raised, and `portainer_exporter_up` stays 1. HTTP 502/503 from a container fetch is treated the same way.

**`container_image_info` shows `status="error"` for one repo, persistently**
Usually a dead or unauthorized upstream repository (e.g. the booklore image, whose Docker Hub repo went away). This is the desired signal — the registry genuinely cannot vouch for that image — not an exporter fault.

**`container_image_freshness_last_check_timestamp 0`**
No freshness cycle has completed yet. The first cycle after startup can take several minutes on a large fleet; wait and re-check before debugging.

**Registry rate limiting (HTTP 429)**
The cycle logs a single WARNING and carries forward each affected image's previous result instead of flipping it to `error`. Images with no previous result are omitted for that cycle.

**Metrics not updating**
- `docker logs container-status-exporter`
- `curl .../health` for the last error
- Verify Portainer is reachable from the container's network
