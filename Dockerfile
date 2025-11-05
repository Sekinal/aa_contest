# syntax=docker/dockerfile:1

# =============================================================================
# Stage 1: Builder - Install dependencies and build environment
# =============================================================================
FROM python:3.12-slim AS builder

# Set build arguments
ARG TARGETPLATFORM
ARG BUILDPLATFORM

# Metadata labels
LABEL maintainer="your-email@example.com"
LABEL org.opencontainers.image.source="https://github.com/your-repo/aa-scraper"
LABEL org.opencontainers.image.description="Production-ready American Airlines flight scraper"

# Build-time environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=100

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy dependency files
WORKDIR /build
COPY pyproject.toml ./

# Install Python dependencies
RUN pip install --upgrade pip setuptools wheel && \
    pip install -e .

# =============================================================================
# Stage 2: Runtime - Minimal production image
# =============================================================================
FROM python:3.12-slim AS runtime

# Runtime environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Browser automation settings
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    # Application directories
    APP_HOME=/app \
    COOKIES_DIR=/app/cookies \
    OUTPUT_DIR=/app/output \
    LOGS_DIR=/app/logs

# Install runtime dependencies for browser automation
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Camoufox/Playwright dependencies
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libxshmfence1 \
    # Additional utilities
    ca-certificates \
    fonts-liberation \
    fonts-noto-color-emoji \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Create non-root user for security
RUN groupadd -r scraper --gid=1000 && \
    useradd -r -g scraper --uid=1000 --home-dir=/app --shell=/bin/bash scraper

# Create application directory structure
RUN mkdir -p ${APP_HOME} ${COOKIES_DIR} ${OUTPUT_DIR} ${LOGS_DIR} && \
    chown -R scraper:scraper ${APP_HOME}

# Switch to non-root user
USER scraper
WORKDIR ${APP_HOME}

# Copy application code
COPY --chown=scraper:scraper aa_scraper ./aa_scraper
COPY --chown=scraper:scraper pyproject.toml ./

# Install Playwright/Camoufox browsers (as non-root user)
RUN python -m playwright install firefox && \
    python -m playwright install-deps firefox 2>/dev/null || true

# Create volume mount points
VOLUME ["${COOKIES_DIR}", "${OUTPUT_DIR}", "${LOGS_DIR}"]

# Set default command
ENTRYPOINT ["python", "-m", "aa_scraper"]
CMD ["--help"]

# Health check (optional - checks if Python imports work)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import aa_scraper; print('OK')" || exit 1

# =============================================================================
# Stage 3: Development - With additional dev tools
# =============================================================================
FROM runtime AS development

USER root

# Install development dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    vim \
    less \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Install development Python packages
RUN pip install --no-cache-dir \
    pytest \
    pytest-asyncio \
    pytest-cov \
    black \
    ruff \
    mypy \
    ipython

USER scraper

# Override entrypoint for development
ENTRYPOINT ["/bin/bash"]
CMD []