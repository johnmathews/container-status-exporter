# Container Status Exporter

A Prometheus exporter that exports Docker container status (running, paused, exited, etc.) and health information from Portainer API.

## Features

- Exports container state metrics: running, paused, exited, created, restarting, dead
- Exports container health status: healthy, unhealthy, starting, none
- Exports container restart count
- Multi-host support via Portainer API
- Prometheus-compatible text format output
- Built-in health check endpoint

## Metrics

### Container State

```text
container_state{container_name="...", hostname="...", image="..."}
```

Values:

- 0 = exited
- 1 = running
- 2 = paused
- 3 = created
- 4 = restarting
- 5 = dead
- 6 = unknown

### Container Health

```text
container_health{container_name="...", hostname="...", image="..."}
```

Values:

- 0 = none (no health check)
- 1 = healthy
- 2 = unhealthy
- 3 = starting

### Container Restart Count

```
container_restart_count{container_name="...", hostname="...", image="..."}
```

### Exporter Status

```text
portainer_exporter_up           # 1 if connected to Portainer, 0 if error
portainer_exporter_last_scrape_timestamp  # Unix timestamp of last successful scrape
```

## Testing

### Run All Tests

```bash
pytest
```

### Run with Coverage Report

```bash
pytest --cov=app --cov-report=html --cov-report=term
```

### Run Specific Test File

```bash
pytest tests/test_enums.py
```

### Run Specific Test

```bash
pytest tests/test_enums.py::TestContainerState::test_running_value
```

### Run Tests with Verbose Output

```bash
pytest -v
```

### Test Structure

- `tests/test_enums.py` - Enum value mappings
- `tests/test_exporter.py` - PortainerExporter initialization and helpers
- `tests/test_api.py` - Portainer API interactions (mocked)
- `tests/test_metrics.py` - Metrics generation and output format
- `tests/test_handlers.py` - HTTP request handlers

Coverage is currently **95%+** across the codebase.

## Requirements

- Python 3.11+
- Portainer API token
- Network access to Portainer API
- Prometheus scraping the /metrics endpoint

## Troubleshooting

### "PORTAINER_TOKEN environment variable is required"

Make sure the PORTAINER_TOKEN environment variable is set.

### "Failed to fetch endpoints"

- Check PORTAINER_URL is correct and accessible
- Verify PORTAINER_TOKEN is valid (regenerate if needed)
- Check network connectivity

### Metrics not updating

- Check logs: `docker logs container-status-exporter`
- Verify Portainer is accessible from the container
- Check the /health endpoint for errors

## Architecture

```text
Portainer API
    ↓
Container Status Exporter (Python)
    ↓
Prometheus (scrapes /metrics every 30s)
    ↓
Grafana (visualizes with State Timeline)
```

## License

MIT
