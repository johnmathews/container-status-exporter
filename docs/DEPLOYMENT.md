# Deployment

## Prerequisites

- A running Portainer instance with API access
- A Portainer API token (generate in Portainer UI: Account Settings > API tokens)
- Docker for building and running the image

## Docker Image

The image is built and pushed automatically by GitHub Actions on push to `main`. It is available at:

```
ghcr.io/johnmathews/container-status-exporter:latest
```

### Manual Build

```bash
docker build -t ghcr.io/johnmathews/container-status-exporter:latest .
docker push ghcr.io/johnmathews/container-status-exporter:latest
```

## Running with Docker Compose

Create a `.env` file:

```
PORTAINER_TOKEN=your_api_token_here
```

Then:

```bash
docker compose up -d
```

The default `docker-compose.yml` assumes Portainer is accessible at `http://host.docker.internal:9000` (Docker Desktop). Adjust `PORTAINER_URL` for your environment.

## Adding to an Existing Stack

Add this service to your existing `docker-compose.yml`:

```yaml
  portainer-exporter:
    image: ghcr.io/johnmathews/container-status-exporter:latest
    container_name: portainer-exporter
    ports:
      - "8081:8081"
    environment:
      PORTAINER_URL: "http://your-portainer-host:9000"
      PORTAINER_TOKEN: "${PORTAINER_TOKEN}"
      SCRAPE_INTERVAL: "30"
      LISTEN_PORT: "8081"
      LOG_LEVEL: "INFO"
    restart: unless-stopped
```

## Prometheus Scrape Config

Add to your `prometheus.yml`:

```yaml
  - job_name: 'container-status'
    scrape_interval: 30s
    static_configs:
      - targets: ['<exporter-host>:8081']
```

## Verification

```bash
# Check metrics endpoint
curl http://localhost:8081/metrics

# Check health endpoint
curl http://localhost:8081/health

# Check container logs
docker logs container-status-exporter
```

## Troubleshooting

**"PORTAINER_TOKEN environment variable is required"**
Set the `PORTAINER_TOKEN` environment variable.

**"Failed to fetch endpoints"**
- Verify `PORTAINER_URL` is correct and reachable from the container
- Verify the API token is valid (regenerate if needed)
- Check network connectivity

**Metrics not updating**
- Check logs: `docker logs container-status-exporter`
- Check the `/health` endpoint for error details
- Verify Portainer is accessible from the container's network
