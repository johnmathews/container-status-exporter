"""
Tests for offline endpoint handling.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from app import (
    ENDPOINT_STATUS_DOWN,
    ENDPOINT_STATUS_UP,
    EndpointStatus,
    PortainerExporter,
)


@pytest.fixture
def exporter(mock_env):
    """Create an exporter instance."""
    with patch.dict("os.environ", mock_env):
        return PortainerExporter()


@pytest.fixture
def endpoints_mixed_status():
    """Endpoints where one is online and one is offline."""
    return [
        {
            "Id": 1,
            "Name": "online-host",
            "Type": 1,
            "URL": "unix:///var/run/docker.sock",
            "Status": ENDPOINT_STATUS_UP,
        },
        {
            "Id": 2,
            "Name": "offline-vm",
            "Type": 1,
            "URL": "tcp://192.168.1.100:2375",
            "Status": ENDPOINT_STATUS_DOWN,
        },
    ]


@pytest.fixture
def endpoints_all_offline():
    """All endpoints are offline."""
    return [
        {
            "Id": 1,
            "Name": "host-1",
            "Type": 1,
            "Status": ENDPOINT_STATUS_DOWN,
        },
        {
            "Id": 2,
            "Name": "host-2",
            "Type": 1,
            "Status": ENDPOINT_STATUS_DOWN,
        },
    ]


class TestOfflineEndpointSkipping:
    """Test that offline endpoints are skipped during collection."""

    def test_skips_offline_endpoints(self, exporter, endpoints_mixed_status, mocker):
        """Test that offline endpoints are not fetched for containers."""
        mocker.patch.object(exporter, "fetch_endpoints", return_value=endpoints_mixed_status)
        mock_fetch = mocker.patch.object(exporter, "fetch_containers", return_value=[])

        exporter.collect_all_metrics()

        # fetch_containers should only be called for the online endpoint
        mock_fetch.assert_called_once_with(1, "online-host")

    def test_all_offline_endpoints_no_fetch(self, exporter, endpoints_all_offline, mocker):
        """Test that no container fetches happen when all endpoints are offline."""
        mocker.patch.object(exporter, "fetch_endpoints", return_value=endpoints_all_offline)
        mock_fetch = mocker.patch.object(exporter, "fetch_containers", return_value=[])

        exporter.collect_all_metrics()

        mock_fetch.assert_not_called()
        assert exporter.last_error is None

    def test_offline_does_not_set_last_error(self, exporter, endpoints_mixed_status, mocker):
        """Test that offline endpoints don't cause last_error to be set."""
        mocker.patch.object(exporter, "fetch_endpoints", return_value=endpoints_mixed_status)
        mocker.patch.object(exporter, "fetch_containers", return_value=[])

        exporter.collect_all_metrics()

        assert exporter.last_error is None

    def test_endpoints_without_status_default_to_online(self, exporter, mocker):
        """Test that endpoints missing the Status field are treated as online."""
        endpoints_no_status = [
            {"Id": 1, "Name": "legacy-host", "Type": 1},
        ]
        mocker.patch.object(exporter, "fetch_endpoints", return_value=endpoints_no_status)
        mock_fetch = mocker.patch.object(exporter, "fetch_containers", return_value=[])

        exporter.collect_all_metrics()

        mock_fetch.assert_called_once_with(1, "legacy-host")


class TestEndpointStatusTracking:
    """Test that endpoint statuses are tracked and exposed as metrics."""

    def test_tracks_endpoint_statuses(self, exporter, endpoints_mixed_status, mocker):
        """Test that endpoint statuses are recorded."""
        mocker.patch.object(exporter, "fetch_endpoints", return_value=endpoints_mixed_status)
        mocker.patch.object(exporter, "fetch_containers", return_value=[])

        exporter.collect_all_metrics()

        assert len(exporter.endpoint_statuses) == 2
        assert exporter.endpoint_statuses[0] == EndpointStatus(endpoint_id=1, hostname="online-host", online=True)
        assert exporter.endpoint_statuses[1] == EndpointStatus(endpoint_id=2, hostname="offline-vm", online=False)

    def test_endpoint_status_metric_in_output(self, exporter, endpoints_mixed_status, mocker):
        """Test that portainer_endpoint_status metric appears in output."""
        mocker.patch.object(exporter, "fetch_endpoints", return_value=endpoints_mixed_status)
        mocker.patch.object(exporter, "fetch_containers", return_value=[])

        exporter.collect_all_metrics()
        output = exporter.generate_metrics_output()

        assert "# HELP portainer_endpoint_status" in output
        assert "# TYPE portainer_endpoint_status gauge" in output
        assert 'portainer_endpoint_status{hostname="online-host"} 1' in output
        assert 'portainer_endpoint_status{hostname="offline-vm"} 0' in output

    def test_endpoint_statuses_cleared_between_collections(self, exporter, mocker):
        """Test that endpoint statuses are reset each collection cycle."""
        endpoints_1 = [{"Id": 1, "Name": "host-1", "Status": ENDPOINT_STATUS_UP}]
        endpoints_2 = [{"Id": 2, "Name": "host-2", "Status": ENDPOINT_STATUS_DOWN}]

        mocker.patch.object(exporter, "fetch_containers", return_value=[])

        mocker.patch.object(exporter, "fetch_endpoints", return_value=endpoints_1)
        exporter.collect_all_metrics()
        assert len(exporter.endpoint_statuses) == 1
        assert exporter.endpoint_statuses[0].hostname == "host-1"

        mocker.patch.object(exporter, "fetch_endpoints", return_value=endpoints_2)
        exporter.collect_all_metrics()
        assert len(exporter.endpoint_statuses) == 1
        assert exporter.endpoint_statuses[0].hostname == "host-2"


class TestHTTPErrorHandling:
    """Test that HTTP 502/503 from Portainer are handled as offline endpoints."""

    def test_502_treated_as_offline(self, exporter, mocker):
        """Test that HTTP 502 is treated as offline, not an error."""
        mock_response = MagicMock()
        mock_response.status_code = 502
        mock_response.raise_for_status.side_effect = requests.HTTPError(response=mock_response)
        mocker.patch.object(exporter.session, "get", return_value=mock_response)

        containers = exporter.fetch_containers(1, "offline-host")

        assert containers == []
        assert exporter.last_error is None

    def test_503_treated_as_offline(self, exporter, mocker):
        """Test that HTTP 503 is treated as offline, not an error."""
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.raise_for_status.side_effect = requests.HTTPError(response=mock_response)
        mocker.patch.object(exporter.session, "get", return_value=mock_response)

        containers = exporter.fetch_containers(1, "offline-host")

        assert containers == []
        assert exporter.last_error is None

    def test_500_treated_as_real_error(self, exporter, mocker, caplog):
        """Test that HTTP 500 is logged at ERROR (unlike 502/503) without flipping exporter_up."""
        import logging

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.raise_for_status.side_effect = requests.HTTPError("500 Server Error", response=mock_response)
        mocker.patch.object(exporter.session, "get", return_value=mock_response)

        with caplog.at_level(logging.ERROR):
            containers = exporter.fetch_containers(1, "error-host")

        assert containers == []
        assert any("error-host" in r.message for r in caplog.records if r.levelno == logging.ERROR)
        # Per-endpoint failures never set the fleet-wide error.
        assert exporter.last_error is None

    def test_401_treated_as_real_error(self, exporter, mocker, caplog):
        """Test that HTTP 401 is logged at ERROR (unlike 502/503) without flipping exporter_up."""
        import logging

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.raise_for_status.side_effect = requests.HTTPError("401 Unauthorized", response=mock_response)
        mocker.patch.object(exporter.session, "get", return_value=mock_response)

        with caplog.at_level(logging.ERROR):
            containers = exporter.fetch_containers(1, "auth-error-host")

        assert containers == []
        assert any("auth-error-host" in r.message for r in caplog.records if r.levelno == logging.ERROR)
        assert exporter.last_error is None

    def test_connection_error_treated_as_real_error(self, exporter, mocker, caplog):
        """Test that connection errors are logged at ERROR without flipping exporter_up."""
        import logging

        mocker.patch.object(exporter.session, "get", side_effect=requests.ConnectionError("Connection refused"))

        with caplog.at_level(logging.ERROR):
            containers = exporter.fetch_containers(1, "unreachable-host")

        assert containers == []
        assert any("Connection refused" in r.message for r in caplog.records if r.levelno == logging.ERROR)
        assert exporter.last_error is None


class TestLogMessages:
    """Test that log messages are descriptive and at appropriate levels."""

    def test_offline_endpoint_logs_debug(self, exporter, endpoints_mixed_status, mocker, caplog):
        """Test that offline endpoints are logged at DEBUG level."""
        import logging

        mocker.patch.object(exporter, "fetch_endpoints", return_value=endpoints_mixed_status)
        mocker.patch.object(exporter, "fetch_containers", return_value=[])

        with caplog.at_level(logging.DEBUG):
            exporter.collect_all_metrics()

        debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("offline-vm" in msg and "offline" in msg.lower() for msg in debug_messages)

    def test_summary_log_includes_counts(self, exporter, endpoints_mixed_status, mocker, caplog):
        """Test that the summary log includes online/offline counts."""
        import logging

        mocker.patch.object(exporter, "fetch_endpoints", return_value=endpoints_mixed_status)
        mocker.patch.object(exporter, "fetch_containers", return_value=[])

        with caplog.at_level(logging.INFO):
            exporter.collect_all_metrics()

        info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any("1 online" in msg and "1 offline" in msg for msg in info_messages)

    def test_http_503_logs_debug_not_error(self, exporter, mocker, caplog):
        """Test that HTTP 503 is logged at DEBUG, not ERROR."""
        import logging

        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.raise_for_status.side_effect = requests.HTTPError(response=mock_response)
        mocker.patch.object(exporter.session, "get", return_value=mock_response)

        with caplog.at_level(logging.DEBUG):
            exporter.fetch_containers(1, "offline-host")

        error_messages = [r for r in caplog.records if r.levelno == logging.ERROR]
        debug_messages = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert len(error_messages) == 0
        assert len(debug_messages) >= 1
