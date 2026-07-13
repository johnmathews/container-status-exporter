# Base-image freshness for local builds

**Date:** 2026-07-13

Local builds (jellyfin-with-yt-dlp) were a freshness blind spot: no RepoDigests,
status `local`, `outdated` could never fire — and jellyfin was the original
8-months-stale offender that motivated this exporter.

Mechanism: the standard OCI annotation `org.opencontainers.image.base.name`
(a one-line `LABEL` in the build's Dockerfile) names the base. For `local`
containers carrying it, the collector inspects the base image on the same
endpoint (RepoDigests as pulled) and compares against the registry digest for
the base tag, reusing the existing `_check_remote` path (token cache, 429
carry-forward, metadata). `container_image_info` gains a `base_image` label
(contract test amended deliberately); the `outdated` metric fires as normal, so
downstream alerting needed no changes.

Documented caveat: local-base vs registry assumes pull+rebuild are atomic
(`make jelly-upgrade` is); a pull without rebuild reports ok until the rebuild.

8 new tests (202 total). Consumer side: the jellyfin dockerfile in
proxmox-setup gains the LABEL.
