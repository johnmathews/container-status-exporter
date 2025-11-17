"""
Tests for metrics generation and output format.
"""

import pytest
from unittest.mock import patch
from app import PortainerExporter


class TestGenerateMetricsOutput:
    """Test generate_metrics_output method."""

    @pytest.fixture
    def exporter_with_metrics(self, mock_env, sample_container_metrics):
        """Create an exporter with sample metrics."""
        with patch.dict("os.environ", mock_env):
            exporter = PortainerExporter()
            exporter.metrics = sample_container_metrics
            exporter.last_update = 1234567890
            return exporter

    def test_output_includes_help_comments(self, exporter_with_metrics):
        """Test that output includes HELP comments."""
        output = exporter_with_metrics.generate_metrics_output()
        assert "# HELP container_state" in output
        assert "# HELP container_health" in output
        assert "# HELP container_restart_count" in output

    def test_output_includes_type_comments(self, exporter_with_metrics):
        """Test that output includes TYPE comments."""
        output = exporter_with_metrics.generate_metrics_output()
        assert "# TYPE container_state gauge" in output
        assert "# TYPE container_health gauge" in output
        assert "# TYPE container_restart_count gauge" in output

    def test_output_includes_all_metrics(self, exporter_with_metrics):
        """Test that output includes all metrics."""
        output = exporter_with_metrics.generate_metrics_output()

        # Should have 3 metrics per container (state, health, restart_count)
        # Plus 2 exporter metrics (up, last_scrape_timestamp)
        lines = [line for line in output.split("\n") if line and not line.startswith("#")]
        assert len(lines) >= 9  # 3 containers * 3 metrics + 2 exporter metrics

    def test_metrics_have_correct_labels(self, exporter_with_metrics):
        """Test that metrics include correct labels."""
        output = exporter_with_metrics.generate_metrics_output()

        assert 'container_name="web-server"' in output
        assert 'hostname="docker-host-1"' in output
        assert 'image="nginx:latest"' in output

    def test_state_metric_values(self, exporter_with_metrics):
        """Test that state metrics have correct values."""
        output = exporter_with_metrics.generate_metrics_output()

        # Check that running container has state 1
        assert 'container_state{container_name="web-server",hostname="docker-host-1",image="nginx:latest"} 1' in output
        # Check that exited container has state 0
        assert 'container_state{container_name="backup",hostname="docker-host-2",image="busybox:latest"} 0' in output

    def test_health_metric_values(self, exporter_with_metrics):
        """Test that health metrics have correct values."""
        output = exporter_with_metrics.generate_metrics_output()

        # Check healthy container
        assert 'container_health{container_name="web-server",hostname="docker-host-1",image="nginx:latest"} 1' in output
        # Check unhealthy container
        assert 'container_health{container_name="database",hostname="docker-host-1",image="postgres:15"} 2' in output

    def test_restart_count_metrics(self, exporter_with_metrics):
        """Test that restart count metrics are correct."""
        output = exporter_with_metrics.generate_metrics_output()

        # Check containers with different restart counts
        assert 'container_restart_count{container_name="web-server",hostname="docker-host-1",image="nginx:latest"} 0' in output
        assert 'container_restart_count{container_name="database",hostname="docker-host-1",image="postgres:15"} 3' in output

    def test_exporter_up_metric_success(self, exporter_with_metrics):
        """Test exporter_up metric when no error."""
        exporter_with_metrics.last_error = None
        output = exporter_with_metrics.generate_metrics_output()
        assert "portainer_exporter_up 1" in output

    def test_exporter_up_metric_failure(self, exporter_with_metrics):
        """Test exporter_up metric when error occurred."""
        exporter_with_metrics.last_error = "API error"
        output = exporter_with_metrics.generate_metrics_output()
        assert "portainer_exporter_up 0" in output

    def test_last_scrape_timestamp_metric(self, exporter_with_metrics):
        """Test last scrape timestamp metric."""
        output = exporter_with_metrics.generate_metrics_output()
        assert f"portainer_exporter_last_scrape_timestamp {int(exporter_with_metrics.last_update)}" in output

    def test_empty_metrics_list(self, mock_env):
        """Test output with empty metrics list."""
        with patch.dict("os.environ", mock_env):
            exporter = PortainerExporter()
            exporter.metrics = []
            exporter.last_update = 1234567890
            output = exporter.generate_metrics_output()

            # Should still have HELP and TYPE comments
            assert "# HELP container_state" in output
            # Should have exporter metrics
            assert "portainer_exporter_up" in output
            assert "portainer_exporter_last_scrape_timestamp" in output

    def test_output_ends_with_newline(self, exporter_with_metrics):
        """Test that output ends with newline."""
        output = exporter_with_metrics.generate_metrics_output()
        assert output.endswith("\n")

    def test_prometheus_format_compliance(self, exporter_with_metrics):
        """Test output follows Prometheus text format."""
        output = exporter_with_metrics.generate_metrics_output()
        lines = output.strip().split("\n")

        for line in lines:
            if line.startswith("#"):
                # Comments should start with #
                assert line.startswith("# HELP") or line.startswith("# TYPE")
            elif line:
                # Metrics should have metric_name{labels} value format
                assert "{" in line or line.split()[0] != ""

    def test_metric_name_consistency(self, exporter_with_metrics):
        """Test that all metric names are consistent."""
        output = exporter_with_metrics.generate_metrics_output()

        # Count occurrences
        state_count = output.count("container_state{")
        health_count = output.count("container_health{")
        restart_count = output.count("container_restart_count{")

        # Each metric should appear for every container
        assert state_count == len(exporter_with_metrics.metrics)
        assert health_count == len(exporter_with_metrics.metrics)
        assert restart_count == len(exporter_with_metrics.metrics)
