# Image freshness metrics: current vs available for every running container

**Date:** 2026-07-12

## Why

Diun (on the infra VM) notifies when a watched tag changes upstream, but nothing
answered "which running containers are actually behind?" — Diun has no knowledge
of deployed state. Jellyfin sat 8 months stale on a `latest` tag without anyone
noticing. This exporter already joins Portainer's fleet view with Prometheus, so
it was the natural home (rather than a fourth exporter repo).

## What

New `freshness.py` + a second collection thread (6h interval):

- Running side: per unique image on each endpoint, one Portainer image-inspect
  call yields `RepoDigests`, the OCI version label, and the build date.
- Registry side: one manifest HEAD per unique image reference fleet-wide gives
  the digest the registry currently serves. Anonymous OCI token flow (parse the
  WWW-Authenticate challenge, fetch a pull-scoped token) covers Docker Hub,
  ghcr, quay, gcr and lscr without credentials. Version/build-date of the
  remote image comes from the config blob, cached by digest.
- Compare: remote digest not in RepoDigests => `outdated`.

Edge cases handled explicitly: locally-built images (`local`), digest-pinned
references (`pinned`), dead/denied repos (`error` — booklore's Docker Hub repo
was exactly this).

## Notes

- HEAD manifest requests don't count against Docker Hub pull-rate limits;
  metadata blobs are cached by digest, so steady-state cycles are ~40 HEADs
  per 6 hours.
- The "single-file application" principle became "two small files" — the
  registry client is a genuinely separate concern with its own test surface
  (26 new tests, 119 total).
