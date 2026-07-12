#!/usr/bin/env python3
"""
Image freshness checking for the Container Status Exporter.

Compares the digest of each running container's image against the digest the
upstream registry currently serves for the same tag, and exposes the result as
Prometheus metrics. Registry access is anonymous (public images only) using the
standard OCI distribution token flow: on a 401, the WWW-Authenticate challenge
is parsed and a pull-scoped bearer token is fetched from the advertised realm.
This works for Docker Hub, ghcr.io, quay.io, gcr.io and lscr.io alike.

HEAD manifest requests do not count against Docker Hub pull-rate limits, and
version/created metadata blobs are cached by digest (immutable), so a check
cycle is cheap. The default interval is 6 hours.
"""

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from threading import Thread
from typing import Any, cast

import requests

logger = logging.getLogger(__name__)

MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    ]
)

VERSION_LABEL = "org.opencontainers.image.version"
CREATED_LABEL = "org.opencontainers.image.created"

# Freshness status values (exposed as the `status` label on container_image_info)
STATUS_OK = "ok"  # running digest matches the registry
STATUS_OUTDATED = "outdated"  # registry serves a different digest for this tag
STATUS_LOCAL = "local"  # locally-built image, no RepoDigests to compare
STATUS_PINNED = "pinned"  # image pinned by digest (name@sha256:...), never outdated
STATUS_ERROR = "error"  # registry check failed (dead repo, network, auth)


class RegistryError(Exception):
    """Raised when a registry request fails."""


@dataclass
class ImageRef:
    """A parsed image reference."""

    registry: str  # API host, e.g. registry-1.docker.io or ghcr.io
    repository: str  # e.g. library/nginx or immich-app/immich-server
    tag: str  # e.g. latest
    original: str  # the reference as docker reported it


def parse_image_ref(image: str) -> ImageRef | None:
    """
    Parse a docker image reference into registry/repository/tag.

    Returns None for digest-pinned references (name@sha256:...) — those are
    immutable by definition and handled by the caller as STATUS_PINNED.
    """
    if "@" in image:
        return None

    name = image
    tag = "latest"

    # The tag is a ':' in the last path component (registries may carry ports)
    last_slash = name.rfind("/")
    if ":" in name[last_slash + 1 :]:
        name, tag = name.rsplit(":", 1)

    first, _, rest = name.partition("/")
    # The first component is a registry host if it contains a dot/port or is localhost
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, repository = first, rest
    else:
        registry, repository = "docker.io", name

    if registry == "docker.io":
        registry = "registry-1.docker.io"
        if "/" not in repository:
            repository = f"library/{repository}"

    return ImageRef(registry=registry, repository=repository, tag=tag, original=image)


def parse_rfc3339(value: str) -> float:
    """Parse an RFC3339 timestamp (docker uses nanosecond precision) to a unix ts, 0 on failure."""
    if not value:
        return 0.0
    try:
        # Trim sub-second precision beyond microseconds and normalize Z
        cleaned = re.sub(r"\.(\d{6})\d*", r".\1", value).replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned).timestamp()
    except ValueError:
        logger.debug(f"Could not parse timestamp: {value}")
        return 0.0


class RegistryClient:
    """Anonymous client for OCI-distribution registries."""

    def __init__(self, timeout: int = 10, platform: str = "linux/amd64") -> None:
        self.timeout = timeout
        self.platform = platform
        self.session: requests.Session = requests.Session()
        # version/created metadata never changes for a given digest
        self._meta_cache: dict[str, tuple[str, float]] = {}

    def _fetch_token(self, challenge: str, repository: str) -> str | None:
        """Fetch an anonymous pull token from the realm in a WWW-Authenticate challenge."""
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = params.get("realm")
        if not realm or not challenge.lower().startswith("bearer"):
            return None
        query: dict[str, str] = {"scope": f"repository:{repository}:pull"}
        if "service" in params:
            query["service"] = params["service"]
        try:
            response = self.session.get(realm, params=query, timeout=self.timeout)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            token = data.get("token") or data.get("access_token")
            return cast(str | None, token)
        except requests.RequestException as e:
            logger.debug(f"Token fetch from {realm} failed: {e}")
            return None

    def _request(self, method: str, ref: ImageRef, path: str, accept: str) -> requests.Response:
        """Perform a registry request, transparently handling the 401 token dance."""
        url = f"https://{ref.registry}/v2/{ref.repository}/{path}"
        headers = {"Accept": accept}
        response = self.session.request(method, url, headers=headers, timeout=self.timeout)
        if response.status_code == 401:
            challenge = response.headers.get("WWW-Authenticate", "")
            token = self._fetch_token(challenge, ref.repository)
            if token:
                headers["Authorization"] = f"Bearer {token}"
                response = self.session.request(method, url, headers=headers, timeout=self.timeout)
        if response.status_code != 200:
            raise RegistryError(f"{method} {url} -> HTTP {response.status_code}")
        return response

    def get_remote_digest(self, ref: ImageRef) -> str:
        """HEAD the manifest for ref's tag and return its content digest."""
        response = self._request("HEAD", ref, f"manifests/{ref.tag}", MANIFEST_ACCEPT)
        digest = response.headers.get("Docker-Content-Digest", "")
        if not digest:
            raise RegistryError(f"No Docker-Content-Digest header from {ref.registry}/{ref.repository}")
        return digest

    def get_remote_metadata(self, ref: ImageRef, digest: str) -> tuple[str, float]:
        """
        Return (version, created_ts) for the image the registry currently serves.

        Resolves manifest lists to the configured platform, then reads the OCI
        version/created fields from the image config blob. Results are cached by
        digest, so repeat cycles cost nothing until the image actually changes.
        """
        if digest in self._meta_cache:
            return self._meta_cache[digest]

        manifest: dict[str, Any] = self._request("GET", ref, f"manifests/{ref.tag}", MANIFEST_ACCEPT).json()

        # Multi-arch index: descend into the manifest for our platform
        if "manifests" in manifest:
            os_name, _, arch = self.platform.partition("/")
            child_digest = ""
            for entry in cast(list[dict[str, Any]], manifest["manifests"]):
                platform = cast(dict[str, Any], entry.get("platform", {}))
                if platform.get("os") == os_name and platform.get("architecture") == arch:
                    child_digest = cast(str, entry.get("digest", ""))
                    break
            if not child_digest:
                raise RegistryError(f"No {self.platform} manifest in index for {ref.original}")
            manifest = self._request("GET", ref, f"manifests/{child_digest}", MANIFEST_ACCEPT).json()

        config_digest = cast(str, cast(dict[str, Any], manifest.get("config", {})).get("digest", ""))
        if not config_digest:
            raise RegistryError(f"No config digest in manifest for {ref.original}")

        config: dict[str, Any] = self._request("GET", ref, f"blobs/{config_digest}", "application/json").json()
        labels = cast(dict[str, str], cast(dict[str, Any], config.get("config", {})).get("Labels") or {})
        version = labels.get(VERSION_LABEL, "")
        created = parse_rfc3339(labels.get(CREATED_LABEL) or cast(str, config.get("created", "")))

        meta = (version, created)
        self._meta_cache[digest] = meta
        return meta


@dataclass
class ImageFreshness:
    """Freshness result for one running container."""

    container_name: str
    hostname: str
    image: str
    status: str
    current_version: str = ""
    available_version: str = ""
    current_created: float = 0.0
    available_created: float = 0.0

    @property
    def outdated(self) -> int:
        return 1 if self.status == STATUS_OUTDATED else 0


@dataclass
class RemoteState:
    """Registry-side state for one image reference, shared across hosts."""

    status: str
    digest: str = ""
    version: str = ""
    created: float = 0.0


class FreshnessCollector:
    """
    Periodically joins Portainer's view of running containers with upstream
    registry state and renders the result as Prometheus metrics.
    """

    def __init__(self) -> None:
        self.portainer_url: str = os.getenv("PORTAINER_URL", "http://localhost:9000").rstrip("/")
        self.check_interval: int = int(os.getenv("REGISTRY_CHECK_INTERVAL", "21600"))
        timeout = int(os.getenv("REGISTRY_TIMEOUT", "10"))
        platform = os.getenv("REGISTRY_PLATFORM", "linux/amd64")

        self.session: requests.Session = requests.Session()
        self.session.headers.update({"X-API-Key": os.getenv("PORTAINER_TOKEN", "")})
        self.registry = RegistryClient(timeout=timeout, platform=platform)

        self.results: list[ImageFreshness] = []
        self.last_check: float = 0

    # -- Portainer helpers ------------------------------------------------

    def _get_json(self, path: str) -> Any:
        response = self.session.get(f"{self.portainer_url}{path}", timeout=10)
        response.raise_for_status()
        return response.json()

    def _inspect_image(self, endpoint_id: int, image_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._get_json(f"/api/endpoints/{endpoint_id}/docker/images/{image_id}/json"))

    # -- Registry side -----------------------------------------------------

    def _check_remote(self, image: str, cache: dict[str, RemoteState]) -> RemoteState:
        """Resolve registry state for an image reference, once per cycle across all hosts."""
        if image in cache:
            return cache[image]

        ref = parse_image_ref(image)
        if ref is None:
            state = RemoteState(status=STATUS_PINNED)
        else:
            try:
                digest = self.registry.get_remote_digest(ref)
                version, created = "", 0.0
                try:
                    version, created = self.registry.get_remote_metadata(ref, digest)
                except (RegistryError, requests.RequestException) as e:
                    # Metadata is decoration; the digest comparison already succeeded
                    logger.debug(f"No remote metadata for {image}: {e}")
                state = RemoteState(status=STATUS_OK, digest=digest, version=version, created=created)
            except (RegistryError, requests.RequestException) as e:
                logger.warning(f"Registry check failed for {image}: {e}")
                state = RemoteState(status=STATUS_ERROR)

        cache[image] = state
        return state

    # -- Collection --------------------------------------------------------

    def collect(self) -> None:
        """One full freshness cycle across all online Portainer endpoints."""
        try:
            data: Any = self._get_json("/api/endpoints")
        except requests.RequestException as e:
            logger.error(f"Freshness: failed to fetch endpoints: {e}")
            return

        # Handle both list and paginated responses (mirrors app.py's fetch_endpoints)
        if isinstance(data, dict) and "results" in data:
            data = data["results"]
        if not isinstance(data, list):
            logger.error(f"Freshness: unexpected /api/endpoints payload type: {type(data).__name__}")
            data = []
        endpoints = cast(list[Any], data)

        results: list[ImageFreshness] = []
        remote_cache: dict[str, RemoteState] = {}

        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                logger.debug(f"Freshness: skipping malformed endpoint entry: {endpoint!r}")
                continue
            if endpoint.get("Status") != 1:  # 1=up per Portainer
                continue
            endpoint_id = cast(int, endpoint.get("Id", 0))
            hostname = cast(str, endpoint.get("Name", "unknown")).lower()

            try:
                containers = cast(
                    list[Any],
                    self._get_json(f"/api/endpoints/{endpoint_id}/docker/containers/json"),
                )
            except requests.RequestException as e:
                logger.debug(f"Freshness: skipping endpoint '{hostname}': {e}")
                continue

            inspect_cache: dict[str, dict[str, Any]] = {}
            for container in containers:
                if not isinstance(container, dict):
                    logger.debug(f"Freshness: skipping malformed container entry on '{hostname}': {container!r}")
                    continue
                names = cast(list[str], container.get("Names") or ["unknown"])
                name = names[0].lstrip("/")
                image = cast(str, container.get("Image", "unknown"))
                image_id = cast(str, container.get("ImageID", ""))

                try:
                    if image_id not in inspect_cache:
                        inspect_cache[image_id] = self._inspect_image(endpoint_id, image_id or image)
                    inspected = inspect_cache[image_id]
                except requests.RequestException as e:
                    logger.debug(f"Freshness: cannot inspect {image} on '{hostname}': {e}")
                    results.append(ImageFreshness(name, hostname, image, STATUS_ERROR))
                    continue

                labels = cast(dict[str, str], cast(dict[str, Any], inspected.get("Config", {})).get("Labels") or {})
                current_version = labels.get(VERSION_LABEL, "")
                current_created = parse_rfc3339(cast(str, inspected.get("Created", "")))
                repo_digests = {
                    entry.split("@", 1)[1]
                    for entry in cast(list[str], inspected.get("RepoDigests") or [])
                    if "@" in entry
                }

                if "@" in image:
                    status = STATUS_PINNED
                    remote = RemoteState(status=STATUS_PINNED)
                elif not repo_digests:
                    status = STATUS_LOCAL
                    remote = RemoteState(status=STATUS_LOCAL)
                else:
                    remote = self._check_remote(image, remote_cache)
                    if remote.status == STATUS_OK:
                        status = STATUS_OK if remote.digest in repo_digests else STATUS_OUTDATED
                    else:
                        status = remote.status

                results.append(
                    ImageFreshness(
                        container_name=name,
                        hostname=hostname,
                        image=image,
                        status=status,
                        current_version=current_version,
                        available_version=remote.version,
                        current_created=current_created,
                        available_created=remote.created,
                    )
                )

        self.results = results
        self.last_check = time.time()
        outdated = sum(r.outdated for r in results)
        logger.info(f"Freshness: checked {len(results)} containers, {outdated} outdated")

    # -- Rendering ----------------------------------------------------------

    def generate_output(self) -> str:
        """Render freshness metrics in Prometheus text format."""
        # Snapshot shared state once: the freshness thread may replace these mid-render
        results = self.results
        last_check = self.last_check
        output: list[str] = []

        output.append("# HELP container_image_outdated Whether the registry serves a newer image for this tag (1=yes)")
        output.append("# TYPE container_image_outdated gauge")
        for r in results:
            labels = f'container_name="{r.container_name}",hostname="{r.hostname}",image="{r.image}"'
            output.append(f"container_image_outdated{{{labels}}} {r.outdated}")

        output.append("")
        output.append(
            "# HELP container_image_info Image freshness detail "
            "(status: ok|outdated|local|pinned|error; versions from OCI labels where published)"
        )
        output.append("# TYPE container_image_info gauge")
        for r in results:
            labels = (
                f'container_name="{r.container_name}",hostname="{r.hostname}",image="{r.image}",'
                f'status="{r.status}",current_version="{r.current_version}",'
                f'available_version="{r.available_version}"'
            )
            output.append(f"container_image_info{{{labels}}} 1")

        output.append("")
        output.append("# HELP container_image_current_created_timestamp Build time of the running image (unix ts)")
        output.append("# TYPE container_image_current_created_timestamp gauge")
        for r in results:
            if r.current_created:
                labels = f'container_name="{r.container_name}",hostname="{r.hostname}",image="{r.image}"'
                output.append(f"container_image_current_created_timestamp{{{labels}}} {int(r.current_created)}")

        output.append("")
        output.append(
            "# HELP container_image_available_created_timestamp Build time of the image the registry serves (unix ts)"
        )
        output.append("# TYPE container_image_available_created_timestamp gauge")
        for r in results:
            if r.available_created:
                labels = f'container_name="{r.container_name}",hostname="{r.hostname}",image="{r.image}"'
                output.append(f"container_image_available_created_timestamp{{{labels}}} {int(r.available_created)}")

        output.append("")
        output.append("# HELP container_image_freshness_last_check_timestamp Unix timestamp of last freshness cycle")
        output.append("# TYPE container_image_freshness_last_check_timestamp gauge")
        output.append(f"container_image_freshness_last_check_timestamp {int(last_check)}")

        return "\n".join(output) + "\n"


def _collect_safely(collector: FreshnessCollector) -> None:
    """Run one freshness cycle, containing any exception so the daemon thread survives."""
    try:
        collector.collect()
    except Exception:
        logger.exception("Freshness cycle failed; will retry next interval")


def run_freshness_thread(collector: FreshnessCollector) -> Thread:
    """Run freshness collection in a background thread on its own (slow) interval."""

    def loop() -> None:
        while True:
            _collect_safely(collector)
            time.sleep(collector.check_interval)

    thread = Thread(target=loop, daemon=True)
    thread.start()
    return thread
