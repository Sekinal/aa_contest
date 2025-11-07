#!/bin/bash
set -e

# Fix permissions on mounted volumes at runtime
fix_permissions() {
    for dir in /app/cookies /app/output /app/logs; do
        if [ -d "$dir" ]; then
            echo "🔧 Fixing permissions on $dir..."
            chown -R scraper:scraper "$dir" 2>/dev/null || true
            chmod -R 755 "$dir" 2>/dev/null || true
        fi
    done
}

# Check if proxy file exists (if specified)
check_proxy_file() {
    if [ -n "$1" ]; then
        if [ -f "$1" ]; then
            echo "🌐 Proxy file found: $1"
            # Fix permissions on proxy file
            chown scraper:scraper "$1" 2>/dev/null || true
            chmod 600 "$1" 2>/dev/null || true
        else
            echo "❌ ERROR: Proxy file not found: $1"
            exit 1
        fi
    fi
}

# Extract proxy file path from arguments
extract_proxy_file() {
    for arg in "$@"; do
        case "$arg" in
            --proxy-file=*)
                echo "${arg#--proxy-file=}"
                return 0
                ;;
        esac
    done
}

echo "🚀 AA Scraper starting..."

# Check for proxy file argument
PROXY_FILE=$(extract_proxy_file "$@")
check_proxy_file "$PROXY_FILE"

fix_permissions

# Switch to scraper user and run the app
# If no arguments, show help. Otherwise run with provided args.
if [ $# -eq 0 ]; then
    exec gosu scraper python -m aa_scraper --help
else
    exec gosu scraper python -m aa_scraper "$@"
fi