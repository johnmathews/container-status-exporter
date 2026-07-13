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

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
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
# Standard OCI annotation naming the base image a local build was built FROM.
# Locally-built images that carry it get base-image freshness tracking.
BASE_NAME_LABEL = "org.opencontainers.image.base.name"

# Freshness status values (exposed as the `status` label on container_image_info)
STATUS_OK = "ok"  # running digest matches the registry
STATUS_OUTDATED = "outdated"  # registry serves a different digest for this tag
STATUS_LOCAL = "local"  # locally-built image, no RepoDigests to compare
STATUS_PINNED = "pinned"  # image pinned by digest (name@sha256:...), never outdated
STATUS_ERROR = "error"  # registry check failed (dead repo, network, auth)

# Anonymous pull tokens are cached this long (spec minimum; Hub issues ~300s tokens)
TOKEN_TTL_SECONDS = 60.0
# Backoff before the single retry of a transient digest-HEAD failure
HEAD_RETRY_BACKOFF_SECONDS = 2.0
# Cap on any HTTP response body we read (manifests, token JSON, config blobs,
# Portainer payloads). Real manifests/configs are tens of KiB; 4 MiB is generous
# headroom while keeping a hostile or compromised server from OOMing the exporter.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
# Portainer reports Status 1 for endpoints it can reach ("up"), 2 for down
PORTAINER_ENDPOINT_UP = 1
# Timeout for Portainer API calls (seconds); registry calls use REGISTRY_TIMEOUT
PORTAINER_TIMEOUT_SECONDS = 10


class RegistryError(Exception):
    """Raised when a registry request fails."""


class RegistryRateLimited(RegistryError):  # noqa: N818 -- condition, not error: image is fine, registry is busy
    """Raised on HTTP 429: the registry is rate limiting us, the image is not broken."""


@dataclass
class ImageRef:
    """A parsed image reference."""

    registry: str  # API host, e.g. registry-1.docker.io or ghcr.io
    repository: str  # e.g. library/nginx or immich-app/immich-server
    tag: str  # e.g. latest
    original: str  # the reference as docker reported it


# A bare image ID (canonical `sha256:<hex>`, a truncated `sha256:` form, or a naked
# 64-hex digest) names no repository at all, so there is nothing to ask a registry.
_IMAGE_ID_RE = re.compile(r"^(sha256:[0-9a-fA-F]+|[0-9a-fA-F]{64})$")


def is_image_id(image: str) -> bool:
    """True when a container's Image field is a bare image ID rather than a repo reference."""
    return bool(_IMAGE_ID_RE.match(image))


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
        parsed = datetime.fromisoformat(cleaned)
        if parsed.tzinfo is None:
            # Registry/docker timestamps are UTC; never interpret them in the host's local TZ
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    except ValueError:
        logger.debug(f"Could not parse timestamp: {value}")
        return 0.0


def escape_label_value(value: str) -> str:
    r"""Escape a Prometheus label value per the text exposition format spec.

    Backslash, double-quote and newline become \\, \" and \n. Clean values
    pass through unchanged, so escaping is an identity transform for the
    healthy path.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _read_bounded(response: requests.Response, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    """
    Read a (streamed) response body incrementally, capped at `limit` bytes.

    An honest oversized Content-Length is rejected before reading anything;
    otherwise the body is consumed in chunks and abandoned the moment it
    exceeds the cap, so a hostile server cannot OOM the exporter. Raises
    RegistryError when the cap is exceeded.
    """
    declared = response.headers.get("Content-Length", "")
    if declared.isdigit() and int(declared) > limit:
        response.close()
        raise RegistryError(f"Response body too large: Content-Length {declared} exceeds {limit} bytes")
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=65536):
        total += len(chunk)
        if total > limit:
            response.close()
            raise RegistryError(f"Response body too large: exceeds {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _read_json_bounded(response: requests.Response, limit: int = MAX_RESPONSE_BYTES) -> Any:
    """Parse a JSON body read via _read_bounded; RegistryError on oversize or malformed JSON."""
    body = _read_bounded(response, limit)
    try:
        return json.loads(body)
    except ValueError as e:
        raise RegistryError(f"Invalid JSON response: {e}") from e


class RegistryClient:
    """Anonymous client for OCI-distribution registries."""

    def __init__(self, timeout: int = 10, platform: str = "linux/amd64") -> None:
        self.timeout = timeout
        self.platform = platform
        self.session: requests.Session = requests.Session()
        # version/created metadata never changes for a given digest
        self._meta_cache: dict[str, tuple[str, float]] = {}
        # anonymous pull tokens: (registry, repository) -> (token, fetched_at unix ts)
        self.token_ttl: float = TOKEN_TTL_SECONDS
        self._token_cache: dict[tuple[str, str], tuple[str, float]] = {}

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
            response = self.session.get(realm, params=query, timeout=self.timeout, stream=True)
            if response.status_code == 429:
                raise RegistryRateLimited(f"Token fetch from {realm} -> HTTP 429 (rate limited)")
            response.raise_for_status()
            data: dict[str, Any] = _read_json_bounded(response)
            token = data.get("token") or data.get("access_token")
            return cast(str | None, token)
        except requests.RequestException as e:
            logger.debug(f"Token fetch from {realm} failed: {e}")
            return None

    def _request(self, method: str, ref: ImageRef, path: str, accept: str) -> requests.Response:
        """
        Perform a registry request, transparently handling the 401 token dance.

        Anonymous tokens are cached per (registry, repository) for a short TTL;
        a 401 while presenting a cached token invalidates it and re-fetches once
        (the same single-retry semantics as the uncached dance). HTTP 429 raises
        RegistryRateLimited so callers can degrade instead of reporting errors.
        """
        # https only, deliberately: plain-http registries (e.g. localhost:5000)
        # are unsupported -- nothing in the fleet uses one.
        url = f"https://{ref.registry}/v2/{ref.repository}/{path}"
        headers = {"Accept": accept}
        key = (ref.registry, ref.repository)
        cached = self._token_cache.get(key)
        if cached is not None and time.time() - cached[1] < self.token_ttl:
            headers["Authorization"] = f"Bearer {cached[0]}"
        else:
            self._token_cache.pop(key, None)
        # stream=True so callers read bodies through _read_bounded (HEADs have no body)
        response = self.session.request(method, url, headers=headers, timeout=self.timeout, stream=True)
        if response.status_code == 401:
            self._token_cache.pop(key, None)  # cached token stale/revoked
            challenge = response.headers.get("WWW-Authenticate", "")
            token = self._fetch_token(challenge, ref.repository)
            if token:
                self._token_cache[key] = (token, time.time())
                headers["Authorization"] = f"Bearer {token}"
                response = self.session.request(method, url, headers=headers, timeout=self.timeout, stream=True)
        if response.status_code == 429:
            raise RegistryRateLimited(f"{method} {url} -> HTTP 429 (rate limited)")
        if response.status_code != 200:
            raise RegistryError(f"{method} {url} -> HTTP {response.status_code}")
        return response

    def get_remote_digest(self, ref: ImageRef) -> str:
        """
        HEAD the manifest for ref's tag and return its content digest.

        Retries once after a short backoff on transient transport errors: the
        HEAD is cheap, idempotent and exempt from Docker Hub pull-rate limits.
        """
        try:
            response = self._request("HEAD", ref, f"manifests/{ref.tag}", MANIFEST_ACCEPT)
        except requests.RequestException as e:
            logger.debug(f"Transient error HEADing {ref.original}, retrying once: {e}")
            time.sleep(HEAD_RETRY_BACKOFF_SECONDS)
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

        # GET the digest we just compared, not the tag: a tag re-pushed between the
        # HEAD and this GET would cache the wrong metadata under this digest forever.
        manifest: dict[str, Any] = _read_json_bounded(self._request("GET", ref, f"manifests/{digest}", MANIFEST_ACCEPT))

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
            manifest = _read_json_bounded(self._request("GET", ref, f"manifests/{child_digest}", MANIFEST_ACCEPT))

        config_digest = cast(str, cast(dict[str, Any], manifest.get("config", {})).get("digest", ""))
        if not config_digest:
            raise RegistryError(f"No config digest in manifest for {ref.original}")

        config: dict[str, Any] = _read_json_bounded(
            self._request("GET", ref, f"blobs/{config_digest}", "application/json")
        )
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
    # For local builds tracked via the OCI base-image annotation: the base ref
    # the freshness verdict actually refers to (empty otherwise)
    base_image: str = ""

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
    # 429 sentinel: consumers carry forward the previous result instead of using status.
    # status stays STATUS_ERROR as a fail-safe for any path that ignores the flag.
    rate_limited: bool = False


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
        response = self.session.get(f"{self.portainer_url}{path}", timeout=PORTAINER_TIMEOUT_SECONDS, stream=True)
        response.raise_for_status()
        try:
            return _read_json_bounded(response)
        except RegistryError as e:
            # Portainer is semi-trusted, but the cap is free. Surface oversize/garbage
            # as the request failure every _get_json caller already degrades on.
            raise requests.RequestException(f"Portainer response for {path}: {e}") from e

    def _inspect_image(self, endpoint_id: int, image_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._get_json(f"/api/endpoints/{endpoint_id}/docker/images/{image_id}/json"))

    # -- Registry side -----------------------------------------------------

    def _check_remote(self, image: str, cache: dict[str, RemoteState]) -> RemoteState:
        """Resolve registry state for an image reference, once per cycle across all hosts."""
        if image in cache:
            return cache[image]

        ref = parse_image_ref(image)
        if ref is None:
            # collect() filters digest-pinned refs before calling us; if one leaks
            # through anyway, fail safe as an error rather than claim it is pinned.
            logger.warning(f"Freshness: unparseable image reference reached _check_remote: {image}")
            state = RemoteState(status=STATUS_ERROR)
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
            except RegistryRateLimited as e:
                # Not a broken image: flag it so collect() degrades gracefully
                # (one WARNING per cycle there, not one per image here)
                logger.debug(f"Registry rate-limited for {image}: {e}")
                state = RemoteState(status=STATUS_ERROR, rate_limited=True)
            except (RegistryError, requests.RequestException) as e:
                logger.warning(f"Registry check failed for {image}: {e}")
                state = RemoteState(status=STATUS_ERROR)

        cache[image] = state
        return state

    def _check_base(
        self,
        endpoint_id: int,
        base_name: str,
        inspect_cache: dict[str, dict[str, Any]],
        remote_cache: dict[str, RemoteState],
    ) -> tuple[RemoteState, set[str]] | None:
        """
        Resolve base-image freshness for a locally-built image.

        Returns (registry state for the base ref, RepoDigests of the base as it
        exists locally on the endpoint), or None when the base cannot be tracked
        (digest-pinned annotation, base image not present locally, or the local
        base has no RepoDigests). Comparing the LOCAL base tag against the
        registry assumes pull and rebuild happen together (make <app>-upgrade
        does); a pull without a rebuild reports ok until the rebuild lands.
        """
        if "@" in base_name:
            logger.debug(f"Freshness: base annotation is digest-pinned, nothing to track: {base_name}")
            return None
        try:
            if base_name not in inspect_cache:
                inspect_cache[base_name] = self._inspect_image(endpoint_id, base_name)
            base_inspected = inspect_cache[base_name]
        except requests.RequestException as e:
            logger.debug(f"Freshness: base image {base_name} not inspectable on endpoint {endpoint_id}: {e}")
            return None
        base_repo_digests = {
            entry.split("@", 1)[1]
            for entry in cast(list[str], base_inspected.get("RepoDigests") or [])
            if "@" in entry
        }
        if not base_repo_digests:
            logger.debug(f"Freshness: base image {base_name} has no RepoDigests, cannot compare")
            return None
        return self._check_remote(base_name, remote_cache), base_repo_digests

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
        # Previous cycle's results: carried forward for images the registry rate-limits
        previous = {(r.hostname, r.container_name, r.image): r for r in self.results}
        rate_limited_count = 0

        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                logger.debug(f"Freshness: skipping malformed endpoint entry: {endpoint!r}")
                continue
            if endpoint.get("Status") != PORTAINER_ENDPOINT_UP:
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

                base_image = ""
                if "@" in image:
                    status = STATUS_PINNED
                    remote = RemoteState(status=STATUS_PINNED)
                elif is_image_id(image) or not repo_digests:
                    # Bare image IDs and local builds have nothing upstream that
                    # matches the image itself -- but a build that names its base
                    # via the OCI annotation is checked against the base's tag.
                    base_name = labels.get(BASE_NAME_LABEL, "")
                    base = (
                        self._check_base(endpoint_id, base_name, inspect_cache, remote_cache) if base_name else None
                    )
                    if base is None:
                        status = STATUS_LOCAL
                        remote = RemoteState(status=STATUS_LOCAL)
                    else:
                        remote, base_repo_digests = base
                        base_image = base_name
                        if remote.rate_limited:
                            rate_limited_count += 1
                            carried = previous.get((hostname, name, image))
                            if carried is not None:
                                results.append(carried)
                            continue
                        if remote.status == STATUS_OK:
                            status = STATUS_OK if remote.digest in base_repo_digests else STATUS_OUTDATED
                        else:
                            status = remote.status
                else:
                    remote = self._check_remote(image, remote_cache)
                    if remote.rate_limited:
                        # Rate limiting says nothing about the image: keep last
                        # cycle's verdict if we have one, otherwise omit it.
                        rate_limited_count += 1
                        carried = previous.get((hostname, name, image))
                        if carried is not None:
                            results.append(carried)
                        continue
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
                        base_image=base_image,
                    )
                )

        if rate_limited_count:
            logger.warning(
                f"Freshness: registry rate limit (HTTP 429) affected {rate_limited_count} container(s) "
                "this cycle; previous results carried forward where available"
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
            labels = (
                f'container_name="{escape_label_value(r.container_name)}",'
                f'hostname="{escape_label_value(r.hostname)}",image="{escape_label_value(r.image)}"'
            )
            output.append(f"container_image_outdated{{{labels}}} {r.outdated}")

        output.append("")
        output.append(
            "# HELP container_image_info Image freshness detail (status: ok|outdated|local|pinned|error; "
            "versions from OCI labels; base_image set for local builds tracked via the OCI base annotation)"
        )
        output.append("# TYPE container_image_info gauge")
        for r in results:
            labels = (
                f'container_name="{escape_label_value(r.container_name)}",'
                f'hostname="{escape_label_value(r.hostname)}",image="{escape_label_value(r.image)}",'
                f'status="{escape_label_value(r.status)}",'
                f'current_version="{escape_label_value(r.current_version)}",'
                f'available_version="{escape_label_value(r.available_version)}",'
                f'base_image="{escape_label_value(r.base_image)}"'
            )
            output.append(f"container_image_info{{{labels}}} 1")

        output.append("")
        output.append("# HELP container_image_current_created_timestamp Build time of the running image (unix ts)")
        output.append("# TYPE container_image_current_created_timestamp gauge")
        for r in results:
            if r.current_created:
                labels = (
                    f'container_name="{escape_label_value(r.container_name)}",'
                    f'hostname="{escape_label_value(r.hostname)}",image="{escape_label_value(r.image)}"'
                )
                output.append(f"container_image_current_created_timestamp{{{labels}}} {int(r.current_created)}")

        output.append("")
        output.append(
            "# HELP container_image_available_created_timestamp Build time of the image the registry serves (unix ts)"
        )
        output.append("# TYPE container_image_available_created_timestamp gauge")
        for r in results:
            if r.available_created:
                labels = (
                    f'container_name="{escape_label_value(r.container_name)}",'
                    f'hostname="{escape_label_value(r.hostname)}",image="{escape_label_value(r.image)}"'
                )
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
