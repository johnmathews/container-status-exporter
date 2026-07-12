"""
Tests for the HTTP boundary: the real server class serving real requests.

These spin up the same server construction main() uses (ThreadingHTTPServer
with MetricsHandler) on an ephemeral port and hit it with urllib, pinning the
actual wiring in MetricsHandler.do_GET — including that a single /metrics response
carries BOTH the container-state families and the freshness families.
"""

import json
import threading
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

import app
from app import EndpointStatus, MetricsHandler, PortainerExporter
from freshness import STATUS_OUTDATED, FreshnessCollector, ImageFreshness


@pytest.fixture
def wired_exporter(mock_env, sample_container_metrics) -> PortainerExporter:
    with patch.dict("os.environ", mock_env):
        exporter = PortainerExporter()
    exporter.metrics = sample_container_metrics
    exporter.endpoint_statuses = [EndpointStatus(endpoint_id=1, hostname="docker-host-1", online=True)]
    exporter.last_update = 1234567890
    exporter.last_error = None
    return exporter


@pytest.fixture
def wired_freshness(mock_env) -> FreshnessCollector:
    with patch.dict("os.environ", mock_env):
        collector = FreshnessCollector()
    collector.results = [
        ImageFreshness(
            container_name="web-server",
            hostname="docker-host-1",
            image="nginx:latest",
            status=STATUS_OUTDATED,
            current_version="1.27",
            available_version="1.28",
            current_created=1700000000.0,
            available_created=1780000000.0,
        ),
    ]
    collector.last_check = 1780000123.0
    return collector


@pytest.fixture
def server_url(wired_exporter, wired_freshness):
    """Run the app's real HTTP server on an ephemeral port, wired to fixture data."""
    previous_exporter = MetricsHandler.exporter
    previous_freshness = MetricsHandler.freshness
    MetricsHandler.exporter = wired_exporter
    MetricsHandler.freshness = wired_freshness

    # Use the same server class main() constructs.
    server = app.ThreadingHTTPServer(("127.0.0.1", 0), MetricsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        MetricsHandler.exporter = previous_exporter
        MetricsHandler.freshness = previous_freshness


def _get(url: str) -> tuple[int, str, str]:
    """GET a URL; return (status, content_type, body)."""
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8")


class TestHTTPBoundary:
    def test_metrics_returns_200_text_plain(self, server_url):
        status, content_type, _ = _get(f"{server_url}/metrics")
        assert status == 200
        assert content_type.startswith("text/plain")

    def test_metrics_response_contains_app_and_freshness_families(self, server_url):
        """One /metrics response must carry BOTH modules' metrics (do_GET wiring)."""
        _, _, body = _get(f"{server_url}/metrics")
        lines = body.split("\n")
        # app.py families
        assert any(line.startswith("container_state{") for line in lines)
        assert "portainer_exporter_up 1" in lines
        # freshness.py families, appended by do_GET
        assert any(line.startswith("container_image_outdated{") for line in lines)
        assert any(line.startswith("container_image_freshness_last_check_timestamp ") for line in lines)

    def test_metrics_sample_values_come_from_wired_collectors(self, server_url):
        _, _, body = _get(f"{server_url}/metrics")
        assert 'container_state{container_name="web-server",hostname="docker-host-1",image="nginx:latest"} 1' in body
        assert (
            'container_image_outdated{container_name="web-server",hostname="docker-host-1",image="nginx:latest"} 1'
            in body
        )

    def test_health_returns_200_json_with_real_shape(self, server_url):
        status, content_type, body = _get(f"{server_url}/health")
        assert status == 200
        assert content_type.startswith("application/json")
        payload = json.loads(body)
        assert set(payload) == {"status", "last_error"}
        assert payload["status"] == "up"
        assert payload["last_error"] is None

    def test_health_reports_last_error(self, server_url, wired_exporter):
        wired_exporter.last_error = "Connection failed"
        _, _, body = _get(f"{server_url}/health")
        assert json.loads(body)["last_error"] == "Connection failed"

    def test_unknown_path_returns_404(self, server_url):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(f"{server_url}/nope")
        assert exc_info.value.code == 404

    def test_log_message_is_suppressed(self):
        """log_message is overridden to a no-op so scrapes don't spam stderr."""
        handler = MetricsHandler.__new__(MetricsHandler)
        assert handler.log_message("test format %s %s", "arg1", "arg2") is None
