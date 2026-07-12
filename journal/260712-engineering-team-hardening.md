# 1. Engineering-team hardening cycle (W1–W9)

**Date:** 2026-07-12. Full evaluate → plan → develop cycle over the whole repo, one day after
the image-freshness feature (a2678fc) shipped. Run artifacts (evaluation report, improvement
plan) live in `.engineering-team/runs/manual-20260712T000000Z/`.

## 1.1 What the evaluation found

Four parallel reviewers (code quality, tests, security/deployment, docs), findings verified
against code, specs, and the live exporter. Highlights:

1. **`portainer_exporter_up` could never report 0** (reproduced): fetch errors were swallowed
   and then unconditionally wiped, so a Portainer outage showed `up 1` while every container
   series silently vanished. Pre-existing in the mature `app.py`, not the new code.
2. **The freshness thread died permanently on any unexpected exception** — no guard in its
   loop, and it lacked `app.py`'s handling of Portainer's paginated endpoints shape.
3. **Label values were unescaped**: `current_version` comes from upstream-controlled OCI image
   labels; one quote/newline would corrupt the entire scrape (demonstrated empirically).
4. **The prod container re-installed dependencies at every start** (`CMD ["uv","run",…]`),
   including the dev group — negating `--no-dev` and requiring PyPI at boot.
5. **The frozen metric contract was not regression-protected** (mutation-proved): renaming
   `container_image_info` or unwiring freshness from `/metrics` left all 119 tests green.
6. **6 Dependabot alerts** (2 high: urllib3), all fixable in one lockfile upgrade.
7. **docs/deployment.md was stale** — predated freshness, wrong service names, no real
   deploy procedure. (It was also tracked as `DEPLOYMENT.md`, breaking case-sensitive links.)

Investigated and cleared: the live "31/76 outdated incl. this exporter itself" scare was a
transient deploy-vs-CI race (digests re-verified matching); digest-comparison semantics,
session-per-thread discipline, and the token flow all checked out against current OCI/Docker
docs. Expected oddities (booklore 401→error, jellyfin local, valkey pinned, offline dev lxc)
confirmed behaving as designed.

## 1.2 What shipped (nine commits, W1–W9)

1. **W1** — lockfile upgrade: urllib3 2.7.0, requests 2.34.2, idna 3.18, pytest 9.1.1,
   Pygments 2.20.0. Clears all six alerts.
2. **W2** — `tests/test_contract.py` golden-locks the 11-family metric surface (names, label
   keys, HELP/TYPE); real HTTP-boundary tests replace tautological ones. Mutation-verified.
3. **W3** — truthful collector: full Portainer failure now keeps the previous snapshot, sets
   `last_error` (`up` renders 0), freezes `last_update`; per-endpoint failure no longer flips
   `up` fleet-wide; atomic build-then-swap publish; `ThreadingHTTPServer` + 30 s handler
   timeout.
4. **W4** — freshness thread survives any exception (`_collect_safely`); paginated endpoints
   shape handled; metadata failure can no longer flip a successful digest verdict to `error`.
5. **W5** — `escape_label_value` applied at every interpolation site in both renderers;
   healthy-path output proven byte-identical.
6. **W6** — Dockerfile runs `/app/.venv/bin/python` directly (no boot-time sync, dev deps
   genuinely excluded — verified with `--network none`), Python 3.13, uv image pinned to
   0.11, `workflow_dispatch` added to CI.
7. **W7** — registry graceful degradation: HTTP 429 carries the previous cycle's result
   forward instead of marking images `error`; one 2 s-backoff retry on transient HEAD
   failures; anonymous tokens cached per (registry, repo) with 60 s TTL.
8. **W8** — 4 MiB bounded reads on all registry/Portainer responses (OOM surface closed);
   metadata fetched by the already-compared digest (TOCTOU closed); tz-naive timestamps read
   as UTC; bare `sha256:` image IDs classify as `local`, not `error`.
9. **W9** — docs: deployment.md rewritten around the real prod topology (compose service
   `container-health-exporter` vs container `container-status-exporter`, pull:never
   procedure, registry egress + kill switch, token `=`-padding gotcha); CLAUDE.md guardrail
   completed to all 11 metrics + Downstream Consumers section; architecture.md brought to
   the two-module reality.

Wrap-up review (fresh-eyes docs audit + adversarial code review of the branch) added one more
fix: `app.py`'s collector loop got the same malformed-entry guard and thread-level exception
wrapper (`_collect_all_safely`) that W4 gave freshness.py — the reviewer caught that the
freshness fixes had made the *mature* collector the fragile one.

## 1.3 Decisions and non-goals

1. Metric surface untouched throughout — frozen production contract (Prometheus job
   `container-status`, Grafana dashboard `image-freshness`, two alert rules).
2. No `prometheus_client` migration: a 10-line escape helper bought the safety without
   changing the rendering path of a frozen contract.
3. SSRF registry-host allowlist deliberately deferred (LAN-only; anyone who can register a
   hostile image name on a Portainer endpoint is already privileged).
4. No persistent metadata cache: restarts re-spend some Hub budget, but HEADs are exempt so
   steady state is free.
5. 429 degradation works by carry-forward rather than a new status value, keeping the status
   set (ok|outdated|local|pinned|error) frozen.

## 1.4 Numbers

1. Tests: 119 → **194**; coverage 83.7 % → **95.8 %** (app.py 78 → 92 %, freshness.py
   89 → 98.6 %).
2. Dependabot alerts: 6 → 0 (on merge).
3. Nine work-unit commits plus a wrap-up polish commit, each suite-green.

## 1.5 Follow-ups

1. **Deployment to infra is pending an explicit go-ahead** — merge to main auto-builds
   `ghcr.io/johnmathews/container-status-exporter:latest`; deploy per docs/deployment.md.
2. Grafana-side staleness alert on `container_image_freshness_last_check_timestamp` belongs
   in the proxmox-setup repo (the root cause — silent thread death — is fixed regardless).
3. The a2678fc commit message's "24 new tests" is off by two (26); immutable, noted here.
