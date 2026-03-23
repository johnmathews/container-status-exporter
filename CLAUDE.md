# Container Status Exporter

## Project Principles

- **Single-file application**: All logic lives in `app.py`. Keep it this way for transparency and ease of deployment.
- **Minimal dependencies**: Only `requests` at runtime. No frameworks.

## Editing Guardrails

- **Metric names and label structure** (`container_state`, `container_health`, `container_restart_count`, `portainer_exporter_up`, `portainer_exporter_last_scrape_timestamp`) must not be changed without updating downstream Prometheus queries and Grafana dashboards.
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
