# Deployment Checklist

## ✅ Implementation Complete (Do NOT modify)

- [x] Python exporter application written (app.py)
- [x] Dockerfile created and optimized
- [x] Requirements.txt with dependencies
- [x] Docker-compose for testing
- [x] README.md with full documentation
- [x] Ansible role created (portainer_exporter)
- [x] docker-compose.yml.j2 updated with exporter service
- [x] prometheus.yml.j2 updated with scrape job
- [x] infra_vm.yml playbook updated
- [x] Grafana dashboard JSON created
- [x] Implementation guides written

## 🔄 To Do (In Order)

### 1. Generate Portainer API Token
- [ ] Open Portainer: http://192.168.2.106:9000
- [ ] Click your username (bottom left) → Account Settings
- [ ] Scroll to "API tokens"
- [ ] Click "Generate new API token"
- [ ] **Copy the token** (write it down, you won't see it again!)
- [ ] Save in a safe location

### 2. Add Token to Vault
- [ ] Run: `ansible-vault edit group_vars/all/vault.yml`
- [ ] Find the vault file (may be at end or beginning)
- [ ] Add new line: `vault_portainer_api_token: "paste_your_token_here"`
- [ ] Save and exit (Ctrl+X in vim, type :wq)
- [ ] Verify it's encrypted

### 3. Build Docker Image
- [ ] `cd container-status-exporter`
- [ ] `docker build -t ghcr.io/johnmathews/container-status-exporter:latest .`
- [ ] Verify build succeeds (no errors)

### 4. Push to GitHub Container Registry
- [ ] `docker push ghcr.io/johnmathews/container-status-exporter:latest`
- [ ] If login needed: `echo $GITHUB_TOKEN | docker login ghcr.io -u johnmathews --password-stdin`
- [ ] Verify push succeeds

### 5. Deploy with Ansible
- [ ] `cd /Users/john/projects/home-server/proxmox-setup`
- [ ] `make requirements` (if needed)
- [ ] `ansible-playbook playbooks/infra_vm.yml -i inventory.ini -t portainer_exporter`
- [ ] Wait for playbook to complete (should be 1-2 minutes)
- [ ] Check for any errors in output

### 6. Verify Container Running
- [ ] `ssh john@192.168.2.106`
- [ ] `docker ps | grep portainer-exporter` (should show running)
- [ ] `docker logs portainer-exporter` (check for errors)
- [ ] Exit SSH

### 7. Test Metrics Endpoint
- [ ] `curl http://192.168.2.106:8081/metrics | head -20`
- [ ] Should see Prometheus metrics format
- [ ] Look for lines like `container_state{...}` 

### 8. Check Prometheus
- [ ] Open Prometheus: http://192.168.2.115:9090
- [ ] Go to Status → Targets
- [ ] Look for `container-status` job
- [ ] Should show: `192.168.2.106:8081 (1/1 up)`
- [ ] If down, check exporter logs again

### 9. View Grafana Dashboard
- [ ] Open Grafana: http://192.168.2.106:3000
- [ ] Go to Dashboards → Browse
- [ ] Search for "Container Status"
- [ ] Open the dashboard
- [ ] **IMPORTANT**: Wait 2-3 minutes for data to populate
- [ ] Use hostname filter to test different hosts
- [ ] Verify you see color-coded timeline

### 10. Final Checks
- [ ] Container state shows correct colors (green=running, yellow=paused)
- [ ] Health status shows correct colors (green=healthy)
- [ ] Restart count shows a number
- [ ] No errors in container logs
- [ ] Prometheus is scraping metrics successfully

## Troubleshooting During Deployment

### If push fails (no authentication)
```bash
# Generate GitHub token (if needed)
# Go to: https://github.com/settings/tokens
# Create token with 'write:packages' permission
# Then: echo $YOUR_TOKEN | docker login ghcr.io -u johnmathews --password-stdin
```

### If Ansible playbook fails
```bash
# Check vault syntax
ansible-vault view group_vars/all/vault.yml

# Run with verbose output
ansible-playbook playbooks/infra_vm.yml -i inventory.ini -t portainer_exporter -vv
```

### If no metrics appear
1. Check exporter logs: `docker logs portainer-exporter`
2. Verify Portainer token is correct
3. Test Portainer API manually: `curl -H "X-API-Key: TOKEN" http://192.168.2.106:9000/api/endpoints`
4. Check firewall between infra_vm and Portainer

### If Prometheus shows "Down"
1. Check exporter is running: `docker ps | grep portainer-exporter`
2. Check exporter logs for errors
3. Test endpoint: `curl http://192.168.2.106:8081/health`
4. Verify network connectivity between prometheus LXC and infra_vm

### If Grafana dashboard is empty
1. Wait 3-5 minutes (first data collection takes time)
2. Check Prometheus has data: `container_state` query in http://192.168.2.115:9090
3. If no data in Prometheus, Prometheus isn't scraping exporter
4. Check Prometheus targets page (step 8 above)

## Success Criteria

✅ All steps completed without errors
✅ Container running on infra_vm
✅ Metrics endpoint returns data
✅ Prometheus shows target as UP
✅ Grafana dashboard shows data within 5 minutes
✅ Can see container state changes in real-time

## Estimated Time

- Portainer token: 5 minutes
- Vault update: 2 minutes
- Build image: 3 minutes
- Push to registry: 2 minutes
- Deploy: 3 minutes
- Verification: 5 minutes
- Wait for data: 3 minutes
- **TOTAL: ~20 minutes**

## After Deployment

1. **Commit changes** to git repo
   ```bash
   git add -A
   git commit -m "Add Portainer container status exporter for Grafana monitoring"
   git push
   ```

2. **Update separate repo** (container-status-exporter)
   - Create GitHub repo at https://github.com/johnmathews/container-status-exporter
   - Push the container-status-exporter directory contents there
   - Optional: Set up CI/CD for auto-building Docker images

3. **Documentation**
   - Share IMPLEMENTATION_SUMMARY.md with team if needed
   - Bookmark Grafana dashboard
   - Add to Grafana home dashboard if desired

## Support & References

- Exporter app: `container-status-exporter/README.md`
- Implementation guide: `CONTAINER_STATUS_EXPORTER.md`
- Summary: `IMPLEMENTATION_SUMMARY.md`
- This checklist: `DEPLOYMENT_CHECKLIST.md`

## Questions?

If anything fails:
1. Check the specific troubleshooting section above
2. Review CONTAINER_STATUS_EXPORTER.md section "Troubleshooting"
3. Check exporter logs: `docker logs portainer-exporter`
4. Check Prometheus logs: Depends on your setup
5. Verify network connectivity between components

---

Good luck! 🚀
