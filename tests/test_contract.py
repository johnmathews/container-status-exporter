"""
Contract-lock tests for the frozen Prometheus metric surface.

The metric names and label keys emitted by this exporter are a production
contract consumed by Prometheus recording rules and Grafana dashboards.
These tests hard-code that contract as literals and parse the real generated
exposition text, so ANY drift (renamed metric, added/removed label key,
missing HELP/TYPE comment) fails loudly.

Do not "fix" these tests to match a code change without also updating the
downstream Prometheus queries and Grafana dashboards (see CLAUDE.md).
"""

import re
from unittest.mock import patch

import pytest

from app import EndpointStatus, PortainerExporter
from freshness import STATUS_OK, STATUS_OUTDATED, FreshnessCollector, ImageFreshness

# --- The frozen contract: metric family name -> exact label KEY set ---------

FROZEN_CONTRACT: dict[str, frozenset[str]] = {
    # app.py: PortainerExporter.generate_metrics_output()
    "container_state": frozenset({"container_name", "hostname", "image"}),
    "container_health": frozenset({"container_name", "hostname", "image"}),
    "container_restart_count": frozenset({"container_name", "hostname", "image"}),
    "portainer_endpoint_status": frozenset({"hostname"}),
    "portainer_exporter_up": frozenset(),
    "portainer_exporter_last_scrape_timestamp": frozenset(),
    # freshness.py: FreshnessCollector.generate_output()
    "container_image_outdated": frozenset({"container_name", "hostname", "image"}),
    "container_image_info": frozenset(
        {"container_name", "hostname", "image", "status", "current_version", "available_version", "base_image"}
    ),
    "container_image_current_created_timestamp": frozenset({"container_name", "hostname", "image"}),
    "container_image_available_created_timestamp": frozenset({"container_name", "hostname", "image"}),
    "container_image_freshness_last_check_timestamp": frozenset(),
}

# --- Real exposition-format parsing ------------------------------------------

METRIC_NAME_PATTERN = r"[a-zA-Z_:][a-zA-Z0-9_:]*"
LABEL_PATTERN = r'[a-zA-Z_][a-zA-Z0-9_]*="[^"]*"'
SAMPLE_LINE_RE = re.compile(
    rf"^(?P<name>{METRIC_NAME_PATTERN})"
    rf"(?:\{{(?P<labels>{LABEL_PATTERN}(?:,{LABEL_PATTERN})*)\}})?"
    rf" (?P<value>\S+)$"
)
LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="([^"]*)"')
HELP_RE = re.compile(rf"^# HELP ({METRIC_NAME_PATTERN}) \S.*$")
TYPE_RE = re.compile(rf"^# TYPE ({METRIC_NAME_PATTERN}) (counter|gauge|histogram|summary|untyped)$")


def parse_exposition(text: str) -> tuple[dict[str, list[dict[str, str]]], set[str], set[str]]:
    """
    Parse Prometheus text exposition format.

    Returns (samples, help_names, type_names) where samples maps each metric
    family name to the list of label dicts of its sample lines. Raises
    AssertionError on any line that is neither blank, a valid HELP/TYPE
    comment, nor a valid sample line with a float-parseable value.
    """
    samples: dict[str, list[dict[str, str]]] = {}
    help_names: set[str] = set()
    type_names: set[str] = set()

    for line in text.split("\n"):
        if not line.strip():
            continue
        if line.startswith("#"):
            help_match = HELP_RE.match(line)
            type_match = TYPE_RE.match(line)
            assert help_match or type_match, f"Invalid comment line: {line!r}"
            if help_match:
                help_names.add(help_match.group(1))
            if type_match:
                type_names.add(type_match.group(1))
            continue
        match = SAMPLE_LINE_RE.match(line)
        assert match, f"Line does not match Prometheus exposition format: {line!r}"
        float(match.group("value"))  # must be a parseable number
        labels = dict(LABEL_RE.findall(match.group("labels") or ""))
        samples.setdefault(match.group("name"), []).append(labels)

    return samples, help_names, type_names


# --- Fixtures wiring realistic data so EVERY family emits samples ------------


@pytest.fixture
def exporter(mock_env, sample_container_metrics) -> PortainerExporter:
    """A PortainerExporter populated so all app-side families are emitted."""
    with patch.dict("os.environ", mock_env):
        exporter = PortainerExporter()
    exporter.metrics = sample_container_metrics
    exporter.endpoint_statuses = [
        EndpointStatus(endpoint_id=1, hostname="docker-host-1", online=True),
        EndpointStatus(endpoint_id=2, hostname="docker-host-2", online=False),
    ]
    exporter.last_update = 1234567890
    exporter.last_error = None
    return exporter


@pytest.fixture
def freshness(monkeypatch) -> FreshnessCollector:
    """A FreshnessCollector populated so all freshness families are emitted."""
    monkeypatch.setenv("PORTAINER_URL", "http://localhost:9000")
    monkeypatch.setenv("PORTAINER_TOKEN", "test-token-123")
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
        ImageFreshness(
            container_name="database",
            hostname="docker-host-2",
            image="postgres:15",
            status=STATUS_OK,
            current_version="15.6",
            available_version="15.6",
            current_created=1710000000.0,
            available_created=1710000000.0,
        ),
    ]
    collector.last_check = 1780000123.0
    return collector


@pytest.fixture
def combined_output(exporter, freshness) -> str:
    """The full /metrics body exactly as MetricsHandler.do_GET assembles it."""
    return exporter.generate_metrics_output() + "\n" + freshness.generate_output()


# --- The contract-lock assertions ---------------------------------------------


class TestFrozenMetricContract:
    def test_exact_metric_family_set(self, combined_output):
        """The set of emitted metric families is EXACTLY the frozen contract."""
        samples, _, _ = parse_exposition(combined_output)
        assert set(samples) == set(FROZEN_CONTRACT), (
            f"Metric surface drifted. Missing: {set(FROZEN_CONTRACT) - set(samples)}; "
            f"unexpected: {set(samples) - set(FROZEN_CONTRACT)}"
        )

    def test_every_family_has_samples(self, combined_output):
        """Fixture data must exercise every family (otherwise this suite proves nothing)."""
        samples, _, _ = parse_exposition(combined_output)
        for name in FROZEN_CONTRACT:
            assert samples.get(name), f"No sample lines emitted for {name}"

    def test_label_key_sets_are_frozen(self, combined_output):
        """Every sample of every family carries EXACTLY the frozen label keys."""
        samples, _, _ = parse_exposition(combined_output)
        for name, expected_keys in FROZEN_CONTRACT.items():
            for labels in samples[name]:
                assert frozenset(labels) == expected_keys, (
                    f"{name} label keys drifted: got {sorted(labels)}, contract is {sorted(expected_keys)}"
                )

    def test_help_line_for_every_family(self, combined_output):
        _, help_names, _ = parse_exposition(combined_output)
        assert help_names == set(FROZEN_CONTRACT), (
            f"HELP comments drifted. Missing: {set(FROZEN_CONTRACT) - help_names}; "
            f"unexpected: {help_names - set(FROZEN_CONTRACT)}"
        )

    def test_type_line_for_every_family(self, combined_output):
        _, _, type_names = parse_exposition(combined_output)
        assert type_names == set(FROZEN_CONTRACT), (
            f"TYPE comments drifted. Missing: {set(FROZEN_CONTRACT) - type_names}; "
            f"unexpected: {type_names - set(FROZEN_CONTRACT)}"
        )

    def test_every_line_is_valid_exposition_format(self, combined_output):
        """parse_exposition asserts per-line shape; run it over the full body."""
        samples, help_names, type_names = parse_exposition(combined_output)
        assert samples and help_names and type_names
