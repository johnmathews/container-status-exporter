# Manual Setup Guide

After building and pushing the Docker image, follow these steps to integrate it with your infrastructure.

## Step 1: Build & Push Docker Image

```bash
cd container-status-exporter
docker build -t ghcr.io/johnmathews/container-status-exporter:latest .
docker push ghcr.io/johnmathews/container-status-exporter:latest
```

## Step 2: Add to infra_vm Docker Compose

SSH into infra_vm and edit `/srv/infra/docker-compose.yml`:

```bash
ssh john@192.168.2.106
nano /srv/infra/docker-compose.yml
```

Add this service after the `portainer` service:

```yaml
  portainer-exporter:
    image: ghcr.io/johnmathews/container-status-exporter:latest
    container_name: portainer-exporter
    ports:
      - "8081:8081"
    environment:
      PORTAINER_URL: "http://192.168.2.106:9000"
      PORTAINER_TOKEN: "your_api_token_here"  # Replace with actual token
      SCRAPE_INTERVAL: "30"
      LISTEN_PORT: "8081"
      LOG_LEVEL: "INFO"
    restart: always
    depends_on:
      - portainer
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8081/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 5s
```

Start the container:

```bash
docker-compose up -d portainer-exporter
docker logs portainer-exporter
```

## Step 3: Add Prometheus Scrape Job

On the Prometheus host, edit the Prometheus config (usually at `/srv/apps/prometheus/prometheus.yml`):

Add this job after the `nut` job:

```yaml
   - job_name: 'container-status'
     scrape_interval: 30s
     scrape_timeout: 15s
     static_configs:
       - targets: ['192.168.2.106:8081']
         labels: {hostname: 'infra'}
     relabel_configs:
       - source_labels: [__address__]
         regex: '([^:]+)(?::\d+)?'
         target_label: host
         replacement: '$1'
```

Restart Prometheus to apply the config:

```bash
docker-compose down prometheus
docker-compose up -d prometheus
```

## Step 4: Verify Metrics

Check that Prometheus is scraping the metrics:

1. Open Prometheus: http://192.168.2.115:9090
2. Go to Status → Targets
3. Look for `container-status` job
4. Should show `1/1 up`

Test the metrics directly:

```bash
curl http://192.168.2.106:8081/metrics | head -20
```

## Step 5: Import Grafana Dashboard

1. Open Grafana: http://192.168.2.106:3000
2. Go to Dashboards → New → Import
3. Click "Upload JSON file"
4. Select: `/path/to/container-status.json`
5. Choose Prometheus as datasource
6. Click Import

The dashboard should now show container states in real-time!

## Troubleshooting

**Metrics not appearing:**
- Check exporter logs: `docker logs portainer-exporter`
- Verify Portainer token is correct
- Check connectivity: `curl http://192.168.2.106:9000/api/endpoints -H "X-API-Key: YOUR_TOKEN"`

**Prometheus shows DOWN:**
- Check exporter is running: `docker ps | grep portainer-exporter`
- Check firewall between prometheus and infra_vm

**Grafana dashboard empty:**
- Wait 3-5 minutes for initial data collection
- Check Prometheus has data: query `container_state` directly
- Verify datasource is correctly configured

## Next Time

When updating the exporter code:

1. Make changes in `container-status-exporter/`
2. Rebuild and push image
3. On infra_vm: `docker-compose pull portainer-exporter && docker-compose up -d portainer-exporter`
4. Done!
