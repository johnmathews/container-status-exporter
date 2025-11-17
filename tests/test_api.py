"""
Tests for Portainer API interactions.
"""

import pytest
from unittest.mock import patch, MagicMock
import requests
from app import PortainerExporter


class TestFetchEndpoints:
    """Test fetch_endpoints method."""

    @pytest.fixture
    def exporter(self, mock_env):
        """Create an exporter instance."""
        with patch.dict("os.environ", mock_env):
            return PortainerExporter()

    def test_fetch_endpoints_success(self, exporter, sample_endpoints, mocker):
        """Test successful endpoint fetching."""
        mock_response = MagicMock()
        mock_response.json.return_value = sample_endpoints
        mock_response.raise_for_status.return_value = None
        mocker.patch.object(exporter.session, "get", return_value=mock_response)

        endpoints = exporter.fetch_endpoints()

        assert len(endpoints) == 2
        assert endpoints[0]["Name"] == "docker-host-1"
        assert endpoints[1]["Name"] == "docker-host-2"
        exporter.session.get.assert_called_once()

    def test_fetch_endpoints_paginated_response(self, exporter, sample_endpoints, mocker):
        """Test fetching endpoints from paginated response."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": sample_endpoints}
        mock_response.raise_for_status.return_value = None
        mocker.patch.object(exporter.session, "get", return_value=mock_response)

        endpoints = exporter.fetch_endpoints()

        assert len(endpoints) == 2
        assert endpoints[0]["Name"] == "docker-host-1"

    def test_fetch_endpoints_api_error(self, exporter, mocker):
        """Test handling of API errors when fetching endpoints."""
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = requests.RequestException("Connection failed")
        mocker.patch.object(exporter.session, "get", return_value=mock_response)

        endpoints = exporter.fetch_endpoints()

        assert endpoints == []
        assert "Connection failed" in exporter.last_error

    def test_fetch_endpoints_timeout(self, exporter, mocker):
        """Test handling of timeout when fetching endpoints."""
        mocker.patch.object(
            exporter.session,
            "get",
            side_effect=requests.Timeout("Request timed out")
        )

        endpoints = exporter.fetch_endpoints()

        assert endpoints == []
        assert "Request timed out" in exporter.last_error

    def test_fetch_endpoints_calls_correct_url(self, exporter, sample_endpoints, mocker):
        """Test that correct API endpoint is called."""
        mock_response = MagicMock()
        mock_response.json.return_value = sample_endpoints
        mock_response.raise_for_status.return_value = None
        mock_get = mocker.patch.object(exporter.session, "get", return_value=mock_response)

        exporter.fetch_endpoints()

        mock_get.assert_called_once_with("http://localhost:9000/api/endpoints", timeout=10)


class TestFetchContainers:
    """Test fetch_containers method."""

    @pytest.fixture
    def exporter(self, mock_env):
        """Create an exporter instance."""
        with patch.dict("os.environ", mock_env):
            return PortainerExporter()

    def test_fetch_containers_success(self, exporter, sample_containers, mocker):
        """Test successful container fetching."""
        mock_response = MagicMock()
        mock_response.json.return_value = sample_containers
        mock_response.raise_for_status.return_value = None
        mocker.patch.object(exporter.session, "get", return_value=mock_response)

        containers = exporter.fetch_containers(1, "docker-host-1")

        assert len(containers) == 4
        assert containers[0].name == "web-server"
        assert containers[0].state == 1  # running
        assert containers[0].health == 1  # healthy

    def test_fetch_containers_parsing(self, exporter, sample_containers, mocker):
        """Test container data is parsed correctly."""
        mock_response = MagicMock()
        mock_response.json.return_value = sample_containers
        mock_response.raise_for_status.return_value = None
        mocker.patch.object(exporter.session, "get", return_value=mock_response)

        containers = exporter.fetch_containers(1, "docker-host-1")

        # Check first container
        assert containers[0].name == "web-server"
        assert containers[0].hostname == "docker-host-1"
        assert containers[0].image == "nginx:latest"
        assert containers[0].restart_count == 0

        # Check second container
        assert containers[1].name == "database"
        assert containers[1].state == 1  # running
        assert containers[1].health == 2  # unhealthy
        assert containers[1].restart_count == 3

    def test_fetch_containers_health_parsing(self, exporter, sample_containers, mocker):
        """Test health status parsing from container status."""
        mock_response = MagicMock()
        mock_response.json.return_value = sample_containers
        mock_response.raise_for_status.return_value = None
        mocker.patch.object(exporter.session, "get", return_value=mock_response)

        containers = exporter.fetch_containers(1, "docker-host-1")

        assert containers[0].health == 1  # healthy
        assert containers[1].health == 2  # unhealthy
        assert containers[2].health == 0  # none (exited)

    def test_fetch_containers_removes_leading_slash(self, exporter, mocker):
        """Test that leading slashes are removed from container names."""
        containers_with_slash = [{
            "Id": "abc123",
            "Names": ["/my-container"],
            "Image": "image:latest",
            "State": "running",
            "Status": "Up 1 hour",
            "RestartCount": 0,
        }]
        mock_response = MagicMock()
        mock_response.json.return_value = containers_with_slash
        mock_response.raise_for_status.return_value = None
        mocker.patch.object(exporter.session, "get", return_value=mock_response)

        containers = exporter.fetch_containers(1, "docker-host-1")

        assert containers[0].name == "my-container"

    def test_fetch_containers_api_error(self, exporter, mocker):
        """Test handling of API errors when fetching containers."""
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = requests.RequestException("API error")
        mocker.patch.object(exporter.session, "get", return_value=mock_response)

        containers = exporter.fetch_containers(1, "docker-host-1")

        assert containers == []
        assert "API error" in exporter.last_error

    def test_fetch_containers_calls_correct_url(self, exporter, sample_containers, mocker):
        """Test that correct API endpoint is called."""
        mock_response = MagicMock()
        mock_response.json.return_value = sample_containers
        mock_response.raise_for_status.return_value = None
        mock_get = mocker.patch.object(exporter.session, "get", return_value=mock_response)

        exporter.fetch_containers(1, "docker-host-1")

        mock_get.assert_called_once_with(
            "http://localhost:9000/api/endpoints/1/docker/containers/json?all=true",
            timeout=10
        )


class TestCollectAllMetrics:
    """Test collect_all_metrics method."""

    @pytest.fixture
    def exporter(self, mock_env):
        """Create an exporter instance."""
        with patch.dict("os.environ", mock_env):
            return PortainerExporter()

    def test_collect_all_metrics_success(self, exporter, sample_endpoints, sample_containers, mocker):
        """Test successful metrics collection from all endpoints."""
        # Mock fetch_endpoints
        mocker.patch.object(exporter, "fetch_endpoints", return_value=sample_endpoints)

        # Mock fetch_containers
        mocker.patch.object(exporter, "fetch_containers", return_value=[
            mocker.MagicMock(name="web", hostname="docker-host-1", image="nginx", state=1, health=1, restart_count=0),
            mocker.MagicMock(name="db", hostname="docker-host-1", image="postgres", state=1, health=2, restart_count=1),
        ])

        exporter.collect_all_metrics()

        assert len(exporter.metrics) == 4  # 2 calls to fetch_containers with 2 containers each
        assert exporter.last_error is None
        assert exporter.last_update > 0

    def test_collect_all_metrics_clears_previous(self, exporter, sample_endpoints, mocker):
        """Test that metrics are cleared before collection."""
        mocker.patch.object(exporter, "fetch_endpoints", return_value=sample_endpoints)
        mocker.patch.object(exporter, "fetch_containers", return_value=[])

        # Add some initial metrics
        exporter.metrics = [mocker.MagicMock()]
        assert len(exporter.metrics) == 1

        exporter.collect_all_metrics()

        # Should be cleared and have new results
        assert len(exporter.metrics) == 0

    def test_collect_all_metrics_handles_fetch_error(self, exporter, mocker):
        """Test handling when fetch_endpoints fails."""
        mocker.patch.object(exporter, "fetch_endpoints", side_effect=Exception("Network error"))

        exporter.collect_all_metrics()

        assert exporter.last_error == "Network error"
        assert len(exporter.metrics) == 0
