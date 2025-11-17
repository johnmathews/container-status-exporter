"""
Pytest configuration and shared fixtures for Container Status Exporter tests.
"""

import pytest
import os
from unittest.mock import Mock, patch, MagicMock


@pytest.fixture
def mock_env(monkeypatch):
    """Set up environment variables for testing."""
    monkeypatch.setenv("PORTAINER_URL", "http://localhost:9000")
    monkeypatch.setenv("PORTAINER_TOKEN", "test-token-123")
    monkeypatch.setenv("SCRAPE_INTERVAL", "30")
    monkeypatch.setenv("LISTEN_PORT", "8081")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    return {
        "PORTAINER_URL": "http://localhost:9000",
        "PORTAINER_TOKEN": "test-token-123",
        "SCRAPE_INTERVAL": "30",
        "LISTEN_PORT": "8081",
        "LOG_LEVEL": "INFO",
    }


@pytest.fixture
def mock_session(mocker):
    """Create a mock requests Session."""
    return mocker.MagicMock()


@pytest.fixture
def sample_endpoints():
    """Sample Portainer API response for endpoints."""
    return [
        {
            "Id": 1,
            "Name": "docker-host-1",
            "Type": 1,
            "URL": "unix:///var/run/docker.sock",
        },
        {
            "Id": 2,
            "Name": "docker-host-2",
            "Type": 1,
            "URL": "tcp://192.168.1.100:2375",
        },
    ]


@pytest.fixture
def sample_containers():
    """Sample Portainer API response for containers."""
    return [
        {
            "Id": "abc123",
            "Names": ["/web-server"],
            "Image": "nginx:latest",
            "State": "running",
            "Status": "Up 2 hours (healthy)",
            "RestartCount": 0,
        },
        {
            "Id": "def456",
            "Names": ["/database"],
            "Image": "postgres:15",
            "State": "running",
            "Status": "Up 5 days (unhealthy)",
            "RestartCount": 3,
        },
        {
            "Id": "ghi789",
            "Names": ["/backup"],
            "Image": "busybox:latest",
            "State": "exited",
            "Status": "Exited (0) 2 days ago",
            "RestartCount": 1,
        },
        {
            "Id": "jkl012",
            "Names": ["/temp"],
            "Image": "alpine:latest",
            "State": "created",
            "Status": "Created",
            "RestartCount": 0,
        },
    ]


@pytest.fixture
def sample_container_metrics():
    """Sample ContainerMetrics objects for testing."""
    from app import ContainerMetrics

    return [
        ContainerMetrics(
            name="web-server",
            hostname="docker-host-1",
            image="nginx:latest",
            state=1,  # running
            health=1,  # healthy
            restart_count=0,
        ),
        ContainerMetrics(
            name="database",
            hostname="docker-host-1",
            image="postgres:15",
            state=1,  # running
            health=2,  # unhealthy
            restart_count=3,
        ),
        ContainerMetrics(
            name="backup",
            hostname="docker-host-2",
            image="busybox:latest",
            state=0,  # exited
            health=0,  # none
            restart_count=1,
        ),
    ]
