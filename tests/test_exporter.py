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
