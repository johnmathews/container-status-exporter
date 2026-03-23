# 260324 - Conformance Assessment

## Context

Engineering team assessment to bring the project into conformance with global CLAUDE.md standards. The exporter has been running in production successfully; no bugs or urgent issues.

## Changes Made

### Dependency Management: pip -> uv
- Deleted `requirements.txt`, created `pyproject.toml` with all project metadata
- Generated `uv.lock` for reproducible installs
- Updated `Dockerfile` to use uv (copies uv binary from official image, runs `uv sync`)
- Consolidated `pytest.ini`, `.coveragerc`, and `pyrightconfig.json` into `pyproject.toml`

### Linter: Added ruff
- Added ruff configuration to `pyproject.toml` (line-length=120, py311 target)
- Rules: E, F, W, I, N, UP, B, A, SIM (ignoring A002 for BaseHTTPRequestHandler override)
- Fixed import sorting, formatting issues across all files

### Documentation: Consolidated
- Removed 6 overlapping docs files (START_HERE, QUICKSTART, MANUAL_SETUP, DEPLOYMENT, IMPLEMENTATION, SUMMARY)
- Created 2 focused docs: `docs/architecture.md` (how it works, metrics reference) and `docs/deployment.md` (running, configuring, troubleshooting)
- Rewrote `README.md` to be accurate and concise
- Removed all references to Ansible roles and files that don't exist in this repo

### Project Files
- Deleted `AGENTS.md` (outdated — claimed "No test suite" despite 78 tests existing)
- Created `CLAUDE.md` with accurate project guidance (metric stability warnings, dev commands, env var reference)
- Created `/journal` directory (this entry)

### Minor Fixes
- Removed deprecated `version: '3.8'` from `docker-compose.yml`
- Changed Dockerfile healthcheck from `requests` library to stdlib `urllib.request`

## Decisions

- Kept Python 3.11 as target (user preference, production stability)
- Set ruff line-length to 120 rather than 88 — Prometheus HELP strings and metric label lines are inherently long
- Ignored ruff A002 rule — `format` parameter in `log_message` is a BaseHTTPRequestHandler override, can't rename

## Test Results

78 tests passing, 77.42% coverage — unchanged from before modifications.
