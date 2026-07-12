# Container Status Exporter

## Project Principles

- **Two small files, no framework**: container state lives in `app.py`, image freshness in `freshness.py`. Keep it this flat for transparency and ease of deployment.
- **Minimal dependencies**: Only `requests` at runtime. No frameworks.

## Editing Guardrails

- **Metric names and label structure** (`container_state`, `container_health`, `container_restart_count`, `portainer_exporter_up`, `portainer_exporter_last_scrape_timestamp`, `container_image_outdated`, `container_image_info`, `container_image_current_created_timestamp`, `container_image_available_created_timestamp`) must not be changed without updating downstream Prometheus queries and Grafana dashboards.
- HTTP response format must remain Prometheus text exposition format compliant (HELP/TYPE comments, `metric_name{labels} value` lines).

## Development

- Use `uv` for all dependency management.
- Run tests: `uv run pytest`
- Run tests with coverage: `uv run pytest --cov=app --cov-report=term-missing`
- Lint: `uv run ruff check .`
- Format: `uv run ruff format .`

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORTAINER_URL` | `http://localhost:9000` | Portainer API base URL |
| `PORTAINER_TOKEN` | (required) | Portainer API token |
| `SCRAPE_INTERVAL` | `30` | Seconds between metric collections |
| `LISTEN_PORT` | `8081` | HTTP server port |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `FRESHNESS_ENABLED` | `true` | Enable image freshness checks (registry digest comparison) |
| `REGISTRY_CHECK_INTERVAL` | `21600` | Seconds between registry freshness cycles (6h) |
| `REGISTRY_TIMEOUT` | `10` | Per-request timeout for registry calls |
| `REGISTRY_PLATFORM` | `linux/amd64` | Platform used to resolve multi-arch manifests |
