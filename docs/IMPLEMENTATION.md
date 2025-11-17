# Container Status Exporter Implementation Guide

This document outlines the complete implementation of the Portainer Container Status Exporter for monitoring Docker container states across all hosts.

## What Has Been Created

### 1. Exporter Code (`/container-status-exporter/`)
A complete Python application that:
- Connects to Portainer API
- Queries all Docker endpoints (hosts)
- Fetches container states from each endpoint
- Exports Prometheus metrics on port 8081

**Files:**
- `app.py` - Main exporter application
- `requirements.txt` - Python dependencies (requests)
- `Dockerfile` - Docker image definition
- `docker-compose.yml` - For local testing
- `README.md` - Complete documentation

### 2. Ansible Role (`/roles/portainer_exporter/`)
Automated deployment of the exporter container

**Files:**
- `tasks/main.yml` - Deployment tasks
- `defaults/main.yml` - Configuration defaults
- `handlers/main.yml` - Handler for restart notifications

### 3. Updated Configurations
- **`roles/infra_vm/templates/docker-compose.yml.j2`** - Added portainer-exporter service
- **`roles/prometheus_lxc/templates/prometheus/prometheus.yml.j2`** - Added container-status scrape job
- **`playbooks/infra_vm.yml`** - Added portainer_exporter role
- **`roles/infra_vm/files/grafana/dashboards/container-status.json`** - Dashboard for visualization

## What You Need To Do

### STEP 1: Generate Portainer API Token (Manual)

1. Log into Portainer UI: `http://192.168.2.106:9000`
2. Click your **username** in the bottom-left corner → **"Account Settings"**
3. Scroll to **"API tokens"** section
4. Click **"Generate new API token"**
5. **Copy the token immediately** (you won't see it again)
6. Save it somewhere secure (you'll need it in the next step)

### STEP 2: Add Portainer Credentials to Vault

You need to add the Portainer token to your encrypted vault.yml:

```bash
# Edit the vault
ansible-vault edit group_vars/all/vault.yml
```

Add these lines to the vault (maintaining YAML structure):
```yaml
vault_portainer_api_token: "your_actual_token_here"
```

### STEP 3: Build and Push Docker Image

Since the exporter image is referenced as `ghcr.io/johnmathews/container-status-exporter:latest`, you need to build and push it:

```bash
cd container-status-exporter

# Build the image locally
docker build -t ghcr.io/johnmathews/container-status-exporter:latest .

# Push to GitHub Container Registry
# First, you may need to log in:
# echo $GITHUB_TOKEN | docker login ghcr.io -u johnmathews --password-stdin
docker push ghcr.io/johnmathews/container-status-exporter:latest

# Alternatively, for local testing only:
docker build -t container-status-exporter:latest .
# Then update the image in the docker-compose to use just the local name
```

### STEP 4: Test the Exporter Locally (Optional but Recommended)

```bash
cd container-status-exporter

# Create a .env file for testing
cat > .env << 'ENVEOF'
PORTAINER_URL=http://192.168.2.106:9000
PORTAINER_TOKEN=your_token_here
ENVEOF

# Run with docker-compose
docker-compose up -d

# Check logs
docker-compose logs -f exporter

# Test metrics endpoint
curl http://localhost:8081/metrics

# Test health endpoint
curl http://localhost:8081/health

# Cleanup
docker-compose down
```

### STEP 5: Deploy with Ansible

Once you've added the vault credentials and built the image, deploy everything:

```bash
# Run the full site playbook (or just infra_vm)
make site

# Or run just the infra_vm playbook
ansible-playbook playbooks/infra_vm.yml -i inventory.ini

# Specifically tag just the exporter
ansible-playbook playbooks/infra_vm.yml -i inventory.ini -t portainer_exporter
```

### STEP 6: Verify Everything Works

After deployment, verify the metrics are being collected:

```bash
# SSH into infra_vm
ssh john@192.168.2.106

# Check if the container is running
docker ps | grep portainer-exporter

# Check logs
docker logs portainer-exporter

# Test the metrics endpoint
curl http://localhost:8081/metrics

# Check Prometheus is scraping
# Visit: http://192.168.2.115:9090/targets (prometheus)
# Look for "container-status" job - should show 1up endpoint
```

### STEP 7: View in Grafana

1. Navigate to Grafana: `http://192.168.2.106:3000`
2. Go to **Dashboards** → **Browse** → Search for **"Container Status"**
3. You should see three panels:
   - **Container State Timeline** - Shows running/paused/exited status over time
   - **Container Health Timeline** - Shows healthy/unhealthy/starting status
   - **Container Restart Count** - Shows restart frequency

4. Use the **"Hostname"** dropdown to filter by specific hosts

## Architecture

```
┌─────────────────────────────────────────────┐
│    Portainer API (192.168.2.106:9000)       │
│    Manages all Docker hosts/endpoints       │
└──────────────────┬──────────────────────────┘
                   │
        ┌──────────┼──────────┐
        │          │          │
    proxmox    media-vm   jellyfin (+ 6 more)
    (docker1) (docker2)   (docker3)
        │          │          │
        └──────────┼──────────┘
                   │
┌──────────────────▼──────────────────────────┐
│ Container Status Exporter (Python)          │
│ Runs on infra_vm (192.168.2.106:8081)       │
│ - Polls Portainer API every 30s             │
│ - Converts container states to metrics      │
│ - Exports Prometheus format on /metrics     │
└──────────────────┬──────────────────────────┘
                   │
        ┌──────────▼──────────┐
        │                     │
   Prometheus          Grafana
  (192.168.2.115)    (192.168.2.106)
   Scrapes metrics    Visualizes with
   every 30s          State Timeline
```

## Metrics Exported

### container_state (Gauge)
Values: 0=exited, 1=running, 2=paused, 3=created, 4=restarting, 5=dead, 6=unknown
Labels: container_name, hostname, image

### container_health (Gauge)
Values: 0=none, 1=healthy, 2=unhealthy, 3=starting
Labels: container_name, hostname, image

### container_restart_count (Gauge)
Number of times container has been restarted
Labels: container_name, hostname, image

### portainer_exporter_up (Gauge)
1 if exporter is connected to Portainer, 0 if error

### portainer_exporter_last_scrape_timestamp (Gauge)
Unix timestamp of last successful metrics collection

## Troubleshooting

### Container won't start: "Failed to authenticate"
- Check the PORTAINER_TOKEN is correct
- Regenerate the token in Portainer UI
- Verify it's in vault.yml correctly

### Metrics not appearing in Prometheus
- Check container logs: `docker logs portainer-exporter`
- Verify Portainer API is accessible from infra_vm
- Check Prometheus targets page: http://192.168.2.115:9090/targets

### No data in Grafana dashboard
- Make sure Prometheus is scraping the metrics (check targets)
- Wait at least 2 minutes for initial data collection
- Check the Prometheus query: `container_state` should return results

### Container health check failing
- The health check requires `curl` in the container
- It pings the /health endpoint which should return JSON
- Check logs for "health check" errors

## Next Steps

1. ✅ Code created in `/container-status-exporter/`
2. ✅ Ansible role created in `/roles/portainer_exporter/`
3. ✅ Configurations updated (docker-compose, prometheus, playbooks)
4. ✅ Grafana dashboard created
5. 🔄 **TODO: Generate Portainer token and add to vault**
6. 🔄 **TODO: Build and push Docker image**
7. 🔄 **TODO: Deploy with Ansible**
8. 🔄 **TODO: Verify in Grafana**

## File Locations Reference

```
/container-status-exporter/
├── app.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .gitignore
└── README.md

/roles/portainer_exporter/
├── tasks/main.yml
├── defaults/main.yml
└── handlers/main.yml

/roles/infra_vm/templates/
└── docker-compose.yml.j2  (updated)

/roles/prometheus_lxc/templates/prometheus/
└── prometheus.yml.j2  (updated)

/roles/infra_vm/files/grafana/dashboards/
└── container-status.json  (new)

/playbooks/
└── infra_vm.yml  (updated)
```

## Questions?

Refer to:
- Exporter README: `/container-status-exporter/README.md`
- Portainer API docs: https://docs.portainer.io/api/overview
- Prometheus metrics format: https://prometheus.io/docs/instrumenting/exposition_formats/
