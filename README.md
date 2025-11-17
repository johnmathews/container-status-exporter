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

```
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

```
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

```
portainer_exporter_up           # 1 if connected to Portainer, 0 if error
portainer_exporter_last_scrape_timestamp  # Unix timestamp of last successful scrape
```

## Setup

### 1. Generate Portainer API Token

1. Log into Portainer UI (http://your-portainer:9000)
2. Go to Account Settings (bottom left, your username)
3. Under "API tokens", click "Generate new API token"
4. Copy the token (you'll only see it once)

### 2. Configure Environment Variables

```bash
export PORTAINER_URL="http://192.168.2.106:9000"
export PORTAINER_TOKEN="<your-api-token>"
export SCRAPE_INTERVAL=30  # seconds
export LISTEN_PORT=8081
export LOG_LEVEL=INFO
```

### 3. Run with Docker

```bash
docker build -t container-status-exporter .
docker run -e PORTAINER_URL=http://host.docker.internal:9000 \
           -e PORTAINER_TOKEN=<token> \
           -p 8081:8081 \
           container-status-exporter
```

Or with docker-compose:

```bash
cp docker-compose.yml docker-compose.override.yml
# Edit docker-compose.override.yml with your PORTAINER_TOKEN
docker-compose up -d
```

### 4. Verify Metrics

```bash
curl http://localhost:8081/metrics
curl http://localhost:8081/health
```

## Integration with Prometheus

Add to your Prometheus `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: "container-status"
    scrape_interval: 30s
    scrape_timeout: 15s
    static_configs:
      - targets: ["192.168.2.106:8081"]
        labels:
          hostname: "infra"
```

## Integration with Grafana

Create a State Timeline panel with query:

```promql
container_state{hostname=~"$hostname"}
```

Configure value mappings:

- 0 → "Exited" (gray)
- 1 → "Running" (green)
- 2 → "Paused" (yellow)
- 3 → "Created" (blue)
- 4 → "Restarting" (orange)
- 5 → "Dead" (red)

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

```
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
