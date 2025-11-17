# Container Status Exporter - START HERE

A Prometheus exporter for Docker container status (running, paused, exited, healthy, unhealthy, etc.) via Portainer API.

## Quick Overview

This exporter:
- Connects to Portainer API
- Polls all Docker hosts every 30 seconds
- Exports container state and health metrics
- Provides Grafana State Timeline visualization

## Files in This Directory

| File | Purpose |
|------|---------|
| **app.py** | Main exporter application (400+ lines) |
| **Dockerfile** | Docker image definition |
| **requirements.txt** | Python dependencies |
| **docker-compose.yml** | For local testing |
| **README.md** | Complete app documentation |
| **QUICKSTART.md** | 5-minute quick start guide |
| **MANUAL_SETUP.md** | Integration steps (docker-compose, prometheus) |
| **DEPLOYMENT.md** | Detailed deployment walkthrough |
| **IMPLEMENTATION.md** | Technical deep-dive |
| **SUMMARY.md** | Features, metrics, architecture |

## Next Steps

1. **Read QUICKSTART.md** (5 minutes)
   - Overview of what happens next

2. **Build & Push Docker Image**
   ```bash
   docker build -t ghcr.io/johnmathews/container-status-exporter:latest .
   docker push ghcr.io/johnmathews/container-status-exporter:latest
   ```

3. **Read MANUAL_SETUP.md** (10 minutes)
   - Step-by-step integration instructions

4. **Add to Your Infrastructure**
   - Add container to docker-compose on infra_vm
   - Add scrape job to Prometheus
   - Import Grafana dashboard

5. **Enjoy Your Dashboard**
   - View container states in Grafana
   - Monitor health status
   - Track restarts

## Quick Reference

**Metrics exported:**
- `container_state` - running(1), paused(2), exited(0), etc.
- `container_health` - healthy(1), unhealthy(2), starting(3), none(0)
- `container_restart_count` - number of restarts
- `portainer_exporter_up` - exporter status

**Port:** 8081 (metrics at `/metrics`)

**Polling interval:** 30 seconds (configurable)

## No Ansible Role

This is a standalone application. You manually:
1. Build the Docker image
2. Add it to docker-compose on infra_vm
3. Add scrape job to Prometheus
4. Import dashboard to Grafana

Simple, transparent, easy to maintain.

## Questions?

- **Setup:** Read MANUAL_SETUP.md
- **How it works:** Read IMPLEMENTATION.md
- **Features:** Read SUMMARY.md
- **App details:** Read README.md

---

**Status:** ✅ Ready to build and deploy

**Time to production:** ~20 minutes total
