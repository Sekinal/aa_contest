# syntax=docker/dockerfile:1

# =============================================================================
# Stage 1: Builder - Install dependencies
# =============================================================================
FROM python:3.12-slim AS builder

ARG TARGETPLATFORM
ARG BUILDPLATFORM

LABEL maintainer="your-email@example.com"
LABEL org.opencontainers.image.source="https://github.com/thermostatic/aa-scraper"
LABEL org.opencontainers.image.description="Production-ready American Airlines flight scraper"
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=100

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY pyproject.toml ./

RUN pip install --upgrade pip setuptools wheel && \
    pip install -e .

# =============================================================================
# Stage 2: Development (OPTIONAL - moved before runtime)
# =============================================================================
FROM python:3.12-slim AS development

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_HOME=/app \
    COOKIES_DIR=/app/cookies \
    OUTPUT_DIR=/app/output \
    LOGS_DIR=/app/logs

RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
    libasound2 libatspi2.0-0 libxshmfence1 ca-certificates \
    fonts-liberation fonts-noto-color-emoji tzdata \
    vim less procps \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

RUN groupadd -r scraper --gid=1000 && \
    useradd -r -g scraper --uid=1000 --home-dir=/app --shell=/bin/bash scraper

RUN mkdir -p ${APP_HOME} ${COOKIES_DIR} ${OUTPUT_DIR} ${LOGS_DIR} && \
    chown -R scraper:scraper ${APP_HOME}

USER scraper
WORKDIR ${APP_HOME}

COPY --chown=scraper:scraper aa_scraper ./aa_scraper
COPY --chown=scraper:scraper pyproject.toml ./

USER root
RUN pip install --no-cache-dir \
    pytest pytest-asyncio pytest-cov black ruff mypy ipython
USER scraper

RUN python -m playwright install firefox && \
    python -m playwright install-deps firefox 2>/dev/null || true

VOLUME ["${COOKIES_DIR}", "${OUTPUT_DIR}", "${LOGS_DIR}"]

# Dev entrypoint
ENTRYPOINT ["/bin/bash"]
CMD []

# =============================================================================
# Stage 3: Runtime (Production) - LAST STAGE = DEFAULT BUILD
# =============================================================================
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    APP_HOME=/app \
    COOKIES_DIR=/app/cookies \
    OUTPUT_DIR=/app/output \
    LOGS_DIR=/app/logs

RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
    libasound2 libatspi2.0-0 libxshmfence1 ca-certificates \
    fonts-liberation fonts-noto-color-emoji tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

RUN groupadd -r scraper --gid=1000 && \
    useradd -r -g scraper --uid=1000 --home-dir=/app --shell=/bin/bash scraper

RUN mkdir -p ${APP_HOME} ${COOKIES_DIR} ${OUTPUT_DIR} ${LOGS_DIR} && \
    chown -R scraper:scraper ${APP_HOME}

USER scraper
WORKDIR ${APP_HOME}

COPY --chown=scraper:scraper aa_scraper ./aa_scraper
COPY --chown=scraper:scraper pyproject.toml ./

RUN python -m playwright install firefox && \
    python -m playwright install-deps firefox 2>/dev/null || true

VOLUME ["${COOKIES_DIR}", "${OUTPUT_DIR}", "${LOGS_DIR}"]

# Production entrypoint - THIS IS WHAT WE WANT!
ENTRYPOINT ["python", "-m", "aa_scraper"]
CMD ["--help"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import aa_scraper; print('OK')" || exit 1