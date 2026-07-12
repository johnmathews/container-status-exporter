# Architecture

## Overview

The Container Status Exporter is a two-module Python application that bridges the Portainer API and Prometheus: `app.py` exports container state/health across all managed Docker hosts, and `freshness.py` exports image freshness by comparing running image digests against upstream registries. Both render their metrics into the same `/metrics` response.

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

- **Main thread**: Runs a `ThreadingHTTPServer` serving `/metrics` and `/health` — each scrape is handled on its own per-connection daemon thread (with a 30 s socket timeout), so a slow or hung client cannot block other scrapes
- **Collector thread**: Daemon thread that calls `collect_all_metrics()` every `SCRAPE_INTERVAL` seconds. It builds each snapshot into local lists and publishes by a single reference swap (atomic publish), so a concurrent scrape never observes a cleared or partially-built list; renderers snapshot the shared references once at the top for the same reason
- **Freshness thread**: Daemon thread that runs one registry freshness cycle every `REGISTRY_CHECK_INTERVAL` seconds (default 6 h). Each cycle is wrapped in an exception guard (`_collect_safely`), so one failed cycle is logged and retried next interval instead of killing the thread

## Key Classes

| Class | Module | Purpose |
|-------|--------|---------|
| `PortainerExporter` | `app.py` | Core API client — fetches endpoints and containers, generates Prometheus output |
| `ContainerMetrics` | `app.py` | Dataclass holding per-container metric values |
| `MetricsHandler` | `app.py` | HTTP request handler for `/metrics` and `/health` (appends freshness output) |
| `ContainerState` | `app.py` | Enum mapping Docker states to numeric values |
| `HealthStatus` | `app.py` | Enum mapping health statuses to numeric values |
| `FreshnessCollector` | `freshness.py` | Joins Portainer's running-container view with registry state; renders freshness metrics |
| `RegistryClient` | `freshness.py` | Anonymous OCI-distribution client — token dance, digest HEADs, metadata by digest |
| `ImageRef` / `ImageFreshness` | `freshness.py` | Dataclasses: a parsed image reference / the per-container freshness result |

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

1 if the last collection reached Portainer, 0 if the Portainer endpoints fetch failed. On failure the previous metrics snapshot is retained (existing series keep being served) and `last_update` does not advance — the exporter reports its outage truthfully instead of serving an empty-but-"up" response.

### portainer_exporter_last_scrape_timestamp (gauge)

Unix timestamp of the last successful metrics collection.

## Offline Endpoint Handling

Portainer tracks Docker host availability. Each endpoint in the `/api/endpoints` response includes a `Status` field (1=up, 2=down). The exporter checks this before attempting to fetch containers:

- **Online endpoints (Status=1)**: Containers are fetched normally
- **Offline endpoints (Status=2)**: Skipped with a DEBUG-level log. No error is generated, `portainer_exporter_up` remains 1
- **HTTP 502/503 from container fetch**: Treated as "endpoint offline" (DEBUG log, no error). This handles cases where Portainer's status field hasn't yet caught up with the actual state.
- **Other HTTP errors (401, 500, etc.) from a container fetch**: Logged at ERROR, but do **not** set `last_error` — a single flaky endpoint must not flip `portainer_exporter_up` to 0 fleet-wide. Only a failure of the top-level `/api/endpoints` fetch (Portainer itself unreachable) sets `last_error`

## Portainer API Usage

- `GET /api/endpoints` — lists all Docker environments (includes `Status` field: 1=up, 2=down)
- `GET /api/endpoints/{id}/docker/containers/json?all=true` — lists all containers on a specific endpoint (proxied Docker API)
- `GET /api/endpoints/{id}/docker/images/{id}/json` — image inspect (used by `freshness.py` for `RepoDigests`, OCI version label, and build date)
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

Statuses: `ok`, `outdated`, `local` (locally-built image or a bare `sha256:...`
image ID — nothing upstream to compare), `pinned` (digest-pinned reference,
immutable), `error` (registry check failed).

### Registry hardening

- **HTTP 429 (rate limited)** is not an error verdict: the affected image's
  previous result is carried forward (or omitted if there is none), and the
  cycle logs a single WARNING. Metadata-fetch failures likewise never flip an
  already-compared image to `error` — metadata just degrades to empty.
- The digest HEAD is retried **once** after a 2 s backoff on transient
  transport errors; anonymous pull tokens are cached per (registry, repository)
  for 60 s (re-fetched once on a 401).
- Every HTTP response body (registry and Portainer) is read through a **4 MiB
  bounded reader**, so a hostile or broken server cannot OOM the exporter.
- Remote metadata is fetched by the **digest** just compared (not the tag),
  eliminating a HEAD-vs-GET race that could cache wrong metadata forever.

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

## Output Rendering

Both modules render Prometheus text exposition format by string assembly. Every
label value passes through `escape_label_value()` (`\` → `\\`, `"` → `\"`,
newline → `\n`), so hostile image names or version labels cannot corrupt the
exposition. The full metric surface is contract-locked by
`tests/test_contract.py` — see CLAUDE.md's editing guardrails.
