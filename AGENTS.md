# AGENTS.md - Container Status Exporter

## Project Purpose
A lightweight Prometheus exporter that polls the Portainer API to export Docker container state, health status, and restart counts. Integrates with Prometheus and Grafana for monitoring container status across multiple Docker hosts.

## Key Principles
- **Single-file application**: All logic in `app.py` for transparency and ease of deployment
- **Minimal dependencies**: Only `requests` library (no frameworks)
- **Threaded architecture**: Background collection thread + HTTP server in main thread
- **Enum-based metrics**: `ContainerState` and `HealthStatus` enums map container states to numeric values for Prometheus

## Code Structure
- `app.py`: Main exporter (~298 lines)
  - `PortainerExporter`: Core API client and metrics collection
  - `ContainerMetrics`: Dataclass holding metric values
  - `MetricsHandler`: HTTP request handler for `/metrics` and `/health` endpoints
  - Helper functions for state/health conversion and thread management

## Testing & Build
- **No test suite**: Simple single-file application with manual testing
- **Run locally**: `python app.py` (requires PORTAINER_TOKEN env var)
- **Build Docker image**: `docker build -t container-status-exporter .`
- **Test with compose**: `docker-compose up` (set PORTAINER_TOKEN in env)
- **Verify metrics**: `curl http://localhost:8081/metrics`

## Code Style
- **Imports**: Standard library + `requests`, organized at top
- **Naming**: `snake_case` for functions, `UPPER_CASE` for enum values, `PascalCase` for classes
- **Docstrings**: Present for classes and public methods
- **Error handling**: Try-catch blocks log errors to logger, return empty lists/None on failure
- **Types**: Use `typing` module annotations and dataclasses where appropriate

## Editing Guardrails
- ✅ **Safe to edit**: `app.py` (core logic), `requirements.txt` (dependencies), `Dockerfile`, `docker-compose.yml`
- ✅ **Safe to extend**: Add new metrics, refactor data collection, improve error handling
- ⚠️ **Be careful**: HTTP response format must remain Prometheus-compatible (HELP/TYPE comments)
- ❌ **Never touch**: Don't change metric names or label structure without updating Prometheus/Grafana configs
- ❌ **Never touch**: Documentation files (README.md, QUICKSTART.md, etc.) unless explicitly requested
