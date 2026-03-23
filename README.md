# Container Status Exporter

A Prometheus exporter that exports Docker container status and health metrics from the Portainer API.

## Features

- Container state metrics: running, paused, exited, created, restarting, dead
- Container health status: healthy, unhealthy, starting, none
- Container restart count tracking
- Multi-host support via Portainer API
- Graceful handling of offline endpoints (skipped, not errored)
- Prometheus-compatible text format output
- Built-in health check endpoint

## Quick Start

```bash
# Set your Portainer API token
export PORTAINER_TOKEN="your-token-here"

# Run with Docker Compose
docker compose up -d

# Check metrics
curl http://localhost:8081/metrics
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PORTAINER_URL` | `http://localhost:9000` | Portainer API base URL |
| `PORTAINER_TOKEN` | (required) | Portainer API token |
| `SCRAPE_INTERVAL` | `30` | Seconds between metric collections |
| `LISTEN_PORT` | `8081` | HTTP server port |
| `LOG_LEVEL` | `INFO` | Python logging level |

## Development

Requires [uv](https://docs.astral.sh/uv/).

```bash
# Run tests
uv run pytest

# Run tests with coverage
uv run pytest --cov=app --cov-report=term-missing

# Lint
uv run ruff check .

# Format
uv run ruff format .
```

### Test Structure

- `tests/test_enums.py` — Enum value mappings
- `tests/test_exporter.py` — PortainerExporter initialization and helpers
- `tests/test_api.py` — Portainer API interactions (mocked)
- `tests/test_metrics.py` — Metrics generation and output format
- `tests/test_handlers.py` — HTTP request handlers
- `tests/test_offline_endpoints.py` — Offline endpoint handling and error differentiation

## Documentation

- [Architecture](docs/architecture.md) — How the exporter works, metrics reference, Portainer API usage
- [Deployment](docs/deployment.md) — Docker, Prometheus, and troubleshooting

## License

MIT
