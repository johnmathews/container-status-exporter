# 260324 - Offline Endpoint Handling

## Problem

Portainer manages Docker endpoints including VMs that may be stopped. When a VM is offline, the exporter was:
- Attempting to fetch containers from every endpoint regardless of status
- Receiving HTTP 502/503 from Portainer for offline endpoints
- Logging these as errors every 30 seconds
- Setting `last_error`, which made `portainer_exporter_up` report 0 even though the exporter itself was fine

## Solution

### Pre-flight check via endpoint Status field
Portainer's `/api/endpoints` response includes a `Status` field (1=up, 2=down). The exporter now checks this before calling `fetch_containers()`, skipping offline endpoints entirely.

### Graceful HTTP error handling
`fetch_containers()` now distinguishes between:
- HTTP 502/503: Treated as "endpoint offline" — DEBUG log, no error set
- Other HTTP errors (401, 500, etc.): Treated as real errors — ERROR log, sets `last_error`

### New metric: `portainer_endpoint_status`
Exposes each endpoint's online/offline status as a gauge (1=online, 0=offline). This allows Grafana alerting on endpoint availability.

### Improved log messages
- Collection summary now includes online/offline counts
- Endpoint-related messages include the endpoint name for clarity
- Portainer URL included in connection error messages

## Test Coverage

Added 15 new tests in `tests/test_offline_endpoints.py`:
- Offline endpoint skipping (4 tests)
- Endpoint status tracking and metrics (3 tests)
- HTTP error code differentiation (5 tests)
- Log message level verification (3 tests)

Total: 93 tests, 80.24% coverage (up from 78 tests, 77.42%)
