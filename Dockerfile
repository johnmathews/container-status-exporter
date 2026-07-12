FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

WORKDIR /app

# Install dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy application
COPY app.py freshness.py .

# Create non-root user for security
RUN useradd -m -u 1000 exporter && chown -R exporter:exporter /app
USER exporter

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8081/health')"

# Expose metrics port
EXPOSE 8081

# Run the exporter directly from the build-time venv (no uv sync at startup)
CMD ["/app/.venv/bin/python", "app.py"]
