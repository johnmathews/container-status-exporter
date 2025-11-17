FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app.py .

# Create non-root user for security
RUN useradd -m -u 1000 exporter && chown -R exporter:exporter /app
USER exporter

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8081/health', timeout=5)"

# Expose metrics port
EXPOSE 8081

# Run the exporter
CMD ["python", "app.py"]
