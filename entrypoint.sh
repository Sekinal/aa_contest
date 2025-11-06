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

echo "🚀 AA Scraper starting..."
fix_permissions

# Switch to scraper user and run the app
# If no arguments, show help. Otherwise run with provided args.
if [ $# -eq 0 ]; then
    exec gosu scraper python -m aa_scraper --help
else
    exec gosu scraper python -m aa_scraper "$@"
fi
