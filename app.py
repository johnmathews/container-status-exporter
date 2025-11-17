#!/usr/bin/env python3
"""
Portainer Container Status Exporter for Prometheus

Exports container state and health metrics from Portainer API
to be scraped by Prometheus.
"""

import os
import sys
import time
import logging
import requests
from typing import Dict, List, Any
from dataclasses import dataclass
from enum import Enum
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

# Configure logging
logging.basicConfig(
    level=logging.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(levelname)s - %(message)s"
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


@dataclass
class ContainerMetrics:
    """Dataclass to hold container metrics"""
    name: str
    hostname: str
    image: str
    state: int
    health: int
    restart_count: int


class PortainerExporter:
    """Main exporter class for Portainer API"""

    def __init__(self):
        self.portainer_url = os.getenv("PORTAINER_URL", "http://localhost:9000").rstrip("/")
        self.portainer_token = os.getenv("PORTAINER_TOKEN", "")
        self.scrape_interval = int(os.getenv("SCRAPE_INTERVAL", "30"))
        self.listen_port = int(os.getenv("LISTEN_PORT", "8081"))

        if not self.portainer_token:
            raise ValueError("PORTAINER_TOKEN environment variable is required")

        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": self.portainer_token})
        self.metrics: List[ContainerMetrics] = []
        self.last_error = None
        self.last_update = 0

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
        if "healthy" in container_status.lower():
            return "healthy"
        elif "unhealthy" in container_status.lower():
            return "unhealthy"
        elif "starting" in container_status.lower():
            return "starting"
        return "none"

    def fetch_endpoints(self) -> List[Dict[str, Any]]:
        """Fetch all endpoints (Docker hosts) from Portainer"""
        try:
            url = f"{self.portainer_url}/api/endpoints"
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            endpoints = response.json()
            
            # Handle both list and paginated responses
            if isinstance(endpoints, dict) and "results" in endpoints:
                endpoints = endpoints["results"]
            
            logger.info(f"Found {len(endpoints)} endpoints")
            return endpoints if isinstance(endpoints, list) else []
        except requests.RequestException as e:
            logger.error(f"Failed to fetch endpoints: {e}")
            self.last_error = str(e)
            return []

    def fetch_containers(self, endpoint_id: int, hostname: str) -> List[ContainerMetrics]:
        """Fetch all containers from a specific endpoint"""
        containers = []
        try:
            url = f"{self.portainer_url}/api/endpoints/{endpoint_id}/docker/containers/json?all=true"
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            container_list = response.json()

            for container in container_list:
                state = container.get("State", "unknown").lower()
                status = container.get("Status", "")
                health = self._parse_health_status(status)
                restart_count = container.get("RestartCount", 0)
                
                # Extract container name (remove leading slash)
                names = container.get("Names", [])
                container_name = names[0].lstrip("/") if names else "unknown"
                
                # Get image name
                image = container.get("Image", "unknown")
                
                metrics = ContainerMetrics(
                    name=container_name,
                    hostname=hostname,
                    image=image,
                    state=self._get_state_value(state),
                    health=self._get_health_value(health),
                    restart_count=restart_count
                )
                containers.append(metrics)

            logger.info(f"Found {len(containers)} containers on {hostname}")
            return containers
        except requests.RequestException as e:
            logger.error(f"Failed to fetch containers from {hostname}: {e}")
            self.last_error = str(e)
            return []

    def collect_all_metrics(self) -> None:
        """Collect metrics from all endpoints and containers"""
        try:
            self.metrics = []
            endpoints = self.fetch_endpoints()
            
            for endpoint in endpoints:
                endpoint_id = endpoint.get("Id")
                hostname = endpoint.get("Name", "unknown")
                containers = self.fetch_containers(endpoint_id, hostname)
                self.metrics.extend(containers)
            
            self.last_update = time.time()
            self.last_error = None
            logger.info(f"Successfully collected {len(self.metrics)} container metrics")
        except Exception as e:
            logger.error(f"Error collecting metrics: {e}")
            self.last_error = str(e)

    def generate_metrics_output(self) -> str:
        """Generate Prometheus metrics in text format"""
        output = []
        
        # Add HELP and TYPE comments
        output.append("# HELP container_state Container state (0=exited, 1=running, 2=paused, 3=created, 4=restarting, 5=dead, 6=unknown)")
        output.append("# TYPE container_state gauge")
        
        for metric in self.metrics:
            labels = f'container_name="{metric.name}",hostname="{metric.hostname}",image="{metric.image}"'
            output.append(f"container_state{{{labels}}} {metric.state}")
        
        output.append("")
        output.append("# HELP container_health Container health status (0=none, 1=healthy, 2=unhealthy, 3=starting)")
        output.append("# TYPE container_health gauge")
        
        for metric in self.metrics:
            labels = f'container_name="{metric.name}",hostname="{metric.hostname}",image="{metric.image}"'
            output.append(f"container_health{{{labels}}} {metric.health}")
        
        output.append("")
        output.append("# HELP container_restart_count Number of times the container has been restarted")
        output.append("# TYPE container_restart_count gauge")
        
        for metric in self.metrics:
            labels = f'container_name="{metric.name}",hostname="{metric.hostname}",image="{metric.image}"'
            output.append(f"container_restart_count{{{labels}}} {metric.restart_count}")
        
        output.append("")
        output.append("# HELP portainer_exporter_up Whether the exporter is up and connected to Portainer")
        output.append("# TYPE portainer_exporter_up gauge")
        up_status = 1 if self.last_error is None else 0
        output.append(f"portainer_exporter_up {up_status}")
        
        output.append("")
        output.append("# HELP portainer_exporter_last_scrape_timestamp Unix timestamp of last successful scrape")
        output.append("# TYPE portainer_exporter_last_scrape_timestamp gauge")
        output.append(f"portainer_exporter_last_scrape_timestamp {int(self.last_update)}")
        
        return "\n".join(output) + "\n"


class MetricsHandler(BaseHTTPRequestHandler):
    """HTTP request handler for /metrics endpoint"""
    
    exporter = None

    def do_GET(self):
        if self.path == "/metrics":
            self.send_response(200)
            self.send_header("Content-type", "text/plain; version=0.0.4")
            self.end_headers()
            metrics = self.exporter.generate_metrics_output()
            self.wfile.write(metrics.encode("utf-8"))
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            import json
            health = json.dumps({"status": "up", "last_error": self.exporter.last_error})
            self.wfile.write(health.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress default logging"""
        pass


def run_collector_thread(exporter: PortainerExporter, interval: int) -> Thread:
    """Run metrics collection in a background thread"""
    def collector():
        while True:
            exporter.collect_all_metrics()
            time.sleep(interval)
    
    thread = Thread(target=collector, daemon=True)
    thread.start()
    return thread


def main():
    logger.info("Starting Portainer Container Status Exporter")
    
    try:
        exporter = PortainerExporter()
        MetricsHandler.exporter = exporter
        
        # Initial collection
        logger.info("Performing initial metrics collection...")
        exporter.collect_all_metrics()
        
        # Start background collection thread
        logger.info(f"Starting background collection every {exporter.scrape_interval} seconds")
        run_collector_thread(exporter, exporter.scrape_interval)
        
        # Start HTTP server
        server = HTTPServer(("0.0.0.0", exporter.listen_port), MetricsHandler)
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
