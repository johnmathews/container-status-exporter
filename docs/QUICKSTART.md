# Container Status Exporter - START HERE

## 🎯 What You're Getting

A complete solution to monitor Docker container states (running, paused, exited, etc.) across all your hosts in **Grafana with beautiful State Timeline visualization**.

**Result**: A dashboard showing which containers are running/paused/healthy on each host in real-time.

---

## ⚡ Quick Start (20 minutes)

### 1. Generate Portainer API Token (5 min)
```bash
# Open browser: http://192.168.2.106:9000
# Click username (bottom left) → Account Settings
# Scroll to "API tokens" → "Generate new API token"
# Copy the token (save it!)
```

### 2. Add Token to Vault (2 min)
```bash
ansible-vault edit group_vars/all/vault.yml
# Add this line:
# vault_portainer_api_token: "your_token_here"
```

### 3. Build & Push Docker Image (5 min)
```bash
cd container-status-exporter
docker build -t ghcr.io/johnmathews/container-status-exporter:latest .
docker push ghcr.io/johnmathews/container-status-exporter:latest
```

### 4. Deploy (3 min)
```bash
ansible-playbook playbooks/infra_vm.yml -i inventory.ini -t portainer_exporter
```

### 5. Verify in Grafana (5 min)
```
http://192.168.2.106:3000
→ Dashboards → Container Status
→ Wait 2-3 minutes for data
→ Done! 🎉
```

---

## 📖 Documentation

| Document | Purpose | Read When |
|----------|---------|-----------|
| **DEPLOYMENT_CHECKLIST.md** | Step-by-step with exact commands | Ready to deploy |
| **IMPLEMENTATION_SUMMARY.md** | What was built & architecture | Want to understand system |
| **CONTAINER_STATUS_EXPORTER.md** | Complete guide & troubleshooting | Need detailed info |
| **container-status-exporter/README.md** | Exporter app documentation | Local testing/development |

**Recommended reading order:**
1. This file (you're reading it!)
2. DEPLOYMENT_CHECKLIST.md (before deploying)
3. IMPLEMENTATION_SUMMARY.md (after deployment)
4. CONTAINER_STATUS_EXPORTER.md (if you need to troubleshoot)

---

## 📦 What Was Created

### Code (Ready to Deploy)
```
container-status-exporter/
├── app.py                 (400+ lines, production-ready)
├── Dockerfile             (Python 3.11 slim, optimized)
├── requirements.txt       (just: requests)
├── docker-compose.yml     (for local testing)
└── README.md             (full documentation)

roles/portainer_exporter/  (Ansible deployment role)
├── tasks/main.yml
├── defaults/main.yml
└── handlers/main.yml
```

### Updated Configs (Ready to Use)
```
roles/infra_vm/templates/docker-compose.yml.j2        ← Added exporter
roles/prometheus_lxc/templates/prometheus/prometheus.yml.j2  ← Added scrape
playbooks/infra_vm.yml                                 ← Added role
roles/infra_vm/files/grafana/dashboards/container-status.json  ← New dashboard
```

### Documentation (Ready to Read)
```
DEPLOYMENT_CHECKLIST.md      ← Start here for deployment
IMPLEMENTATION_SUMMARY.md    ← What was built
CONTAINER_STATUS_EXPORTER.md ← Complete guide
START_HERE.md               ← This file
```

---

## 🎨 Grafana Dashboard Preview

You'll see 3 panels:

**1. Container State Timeline**
- Color timeline showing container states over 24 hours
- Green = Running, Yellow = Paused, Gray = Exited, etc.
- Filterable by hostname

**2. Container Health Timeline**  
- Green = Healthy, Red = Unhealthy, Orange = Starting

**3. Container Restart Count**
- Line chart showing restart frequency
- Helps spot problematic containers

---

## 🔄 How It Works (30-second version)

```
1. Portainer API (manages all Docker hosts)
   ↓
2. Container Status Exporter (Python, on infra_vm)
   - Every 30 seconds: Fetches container state from Portainer
   - Converts to Prometheus metrics
   - Exposes on http://192.168.2.106:8081
   ↓
3. Prometheus scrapes metrics every 30 seconds
   ↓
4. Grafana queries Prometheus
   ↓
5. You see real-time container status in dashboard
```

---

## ✅ Prerequisites (Already Have These)

- ✅ Portainer running on infra_vm (port 9000)
- ✅ Prometheus running (port 9090)
- ✅ Grafana running (port 3000)
- ✅ Ansible playbooks configured
- ✅ Vault encryption set up

---

## 🚨 If Something Goes Wrong

**"I don't know where to start"**
→ Read DEPLOYMENT_CHECKLIST.md (has copy-paste commands)

**"Build failed"**
→ Check Docker is running: `docker info`
→ Check image name is correct

**"Prometheus shows DOWN"**
→ Check exporter running: `ssh john@192.168.2.106 "docker logs portainer-exporter"`
→ Check token is correct in vault

**"Grafana dashboard is empty"**
→ Wait 3-5 minutes (first data takes time)
→ Check Prometheus: http://192.168.2.115:9090
→ Run query: `container_state` (should return data)

**More issues?**
→ See CONTAINER_STATUS_EXPORTER.md "Troubleshooting" section

---

## 📊 Metrics You Get

```promql
container_state{container_name="...", hostname="...", image="..."}
    # Values: 0=exited, 1=running, 2=paused, 3=created, 4=restarting, 5=dead

container_health{container_name="...", hostname="...", image="..."}
    # Values: 0=none, 1=healthy, 2=unhealthy, 3=starting

container_restart_count{container_name="...", hostname="...", image="..."}
    # Shows how many times container restarted
```

All automatically collected every 30 seconds from Portainer API.

---

## 🎯 After Deployment

1. **Commit to git**
   ```bash
   git add -A
   git commit -m "Add Portainer container status exporter"
   git push
   ```

2. **Optional: Sync to separate repo**
   - Push `container-status-exporter/` to https://github.com/johnmathews/container-status-exporter
   - Set up CI/CD for Docker image builds

3. **Monitor & Maintain**
   - Grafana dashboard is auto-loaded
   - Metrics are collected automatically
   - No maintenance needed (runs as container)

---

## 💡 Pro Tips

- **Filter by hostname**: Use the "Hostname" dropdown in Grafana
- **Zoom timeline**: Click and drag on the timeline to zoom
- **Real-time**: Metrics update every 30 seconds
- **Mobile**: Dashboard works on mobile browsers too
- **Share**: You can share the dashboard URL with others

---

## 📚 Full File Locations

```
/Users/john/projects/home-server/proxmox-setup/
├── container-status-exporter/     ← NEW: Exporter code
├── roles/portainer_exporter/      ← NEW: Ansible role
├── roles/infra_vm/                ← UPDATED
├── roles/prometheus_lxc/          ← UPDATED
├── playbooks/infra_vm.yml         ← UPDATED
├── DEPLOYMENT_CHECKLIST.md        ← NEW: Start deployment here
├── IMPLEMENTATION_SUMMARY.md      ← NEW: What was built
├── CONTAINER_STATUS_EXPORTER.md   ← NEW: Complete guide
└── START_HERE.md                  ← NEW: This file
```

---

## ⏱️ Timeline

| Task | Time | Command |
|------|------|---------|
| Generate token | 5 min | Manual (Portainer UI) |
| Add to vault | 2 min | `ansible-vault edit ...` |
| Build image | 3 min | `docker build ...` |
| Push image | 2 min | `docker push ...` |
| Deploy | 3 min | `ansible-playbook ...` |
| Verify | 5 min | Check logs & curl |
| **TOTAL** | **~20 min** | |

---

## 🚀 Ready?

**Next step:** Open `DEPLOYMENT_CHECKLIST.md`

It has:
- ✅ Complete checklist of all steps
- ✅ Exact copy-paste commands
- ✅ Troubleshooting for every step
- ✅ Verification commands

---

## 📞 Need Help?

1. Check the relevant document:
   - Deploy issues → DEPLOYMENT_CHECKLIST.md
   - Understanding system → IMPLEMENTATION_SUMMARY.md
   - Technical details → CONTAINER_STATUS_EXPORTER.md

2. Check logs:
   ```bash
   docker logs portainer-exporter
   docker logs prometheus
   docker logs grafana
   ```

3. Verify components:
   ```bash
   curl http://192.168.2.106:8081/metrics          # Exporter metrics
   curl http://192.168.2.115:9090/api/v1/query    # Prometheus
   curl http://192.168.2.106:3000/api/health      # Grafana
   ```

---

## ✨ Summary

Everything is ready. You just need to:
1. Generate a token (5 min)
2. Run the commands (10 min)
3. Enjoy the dashboard (forever)

**Let's go!** 🚀

---

**Created:** November 17, 2025  
**Status:** ✅ Ready for deployment  
**Estimated setup time:** 20 minutes
