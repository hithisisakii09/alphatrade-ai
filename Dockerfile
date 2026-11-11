# AlphaTrade AI v2.0 Production Container
FROM python:3.12-slim

# Environment flags: enforce unbuffered output, IST timezone, and PAPER mode
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Kolkata \
    ALPHATRADE_EXECUTION_MODE=PAPER

# Install system dependencies (curl for healthcheck, tzdata for IST timekeeping)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    tzdata \
    sqlite3 \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# Create non-root system user for production hardening
RUN useradd -u 1000 -m appuser

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code, configuration, scripts, and dashboard
COPY app/ app/
COPY config/ config/
COPY dashboard/ dashboard/
COPY scripts/ scripts/
COPY pyproject.toml .

# Prepare runtime directories for database, session caching, and backups
RUN mkdir -p .cache data backups \
    && chown -R appuser:appuser /app

USER appuser

# Expose FastAPI port
EXPOSE 8000

# Healthcheck targeting fail-closed health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Launch ASGI server
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
