"""
Tests for collector error semantics and atomic snapshot publishing (W3 / finding C1).

Semantics pinned here:

- FULL failure (the endpoints fetch itself fails): last_error is set,
  portainer_exporter_up renders 0, the PREVIOUS metrics/endpoint-status
  snapshots are retained (series must not silently vanish), and last_update
  does not advance.
- PARTIAL failure (endpoints fetch succeeds, one endpoint's container fetch
  fails): that endpoint's containers are omitted this cycle, other endpoints'
  series are still published, the failure is logged at ERROR but last_error is
  NOT set fleet-wide, so portainer_exporter_up stays 1.
- ATOMIC publish: a reader observing exporter.metrics/endpoint_statuses while
  a collect is in flight always sees the previous complete snapshot, never a
  cleared or partially-built list.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest
import requests

import app
from app import ContainerMetrics, EndpointStatus, MetricsHandler, PortainerExporter


@pytest.fixture
def exporter(mock_env):
    """Create an exporter instance."""
    with patch.dict("os.environ", mock_env):
        return PortainerExporter()


@pytest.fixture
def seeded_exporter(exporter, sample_container_metrics):
    """An exporter holding a previous successful collection snapshot."""
    exporter.metrics = sample_container_metrics
    exporter.endpoint_statuses = [
        EndpointStatus(endpoint_id=1, hostname="docker-host-1", online=True),
        EndpointStatus(endpoint_id=2, hostname="docker-host-2", online=True),
    ]
    exporter.last_update = 1234567890.0
    exporter.last_error = None
    return exporter


def _container_payload(name: str) -> dict:
    return {
        "Id": f"id-{name}",
        "Names": [f"/{name}"],
        "Image": "nginx:latest",
        "State": "running",
        "Status": "Up 2 hours (healthy)",
        "RestartCount": 0,
    }


class TestFullEndpointsFetchFailure:
    """Portainer fully unreachable: up goes 0 and previous series are retained."""

    def test_last_error_set_and_up_zero(self, seeded_exporter, mocker):
        mocker.patch.object(seeded_exporter.session, "get", side_effect=requests.ConnectionError("Connection refused"))

        seeded_exporter.collect_all_metrics()

        assert seeded_exporter.last_error is not None
        assert "Connection refused" in seeded_exporter.last_error
        assert "portainer_exporter_up 0" in seeded_exporter.generate_metrics_output().split("\n")

    def test_previous_metrics_snapshot_retained(self, seeded_exporter, sample_container_metrics, mocker):
        mocker.patch.object(seeded_exporter.session, "get", side_effect=requests.ConnectionError("Connection refused"))

        seeded_exporter.collect_all_metrics()

        assert seeded_exporter.metrics == sample_container_metrics
        assert len(seeded_exporter.endpoint_statuses) == 2
        output = seeded_exporter.generate_metrics_output()
        assert 'container_state{container_name="web-server",hostname="docker-host-1",image="nginx:latest"} 1' in output
        assert 'portainer_endpoint_status{hostname="docker-host-1"} 1' in output

    def test_last_update_not_advanced(self, seeded_exporter, mocker):
        mocker.patch.object(seeded_exporter.session, "get", side_effect=requests.ConnectionError("Connection refused"))

        seeded_exporter.collect_all_metrics()

        assert seeded_exporter.last_update == 1234567890.0

    def test_recovery_clears_last_error(self, seeded_exporter, sample_endpoints, mocker):
        """After Portainer comes back, a successful cycle clears the error and up returns to 1."""
        mocker.patch.object(seeded_exporter.session, "get", side_effect=requests.ConnectionError("Connection refused"))
        seeded_exporter.collect_all_metrics()
        assert seeded_exporter.last_error is not None

        def routed_get(url, timeout=None):
            response = MagicMock()
            response.raise_for_status.return_value = None
            if "/docker/containers/" in url:
                response.json.return_value = [_container_payload("web")]
            else:
                response.json.return_value = sample_endpoints
            return response

        mocker.patch.object(seeded_exporter.session, "get", side_effect=routed_get)
        seeded_exporter.collect_all_metrics()

        assert seeded_exporter.last_error is None
        assert seeded_exporter.last_update > 1234567890.0
        assert "portainer_exporter_up 1" in seeded_exporter.generate_metrics_output().split("\n")


class TestPartialEndpointFailure:
    """One endpoint's container fetch fails: log it, skip it, up stays 1."""

    def test_fetch_containers_error_does_not_set_fleet_wide_error(self, exporter, mocker, caplog):
        mocker.patch.object(exporter.session, "get", side_effect=requests.ConnectionError("Connection refused"))

        with caplog.at_level(logging.ERROR):
            containers = exporter.fetch_containers(1, "flaky-host")

        assert containers == []
        assert exporter.last_error is None
        assert any("flaky-host" in record.message for record in caplog.records if record.levelno == logging.ERROR)

    def test_other_endpoints_still_published_and_up_stays_1(self, exporter, sample_endpoints, mocker):
        def routed_get(url, timeout=None):
            if "/endpoints/2/docker/" in url:
                raise requests.ConnectionError("Connection refused")
            response = MagicMock()
            response.raise_for_status.return_value = None
            if "/docker/containers/" in url:
                response.json.return_value = [_container_payload("web")]
            else:
                response.json.return_value = sample_endpoints
            return response

        mocker.patch.object(exporter.session, "get", side_effect=routed_get)

        exporter.collect_all_metrics()

        # Endpoint 1's series are present, endpoint 2 contributed nothing this cycle.
        assert [m.hostname for m in exporter.metrics] == ["docker-host-1"]
        assert len(exporter.endpoint_statuses) == 2
        # A single flaky endpoint must not flip exporter_up fleet-wide.
        assert exporter.last_error is None
        assert exporter.last_update > 0
        assert "portainer_exporter_up 1" in exporter.generate_metrics_output().split("\n")


class TestAtomicPublish:
    """A reader mid-collect sees the previous complete snapshot, never a partial one."""

    def test_reader_never_observes_partial_snapshot(self, seeded_exporter, sample_endpoints, mocker):
        observed: list[tuple[int, int]] = []

        def observing_fetch(endpoint_id, hostname):
            observed.append((len(seeded_exporter.metrics), len(seeded_exporter.endpoint_statuses)))
            return [
                ContainerMetrics(
                    name=f"c{endpoint_id}-a", hostname=hostname, image="img", state=1, health=1, restart_count=0
                ),
                ContainerMetrics(
                    name=f"c{endpoint_id}-b", hostname=hostname, image="img", state=1, health=1, restart_count=0
                ),
            ]

        mocker.patch.object(seeded_exporter, "fetch_endpoints", return_value=sample_endpoints)
        mocker.patch.object(seeded_exporter, "fetch_containers", side_effect=observing_fetch)

        seeded_exporter.collect_all_metrics()

        # During the collect, every observation must equal the previous complete
        # snapshot (3 metrics, 2 endpoint statuses) — never cleared, never growing.
        assert observed == [(3, 2), (3, 2)]
        # The new snapshot is published whole at the end.
        assert len(seeded_exporter.metrics) == 4
        assert len(seeded_exporter.endpoint_statuses) == 2


class TestServerHardening:
    """main() must serve with a threading server and a per-connection timeout."""

    def test_handler_has_socket_timeout(self):
        assert MetricsHandler.timeout == 30

    def test_main_uses_threading_http_server(self, mock_env, monkeypatch, mocker):
        monkeypatch.setenv("FRESHNESS_ENABLED", "false")
        mocker.patch.object(PortainerExporter, "collect_all_metrics")
        mocker.patch("app.run_collector_thread")
        server = MagicMock()
        server.serve_forever.side_effect = KeyboardInterrupt
        server_cls = mocker.patch("app.ThreadingHTTPServer", return_value=server)

        with pytest.raises(SystemExit) as exc_info:
            app.main()

        assert exc_info.value.code == 0
        server_cls.assert_called_once_with(("0.0.0.0", 8081), MetricsHandler)
        server.serve_forever.assert_called_once()
