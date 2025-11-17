"""
Tests for PortainerExporter class and helper functions.
"""

import pytest
from unittest.mock import patch, MagicMock
from app import PortainerExporter, ContainerState, HealthStatus


class TestPortainerExporterInit:
    """Test PortainerExporter initialization."""

    def test_init_with_valid_env(self, mock_env, mocker):
        """Test initialization with valid environment variables."""
        with patch.dict("os.environ", mock_env):
            exporter = PortainerExporter()
            assert exporter.portainer_url == "http://localhost:9000"
            assert exporter.portainer_token == "test-token-123"
            assert exporter.scrape_interval == 30
            assert exporter.listen_port == 8081

    def test_init_without_token_raises_error(self, monkeypatch):
        """Test initialization fails without PORTAINER_TOKEN."""
        monkeypatch.delenv("PORTAINER_TOKEN", raising=False)
        monkeypatch.setenv("PORTAINER_URL", "http://localhost:9000")
        with pytest.raises(ValueError, match="PORTAINER_TOKEN"):
            PortainerExporter()

    def test_init_strips_trailing_slash(self, mock_env, monkeypatch):
        """Test that trailing slashes are removed from URL."""
        monkeypatch.setenv("PORTAINER_URL", "http://localhost:9000/")
        monkeypatch.setenv("PORTAINER_TOKEN", "test-token-123")
        exporter = PortainerExporter()
        assert exporter.portainer_url == "http://localhost:9000"

    def test_init_sets_api_key_header(self, mock_env):
        """Test that API key header is set in session."""
        with patch.dict("os.environ", mock_env):
            exporter = PortainerExporter()
            assert exporter.session.headers.get("X-API-Key") == "test-token-123"

    def test_init_creates_empty_metrics_list(self, mock_env):
        """Test that metrics list is initialized as empty."""
        with patch.dict("os.environ", mock_env):
            exporter = PortainerExporter()
            assert exporter.metrics == []
            assert exporter.last_error is None
            assert exporter.last_update == 0


class TestGetStateValue:
    """Test _get_state_value helper method."""

    @pytest.fixture
    def exporter(self, mock_env):
        """Create an exporter instance."""
        with patch.dict("os.environ", mock_env):
            return PortainerExporter()

    def test_running_state(self, exporter):
        """Test 'running' maps to RUNNING value."""
        assert exporter._get_state_value("running") == ContainerState.RUNNING.value

    def test_paused_state(self, exporter):
        """Test 'paused' maps to PAUSED value."""
        assert exporter._get_state_value("paused") == ContainerState.PAUSED.value

    def test_exited_state(self, exporter):
        """Test 'exited' maps to EXITED value."""
        assert exporter._get_state_value("exited") == ContainerState.EXITED.value

    def test_created_state(self, exporter):
        """Test 'created' maps to CREATED value."""
        assert exporter._get_state_value("created") == ContainerState.CREATED.value

    def test_restarting_state(self, exporter):
        """Test 'restarting' maps to RESTARTING value."""
        assert exporter._get_state_value("restarting") == ContainerState.RESTARTING.value

    def test_dead_state(self, exporter):
        """Test 'dead' maps to DEAD value."""
        assert exporter._get_state_value("dead") == ContainerState.DEAD.value

    def test_unknown_state(self, exporter):
        """Test unknown state maps to UNKNOWN value."""
        assert exporter._get_state_value("unknown") == ContainerState.UNKNOWN.value

    def test_invalid_state(self, exporter):
        """Test invalid state maps to UNKNOWN value."""
        assert exporter._get_state_value("invalid") == ContainerState.UNKNOWN.value

    def test_case_insensitive(self, exporter):
        """Test state mapping is case insensitive."""
        assert exporter._get_state_value("RUNNING") == ContainerState.RUNNING.value
        assert exporter._get_state_value("RuNnInG") == ContainerState.RUNNING.value


class TestGetHealthValue:
    """Test _get_health_value helper method."""

    @pytest.fixture
    def exporter(self, mock_env):
        """Create an exporter instance."""
        with patch.dict("os.environ", mock_env):
            return PortainerExporter()

    def test_healthy_status(self, exporter):
        """Test 'healthy' maps to HEALTHY value."""
        assert exporter._get_health_value("healthy") == HealthStatus.HEALTHY.value

    def test_unhealthy_status(self, exporter):
        """Test 'unhealthy' maps to UNHEALTHY value."""
        assert exporter._get_health_value("unhealthy") == HealthStatus.UNHEALTHY.value

    def test_starting_status(self, exporter):
        """Test 'starting' maps to STARTING value."""
        assert exporter._get_health_value("starting") == HealthStatus.STARTING.value

    def test_none_status(self, exporter):
        """Test 'none' maps to NONE value."""
        assert exporter._get_health_value("none") == HealthStatus.NONE.value

    def test_unknown_status(self, exporter):
        """Test unknown status maps to NONE value."""
        assert exporter._get_health_value("unknown") == HealthStatus.NONE.value

    def test_case_insensitive(self, exporter):
        """Test health mapping is case insensitive."""
        assert exporter._get_health_value("HEALTHY") == HealthStatus.HEALTHY.value
        assert exporter._get_health_value("HeAlThY") == HealthStatus.HEALTHY.value


class TestParseHealthStatus:
    """Test _parse_health_status helper method."""

    @pytest.fixture
    def exporter(self, mock_env):
        """Create an exporter instance."""
        with patch.dict("os.environ", mock_env):
            return PortainerExporter()

    def test_parse_healthy(self, exporter):
        """Test parsing healthy status from Status string."""
        assert exporter._parse_health_status("Up 2 hours (healthy)") == "healthy"

    def test_parse_unhealthy(self, exporter):
        """Test parsing unhealthy status from Status string."""
        assert exporter._parse_health_status("Up 2 hours (unhealthy)") == "unhealthy"

    def test_parse_starting(self, exporter):
        """Test parsing starting status from Status string."""
        assert exporter._parse_health_status("Up 10 seconds (health: starting)") == "starting"

    def test_parse_none(self, exporter):
        """Test parsing none status from Status string."""
        assert exporter._parse_health_status("Up 2 days ago") == "none"

    def test_parse_case_insensitive(self, exporter):
        """Test parsing is case insensitive."""
        assert exporter._parse_health_status("Up 2 hours (HEALTHY)") == "healthy"
        assert exporter._parse_health_status("Up 2 hours (Unhealthy)") == "unhealthy"

    def test_parse_empty_string(self, exporter):
        """Test parsing empty string returns 'none'."""
        assert exporter._parse_health_status("") == "none"


class TestHostnameLowercase:
    """Test that hostnames are always lowercase in metrics."""

    @pytest.fixture
    def exporter(self, mock_env):
        """Create an exporter instance."""
        with patch.dict("os.environ", mock_env):
            return PortainerExporter()

    def test_fetch_containers_lowercases_hostname(self, exporter, mocker):
        """Test that fetch_containers converts hostname to lowercase."""
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {
                "Id": "abc123",
                "Names": ["/test-container"],
                "Image": "nginx:latest",
                "State": "running",
                "Status": "Up 2 hours",
                "RestartCount": 0,
            }
        ]
        mocker.patch.object(exporter.session, "get", return_value=mock_response)

        # Pass uppercase hostname
        metrics = exporter.fetch_containers(1, "MyDockerHost")
        
        assert len(metrics) == 1
        assert metrics[0].hostname == "mydockerhost"

    def test_fetch_containers_preserves_lowercase_hostname(self, exporter, mocker):
        """Test that lowercase hostnames remain lowercase."""
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {
                "Id": "abc123",
                "Names": ["/test-container"],
                "Image": "nginx:latest",
                "State": "running",
                "Status": "Up 2 hours",
                "RestartCount": 0,
            }
        ]
        mocker.patch.object(exporter.session, "get", return_value=mock_response)

        metrics = exporter.fetch_containers(1, "docker-host-1")
        
        assert len(metrics) == 1
        assert metrics[0].hostname == "docker-host-1"

    def test_fetch_containers_mixed_case_hostname(self, exporter, mocker):
        """Test that mixed case hostnames are converted to lowercase."""
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {
                "Id": "abc123",
                "Names": ["/test-container"],
                "Image": "nginx:latest",
                "State": "running",
                "Status": "Up 2 hours",
                "RestartCount": 0,
            }
        ]
        mocker.patch.object(exporter.session, "get", return_value=mock_response)

        metrics = exporter.fetch_containers(1, "DockerHost-01")
        
        assert len(metrics) == 1
        assert metrics[0].hostname == "dockerhost-01"

    def test_multiple_containers_same_host_lowercase(self, exporter, mocker):
        """Test that multiple containers from same host all have lowercase hostname."""
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {
                "Id": "abc123",
                "Names": ["/web"],
                "Image": "nginx:latest",
                "State": "running",
                "Status": "Up 2 hours",
                "RestartCount": 0,
            },
            {
                "Id": "def456",
                "Names": ["/db"],
                "Image": "postgres:15",
                "State": "running",
                "Status": "Up 2 hours",
                "RestartCount": 0,
            },
            {
                "Id": "ghi789",
                "Names": ["/cache"],
                "Image": "redis:7",
                "State": "running",
                "Status": "Up 2 hours",
                "RestartCount": 0,
            },
        ]
        mocker.patch.object(exporter.session, "get", return_value=mock_response)

        metrics = exporter.fetch_containers(1, "MyProd-Server")
        
        assert len(metrics) == 3
        for metric in metrics:
            assert metric.hostname == "myprod-server"
            assert metric.hostname.islower()

    def test_metrics_output_has_lowercase_hostnames(self, exporter):
        """Test that generated metrics output contains lowercase hostnames."""
        from app import ContainerMetrics
        
        # Manually set metrics with mixed case hostnames
        exporter.metrics = [
            ContainerMetrics(
                name="web-server",
                hostname="MyDockerHost",
                image="nginx:latest",
                state=1,
                health=1,
                restart_count=0,
            ),
            ContainerMetrics(
                name="database",
                hostname="PROD-SERVER",
                image="postgres:15",
                state=1,
                health=1,
                restart_count=0,
            ),
        ]
        
        output = exporter.generate_metrics_output()
        
        # Check that output contains lowercase hostnames
        assert 'hostname="mydockerhost"' in output
        assert 'hostname="prod-server"' in output
        # Ensure no uppercase hostnames in output
        assert 'hostname="MyDockerHost"' not in output
        assert 'hostname="PROD-SERVER"' not in output

    def test_collect_all_metrics_produces_lowercase_hostnames(self, exporter, mocker, sample_endpoints):
        """Test that collect_all_metrics ensures all hostnames are lowercase."""
        # Use endpoints with mixed case names
        endpoints_with_mixed_case = [
            {
                "Id": 1,
                "Name": "MainServer",
                "Type": 1,
                "URL": "unix:///var/run/docker.sock",
            },
            {
                "Id": 2,
                "Name": "BACKUP-HOST",
                "Type": 1,
                "URL": "tcp://192.168.1.100:2375",
            },
        ]
        
        # Mock endpoint fetch
        mock_endpoint_response = MagicMock()
        mock_endpoint_response.json.return_value = endpoints_with_mixed_case
        
        # Mock container fetch
        mock_container_response = MagicMock()
        mock_container_response.json.return_value = [
            {
                "Id": "abc123",
                "Names": ["/container1"],
                "Image": "nginx:latest",
                "State": "running",
                "Status": "Up 2 hours",
                "RestartCount": 0,
            }
        ]
        
        def mock_get(url, timeout=None):
            if "endpoints" in url and "docker" not in url:
                return mock_endpoint_response
            else:
                return mock_container_response
        
        mocker.patch.object(exporter.session, "get", side_effect=mock_get)
        
        exporter.collect_all_metrics()
        
        assert len(exporter.metrics) == 2
        for metric in exporter.metrics:
            assert metric.hostname.islower(), f"Hostname '{metric.hostname}' is not lowercase"
        assert exporter.metrics[0].hostname == "mainserver"
        assert exporter.metrics[1].hostname == "backup-host"
