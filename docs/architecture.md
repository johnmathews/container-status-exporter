# Architecture

## Overview

The Container Status Exporter is a single-file Python application (`app.py`) that bridges the Portainer API and Prometheus. It polls Portainer for container state across all managed Docker hosts and exposes the data as Prometheus metrics.

```
Portainer API (manages Docker hosts)
        |
        v
Container Status Exporter (Python, port 8081)
  - Polls every 30s (configurable)
  - Exports Prometheus-format metrics on /metrics
  - Health check on /health
        |
        v
Prometheus (scrapes /metrics)
        |
        v
Grafana (visualizes with State Timeline)
```

## Threading Model

- **Main thread**: Runs an `HTTPServer` serving `/metrics` and `/health` endpoints
- **Background thread**: Daemon thread that calls `collect_all_metrics()` on a configurable interval, updating the shared `metrics` list

## Key Classes

| Class | Purpose |
|-------|---------|
| `PortainerExporter` | Core API client — fetches endpoints and containers, generates Prometheus output |
| `ContainerMetrics` | Dataclass holding per-container metric values |
| `MetricsHandler` | HTTP request handler for `/metrics` and `/health` |
| `ContainerState` | Enum mapping Docker states to numeric values |
| `HealthStatus` | Enum mapping health statuses to numeric values |

## Metrics

### container_state (gauge)

```
container_state{container_name="...", hostname="...", image="..."} <value>
```

Values: 0=exited, 1=running, 2=paused, 3=created, 4=restarting, 5=dead, 6=unknown

### container_health (gauge)

```
container_health{container_name="...", hostname="...", image="..."} <value>
```

Values: 0=none, 1=healthy, 2=unhealthy, 3=starting

### container_restart_count (gauge)

```
container_restart_count{container_name="...", hostname="...", image="..."} <value>
```

### portainer_endpoint_status (gauge)

```
portainer_endpoint_status{hostname="..."} <value>
```

1 if the Portainer endpoint (Docker host) is online, 0 if offline. Offline endpoints are skipped during container collection — no error is generated.

### portainer_exporter_up (gauge)

1 if connected to Portainer, 0 if last collection had an error.

### portainer_exporter_last_scrape_timestamp (gauge)

Unix timestamp of the last successful metrics collection.

## Offline Endpoint Handling

Portainer tracks Docker host availability. Each endpoint in the `/api/endpoints` response includes a `Status` field (1=up, 2=down). The exporter checks this before attempting to fetch containers:

- **Online endpoints (Status=1)**: Containers are fetched normally
- **Offline endpoints (Status=2)**: Skipped with a DEBUG-level log. No error is generated, `portainer_exporter_up` remains 1
- **HTTP 502/503 from container fetch**: Treated as "endpoint offline" (DEBUG log, no error). This handles cases where Portainer's status field hasn't yet caught up with the actual state.
- **Other HTTP errors (401, 500, etc.)**: Treated as real errors (ERROR log, sets `last_error`)

## Portainer API Usage

- `GET /api/endpoints` — lists all Docker environments (includes `Status` field: 1=up, 2=down)
- `GET /api/endpoints/{id}/docker/containers/json?all=true` — lists all containers on a specific endpoint (proxied Docker API)
- Authentication: `X-API-Key` header with a Portainer API token

## Configuration

All configuration is via environment variables. See [CLAUDE.md](../CLAUDE.md) for the full table.

## Image Freshness (`freshness.py`)

A second background thread (default every 6 hours) answers "is a newer image
available for what is running?":

1. For every online Portainer endpoint, list containers and inspect each unique
   image (`GET /api/endpoints/{id}/docker/images/{id}/json`) for its
   `RepoDigests`, OCI version label, and build date.
2. For every unique image reference across the fleet, HEAD the manifest at the
   upstream registry to get the digest currently served for that tag.
   Anonymous auth uses the standard OCI token dance (401 -> WWW-Authenticate ->
   pull-scoped bearer token), which works for Docker Hub, ghcr.io, quay.io,
   gcr.io and lscr.io. HEAD requests do not count against Hub pull limits.
3. If the registry digest is not among the running image's `RepoDigests`, the
   container is `outdated`. Version and build-date metadata for the remote
   image comes from its config blob and is cached by digest (immutable).

Statuses: `ok`, `outdated`, `local` (locally-built image, nothing to compare),
`pinned` (digest-pinned reference, immutable), `error` (registry check failed).

### Freshness metrics

```
container_image_outdated{container_name, hostname, image} 0|1
container_image_info{container_name, hostname, image, status, current_version, available_version} 1
container_image_current_created_timestamp{container_name, hostname, image} <unix ts>
container_image_available_created_timestamp{container_name, hostname, image} <unix ts>
container_image_freshness_last_check_timestamp <unix ts>
```

Timestamps are only emitted when known, so "days behind" can be computed as
`available - current` without NaN noise.
