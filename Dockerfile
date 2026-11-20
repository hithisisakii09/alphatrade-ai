# ==============================================================================
# AlphaTrade AI v2.0 — Production Multi-Tenant Container Image
# ==============================================================================

FROM python:3.12-slim-bookworm AS base

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    TZ=Asia/Kolkata

# Install essential system utilities and timezone data
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    tzdata \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Upgrade pip and install wheel
RUN pip install --no-cache-dir --upgrade pip wheel setuptools

# Copy project specification and install Python dependencies
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir -e .

# Copy application source code, strategies, configuration, and frontend
COPY app/ ./app/
COPY config/ ./config/
COPY dashboard/ ./dashboard/

# Create a non-root user for cloud security
RUN useradd -m -u 1000 alphatrade && \
    mkdir -p /app/.cache /app/data && \
    chown -R alphatrade:alphatrade /app

USER alphatrade

# Expose web service port
EXPOSE 8080

# Production Health Check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Default entrypoint starts the FastAPI production server
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
