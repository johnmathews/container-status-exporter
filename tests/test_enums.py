"""
Tests for ContainerState and HealthStatus enums.
"""

import pytest
from app import ContainerState, HealthStatus


class TestContainerState:
    """Test ContainerState enum."""

    def test_exited_value(self):
        """Test EXITED state has value 0."""
        assert ContainerState.EXITED.value == 0

    def test_running_value(self):
        """Test RUNNING state has value 1."""
        assert ContainerState.RUNNING.value == 1

    def test_paused_value(self):
        """Test PAUSED state has value 2."""
        assert ContainerState.PAUSED.value == 2

    def test_created_value(self):
        """Test CREATED state has value 3."""
        assert ContainerState.CREATED.value == 3

    def test_restarting_value(self):
        """Test RESTARTING state has value 4."""
        assert ContainerState.RESTARTING.value == 4

    def test_dead_value(self):
        """Test DEAD state has value 5."""
        assert ContainerState.DEAD.value == 5

    def test_unknown_value(self):
        """Test UNKNOWN state has value 6."""
        assert ContainerState.UNKNOWN.value == 6

    def test_all_states_unique(self):
        """Test all states have unique values."""
        values = [state.value for state in ContainerState]
        assert len(values) == len(set(values))


class TestHealthStatus:
    """Test HealthStatus enum."""

    def test_none_value(self):
        """Test NONE health status has value 0."""
        assert HealthStatus.NONE.value == 0

    def test_healthy_value(self):
        """Test HEALTHY status has value 1."""
        assert HealthStatus.HEALTHY.value == 1

    def test_unhealthy_value(self):
        """Test UNHEALTHY status has value 2."""
        assert HealthStatus.UNHEALTHY.value == 2

    def test_starting_value(self):
        """Test STARTING status has value 3."""
        assert HealthStatus.STARTING.value == 3

    def test_all_statuses_unique(self):
        """Test all statuses have unique values."""
        values = [status.value for status in HealthStatus]
        assert len(values) == len(set(values))
