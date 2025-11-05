# 🛫 American Airlines Flight Scraper

A production-ready, asynchronous flight scraper for American Airlines with advanced bot evasion, automatic recovery, and intelligent rate limiting.

[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](https://www.docker.com/)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

## ✨ Features

- 🚀 **Async/Await Architecture** - High-performance concurrent scraping
- 🛡️ **Advanced Bot Evasion** - Automatic Akamai challenge handling
- 🔄 **Auto Cookie Refresh** - Intelligent cookie management with age tracking
- ⚡ **Circuit Breaker Pattern** - Prevents cascading failures
- 📊 **Adaptive Rate Limiting** - Smart backoff with exponential retry
- 🎯 **Award & Revenue Search** - Compare cash vs points pricing
- 📈 **CPP Calculator** - Automatic cents-per-point calculation
- 📝 **Structured Logging** - Production-ready loguru integration
- 🐳 **Docker Support** - Production-grade containerization

---

## 🚀 Quick Start

### Option 1: Docker (Recommended for Production)

**Prerequisites:**
- Docker Engine 20.10+
- Docker Compose 2.0+ (optional)

#### Build the Image

```bash
# Production build
docker build -t aa-scraper:latest .

# Development build with extra tools
docker build --target development -t aa-scraper:dev .
```

#### Extract Cookies (First Time Setup)

```bash
# With visible browser (recommended for first time)
docker run -it --rm \
  -v $(pwd)/cookies:/app/cookies \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/logs:/app/logs \
  --security-opt seccomp=unconfined \
  aa-scraper:latest \
  --extract-cookies --cookies-only --no-headless
```

#### Search Flights

```bash
# One-way search
docker run -it --rm \
  -v $(pwd)/cookies:/app/cookies \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/logs:/app/logs \
  aa-scraper:latest \
  --origin LAX \
  --destination JFK \
  --date 2025-12-15 \
  --passengers 1 \
  --cabin COACH
```

#### Using Docker Compose

Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  aa-scraper:
    build:
      context: .
      target: runtime
    image: aa-scraper:latest
    container_name: aa-scraper
    volumes:
      - ./cookies:/app/cookies
      - ./output:/app/output
      - ./logs:/app/logs
    security_opt:
      - seccomp=unconfined
    environment:
      - TZ=America/New_York
    # Override command for your search
    command: >
      --origin LAX
      --destination JFK
      --date 2025-12-15
      --passengers 2
      --cabin BUSINESS
      --verbose
```

Run with Docker Compose:

```bash
# Extract cookies
docker-compose run --rm aa-scraper --extract-cookies --cookies-only --no-headless

# Search flights
docker-compose up
```

---

### Option 2: Local Installation

**Prerequisites:**
- Python 3.12+
- pip or uv package manager

```bash
# Using uv (recommended)
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
uv pip install -e .

# Or using pip
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

#### Extract Cookies

```bash
# With visible browser
python -m aa_scraper --extract-cookies --cookies-only --no-headless

# Headless mode (faster)
python -m aa_scraper --extract-cookies --cookies-only
```

#### Search Flights

```bash
# Basic search
python -m aa_scraper \
  --origin LAX \
  --destination JFK \
  --date 2025-12-15 \
  --passengers 1

# Using uv
uv run -m aa_scraper \
  --origin LAX \
  --destination JFK \
  --date 2025-12-15
```

---

## 📖 Usage Examples

### Docker Examples

#### Business Class Search

```bash
docker run -it --rm \
  -v $(pwd)/cookies:/app/cookies \
  -v $(pwd)/output:/app/output \
  aa-scraper:latest \
  --origin JFK \
  --destination LHR \
  --date 2025-12-15 \
  --cabin BUSINESS \
  --passengers 2 \
  --verbose
```

#### Multi-Search with Custom Rate Limiting

```bash
docker run -it --rm \
  -v $(pwd)/cookies:/app/cookies \
  -v $(pwd)/output:/app/output \
  aa-scraper:latest \
  --origin SFO \
  --destination BOS \
  --date 2025-12-20 \
  --search-type Award Revenue \
  --rate-limit 0.5 \
  --verbose
```

#### Interactive Shell (Development)

```bash
docker run -it --rm \
  -v $(pwd):/app \
  -v $(pwd)/cookies:/app/cookies \
  aa-scraper:dev
```

### Local Examples

#### Premium Economy with Custom Output

```bash
python -m aa_scraper \
  --origin ORD \
  --destination LAX \
  --date 2025-12-01 \
  --cabin PREMIUM_ECONOMY \
  --output ./my_results \
  --log-file ./my_logs/scraper.log
```

#### Award-Only Search

```bash
python -m aa_scraper \
  --origin DFW \
  --destination MIA \
  --date 2025-11-25 \
  --search-type Award \
  --verbose
```

---

## 🔧 Configuration

### Environment Variables (Docker)

```bash
# Timezone
TZ=America/New_York

# Custom directories (if not using volumes)
COOKIES_DIR=/custom/cookies
OUTPUT_DIR=/custom/output
LOGS_DIR=/custom/logs
```

### Volume Mounts

| Container Path | Description | Required |
|---------------|-------------|----------|
| `/app/cookies` | Cookie storage | Yes |
| `/app/output` | Search results | Yes |
| `/app/logs` | Application logs | Recommended |

### CLI Options Reference

#### Cookie Management

```
--extract-cookies          Extract fresh cookies before searching
--cookies PATH             Custom cookie file path (default: ./cookies/aa_cookies.json)
--no-headless             Show browser during cookie extraction
--cookies-only            Extract cookies and exit (no search)
--cookie-wait-time SEC    Wait time for cookie extraction (default: 15)
```

#### Flight Search

```
--origin CODE             Origin airport (IATA code)
--destination CODE        Destination airport (IATA code)
--date YYYY-MM-DD         Departure date
--passengers NUM          Number of passengers (default: 1)
--cabin CLASS             COACH | BUSINESS | FIRST | PREMIUM_ECONOMY
--search-type TYPE        Award | Revenue (can specify multiple)
```

#### Output & Logging

```
--output DIR              Output directory (default: ./output)
--rate-limit RATE         Requests per second (default: 1.0)
--verbose                 Enable debug logging
--log-file PATH           Log file path (default: ./logs/aa_scraper.log)
```

---

## 📂 Output Structure

```
output/
├── raw_data/
│   ├── LAX_JFK_2025-12-15_20251105_143050_award_raw.json
│   └── LAX_JFK_2025-12-15_20251105_143050_revenue_raw.json
└── LAX_JFK_2025-12-15_20251105_143050_combined.json

cookies/
├── aa_cookies.json
├── aa_cookies_headers.json
└── aa_cookies_referer.txt

logs/
└── aa_scraper.log
```

---

## 🏗️ Architecture

### Docker Multi-Stage Build

```
┌─────────────────────────────────────┐
│  Stage 1: Builder                   │
│  • Install build dependencies       │
│  • Create virtual environment       │
│  • Install Python packages          │
└─────────────────────────────────────┘
              ↓
┌─────────────────────────────────────┐
│  Stage 2: Runtime (Production)      │
│  • Minimal slim image               │
│  • Copy only venv & app code        │
│  • Non-root user (UID 1000)         │
│  • Browser automation support       │
└─────────────────────────────────────┘
              ↓
┌─────────────────────────────────────┐
│  Stage 3: Development (Optional)    │
│  • Development tools                │
│  • Testing frameworks               │
│  • Interactive shell                │
└─────────────────────────────────────┘
```

### Core Components

- **Cookie Manager** - Auto-refresh with age tracking
- **Circuit Breaker** - Protects against cascading failures
- **Rate Limiter** - Adaptive token bucket algorithm
- **API Client** - HTTP/2 & HTTP/1.1 with auto-fallback
- **Retry Logic** - Exponential backoff with jitter
- **Data Parser** - Structured flight data extraction

---

## 🔒 Security Best Practices

### Docker Security

✅ **Non-root user** - Runs as UID 1000  
✅ **Minimal base image** - python:3.12-slim  
✅ **No secrets in image** - Cookies via volumes  
✅ **Read-only root filesystem** - Can add `--read-only` flag  
✅ **Resource limits** - Add memory/CPU limits  

### Production Deployment

```bash
# With resource limits and security hardening
docker run -it --rm \
  --memory="2g" \
  --cpus="2" \
  --read-only \
  --tmpfs /tmp \
  -v $(pwd)/cookies:/app/cookies \
  -v $(pwd)/output:/app/output:ro \
  -v $(pwd)/logs:/app/logs \
  --security-opt=no-new-privileges \
  --cap-drop=ALL \
  aa-scraper:latest \
  --origin LAX --destination JFK --date 2025-12-15
```

---

## 🐛 Troubleshooting

### Docker Issues

#### **Browser fails to start in headless mode**

```bash
# Add security opt for browser automation
docker run --security-opt seccomp=unconfined ...
```

#### **Permission denied on volume mounts**

```bash
# Fix permissions on host
sudo chown -R 1000:1000 cookies output logs

# Or run with user flag
docker run --user $(id -u):$(id -g) ...
```

#### **Container exits immediately**

```bash
# Check logs
docker logs <container-id>

# Run interactively
docker run -it aa-scraper:latest /bin/bash
```

### Cookie Issues

#### **Cookies expired**

```bash
# Docker: Force refresh
docker run -it --rm \
  -v $(pwd)/cookies:/app/cookies \
  --security-opt seccomp=unconfined \
  aa-scraper:latest \
  --extract-cookies --cookies-only --no-headless

# Local: Force refresh
python -m aa_scraper --extract-cookies --cookies-only
```

#### **Akamai challenge fails**

- Use `--no-headless` to see what's happening
- Increase `--cookie-wait-time` to 30+ seconds
- Check your network/VPN isn't flagged

### Rate Limiting

- Circuit breaker opens after 3 failures (5-minute timeout)
- Decrease `--rate-limit` value (e.g., 0.5)
- Scraper automatically handles rate limits with backoff

---

## 📊 Performance Tips

### Docker Optimization

```bash
# Use BuildKit for faster builds
DOCKER_BUILDKIT=1 docker build -t aa-scraper:latest .

# Multi-platform builds
docker buildx build --platform linux/amd64,linux/arm64 -t aa-scraper:latest .

# Layer caching
docker build --cache-from aa-scraper:latest -t aa-scraper:latest .
```

### Parallel Searches

```bash
# Run multiple containers for different routes
docker run -d --name search1 -v $(pwd)/cookies:/app/cookies aa-scraper:latest --origin LAX --destination JFK --date 2025-12-15
docker run -d --name search2 -v $(pwd)/cookies:/app/cookies aa-scraper:latest --origin SFO --destination BOS --date 2025-12-16
```

---

## 🧪 Development

### Build Development Image

```dockerfile
docker build --target development -t aa-scraper:dev .
```

### Run Tests in Container

```bash
docker run -it --rm \
  -v $(pwd):/app \
  aa-scraper:dev \
  -c "pytest tests/ -v"
```

### Code Quality Checks

```bash
docker run -it --rm -v $(pwd):/app aa-scraper:dev -c "black aa_scraper/"
docker run -it --rm -v $(pwd):/app aa-scraper:dev -c "ruff check aa_scraper/"
docker run -it --rm -v $(pwd):/app aa-scraper:dev -c "mypy aa_scraper/"
```

---

## 📦 CI/CD Integration

### GitHub Actions Example

```yaml
name: Build and Test

on: [push, pull_request]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Build Docker image
        run: docker build -t aa-scraper:test .
      
      - name: Run tests
        run: |
          docker run --rm aa-scraper:test \
            python -c "import aa_scraper; print('Import OK')"
      
      - name: Security scan
        uses: aquasecurity/trivy-action@master
        with:
          image-ref: aa-scraper:test
          format: 'sarif'
          output: 'trivy-results.sarif'
```

---

## 📄 License

MIT License - See [LICENSE](LICENSE) file for details.

## 🤝 Contributing

Contributions welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) first.

## 📧 Support

- **Issues**: [GitHub Issues](https://github.com/your-repo/aa-scraper/issues)
- **Discussions**: [GitHub Discussions](https://github.com/your-repo/aa-scraper/discussions)

---

**Made with ❤️ for travel hackers and points enthusiasts**
```

This production-grade setup includes:

✅ **Multi-stage builds** for minimal image size  
✅ **Non-root user** for security  
✅ **Health checks** for monitoring  
✅ **Volume mounts** for data persistence  
✅ **Environment variables** for configuration  
✅ **Development target** with extra tools  
✅ **Comprehensive documentation** with Docker & local usage  
✅ **Security best practices** (read-only, resource limits, etc.)  
✅ **CI/CD examples** for automation  
✅ **Troubleshooting guide** for common issues  

The Dockerfile follows industry best practices including layer caching optimization, minimal attack surface, proper labeling, and security hardening! 🚀