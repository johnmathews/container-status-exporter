"""Tests for the image freshness module."""

import logging
import threading
from datetime import UTC
from typing import Any

import pytest
import requests

from freshness import (
    STATUS_ERROR,
    STATUS_LOCAL,
    STATUS_OK,
    STATUS_OUTDATED,
    STATUS_PINNED,
    FreshnessCollector,
    ImageFreshness,
    ImageRef,
    RegistryClient,
    RegistryError,
    _collect_safely,
    parse_image_ref,
    parse_rfc3339,
    run_freshness_thread,
)


class TestParseImageRef:
    """parse_image_ref must normalize every registry shape used in the homelab."""

    def test_hub_official_image(self):
        ref = parse_image_ref("nginx:latest")
        assert ref == ImageRef("registry-1.docker.io", "library/nginx", "latest", "nginx:latest")

    def test_hub_org_image(self):
        ref = parse_image_ref("jellyfin/jellyfin:latest")
        assert ref.registry == "registry-1.docker.io"
        assert ref.repository == "jellyfin/jellyfin"

    def test_explicit_docker_io(self):
        ref = parse_image_ref("docker.io/grafana/grafana-oss:latest")
        assert ref.registry == "registry-1.docker.io"
        assert ref.repository == "grafana/grafana-oss"

    def test_ghcr(self):
        ref = parse_image_ref("ghcr.io/immich-app/immich-server:release")
        assert ref.registry == "ghcr.io"
        assert ref.repository == "immich-app/immich-server"
        assert ref.tag == "release"

    def test_lscr_and_quay_and_gcr(self):
        assert parse_image_ref("lscr.io/linuxserver/sabnzbd:4.4.1").registry == "lscr.io"
        assert parse_image_ref("quay.io/prometheus/node-exporter:v1.8.2").registry == "quay.io"
        assert parse_image_ref("gcr.io/cadvisor/cadvisor:v0.49.1").registry == "gcr.io"

    def test_no_tag_defaults_to_latest(self):
        ref = parse_image_ref("qmcgaw/gluetun")
        assert ref.tag == "latest"

    def test_registry_with_port(self):
        ref = parse_image_ref("localhost:5000/myimage:dev")
        assert ref.registry == "localhost:5000"
        assert ref.repository == "myimage"
        assert ref.tag == "dev"

    def test_digest_pinned_returns_none(self):
        assert parse_image_ref("valkey/valkey:8-bookworm@sha256:fea8b3e67b15") is None


class TestParseRfc3339:
    def test_nanosecond_precision(self):
        from datetime import datetime

        expected = datetime(2026, 5, 30, 18, 54, 6, 123456, tzinfo=UTC).timestamp()
        assert parse_rfc3339("2026-05-30T18:54:06.123456789Z") == pytest.approx(expected, abs=0.001)

    def test_plain_utc(self):
        assert parse_rfc3339("2026-05-30T18:54:06Z") > 0

    def test_garbage_returns_zero(self):
        assert parse_rfc3339("not-a-date") == 0.0
        assert parse_rfc3339("") == 0.0


def _response(mocker, status=200, headers=None, body=None):
    resp = mocker.MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.json.return_value = body
    return resp


class TestRegistryClient:
    def test_digest_without_auth(self, mocker):
        client = RegistryClient()
        head = _response(mocker, 200, {"Docker-Content-Digest": "sha256:abc"})
        client.session = mocker.MagicMock()
        client.session.request.return_value = head

        ref = parse_image_ref("jellyfin/jellyfin:latest")
        assert client.get_remote_digest(ref) == "sha256:abc"
        method, url = client.session.request.call_args[0]
        assert method == "HEAD"
        assert url == "https://registry-1.docker.io/v2/jellyfin/jellyfin/manifests/latest"

    def test_digest_with_token_dance(self, mocker):
        client = RegistryClient()
        client.session = mocker.MagicMock()
        challenge = 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io"'
        unauthorized = _response(mocker, 401, {"WWW-Authenticate": challenge})
        authorized = _response(mocker, 200, {"Docker-Content-Digest": "sha256:def"})
        client.session.request.side_effect = [unauthorized, authorized]
        token_resp = _response(mocker, 200, body={"token": "tok123"})
        token_resp.raise_for_status = mocker.MagicMock()
        client.session.get.return_value = token_resp

        ref = parse_image_ref("jellyfin/jellyfin:latest")
        assert client.get_remote_digest(ref) == "sha256:def"
        # token request carried the pull scope
        _, kwargs = client.session.get.call_args
        assert kwargs["params"]["scope"] == "repository:jellyfin/jellyfin:pull"
        # retry carried the bearer token
        _, retry_kwargs = client.session.request.call_args
        assert retry_kwargs["headers"]["Authorization"] == "Bearer tok123"

    def test_error_status_raises(self, mocker):
        client = RegistryClient()
        client.session = mocker.MagicMock()
        client.session.request.return_value = _response(mocker, 404)
        with pytest.raises(RegistryError):
            client.get_remote_digest(parse_image_ref("dead/repo:latest"))

    def test_metadata_multiarch_and_cache(self, mocker):
        client = RegistryClient()
        client.session = mocker.MagicMock()
        index = {
            "manifests": [
                {"platform": {"os": "linux", "architecture": "arm64"}, "digest": "sha256:arm"},
                {"platform": {"os": "linux", "architecture": "amd64"}, "digest": "sha256:amd"},
            ]
        }
        child = {"config": {"digest": "sha256:cfg"}}
        config = {
            "created": "2026-06-30T23:54:12Z",
            "config": {"Labels": {"org.opencontainers.image.version": "10.11.11"}},
        }
        client.session.request.side_effect = [
            _response(mocker, 200, body=index),
            _response(mocker, 200, body=child),
            _response(mocker, 200, body=config),
        ]

        ref = parse_image_ref("jellyfin/jellyfin:latest")
        version, created = client.get_remote_metadata(ref, "sha256:top")
        assert version == "10.11.11"
        assert created > 0
        # cached by digest: no further requests
        client.session.request.side_effect = None
        client.session.request.return_value = None
        assert client.get_remote_metadata(ref, "sha256:top") == (version, created)


def _collector(monkeypatch) -> FreshnessCollector:
    monkeypatch.setenv("PORTAINER_URL", "http://localhost:9000")
    monkeypatch.setenv("PORTAINER_TOKEN", "test-token")
    return FreshnessCollector()


class TestFreshnessCollector:
    def _wire_portainer(self, mocker, collector, containers, inspected):
        def get_json(path: str) -> Any:
            if path == "/api/endpoints":
                return [{"Id": 1, "Name": "host-a", "Status": 1}, {"Id": 2, "Name": "down", "Status": 2}]
            if path.endswith("/docker/containers/json"):
                return containers
            if "/docker/images/" in path:
                return inspected
            raise AssertionError(f"unexpected path {path}")

        mocker.patch.object(collector, "_get_json", side_effect=get_json)

    def test_up_to_date_container(self, mocker, monkeypatch):
        collector = _collector(monkeypatch)
        self._wire_portainer(
            mocker,
            collector,
            containers=[{"Names": ["/web"], "Image": "nginx:latest", "ImageID": "sha256:img"}],
            inspected={
                "Created": "2026-06-01T00:00:00Z",
                "Config": {"Labels": {"org.opencontainers.image.version": "1.27"}},
                "RepoDigests": ["nginx@sha256:samedigest"],
            },
        )
        mocker.patch.object(collector.registry, "get_remote_digest", return_value="sha256:samedigest")
        mocker.patch.object(collector.registry, "get_remote_metadata", return_value=("1.27", 1780000000.0))

        collector.collect()
        assert len(collector.results) == 1
        result = collector.results[0]
        assert result.status == STATUS_OK
        assert result.outdated == 0
        assert result.hostname == "host-a"

    def test_outdated_container(self, mocker, monkeypatch):
        collector = _collector(monkeypatch)
        self._wire_portainer(
            mocker,
            collector,
            containers=[{"Names": ["/web"], "Image": "nginx:latest", "ImageID": "sha256:img"}],
            inspected={"Created": "2026-01-01T00:00:00Z", "Config": {}, "RepoDigests": ["nginx@sha256:old"]},
        )
        mocker.patch.object(collector.registry, "get_remote_digest", return_value="sha256:new")
        mocker.patch.object(collector.registry, "get_remote_metadata", return_value=("1.28", 1780000000.0))

        collector.collect()
        result = collector.results[0]
        assert result.status == STATUS_OUTDATED
        assert result.outdated == 1
        assert result.available_version == "1.28"

    def test_local_build_is_not_outdated(self, mocker, monkeypatch):
        collector = _collector(monkeypatch)
        self._wire_portainer(
            mocker,
            collector,
            containers=[{"Names": ["/jf"], "Image": "jellyfin-with-yt-dlp:latest", "ImageID": "sha256:img"}],
            inspected={"Created": "2026-07-12T00:00:00Z", "Config": {}, "RepoDigests": []},
        )
        remote = mocker.patch.object(collector.registry, "get_remote_digest")

        collector.collect()
        assert collector.results[0].status == STATUS_LOCAL
        assert collector.results[0].outdated == 0
        remote.assert_not_called()

    def test_digest_pinned_image(self, mocker, monkeypatch):
        collector = _collector(monkeypatch)
        self._wire_portainer(
            mocker,
            collector,
            containers=[{"Names": ["/redis"], "Image": "valkey/valkey:8@sha256:fea8", "ImageID": "sha256:img"}],
            inspected={"Created": "2026-01-01T00:00:00Z", "Config": {}, "RepoDigests": ["valkey/valkey@sha256:fea8"]},
        )
        collector.collect()
        assert collector.results[0].status == STATUS_PINNED
        assert collector.results[0].outdated == 0

    def test_registry_error_is_reported_not_fatal(self, mocker, monkeypatch):
        collector = _collector(monkeypatch)
        self._wire_portainer(
            mocker,
            collector,
            containers=[{"Names": ["/dead"], "Image": "gone/gone:latest", "ImageID": "sha256:img"}],
            inspected={"Created": "2026-01-01T00:00:00Z", "Config": {}, "RepoDigests": ["gone/gone@sha256:x"]},
        )
        mocker.patch.object(collector.registry, "get_remote_digest", side_effect=RegistryError("denied"))

        collector.collect()
        assert collector.results[0].status == STATUS_ERROR
        assert collector.results[0].outdated == 0

    def test_offline_endpoint_skipped(self, mocker, monkeypatch):
        collector = _collector(monkeypatch)
        mocker.patch.object(collector, "_get_json", return_value=[{"Id": 2, "Name": "down", "Status": 2}])
        collector.collect()
        assert collector.results == []

    def test_paginated_endpoints_response(self, mocker, monkeypatch):
        """Portainer may wrap endpoints in a paginated {"results": [...]} dict."""
        collector = _collector(monkeypatch)

        def get_json(path: str) -> Any:
            if path == "/api/endpoints":
                return {"results": [{"Id": 1, "Name": "host-a", "Status": 1}]}
            if path.endswith("/docker/containers/json"):
                return [{"Names": ["/web"], "Image": "nginx:latest", "ImageID": "sha256:img"}]
            return {"Created": "2026-06-01T00:00:00Z", "Config": {}, "RepoDigests": ["nginx@sha256:same"]}

        mocker.patch.object(collector, "_get_json", side_effect=get_json)
        mocker.patch.object(collector.registry, "get_remote_digest", return_value="sha256:same")
        mocker.patch.object(collector.registry, "get_remote_metadata", return_value=("1.27", 1780000000.0))

        collector.collect()
        assert len(collector.results) == 1
        result = collector.results[0]
        assert result.status == STATUS_OK
        assert result.hostname == "host-a"

    def test_non_list_endpoints_payload_yields_empty_results(self, mocker, monkeypatch):
        """An unrecognized /api/endpoints payload must not raise, just produce no results."""
        collector = _collector(monkeypatch)
        mocker.patch.object(collector, "_get_json", return_value={"unexpected": True})
        collector.collect()
        assert collector.results == []

    def test_malformed_endpoint_and_container_entries_skipped(self, mocker, monkeypatch):
        """Non-dict junk in endpoint/container lists is skipped without killing the cycle."""
        collector = _collector(monkeypatch)

        def get_json(path: str) -> Any:
            if path == "/api/endpoints":
                return ["garbage", 42, None, {"Id": 1, "Name": "host-a", "Status": 1}]
            if path.endswith("/docker/containers/json"):
                return [None, "junk", 3.14, {"Names": ["/web"], "Image": "nginx:latest", "ImageID": "sha256:img"}]
            return {"Created": "2026-06-01T00:00:00Z", "Config": {}, "RepoDigests": ["nginx@sha256:d"]}

        mocker.patch.object(collector, "_get_json", side_effect=get_json)
        mocker.patch.object(collector.registry, "get_remote_digest", return_value="sha256:d")
        mocker.patch.object(collector.registry, "get_remote_metadata", return_value=("", 0.0))

        collector.collect()
        assert len(collector.results) == 1
        assert collector.results[0].container_name == "web"
        assert collector.results[0].status == STATUS_OK

    def test_metadata_failure_does_not_mask_successful_digest_check(self, mocker, monkeypatch):
        """A metadata blob timeout is decoration: the digest verdict must stand."""
        collector = _collector(monkeypatch)
        self._wire_portainer(
            mocker,
            collector,
            containers=[{"Names": ["/web"], "Image": "nginx:latest", "ImageID": "sha256:img"}],
            inspected={"Created": "2026-01-01T00:00:00Z", "Config": {}, "RepoDigests": ["nginx@sha256:old"]},
        )
        mocker.patch.object(collector.registry, "get_remote_digest", return_value="sha256:new")
        mocker.patch.object(
            collector.registry, "get_remote_metadata", side_effect=requests.Timeout("config blob timed out")
        )

        collector.collect()
        result = collector.results[0]
        assert result.status == STATUS_OUTDATED
        assert result.outdated == 1
        assert result.available_version == ""
        assert result.available_created == 0.0

    def test_inspect_failure_renders_error_status(self, mocker, monkeypatch):
        """Image-inspect failure flows through collect() to a status="error" info metric."""
        collector = _collector(monkeypatch)

        def get_json(path: str) -> Any:
            if path == "/api/endpoints":
                return [{"Id": 1, "Name": "host-a", "Status": 1}]
            if path.endswith("/docker/containers/json"):
                return [{"Names": ["/web"], "Image": "nginx:latest", "ImageID": "sha256:img"}]
            raise requests.RequestException("inspect failed")

        mocker.patch.object(collector, "_get_json", side_effect=get_json)

        collector.collect()
        assert len(collector.results) == 1
        assert collector.results[0].status == STATUS_ERROR
        assert collector.results[0].outdated == 0

        output = collector.generate_output()
        assert (
            'container_image_info{container_name="web",hostname="host-a",image="nginx:latest",'
            'status="error",current_version="",available_version=""} 1'
        ) in output

    def test_endpoints_fetch_failure_keeps_previous_results(self, mocker, monkeypatch):
        """A failed endpoints fetch aborts the cycle without wiping the last good results."""
        collector = _collector(monkeypatch)
        previous = [ImageFreshness("web", "host-a", "nginx:latest", STATUS_OK)]
        collector.results = previous
        mocker.patch.object(collector, "_get_json", side_effect=requests.ConnectionError("portainer down"))

        collector.collect()
        assert collector.results is previous

    def test_containers_fetch_failure_skips_endpoint(self, mocker, monkeypatch):
        """A failed containers fetch skips that endpoint, the cycle still completes."""
        collector = _collector(monkeypatch)

        def get_json(path: str) -> Any:
            if path == "/api/endpoints":
                return [{"Id": 1, "Name": "host-a", "Status": 1}]
            raise requests.RequestException("docker socket gone")

        mocker.patch.object(collector, "_get_json", side_effect=get_json)

        collector.collect()
        assert collector.results == []
        assert collector.last_check > 0

    def test_remote_checked_once_per_image_across_hosts(self, mocker, monkeypatch):
        collector = _collector(monkeypatch)

        def get_json(path: str) -> Any:
            if path == "/api/endpoints":
                return [{"Id": 1, "Name": "a", "Status": 1}, {"Id": 2, "Name": "b", "Status": 1}]
            if path.endswith("/docker/containers/json"):
                return [{"Names": ["/n"], "Image": "nginx:latest", "ImageID": "sha256:img"}]
            return {"Created": "2026-01-01T00:00:00Z", "Config": {}, "RepoDigests": ["nginx@sha256:d"]}

        mocker.patch.object(collector, "_get_json", side_effect=get_json)
        digest = mocker.patch.object(collector.registry, "get_remote_digest", return_value="sha256:d")
        mocker.patch.object(collector.registry, "get_remote_metadata", return_value=("", 0.0))

        collector.collect()
        assert len(collector.results) == 2
        assert digest.call_count == 1


class TestGenerateOutput:
    def test_metrics_rendering(self, monkeypatch):
        collector = _collector(monkeypatch)
        collector.results = [
            ImageFreshness(
                container_name="web",
                hostname="host-a",
                image="nginx:latest",
                status=STATUS_OUTDATED,
                current_version="1.27",
                available_version="1.28",
                current_created=1700000000.0,
                available_created=1780000000.0,
            ),
            ImageFreshness(container_name="jf", hostname="host-b", image="local:latest", status=STATUS_LOCAL),
        ]
        collector.last_check = 1780000123.0

        output = collector.generate_output()
        assert 'container_image_outdated{container_name="web",hostname="host-a",image="nginx:latest"} 1' in output
        assert 'container_image_outdated{container_name="jf",hostname="host-b",image="local:latest"} 0' in output
        assert 'status="outdated"' in output
        assert 'current_version="1.27"' in output
        assert 'available_version="1.28"' in output
        assert "container_image_current_created_timestamp" in output
        assert 'image="nginx:latest"} 1780000000' in output
        assert "container_image_freshness_last_check_timestamp 1780000123" in output
        # local build has no created timestamps rendered
        assert 'container_image_current_created_timestamp{container_name="jf"' not in output

    def test_output_is_valid_prometheus_exposition(self, monkeypatch):
        import re

        collector = _collector(monkeypatch)
        collector.results = [
            ImageFreshness(
                container_name="web",
                hostname="host-a",
                image="nginx:latest",
                status=STATUS_OUTDATED,
                current_version="1.27",
                available_version="1.28",
                current_created=1700000000.0,
                available_created=1780000000.0,
            ),
            ImageFreshness("a", "b", "c:1", STATUS_OK),
        ]
        collector.last_check = 1780000123.0

        label = r'[a-zA-Z_][a-zA-Z0-9_]*="[^"]*"'
        sample_re = re.compile(rf"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{{{label}(?:,{label})*\}})? (\S+)$")
        saw_sample = False
        for line in collector.generate_output().strip().split("\n"):
            if not line:
                continue
            if line.startswith("#"):
                assert re.match(r"^# (HELP|TYPE) [a-zA-Z_:][a-zA-Z0-9_:]* \S", line), f"Invalid comment line: {line!r}"
                continue
            match = sample_re.match(line)
            assert match, f"Invalid sample line: {line!r}"
            float(match.group(2))  # value must be a parseable number
            saw_sample = True
        assert saw_sample


class TestFreshnessThreadResilience:
    def test_collect_safely_swallows_and_logs_arbitrary_exception(self, mocker, monkeypatch, caplog):
        """The loop body must survive any exception from collect() and log it with a traceback."""
        collector = _collector(monkeypatch)
        mocker.patch.object(collector, "collect", side_effect=KeyError("surprising JSON shape"))

        with caplog.at_level(logging.ERROR, logger="freshness"):
            _collect_safely(collector)  # must not raise

        assert any(record.exc_info for record in caplog.records), "expected logger.exception with traceback"

    def test_collect_safely_runs_collect_normally(self, mocker, monkeypatch):
        collector = _collector(monkeypatch)
        collect = mocker.patch.object(collector, "collect")
        _collect_safely(collector)
        collect.assert_called_once()

    def test_thread_reaches_sleep_after_collect_exception(self, mocker, monkeypatch):
        """The daemon thread must proceed to the interval sleep even when collect() blows up."""
        collector = _collector(monkeypatch)
        mocker.patch.object(collector, "collect", side_effect=RuntimeError("boom"))
        slept = threading.Event()

        def fake_sleep(seconds: float) -> None:
            assert seconds == collector.check_interval
            slept.set()
            raise SystemExit  # end the otherwise-infinite loop; silently exits the thread

        mocker.patch("freshness.time.sleep", side_effect=fake_sleep)

        thread = run_freshness_thread(collector)
        assert slept.wait(timeout=5), "thread died before reaching sleep: collect() exception escaped the loop guard"
        thread.join(timeout=5)
        assert not thread.is_alive()


class TestParseChallengeEdgeCases:
    def test_non_bearer_challenge_returns_none(self, mocker):
        client = RegistryClient()
        assert client._fetch_token('Basic realm="x"', "repo") is None

    def test_missing_realm_returns_none(self, mocker):
        client = RegistryClient()
        assert client._fetch_token('Bearer service="x"', "repo") is None
