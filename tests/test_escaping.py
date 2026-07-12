r"""
Label-value escaping tests (finding H2).

Label values flow from upstream-controlled data (OCI image labels, container
names, Portainer endpoint names) into the Prometheus exposition text. Per the
text format spec, backslash, double-quote and newline in a label value must be
escaped as \\ , \" and \n — otherwise a single hostile value produces an
invalid line and Prometheus rejects the ENTIRE scrape body.

These tests parse with an escape-aware grammar (stricter than the one in
test_contract.py, whose values never contain quotes): a label value is a
double-quoted string whose body is any character except a raw quote,
backslash, or newline, or one of the three legal escape sequences.
"""

import re
from unittest.mock import patch

import pytest

from app import ContainerMetrics, EndpointStatus, PortainerExporter
from freshness import STATUS_OUTDATED, FreshnessCollector, ImageFreshness, escape_label_value

# --- Escape-aware exposition grammar -----------------------------------------

METRIC_NAME_PATTERN = r"[a-zA-Z_:][a-zA-Z0-9_:]*"
# Body chars: anything but raw ", \, newline; escapes limited to \\ \" \n per spec
ESCAPED_LABEL_VALUE = r'"(?:[^"\\\n]|\\["\\n])*"'
LABEL_PATTERN = rf"[a-zA-Z_][a-zA-Z0-9_]*={ESCAPED_LABEL_VALUE}"
SAMPLE_LINE_RE = re.compile(
    rf"^(?P<name>{METRIC_NAME_PATTERN})"
    rf"(?:\{{(?P<labels>{LABEL_PATTERN}(?:,{LABEL_PATTERN})*)\}})?"
    rf" (?P<value>\S+)$"
)
LABEL_RE = re.compile(rf"([a-zA-Z_][a-zA-Z0-9_]*)=({ESCAPED_LABEL_VALUE})")

# Hostile values exercising all three escape-worthy characters
HOSTILE_VERSION = '1.0 "beta"\nX'  # quotes + raw newline (H2 reproduction)
HOSTILE_AVAILABLE = "2.0\\rc1"  # literal backslash
HOSTILE_NAME = 'web"server'
HOSTILE_IMAGE = 'nginx:"latest"'
HOSTILE_HOSTNAME = 'DC1\\rack"2'  # quote + backslash; app lowercases at render time


def unescape_label_value(value: str) -> str:
    """Reverse the exposition-format escaping (assumes only legal escapes)."""
    out: list[str] = []
    i = 0
    while i < len(value):
        if value[i] == "\\":
            out.append({"n": "\n", '"': '"', "\\": "\\"}[value[i + 1]])
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def assert_line_valid(line: str) -> None:
    """A line must be blank, a HELP/TYPE comment, or a grammar-valid sample."""
    if not line.strip():
        return
    if line.startswith("#"):
        assert line.startswith("# HELP ") or line.startswith("# TYPE "), f"Invalid comment line: {line!r}"
        return
    match = SAMPLE_LINE_RE.match(line)
    assert match, f"Line does not match escape-aware exposition grammar: {line!r}"
    float(match.group("value"))  # value must be a parseable number


def sample_lines(text: str) -> list[str]:
    return [line for line in text.split("\n") if line.strip() and not line.startswith("#")]


def parse_labels(line: str) -> dict[str, str]:
    """Extract labels from a sample line, unescaping the values."""
    match = SAMPLE_LINE_RE.match(line)
    assert match, f"Unparseable sample line: {line!r}"
    return {key: unescape_label_value(raw[1:-1]) for key, raw in LABEL_RE.findall(match.group("labels") or "")}


# --- Fixtures with hostile values in every label position --------------------


@pytest.fixture
def hostile_freshness(monkeypatch) -> FreshnessCollector:
    """A FreshnessCollector whose version labels carry quotes/newline/backslash."""
    monkeypatch.setenv("PORTAINER_URL", "http://localhost:9000")
    monkeypatch.setenv("PORTAINER_TOKEN", "test-token-123")
    collector = FreshnessCollector()
    collector.results = [
        ImageFreshness(
            container_name="web-server",
            hostname="docker-host-1",
            image="nginx:latest",
            status=STATUS_OUTDATED,
            current_version=HOSTILE_VERSION,
            available_version=HOSTILE_AVAILABLE,
            current_created=1700000000.0,
            available_created=1780000000.0,
        ),
    ]
    collector.last_check = 1780000123.0
    return collector


@pytest.fixture
def hostile_exporter(mock_env) -> PortainerExporter:
    """A PortainerExporter with hostile container name/image and endpoint hostname."""
    with patch.dict("os.environ", mock_env):
        exporter = PortainerExporter()
    exporter.metrics = [
        ContainerMetrics(
            name=HOSTILE_NAME,
            hostname=HOSTILE_HOSTNAME,
            image=HOSTILE_IMAGE,
            state=1,
            health=1,
            restart_count=0,
        ),
    ]
    # collect_all_metrics lowercases hostnames at ingestion; mirror that here
    exporter.endpoint_statuses = [
        EndpointStatus(endpoint_id=1, hostname=HOSTILE_HOSTNAME.lower(), online=True),
    ]
    exporter.last_update = 1234567890
    exporter.last_error = None
    return exporter


# --- The escape helper itself -------------------------------------------------


class TestEscapeLabelValue:
    def test_clean_value_is_identity(self):
        assert escape_label_value("nginx:1.27-alpine") == "nginx:1.27-alpine"

    def test_backslash(self):
        assert escape_label_value("a\\b") == "a\\\\b"

    def test_double_quote(self):
        assert escape_label_value('a"b') == 'a\\"b'

    def test_newline(self):
        assert escape_label_value("a\nb") == "a\\nb"

    def test_backslash_n_stays_distinct_from_newline(self):
        """A literal backslash-n must not collide with an escaped newline."""
        assert escape_label_value("a\\nb") == "a\\\\nb"
        assert escape_label_value("a\\nb") != escape_label_value("a\nb")

    def test_h2_reproduction_value(self):
        assert escape_label_value(HOSTILE_VERSION) == '1.0 \\"beta\\"\\nX'


# --- (a) freshness.py: hostile version labels ---------------------------------


class TestFreshnessEscaping:
    def test_exactly_one_line_per_sample(self, hostile_freshness):
        """A raw newline in a label value must not split a sample across lines."""
        output = hostile_freshness.generate_output()
        # 1 result -> outdated, info, current_created, available_created + last_check
        assert len(sample_lines(output)) == 5

    def test_every_line_matches_exposition_grammar(self, hostile_freshness):
        output = hostile_freshness.generate_output()
        for line in output.split("\n"):
            assert_line_valid(line)

    def test_escaped_values_round_trip(self, hostile_freshness):
        output = hostile_freshness.generate_output()
        info_lines = [line for line in sample_lines(output) if line.startswith("container_image_info{")]
        assert len(info_lines) == 1
        labels = parse_labels(info_lines[0])
        assert labels["current_version"] == HOSTILE_VERSION
        assert labels["available_version"] == HOSTILE_AVAILABLE

    def test_rendered_text_contains_escape_sequences(self, hostile_freshness):
        """Literal backslash-n / backslash-quote on the wire, not raw newline/quote."""
        output = hostile_freshness.generate_output()
        assert 'current_version="1.0 \\"beta\\"\\nX"' in output
        assert 'available_version="2.0\\\\rc1"' in output


# --- (b) app.py: hostile container name/image and endpoint hostname -----------


class TestAppEscaping:
    def test_every_line_matches_exposition_grammar(self, hostile_exporter):
        output = hostile_exporter.generate_metrics_output()
        for line in output.split("\n"):
            assert_line_valid(line)

    def test_container_labels_round_trip(self, hostile_exporter):
        output = hostile_exporter.generate_metrics_output()
        state_lines = [line for line in sample_lines(output) if line.startswith("container_state{")]
        assert len(state_lines) == 1
        labels = parse_labels(state_lines[0])
        assert labels["container_name"] == HOSTILE_NAME
        assert labels["image"] == HOSTILE_IMAGE
        # app.py lowercases the hostname at render time; escaping must come after
        assert labels["hostname"] == HOSTILE_HOSTNAME.lower()

    def test_endpoint_hostname_round_trips(self, hostile_exporter):
        output = hostile_exporter.generate_metrics_output()
        ep_lines = [line for line in sample_lines(output) if line.startswith("portainer_endpoint_status{")]
        assert len(ep_lines) == 1
        assert parse_labels(ep_lines[0])["hostname"] == HOSTILE_HOSTNAME.lower()


# --- (c) whole-output: hostile values in BOTH collectors ----------------------


class TestCombinedOutputEscaping:
    def test_combined_output_fully_parseable_and_untruncated(self, hostile_exporter, hostile_freshness):
        """Assemble the body exactly as MetricsHandler.do_GET does; nothing may truncate."""
        output = hostile_exporter.generate_metrics_output() + "\n" + hostile_freshness.generate_output()
        for line in output.split("\n"):
            assert_line_valid(line)
        # app: state/health/restart_count (1 container) + endpoint_status + up + last_scrape = 6
        # freshness: outdated + info + 2 created timestamps + last_check = 5
        assert len(sample_lines(output)) == 11
