# syntax=docker/dockerfile:1.9

# Build stage - install dependencies with uv
FROM python:3.12-slim AS builder
SHELL ["/bin/sh", "-exc"]

# Install system dependencies needed for compilation
RUN <<EOT
  apt-get update -qy
  apt-get install -qyy \
    -o APT::Install-Recommends=false \
    -o APT::Install-Suggests=false \
    build-essential \
    ca-certificates \
    curl \
    git
  apt-get clean
  rm -rf /var/lib/apt/lists/*
EOT

# Install uv - the modern Python package installer
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Configure uv for Docker
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=python3.12 \
    UV_PROJECT_ENVIRONMENT=/app

# Synchronize dependencies ONLY (not the app yet) for better caching
# This layer only rebuilds when uv.lock or pyproject.toml changes
RUN --mount=type=cache,target=/root/.cache \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

# Now install the application itself
COPY . /src
WORKDIR /src
RUN --mount=type=cache,target=/root/.cache \
    uv sync --locked --no-dev --no-editable

# Runtime stage - lean production image
FROM python:3.12-slim AS runtime
SHELL ["/bin/sh", "-exc"]

# Install runtime dependencies + gosu for user switching
# Playwright/Camoufox needs these
RUN <<EOT
  apt-get update -qy
  apt-get install -qyy \
    -o APT::Install-Recommends=false \
    -o APT::Install-Suggests=false \
    ca-certificates \
    fonts-liberation \
    fonts-noto-color-emoji \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libx11-6 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    libxshmfence1 \
    libgtk-3-0 \
    libgdk-pixbuf-2.0-0 \
    libglib2.0-0 \
    libdbus-glib-1-2 \
    gosu \
    tzdata
  apt-get clean
  rm -rf /var/lib/apt/lists/*
EOT

# Create non-root user
RUN <<EOT
  groupadd -r scraper --gid=1000
  useradd -r -d /app -g scraper --uid=1000 --shell=/bin/bash scraper
EOT

# Set up environment
ENV PATH="/app/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH="/app/.cache/camoufox" \
    PYTHONUNBUFFERED=1 \
    COOKIES_DIR="/app/cookies" \
    OUTPUT_DIR="/app/output" \
    LOGS_DIR="/app/logs"

# Copy entrypoint script
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Copy the virtual environment from builder
COPY --from=builder --chown=scraper:scraper /app /app

# Create necessary directories with proper permissions
RUN mkdir -p /app/cookies /app/output /app/logs /app/.cache && \
    chown -R scraper:scraper /app

# Switch to scraper user for pre-installation
USER scraper
WORKDIR /app

# Pre-install Camoufox browser (Firefox) during build
# This downloads the ~713MB binary at build time, not runtime
RUN python <<PYEOF
from camoufox.sync_api import Camoufox
import os
os.makedirs("/app/.cache/camoufox", exist_ok=True)
print("📦 Downloading Camoufox browser (~713MB)...")
with Camoufox(headless=True) as browser:
    page = browser.new_page()
    page.goto("about:blank")
print("✅ Camoufox installed and verified successfully")
PYEOF

# Verify installation works
RUN python -c "import aa_scraper; print(f'aa-scraper v{aa_scraper.__version__} loaded successfully')"

# Switch back to root for entrypoint (it will switch to scraper)
USER root

# Set up volumes for persistent data
VOLUME ["/app/cookies", "/app/output", "/app/logs"]

# Use entrypoint script that fixes permissions
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "aa_scraper", "--help"]

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import aa_scraper" || exit 1
