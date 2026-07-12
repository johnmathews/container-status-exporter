#!/usr/bin/env python3
"""
Portainer Container Status Exporter for Prometheus

Exports container state and health metrics from Portainer API
to be scraped by Prometheus.
"""

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, cast

import requests

from freshness import FreshnessCollector, run_freshness_thread

# Configure logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class ContainerState(Enum):
    """Docker container states as numeric values for Prometheus"""

    EXITED = 0
    RUNNING = 1
    PAUSED = 2
    CREATED = 3
    RESTARTING = 4
    DEAD = 5
    UNKNOWN = 6


class HealthStatus(Enum):
    """Docker health check states as numeric values for Prometheus"""

    NONE = 0
    HEALTHY = 1
    UNHEALTHY = 2
    STARTING = 3


# Portainer endpoint status constants
ENDPOINT_STATUS_UP = 1
ENDPOINT_STATUS_DOWN = 2


@dataclass
class ContainerMetrics:
    """Dataclass to hold container metrics"""

    name: str
    hostname: str
    image: str
    state: int
    health: int
    restart_count: int


@dataclass
class EndpointStatus:
    """Tracks the online/offline status of a Portainer endpoint"""

    endpoint_id: int
    hostname: str
    online: bool


class PortainerExporter:
    """Main exporter class for Portainer API"""

    def __init__(self) -> None:
        self.portainer_url: str = os.getenv("PORTAINER_URL", "http://localhost:9000").rstrip("/")
        self.portainer_token: str = os.getenv("PORTAINER_TOKEN", "")
        self.scrape_interval: int = int(os.getenv("SCRAPE_INTERVAL", "30"))
        self.listen_port: int = int(os.getenv("LISTEN_PORT", "8081"))

        if not self.portainer_token:
            raise ValueError("PORTAINER_TOKEN environment variable is required")

        self.session: requests.Session = requests.Session()
        self.session.headers.update({"X-API-Key": self.portainer_token})
        self.metrics: list[ContainerMetrics] = []
        self.endpoint_statuses: list[EndpointStatus] = []
        self.last_error: str | None = None
        self.last_update: float = 0

    def _get_state_value(self, state: str) -> int:
        """Convert Docker state string to numeric value"""
        state_map = {
            "running": ContainerState.RUNNING.value,
            "paused": ContainerState.PAUSED.value,
            "exited": ContainerState.EXITED.value,
            "created": ContainerState.CREATED.value,
            "restarting": ContainerState.RESTARTING.value,
            "dead": ContainerState.DEAD.value,
        }
        return state_map.get(state.lower(), ContainerState.UNKNOWN.value)

    def _get_health_value(self, health: str) -> int:
        """Convert Docker health status to numeric value"""
        health_map = {
            "healthy": HealthStatus.HEALTHY.value,
            "unhealthy": HealthStatus.UNHEALTHY.value,
            "starting": HealthStatus.STARTING.value,
            "none": HealthStatus.NONE.value,
        }
        return health_map.get(health.lower(), HealthStatus.NONE.value)

    def _parse_health_status(self, container_status: str) -> str:
        """Extract health status from container Status string"""
        status_lower = container_status.lower()
        if "unhealthy" in status_lower:
            return "unhealthy"
        elif "healthy" in status_lower:
            return "healthy"
        elif "starting" in status_lower:
            return "starting"
        return "none"

    def fetch_endpoints(self) -> list[dict[str, Any]]:
        """Fetch all endpoints (Docker hosts) from Portainer.

        Raises requests.RequestException on failure so collect_all_metrics can
        distinguish "Portainer unreachable" from "legitimately zero endpoints".
        """
        url = f"{self.portainer_url}/api/endpoints"
        response = self.session.get(url, timeout=10)
        response.raise_for_status()
        data: Any = response.json()

        # Handle both list and paginated responses
        if isinstance(data, dict) and "results" in data:
            data = data["results"]

        # Type guard to ensure we have a list
        if not isinstance(data, list):
            return []

        # Cast to proper type since we've validated it's a list
        endpoints: list[dict[str, Any]] = cast(list[dict[str, Any]], data)
        logger.info(f"Fetched {len(endpoints)} endpoints from Portainer")
        return endpoints

    def fetch_containers(self, endpoint_id: int, hostname: str) -> list[ContainerMetrics]:
        """Fetch all containers from a specific endpoint"""
        containers: list[ContainerMetrics] = []
        try:
            url = f"{self.portainer_url}/api/endpoints/{endpoint_id}/docker/containers/json?all=true"
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            data: Any = response.json()

            # Ensure we have a list
            if not isinstance(data, list):
                return containers

            container_list: list[Any] = cast(list[Any], data)

            for container in container_list:
                if not isinstance(container, dict):
                    continue

                state_value: str = container.get("State", "unknown")
                if not isinstance(state_value, str):
                    state_value = "unknown"
                state: str = state_value.lower()

                status_value: str = container.get("Status", "")
                if not isinstance(status_value, str):
                    status_value = ""
                status: str = status_value

                health: str = self._parse_health_status(status)

                restart_count_value: int = container.get("RestartCount", 0)
                if not isinstance(restart_count_value, int):
                    restart_count_value = 0
                restart_count: int = restart_count_value

                # Extract container name (remove leading slash)
                names_value: Any = container.get("Names", [])
                names: list[str] = []
                if isinstance(names_value, list):
                    names = cast(list[str], names_value)

                container_name: str = names[0].lstrip("/") if names else "unknown"

                # Get image name
                image_value: str = container.get("Image", "unknown")
                if not isinstance(image_value, str):
                    image_value = "unknown"
                image: str = image_value

                metrics = ContainerMetrics(
                    name=container_name,
                    hostname=hostname.lower(),
                    image=image,
                    state=self._get_state_value(state),
                    health=self._get_health_value(health),
                    restart_count=restart_count,
                )
                containers.append(metrics)

            logger.info(f"Fetched {len(containers)} containers from endpoint '{hostname}'")
            return containers
        except requests.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else None
            if status_code in (502, 503):
                logger.debug(f"Endpoint '{hostname}' is offline (HTTP {status_code}), skipping container fetch")
            else:
                # Per-endpoint failure: log it, but do NOT set last_error —
                # a single flaky endpoint must not flip portainer_exporter_up
                # to 0 fleet-wide.
                logger.error(f"HTTP error fetching containers from endpoint '{hostname}': {e}")
            return []
        except requests.RequestException as e:
            logger.error(f"Failed to fetch containers from endpoint '{hostname}': {e}")
            return []

    def collect_all_metrics(self) -> None:
        """Collect metrics from all endpoints and containers.

        Builds the new snapshot into local lists and publishes it by a single
        reference assignment, so a concurrent scrape never observes a cleared
        or partially-built list. If the endpoints fetch itself fails, the
        previous snapshot is kept, last_error is set (portainer_exporter_up
        renders 0), and last_update does not advance.
        """
        try:
            endpoints = self.fetch_endpoints()
        except Exception as e:
            logger.error(f"Failed to fetch endpoints from Portainer at {self.portainer_url}: {e}")
            self.last_error = str(e)
            return

        new_metrics: list[ContainerMetrics] = []
        new_statuses: list[EndpointStatus] = []
        online_count = 0
        offline_count = 0

        for endpoint in endpoints:
            endpoint_id_value: Any = endpoint.get("Id", 0)
            endpoint_id: int = endpoint_id_value if isinstance(endpoint_id_value, int) else 0

            hostname_value: Any = endpoint.get("Name", "unknown")
            hostname: str = hostname_value if isinstance(hostname_value, str) else "unknown"
            hostname = hostname.lower()

            # Check endpoint status from Portainer (1=up, 2=down)
            status_value: Any = endpoint.get("Status", ENDPOINT_STATUS_UP)
            endpoint_online: bool = status_value == ENDPOINT_STATUS_UP

            new_statuses.append(EndpointStatus(endpoint_id=endpoint_id, hostname=hostname, online=endpoint_online))

            if not endpoint_online:
                offline_count += 1
                logger.debug(f"Skipping offline endpoint '{hostname}' (id={endpoint_id})")
                continue

            online_count += 1
            containers = self.fetch_containers(endpoint_id, hostname)
            new_metrics.extend(containers)

        # Atomic publish: swap in the complete snapshot in one assignment each.
        self.metrics = new_metrics
        self.endpoint_statuses = new_statuses
        self.last_update = time.time()
        self.last_error = None
        logger.info(
            f"Collected {len(new_metrics)} container metrics "
            f"from {online_count} online endpoints ({offline_count} offline, skipped)"
        )

    def generate_metrics_output(self) -> str:
        """Generate Prometheus metrics in text format"""
        # Snapshot shared state once so a concurrent collect cannot cause a
        # torn read while we iterate (collect publishes by reference swap).
        metrics = self.metrics
        endpoint_statuses = self.endpoint_statuses
        last_error = self.last_error
        last_update = self.last_update

        output: list[str] = []

        # Add HELP and TYPE comments
        output.append(
            "# HELP container_state Container state (0=exited, 1=running, 2=paused, 3=created, 4=restarting, 5=dead, 6=unknown)"  # noqa: E501
        )
        output.append("# TYPE container_state gauge")

        for metric in metrics:
            labels: str = f'container_name="{metric.name}",hostname="{metric.hostname.lower()}",image="{metric.image}"'
            output.append(f"container_state{{{labels}}} {metric.state}")

        output.append("")
        output.append("# HELP container_health Container health status (0=none, 1=healthy, 2=unhealthy, 3=starting)")
        output.append("# TYPE container_health gauge")

        for metric in metrics:
            labels = f'container_name="{metric.name}",hostname="{metric.hostname.lower()}",image="{metric.image}"'
            output.append(f"container_health{{{labels}}} {metric.health}")

        output.append("")
        output.append("# HELP container_restart_count Number of times the container has been restarted")
        output.append("# TYPE container_restart_count gauge")

        for metric in metrics:
            labels = f'container_name="{metric.name}",hostname="{metric.hostname.lower()}",image="{metric.image}"'
            output.append(f"container_restart_count{{{labels}}} {metric.restart_count}")

        output.append("")
        output.append("# HELP portainer_endpoint_status Whether a Portainer endpoint is online (1) or offline (0)")
        output.append("# TYPE portainer_endpoint_status gauge")

        for ep in endpoint_statuses:
            output.append(f'portainer_endpoint_status{{hostname="{ep.hostname}"}} {1 if ep.online else 0}')

        output.append("")
        output.append("# HELP portainer_exporter_up Whether the exporter is up and connected to Portainer")
        output.append("# TYPE portainer_exporter_up gauge")
        up_status: int = 1 if last_error is None else 0
        output.append(f"portainer_exporter_up {up_status}")

        output.append("")
        output.append("# HELP portainer_exporter_last_scrape_timestamp Unix timestamp of last successful scrape")
        output.append("# TYPE portainer_exporter_last_scrape_timestamp gauge")
        output.append(f"portainer_exporter_last_scrape_timestamp {int(last_update)}")

        return "\n".join(output) + "\n"


class MetricsHandler(BaseHTTPRequestHandler):
    """HTTP request handler for /metrics endpoint"""

    exporter: PortainerExporter | None = None
    freshness: FreshnessCollector | None = None
    # Per-connection socket timeout so a hung client cannot pin a handler
    # thread indefinitely (applied by BaseHTTPRequestHandler.setup()).
    timeout = 30

    def do_GET(self) -> None:
        if self.path == "/metrics":
            self.send_response(200)
            self.send_header("Content-type", "text/plain; version=0.0.4")
            self.end_headers()
            if self.exporter is not None:
                metrics = self.exporter.generate_metrics_output()
                if self.freshness is not None:
                    metrics += "\n" + self.freshness.generate_output()
                _ = self.wfile.write(metrics.encode("utf-8"))
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            if self.exporter is not None:
                health = json.dumps({"status": "up", "last_error": self.exporter.last_error})
                _ = self.wfile.write(health.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # type: ignore[override]
        """Suppress default logging"""
        pass


def run_collector_thread(exporter: PortainerExporter, interval: int) -> Thread:
    """Run metrics collection in a background thread"""

    def collector() -> None:
        while True:
            exporter.collect_all_metrics()
            time.sleep(interval)

    thread = Thread(target=collector, daemon=True)
    thread.start()
    return thread


def main() -> None:
    logger.info("Starting Portainer Container Status Exporter")

    try:
        exporter = PortainerExporter()
        MetricsHandler.exporter = exporter

        # Initial collection
        logger.info("Performing initial metrics collection...")
        exporter.collect_all_metrics()

        # Start background collection thread
        logger.info(f"Starting background collection every {exporter.scrape_interval} seconds")
        _ = run_collector_thread(exporter, exporter.scrape_interval)

        # Start image-freshness collection thread (registry digest comparison)
        if os.getenv("FRESHNESS_ENABLED", "true").lower() in ("1", "true", "yes"):
            freshness = FreshnessCollector()
            MetricsHandler.freshness = freshness
            logger.info(f"Starting image freshness checks every {freshness.check_interval} seconds")
            _ = run_freshness_thread(freshness)

        # Start HTTP server (threaded so a slow/hung client can't block
        # scrapes; ThreadingHTTPServer sets daemon_threads = True)
        server = ThreadingHTTPServer(("0.0.0.0", exporter.listen_port), MetricsHandler)
        logger.info(f"Starting HTTP server on port {exporter.listen_port}")
        logger.info("Listening for Prometheus scrapes on /metrics")

        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down gracefully")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
