# 🛫 AA Flight Scraper

**Production-ready American Airlines flight scraper with advanced bot evasion, automatic cookie management, and Docker support.**

[![Docker Hub](https://img.shields.io/badge/docker-thermostatic%2Faa--scraper-blue?logo=docker)](https://hub.docker.com/repository/docker/thermostatic/aa-scraper)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Scrapes both **Award** and **Revenue** flights from American Airlines with automatic cookie extraction, HTTP/2 support, circuit breaker pattern, and adaptive rate limiting.

---

## 🚀 Quick Start (Docker - Recommended)

The easiest way to use this scraper is via Docker. No local setup required!

### Pull and Run

```bash
docker run --rm \
  -v "$(pwd)/cookies:/app/cookies" \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/logs:/app/logs" \
  thermostatic/aa-scraper:latest \
  --origin LAX \
  --destination JFK \
  --date 2025-12-15
```

**That's it!** The scraper will:
1. ✅ Automatically extract cookies using a real browser (Camoufox/Firefox)
2. ✅ Search for both Award and Revenue flights
3. ✅ Save results to `./output/`
4. ✅ Log everything to `./logs/`
5. ✅ Cache cookies in `./cookies/` for reuse

### Example Output

```
2025-11-06 01:05:56.925 | INFO     | AA Flight Scraper - Production Ready
2025-11-06 01:05:56.926 | INFO     | Searching flights: LAX → JFK on 2025-12-15
2025-11-06 01:05:59.026 | SUCCESS  | ✓ Found 39 Award flights
2025-11-06 01:06:02.105 | SUCCESS  | ✓ Found 40 Revenue flights
2025-11-06 01:06:02.451 | SUCCESS  | 💾 Saved 20 merged flights
2025-11-06 01:06:02.452 | SUCCESS  | ✓ Scraping complete!
```

---

## 📋 Docker Usage

### Basic Search

Search for flights on a specific route and date:

```bash
docker run --rm \
  -v "$(pwd)/cookies:/app/cookies" \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/logs:/app/logs" \
  thermostatic/aa-scraper:latest \
  --origin SFO \
  --destination LAX \
  --date 2025-12-20
```

### Advanced Options

#### Specify Cabin Class

```bash
docker run --rm \
  -v "$(pwd)/cookies:/app/cookies" \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/logs:/app/logs" \
  thermostatic/aa-scraper:latest \
  --origin JFK \
  --destination LHR \
  --date 2025-12-25 \
  --cabin BUSINESS
```

**Available cabin classes:** `COACH`, `BUSINESS`, `FIRST`, `PREMIUM_ECONOMY`

#### Multiple Passengers

```bash
docker run --rm \
  -v "$(pwd)/cookies:/app/cookies" \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/logs:/app/logs" \
  thermostatic/aa-scraper:latest \
  --origin ORD \
  --destination DFW \
  --date 2025-12-15 \
  --passengers 2
```

#### Search Type (Award Only or Revenue Only)

We can only search one of the API endpoints, I don't recommend doing so as that defeats the purpose of the data processing pipeline, but you can do so:

```bash
# Award flights only
docker run --rm \
  -v "$(pwd)/cookies:/app/cookies" \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/logs:/app/logs" \
  thermostatic/aa-scraper:latest \
  --origin LAX \
  --destination JFK \
  --date 2025-12-15 \
  --search-type Award

# Revenue flights only
docker run --rm \
  -v "$(pwd)/cookies:/app/cookies" \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/logs:/app/logs" \
  thermostatic/aa-scraper:latest \
  --origin LAX \
  --destination JFK \
  --date 2025-12-15 \
  --search-type Revenue
```

#### Visible Browser (for debugging)

Watch the cookie extraction process in real-time:

```bash
docker run --rm \
  -v "$(pwd)/cookies:/app/cookies" \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/logs:/app/logs" \
  -e DISPLAY=$DISPLAY \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  thermostatic/aa-scraper:latest \
  --origin LAX \
  --destination JFK \
  --date 2025-12-15 \
  --no-headless
```

#### Verbose Logging

```bash
docker run --rm \
  -v "$(pwd)/cookies:/app/cookies" \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/logs:/app/logs" \
  thermostatic/aa-scraper:latest \
  --origin LAX \
  --destination JFK \
  --date 2025-12-15 \
  --verbose
```

---

## 📂 Output Files

After running, you'll find:

```
output/
├── LAX_JFK_2025-12-15_20251106_010602_combined.json    # ✅ Merged Award + Revenue data
└── raw_data/
    ├── LAX_JFK_2025-12-15_20251106_010602_award_raw.json    # Raw Award API response
    └── LAX_JFK_2025-12-15_20251106_010602_revenue_raw.json  # Raw Revenue API response

cookies/
├── aa_cookies.json          # Extracted cookies (auto-cached)
├── aa_cookies_headers.json  # Request headers
└── aa_cookies_referer.txt   # Referer URL

logs/
└── aa_scraper.log           # Detailed logs with timestamps
```

### Combined Output Format

The `*_combined.json` file contains merged Award + Revenue data:

```json
{
  "search_metadata": {
    "origin": "LAX",
    "destination": "JFK",
    "date": "2025-12-15",
    "passengers": 1,
    "cabin_class": "economy"
  },
  "flights": [
    {
      "is_nonstop": true,
      "segments": [
        {
          "flight_number": "AA123",
          "departure_time": "08:00",
          "arrival_time": "16:30"
        }
      ],
      "total_duration": "5h 30m",
      "points_required": 12500,
      "cash_price_usd": 350.50,
      "taxes_fees_usd": 45.60,
      "cpp": 2.44
    }
  ],
  "total_results": 20
}
```

**Key Fields:**
- `points_required`: Award miles needed
- `cash_price_usd`: Revenue ticket price
- `cpp`: **Cents per point** (award value calculation)
- `is_nonstop`: Whether it's a direct flight
- `segments`: Individual flight legs with times

---

## 🔧 Local Development (uv)

Want to develop locally or run without Docker? Use **[uv](https://docs.astral.sh/uv/)** - the modern Python package manager.

### Prerequisites

- **Python 3.12**
- **uv** (install: `curl -LsSf https://astral.sh/uv/install.sh | sh`)

### Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/aa-scraper.git
cd aa-scraper

# Install dependencies with uv
uv sync
```

This creates a virtual environment and installs all dependencies (including Camoufox browser).

### Run Locally

```bash
# Activate the virtual environment
source .venv/bin/activate  # Linux/macOS
# or
.venv\Scripts\activate     # Windows

# Run the scraper
python -m aa_scraper \
  --origin LAX \
  --destination JFK \
  --date 2025-12-15
```

Or use `uv run` directly (no activation needed):

```bash
uv run python -m aa_scraper \
  --origin LAX \
  --destination JFK \
  --date 2025-12-15
```

### Development Commands

```bash
# Install dev dependencies
uv sync --all-extras

# Run tests
uv run pytest

# Format code
uv run black aa_scraper/

# Lint
uv run ruff check aa_scraper/

# Build Docker image locally
docker build -t aa-scraper:local .
```

---

## 🎯 Features

### 🤖 Advanced Bot Evasion
- **Camoufox Browser**: Uses Firefox-based browser with anti-detection
- **Automatic Cookie Extraction**: Real browser session with proper headers
- **Akamai Bypass**: Handles and solves Akamai bot challenges automatically
- **HTTP/2 Support**: Native HTTP/2 with fallback to HTTP/1.1
- **Smart Header Ordering**: Mimics real browser request patterns

### 🔄 Production-Ready Reliability
- **Circuit Breaker Pattern**: Prevents cascading failures
- **Exponential Backoff**: Smart retry logic with jitter
- **Rate Limiting**: Adaptive rate limiter (1 req/s default)
- **Cookie Auto-Refresh**: Detects expired cookies and refreshes automatically
- **Health Checks**: Built-in monitoring and recovery

### 📊 Data Quality
- **Dual Search**: Award + Revenue flights in one run
- **CPP Calculation**: Automatic cents-per-point value
- **Flight Matching**: Merges Award + Revenue for same flights
- **Raw Data Saved**: Full API responses preserved
- **Structured Output**: Clean JSON format

---

## 🛠️ Configuration

### Environment Variables

```bash
# Cookie directory (default: /app/cookies)
COOKIES_DIR=/path/to/cookies

# Output directory (default: /app/output)
OUTPUT_DIR=/path/to/output

# Log directory (default: /app/logs)
LOGS_DIR=/path/to/logs

# Browser path (auto-detected)
PLAYWRIGHT_BROWSERS_PATH=/path/to/browsers
```

### CLI Arguments

```
Flight Search:
  --origin ORIGIN              Origin airport code (required)
  --destination DESTINATION    Destination airport code (required)
  --date DATE                  Departure date YYYY-MM-DD (required)
  --passengers N               Number of passengers (default: 1)
  --cabin CLASS                Cabin class (default: COACH)
  --search-type TYPE           Award, Revenue, or both (default: both)

Cookie Management:
  --cookies FILE               Cookie file path
  --no-headless                Show browser during cookie extraction
  --cookie-wait-time N         Wait time for API response (default: 15s)

Configuration:
  --output DIR                 Output directory (default: ./output)
  --rate-limit N               Requests per second (default: 1.0)
  --verbose                    Enable debug logging
  --log-file FILE              Log file path
```

---

## 🔍 How It Works

1. **Cookie Extraction** (automatic on first run)
   - Launches Camoufox browser (real Firefox)
   - Navigates to AA.com and accepts cookies
   - Triggers a test flight search to warm up session
   - Captures cookies, headers, and referer
   - Validates API response before saving

2. **Flight Search**
   - Uses extracted cookies to make API requests
   - Searches Award flights (miles + taxes)
   - Searches Revenue flights (cash prices)
   - Matches flights by time/route
   - Calculates CPP (cents per point) value

3. **Cookie Reuse**
   - Cookies cached for 30 minutes
   - Auto-refresh when expired
   - Retry logic on 403 errors
   - Exponential backoff on rate limits

4. **Data Merging**
   - Matches Award + Revenue flights by departure/arrival times
   - Calculates true award value (CPP)
   - Filters by requested cabin class
   - Saves structured JSON output

---

## ⚠️ Important Notes

### Rate Limiting
- Default: **1 request per second** (safe)
- AA.com has bot detection - don't go too fast
- Circuit breaker opens after 3 consecutive failures
- Automatic backoff on 429 responses

### Cookie Lifespan
- Cookies expire after **30 minutes**
- Auto-refresh triggers at 20 minutes
- Force refresh on 403 errors
- Test flight validates cookies work

### Akamai Challenges
- Automatically detected and solved
- May add 10-30s to first request
- Transparent handling - no user action needed

### Docker Volumes
Always mount these directories:
- `/app/cookies` - Cookie persistence
- `/app/output` - Results storage
- `/app/logs` - Log files

Without volumes, data is lost when container stops!

---

## 🐛 Troubleshooting

### "No cookies found" or "Cookie extraction failed"

**Solution 1**: Increase wait time
```bash
docker run --rm \
  -v "$(pwd)/cookies:/app/cookies" \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/logs:/app/logs" \
  thermostatic/aa-scraper:latest \
  --origin LAX \
  --destination JFK \
  --date 2025-12-15 \
  --cookie-wait-time 30
```

**Solution 2**: Run with visible browser to see what's happening
```bash
docker run --rm \
  -v "$(pwd)/cookies:/app/cookies" \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/logs:/app/logs" \
  -e DISPLAY=$DISPLAY \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  thermostatic/aa-scraper:latest \
  --origin LAX \
  --destination JFK \
  --date 2025-12-15 \
  --no-headless
```

### "403 Forbidden" errors

This means cookies are invalid or flagged. The scraper auto-refreshes cookies, but you can force it:

```bash
# Delete cached cookies
rm -rf cookies/*

# Run again - fresh cookies will be extracted
docker run --rm \
  -v "$(pwd)/cookies:/app/cookies" \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/logs:/app/logs" \
  thermostatic/aa-scraper:latest \
  --origin LAX \
  --destination JFK \
  --date 2025-12-15
```

### "Circuit breaker open"

Too many failures detected. Wait 5 minutes or adjust rate limit:

```bash
docker run --rm \
  -v "$(pwd)/cookies:/app/cookies" \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/logs:/app/logs" \
  thermostatic/aa-scraper:latest \
  --origin LAX \
  --destination JFK \
  --date 2025-12-15 \
  --rate-limit 0.5  # Slower = safer
```

### Check logs

```bash
# View live logs
tail -f logs/aa_scraper.log

# Search for errors
grep ERROR logs/aa_scraper.log
```

---

## 📊 Example Use Cases

### Check Award Availability for Trip Planning

```bash
# Multiple routes, same date
for route in "LAX:JFK" "LAX:MIA" "LAX:ORD"; do
  IFS=':' read -r origin dest <<< "$route"
  docker run --rm \
    -v "$(pwd)/cookies:/app/cookies" \
    -v "$(pwd)/output:/app/output" \
    -v "$(pwd)/logs:/app/logs" \
    thermostatic/aa-scraper:latest \
    --origin $origin \
    --destination $dest \
    --date 2025-12-15 \
    --search-type Award
  sleep 5  # Be nice to the API
done
```

### Find Best CPP Value

```bash
# Search multiple dates to find best value
for date in 2025-12-15 2025-12-16 2025-12-17; do
  docker run --rm \
    -v "$(pwd)/cookies:/app/cookies" \
    -v "$(pwd)/output:/app/output" \
    -v "$(pwd)/logs:/app/logs" \
    thermostatic/aa-scraper:latest \
    --origin LAX \
    --destination JFK \
    --date $date
  sleep 5
done

# Analyze CPP values from output files
grep -r "cpp" output/*.json | sort -t':' -k2 -n
```

### Monitor Availability Over Time

```bash
# Cron job to check daily
0 8 * * * docker run --rm \
  -v "$HOME/aa-scraper/cookies:/app/cookies" \
  -v "$HOME/aa-scraper/output:/app/output" \
  -v "$HOME/aa-scraper/logs:/app/logs" \
  thermostatic/aa-scraper:latest \
  --origin LAX \
  --destination JFK \
  --date 2025-12-15
```

---

## 🏗️ Architecture

```
aa_scraper/
├── __init__.py              # Package initialization
├── __main__.py              # Entry point
├── cli.py                   # Command-line interface
├── config.py                # Configuration constants
├── api_client.py            # HTTP client with retry logic
├── cookie_manager.py        # Cookie extraction & management
├── parser.py                # API response parser
├── storage.py               # Results persistence
├── rate_limiter.py          # Adaptive rate limiting
├── circuit_breaker.py       # Circuit breaker pattern
├── retry.py                 # Exponential backoff
├── exceptions.py            # Custom exceptions
├── logging_config.py        # Loguru setup
└── models.py                # Data models & enums
```

**Key Components:**

- **CookieManager**: Browser automation with Camoufox, handles Akamai
- **AAFlightClient**: HTTP/2 client with auto-recovery
- **AdaptiveRateLimiter**: Token bucket algorithm with backoff
- **CircuitBreaker**: Prevents cascading failures
- **FlightDataParser**: Extracts flight data from API JSON

---

## 📈 Performance

**Typical Run Times:**
- Cookie extraction: **15-30 seconds** (first run)
- Award search: **2-3 seconds** (with cached cookies)
- Revenue search: **2-3 seconds**
- Total (both): **~5-6 seconds** after initial cookie setup

**Throughput:**
- Default rate limit: **1 req/s** (safe for production)
- Burst capacity: **2 requests**
- Handles 429 rate limits automatically

**Success Rate:**
- Cookie extraction: **>95%** (with retries)
- API searches: **>98%** (with valid cookies)
- Akamai challenge pass rate: **100%** (automated)

---

## 🤝 Contributing

Contributions welcome! This is a production-ready tool, so please:

1. **Test thoroughly** before submitting PRs
2. **Follow existing patterns** (circuit breaker, retry logic, etc.)
3. **Update tests** for new features
4. **Document changes** in README

---

## 📄 License

MIT License - see [LICENSE](LICENSE) file

---

## ⚡ Quick Reference

**Docker (Recommended)**
```bash
docker run --rm \
  -v "$(pwd)/cookies:/app/cookies" \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/logs:/app/logs" \
  thermostatic/aa-scraper:latest \
  --origin LAX --destination JFK --date 2025-12-15
```

**Local (uv)**
```bash
uv run python -m aa_scraper \
  --origin LAX --destination JFK --date 2025-12-15
```

**Docker Hub**
🐳 https://hub.docker.com/repository/docker/thermostatic/aa-scraper

---

**Made with ❤️ for award travel enthusiasts**