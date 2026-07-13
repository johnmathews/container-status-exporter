"""Tests for the image freshness module."""

import json
import logging
import threading
import time as time_module
from datetime import UTC, datetime
from typing import Any

import pytest
import requests

from freshness import (
    MAX_RESPONSE_BYTES,
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
    RegistryRateLimited,
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

    def test_naive_timestamp_interpreted_as_utc(self, monkeypatch):
        """A tz-less timestamp is registry data in UTC, never the exporter host's local time."""
        monkeypatch.setenv("TZ", "America/New_York")  # UTC-4/-5: local != UTC year-round
        time_module.tzset()
        try:
            expected = datetime(2026, 5, 30, 18, 54, 6, tzinfo=UTC).timestamp()
            assert parse_rfc3339("2026-05-30T18:54:06") == expected
        finally:
            monkeypatch.undo()
            time_module.tzset()


def _response(mocker, status=200, headers=None, body=None, raw=b""):
    """Mock a requests.Response: `body` is the decoded JSON, `raw` overrides the wire bytes."""
    resp = mocker.MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.json.return_value = body
    payload = raw or (b"" if body is None else json.dumps(body).encode())
    resp.iter_content.side_effect = lambda chunk_size=65536: (
        payload[i : i + chunk_size] for i in range(0, len(payload), chunk_size)
    )
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
            'status="error",current_version="",available_version="",base_image=""} 1'
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


class TestRegistryRateLimited:
    """HTTP 429 must surface as a distinct exception, not a generic RegistryError."""

    def test_is_a_registry_error_subclass(self):
        assert issubclass(RegistryRateLimited, RegistryError)

    def test_429_on_manifest_head_raises_rate_limited(self, mocker):
        client = RegistryClient()
        client.session = mocker.MagicMock()
        client.session.request.return_value = _response(mocker, 429)
        with pytest.raises(RegistryRateLimited):
            client.get_remote_digest(parse_image_ref("nginx:latest"))

    def test_429_on_token_fetch_raises_rate_limited(self, mocker):
        client = RegistryClient()
        client.session = mocker.MagicMock()
        client.session.get.return_value = _response(mocker, 429)
        challenge = 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io"'
        with pytest.raises(RegistryRateLimited):
            client._fetch_token(challenge, "library/nginx")

    @pytest.mark.parametrize("status", [401, 404, 500])
    def test_non_429_http_errors_still_raise_plain_registry_error(self, mocker, status):
        """The dead-repo signal (401/404/500 -> status=error) must survive 429 handling."""
        client = RegistryClient()
        client.session = mocker.MagicMock()
        client.session.request.return_value = _response(mocker, status)
        with pytest.raises(RegistryError) as excinfo:
            client.get_remote_digest(parse_image_ref("dead/repo:latest"))
        assert not isinstance(excinfo.value, RegistryRateLimited)


class TestHeadTransientRetry:
    """The digest HEAD is cheap and idempotent: one retry after a short backoff."""

    def test_transient_error_then_success(self, mocker):
        client = RegistryClient()
        client.session = mocker.MagicMock()
        head = _response(mocker, 200, {"Docker-Content-Digest": "sha256:abc"})
        client.session.request.side_effect = [requests.ConnectionError("connection reset"), head]
        sleep = mocker.patch("freshness.time.sleep")

        assert client.get_remote_digest(parse_image_ref("nginx:latest")) == "sha256:abc"
        assert client.session.request.call_count == 2
        sleep.assert_called_once()
        assert sleep.call_args[0][0] == pytest.approx(2, abs=0.5)

    def test_second_transient_failure_propagates(self, mocker):
        client = RegistryClient()
        client.session = mocker.MagicMock()
        client.session.request.side_effect = requests.ConnectionError("still down")
        mocker.patch("freshness.time.sleep")

        with pytest.raises(requests.RequestException):
            client.get_remote_digest(parse_image_ref("nginx:latest"))
        assert client.session.request.call_count == 2


class TestTokenCache:
    """Anonymous tokens are cached per (registry, repository) with a short TTL."""

    CHALLENGE = 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io"'

    def _client(self, mocker) -> RegistryClient:
        client = RegistryClient()
        client.session = mocker.MagicMock()
        return client

    def test_second_request_within_ttl_reuses_cached_token(self, mocker):
        client = self._client(mocker)
        unauthorized = _response(mocker, 401, {"WWW-Authenticate": self.CHALLENGE})
        ok1 = _response(mocker, 200, {"Docker-Content-Digest": "sha256:a"})
        ok2 = _response(mocker, 200, {"Docker-Content-Digest": "sha256:a"})
        client.session.request.side_effect = [unauthorized, ok1, ok2]
        client.session.get.return_value = _response(mocker, 200, body={"token": "tok1"})

        ref = parse_image_ref("nginx:latest")
        assert client.get_remote_digest(ref) == "sha256:a"
        assert client.get_remote_digest(ref) == "sha256:a"

        # auth endpoint hit exactly once; second request carried the cached token up front
        assert client.session.get.call_count == 1
        assert client.session.request.call_count == 3
        _, kwargs = client.session.request.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer tok1"

    def test_expired_ttl_refetches_token(self, mocker):
        client = self._client(mocker)
        client.token_ttl = 0.0  # everything is instantly stale
        unauthorized1 = _response(mocker, 401, {"WWW-Authenticate": self.CHALLENGE})
        ok1 = _response(mocker, 200, {"Docker-Content-Digest": "sha256:a"})
        unauthorized2 = _response(mocker, 401, {"WWW-Authenticate": self.CHALLENGE})
        ok2 = _response(mocker, 200, {"Docker-Content-Digest": "sha256:a"})
        client.session.request.side_effect = [unauthorized1, ok1, unauthorized2, ok2]
        client.session.get.return_value = _response(mocker, 200, body={"token": "tok"})

        ref = parse_image_ref("nginx:latest")
        client.get_remote_digest(ref)
        client.get_remote_digest(ref)

        assert client.session.get.call_count == 2

    def test_401_with_cached_token_invalidates_and_refetches_once(self, mocker):
        client = self._client(mocker)
        unauthorized1 = _response(mocker, 401, {"WWW-Authenticate": self.CHALLENGE})
        ok1 = _response(mocker, 200, {"Docker-Content-Digest": "sha256:a"})
        # second call: cached token rejected (revoked), one re-dance, then success
        unauthorized2 = _response(mocker, 401, {"WWW-Authenticate": self.CHALLENGE})
        ok2 = _response(mocker, 200, {"Docker-Content-Digest": "sha256:b"})
        client.session.request.side_effect = [unauthorized1, ok1, unauthorized2, ok2]
        token1 = _response(mocker, 200, body={"token": "tok1"})
        token2 = _response(mocker, 200, body={"token": "tok2"})
        client.session.get.side_effect = [token1, token2]

        ref = parse_image_ref("nginx:latest")
        assert client.get_remote_digest(ref) == "sha256:a"
        assert client.get_remote_digest(ref) == "sha256:b"

        assert client.session.get.call_count == 2
        _, kwargs = client.session.request.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer tok2"

    def test_cache_is_keyed_per_repository(self, mocker):
        client = self._client(mocker)
        unauthorized1 = _response(mocker, 401, {"WWW-Authenticate": self.CHALLENGE})
        ok1 = _response(mocker, 200, {"Docker-Content-Digest": "sha256:a"})
        unauthorized2 = _response(mocker, 401, {"WWW-Authenticate": self.CHALLENGE})
        ok2 = _response(mocker, 200, {"Docker-Content-Digest": "sha256:b"})
        client.session.request.side_effect = [unauthorized1, ok1, unauthorized2, ok2]
        client.session.get.return_value = _response(mocker, 200, body={"token": "tok"})

        client.get_remote_digest(parse_image_ref("nginx:latest"))
        client.get_remote_digest(parse_image_ref("jellyfin/jellyfin:latest"))

        # different repository -> different scope -> its own token dance
        assert client.session.get.call_count == 2


class TestRateLimitGracefulDegradation:
    """A rate-limited cycle must not flip previously-known images to status=error."""

    CONTAINERS = [{"Names": ["/web"], "Image": "nginx:latest", "ImageID": "sha256:img"}]
    INSPECTED = {"Created": "2026-01-01T00:00:00Z", "Config": {}, "RepoDigests": ["nginx@sha256:old"]}

    def _wire_portainer(self, mocker, collector, containers, inspected):
        def get_json(path: str) -> Any:
            if path == "/api/endpoints":
                return [{"Id": 1, "Name": "host-a", "Status": 1}]
            if path.endswith("/docker/containers/json"):
                return containers
            if "/docker/images/" in path:
                return inspected
            raise AssertionError(f"unexpected path {path}")

        mocker.patch.object(collector, "_get_json", side_effect=get_json)

    def test_429_with_previous_result_carries_it_forward(self, mocker, monkeypatch, caplog):
        collector = _collector(monkeypatch)
        collector.results = [ImageFreshness("web", "host-a", "nginx:latest", STATUS_OUTDATED, available_version="1.28")]
        self._wire_portainer(mocker, collector, self.CONTAINERS, self.INSPECTED)
        mocker.patch.object(collector.registry, "get_remote_digest", side_effect=RegistryRateLimited("429"))

        with caplog.at_level(logging.WARNING, logger="freshness"):
            collector.collect()

        assert len(collector.results) == 1
        carried = collector.results[0]
        assert carried.status == STATUS_OUTDATED
        assert carried.available_version == "1.28"
        assert not any(r.status == STATUS_ERROR for r in collector.results)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "429" in warnings[0].getMessage()

    def test_429_with_no_previous_result_omits_image(self, mocker, monkeypatch):
        collector = _collector(monkeypatch)
        collector.results = []
        self._wire_portainer(mocker, collector, self.CONTAINERS, self.INSPECTED)
        mocker.patch.object(collector.registry, "get_remote_digest", side_effect=RegistryRateLimited("429"))

        collector.collect()

        assert collector.results == []
        assert collector.last_check > 0

    def test_429_does_not_contaminate_remote_cache_for_other_containers(self, mocker, monkeypatch, caplog):
        """Two containers share one image: one 429 must not error either, and warns once."""
        collector = _collector(monkeypatch)
        collector.results = [ImageFreshness("web1", "host-a", "nginx:latest", STATUS_OK)]
        containers = [
            {"Names": ["/web1"], "Image": "nginx:latest", "ImageID": "sha256:img"},
            {"Names": ["/web2"], "Image": "nginx:latest", "ImageID": "sha256:img"},
        ]
        self._wire_portainer(mocker, collector, containers, self.INSPECTED)
        digest = mocker.patch.object(collector.registry, "get_remote_digest", side_effect=RegistryRateLimited("429"))

        with caplog.at_level(logging.WARNING, logger="freshness"):
            collector.collect()

        assert digest.call_count == 1  # per-cycle cache still deduplicates the image
        assert not any(r.status == STATUS_ERROR for r in collector.results)
        statuses = {r.container_name: r.status for r in collector.results}
        assert statuses == {"web1": STATUS_OK}  # web2 had no previous result -> absent
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    def test_non_429_registry_error_still_yields_error_status(self, mocker, monkeypatch):
        """Graceful degradation is 429-only: dead repos must keep reading status=error."""
        collector = _collector(monkeypatch)
        collector.results = [ImageFreshness("web", "host-a", "nginx:latest", STATUS_OK)]
        self._wire_portainer(mocker, collector, self.CONTAINERS, self.INSPECTED)
        mocker.patch.object(collector.registry, "get_remote_digest", side_effect=RegistryError("HTTP 401"))

        collector.collect()

        assert len(collector.results) == 1
        assert collector.results[0].status == STATUS_ERROR

    def test_transient_head_blip_recovers_within_cycle(self, mocker, monkeypatch):
        """ConnectionError then success on the HEAD retry -> normal verdict, two HEADs, ~2s backoff."""
        collector = _collector(monkeypatch)
        self._wire_portainer(mocker, collector, self.CONTAINERS, self.INSPECTED)
        collector.registry.session = mocker.MagicMock()
        head = _response(mocker, 200, {"Docker-Content-Digest": "sha256:old"})
        collector.registry.session.request.side_effect = [requests.ConnectionError("blip"), head]
        mocker.patch.object(collector.registry, "get_remote_metadata", return_value=("", 0.0))
        sleep = mocker.patch("freshness.time.sleep")

        collector.collect()

        assert len(collector.results) == 1
        assert collector.results[0].status == STATUS_OK  # sha256:old matches RepoDigests
        assert collector.registry.session.request.call_count == 2
        sleep.assert_called_once()
        assert sleep.call_args[0][0] == pytest.approx(2, abs=0.5)


def _wire_single_host(mocker, collector, containers, inspected):
    def get_json(path: str) -> Any:
        if path == "/api/endpoints":
            return [{"Id": 1, "Name": "host-a", "Status": 1}]
        if path.endswith("/docker/containers/json"):
            return containers
        if "/docker/images/" in path:
            return inspected
        raise AssertionError(f"unexpected path {path}")

    mocker.patch.object(collector, "_get_json", side_effect=get_json)


class TestBoundedResponseReads:
    """Registry/Portainer bodies are read incrementally and capped, never slurped unbounded."""

    TOKEN_CHALLENGE = 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io"'

    def test_oversized_streamed_manifest_raises_registry_error(self, mocker):
        client = RegistryClient()
        client.session = mocker.MagicMock()
        client.session.request.return_value = _response(mocker, 200, raw=b"x" * (MAX_RESPONSE_BYTES + 1))
        with pytest.raises(RegistryError):
            client.get_remote_metadata(parse_image_ref("nginx:latest"), "sha256:top")

    def test_oversized_content_length_rejected_before_reading_body(self, mocker):
        client = RegistryClient()
        client.session = mocker.MagicMock()
        resp = _response(mocker, 200, headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)}, body={})
        client.session.request.return_value = resp
        with pytest.raises(RegistryError):
            client.get_remote_metadata(parse_image_ref("nginx:latest"), "sha256:top")
        resp.iter_content.assert_not_called()

    def test_oversized_token_response_raises_registry_error(self, mocker):
        client = RegistryClient()
        client.session = mocker.MagicMock()
        client.session.request.return_value = _response(mocker, 401, {"WWW-Authenticate": self.TOKEN_CHALLENGE})
        client.session.get.return_value = _response(mocker, 200, raw=b"x" * (MAX_RESPONSE_BYTES + 1))
        with pytest.raises(RegistryError):
            client.get_remote_digest(parse_image_ref("nginx:latest"))

    def test_malformed_json_body_raises_registry_error(self, mocker):
        """json.loads replaced response.json(): parse failures must still surface as RegistryError."""
        client = RegistryClient()
        client.session = mocker.MagicMock()
        client.session.request.return_value = _response(mocker, 200, raw=b"not json{")
        with pytest.raises(RegistryError):
            client.get_remote_metadata(parse_image_ref("nginx:latest"), "sha256:top")

    def test_normal_size_response_unaffected(self, mocker):
        client = RegistryClient()
        client.session = mocker.MagicMock()
        manifest = {"config": {"digest": "sha256:cfg"}}
        config = {"created": "2026-06-30T23:54:12Z", "config": {"Labels": {}}}
        client.session.request.side_effect = [
            _response(mocker, 200, body=manifest),
            _response(mocker, 200, body=config),
        ]
        version, created = client.get_remote_metadata(parse_image_ref("nginx:latest"), "sha256:top")
        assert version == ""
        assert created > 0

    def test_oversized_registry_body_yields_error_status_in_collect(self, mocker, monkeypatch):
        """A hostile registry streaming an endless token body -> status=error, not OOM."""
        collector = _collector(monkeypatch)
        _wire_single_host(
            mocker,
            collector,
            containers=[{"Names": ["/web"], "Image": "nginx:latest", "ImageID": "sha256:img"}],
            inspected={"Created": "2026-01-01T00:00:00Z", "Config": {}, "RepoDigests": ["nginx@sha256:old"]},
        )
        collector.registry.session = mocker.MagicMock()
        collector.registry.session.request.return_value = _response(
            mocker, 401, {"WWW-Authenticate": self.TOKEN_CHALLENGE}
        )
        collector.registry.session.get.return_value = _response(mocker, 200, raw=b"x" * (MAX_RESPONSE_BYTES + 1))

        collector.collect()
        assert len(collector.results) == 1
        assert collector.results[0].status == STATUS_ERROR

    def test_oversized_portainer_response_raises_request_exception(self, mocker, monkeypatch):
        """Portainer is semi-trusted, but the cap is free: oversize surfaces as the usual request failure."""
        collector = _collector(monkeypatch)
        collector.session = mocker.MagicMock()
        collector.session.get.return_value = _response(mocker, 200, raw=b"x" * (MAX_RESPONSE_BYTES + 1))
        with pytest.raises(requests.RequestException):
            collector._get_json("/api/endpoints")

    def test_normal_portainer_response_parsed(self, mocker, monkeypatch):
        collector = _collector(monkeypatch)
        collector.session = mocker.MagicMock()
        collector.session.get.return_value = _response(mocker, 200, body=[{"Id": 1}])
        assert collector._get_json("/api/endpoints") == [{"Id": 1}]


class TestMetadataFetchedByDigest:
    """get_remote_metadata must GET the digest it just compared, not re-resolve the movable tag."""

    def test_manifest_requested_by_digest_not_tag(self, mocker):
        client = RegistryClient()
        client.session = mocker.MagicMock()
        manifest = {"config": {"digest": "sha256:cfg"}}
        config = {"created": "2026-06-30T23:54:12Z", "config": {"Labels": {}}}
        client.session.request.side_effect = [
            _response(mocker, 200, body=manifest),
            _response(mocker, 200, body=config),
        ]

        client.get_remote_metadata(parse_image_ref("jellyfin/jellyfin:latest"), "sha256:top")

        first_url = client.session.request.call_args_list[0][0][1]
        assert first_url == "https://registry-1.docker.io/v2/jellyfin/jellyfin/manifests/sha256:top"
        assert "/manifests/latest" not in first_url


class TestBareImageIdRefs:
    """A container whose Image is a bare image ID names no repository: local, not a registry error."""

    INSPECTED = {"Created": "2026-01-01T00:00:00Z", "Config": {}, "RepoDigests": ["nginx@sha256:old"]}

    @pytest.mark.parametrize(
        "image",
        [
            "sha256:" + "a" * 64,  # canonical image ID
            "sha256:abc123",  # truncated sha256: form
            "f" * 64,  # bare 64-hex ID without the sha256: prefix
        ],
    )
    def test_bare_image_id_is_local_without_registry_call(self, mocker, monkeypatch, image):
        collector = _collector(monkeypatch)
        _wire_single_host(
            mocker,
            collector,
            containers=[{"Names": ["/orphan"], "Image": image, "ImageID": "sha256:img"}],
            inspected=self.INSPECTED,  # RepoDigests non-empty: detection must key off the ref shape
        )
        digest = mocker.patch.object(collector.registry, "get_remote_digest")

        collector.collect()
        assert len(collector.results) == 1
        assert collector.results[0].status == STATUS_LOCAL
        assert collector.results[0].outdated == 0
        digest.assert_not_called()

    def test_normal_image_still_checked_against_registry(self, mocker, monkeypatch):
        collector = _collector(monkeypatch)
        _wire_single_host(
            mocker,
            collector,
            containers=[{"Names": ["/web"], "Image": "nginx:latest", "ImageID": "sha256:img"}],
            inspected=self.INSPECTED,
        )
        digest = mocker.patch.object(collector.registry, "get_remote_digest", return_value="sha256:old")
        mocker.patch.object(collector.registry, "get_remote_metadata", return_value=("", 0.0))

        collector.collect()
        assert collector.results[0].status == STATUS_OK
        digest.assert_called_once()


class TestCheckRemotePinnedLeak:
    """collect() filters pinned refs before _check_remote; a leak must fail safe, not report pinned."""

    def test_unparseable_ref_reaching_check_remote_is_error(self, monkeypatch):
        collector = _collector(monkeypatch)
        state = collector._check_remote("valkey/valkey:8@sha256:fea8", {})
        assert state.status == STATUS_ERROR


class TestBaseImageFreshness:
    """Local builds carrying the OCI base-image annotation get base freshness tracking."""

    BASE_LABEL = "org.opencontainers.image.base.name"

    def _wire(self, mocker, monkeypatch, container_labels, base_inspect=None):
        collector = _collector(monkeypatch)

        def get_json(path: str) -> Any:
            if path == "/api/endpoints":
                return [{"Id": 1, "Name": "host-a", "Status": 1}]
            if path.endswith("/docker/containers/json"):
                return [{"Names": ["/jf"], "Image": "local-build:latest", "ImageID": "sha256:img"}]
            if "/docker/images/sha256:img/json" in path:
                return {"Created": "2026-07-12T00:00:00Z", "Config": {"Labels": container_labels}, "RepoDigests": []}
            if "/docker/images/jellyfin/jellyfin:latest/json" in path:
                if base_inspect is None:
                    raise requests.RequestException("no such image")
                return base_inspect
            raise AssertionError(f"unexpected path {path}")

        mocker.patch.object(collector, "_get_json", side_effect=get_json)
        return collector

    def test_base_up_to_date(self, mocker, monkeypatch):
        collector = self._wire(
            mocker, monkeypatch,
            container_labels={self.BASE_LABEL: "jellyfin/jellyfin:latest",
                              "org.opencontainers.image.version": "10.11.11"},
            base_inspect={"RepoDigests": ["jellyfin/jellyfin@sha256:samebase"]},
        )
        mocker.patch.object(collector.registry, "get_remote_digest", return_value="sha256:samebase")
        mocker.patch.object(collector.registry, "get_remote_metadata", return_value=("10.11.11", 1780000000.0))

        collector.collect()
        result = collector.results[0]
        assert result.status == STATUS_OK
        assert result.base_image == "jellyfin/jellyfin:latest"
        assert result.outdated == 0

    def test_base_outdated(self, mocker, monkeypatch):
        collector = self._wire(
            mocker, monkeypatch,
            container_labels={self.BASE_LABEL: "jellyfin/jellyfin:latest"},
            base_inspect={"RepoDigests": ["jellyfin/jellyfin@sha256:oldbase"]},
        )
        mocker.patch.object(collector.registry, "get_remote_digest", return_value="sha256:newbase")
        mocker.patch.object(collector.registry, "get_remote_metadata", return_value=("10.11.12", 1790000000.0))

        collector.collect()
        result = collector.results[0]
        assert result.status == STATUS_OUTDATED
        assert result.outdated == 1
        assert result.available_version == "10.11.12"
        assert result.base_image == "jellyfin/jellyfin:latest"

    def test_no_annotation_stays_local(self, mocker, monkeypatch):
        collector = self._wire(mocker, monkeypatch, container_labels={})
        remote = mocker.patch.object(collector.registry, "get_remote_digest")

        collector.collect()
        assert collector.results[0].status == STATUS_LOCAL
        assert collector.results[0].base_image == ""
        remote.assert_not_called()

    def test_base_missing_locally_stays_local(self, mocker, monkeypatch):
        collector = self._wire(
            mocker, monkeypatch,
            container_labels={self.BASE_LABEL: "jellyfin/jellyfin:latest"},
            base_inspect=None,  # inspect raises
        )
        collector.collect()
        assert collector.results[0].status == STATUS_LOCAL

    def test_digest_pinned_annotation_stays_local(self, mocker, monkeypatch):
        collector = self._wire(
            mocker, monkeypatch,
            container_labels={self.BASE_LABEL: "jellyfin/jellyfin@sha256:abc"},
        )
        collector.collect()
        assert collector.results[0].status == STATUS_LOCAL

    def test_base_without_repodigests_stays_local(self, mocker, monkeypatch):
        collector = self._wire(
            mocker, monkeypatch,
            container_labels={self.BASE_LABEL: "jellyfin/jellyfin:latest"},
            base_inspect={"RepoDigests": []},
        )
        collector.collect()
        assert collector.results[0].status == STATUS_LOCAL

    def test_base_rate_limited_carries_forward(self, mocker, monkeypatch):
        collector = self._wire(
            mocker, monkeypatch,
            container_labels={self.BASE_LABEL: "jellyfin/jellyfin:latest"},
            base_inspect={"RepoDigests": ["jellyfin/jellyfin@sha256:oldbase"]},
        )
        prior = ImageFreshness("jf", "host-a", "local-build:latest", STATUS_OUTDATED,
                               base_image="jellyfin/jellyfin:latest")
        collector.results = [prior]
        from freshness import RegistryRateLimited
        mocker.patch.object(collector.registry, "get_remote_digest",
                            side_effect=RegistryRateLimited("429"))

        collector.collect()
        assert collector.results == [prior]

    def test_base_image_label_rendered(self, monkeypatch):
        collector = _collector(monkeypatch)
        collector.results = [
            ImageFreshness("jf", "host-a", "local-build:latest", STATUS_OUTDATED,
                           base_image="jellyfin/jellyfin:latest"),
            ImageFreshness("web", "host-a", "nginx:latest", STATUS_OK),
        ]
        output = collector.generate_output()
        assert 'base_image="jellyfin/jellyfin:latest"' in output
        assert 'container_name="web"' in output and 'base_image=""' in output
