#!/usr/bin/env python3
"""
Helper script to extract cookies from curl command
Usage: python extract_cookies.py < curl_command.txt > cookies.json
"""

import sys
import json
import re


def parse_curl_cookies(curl_command: str) -> dict:
    """Extract cookies from curl command"""
    cookies = {}
    
    # Find the Cookie header
    cookie_match = re.search(r"-H\s+'Cookie:\s*([^']+)'", curl_command)
    if not cookie_match:
        cookie_match = re.search(r'-H\s+"Cookie:\s*([^"]+)"', curl_command)
    
    if not cookie_match:
        print("Error: No Cookie header found in curl command", file=sys.stderr)
        return cookies
    
    cookie_string = cookie_match.group(1)
    
    # Parse cookie string
    for item in cookie_string.split(';'):
        item = item.strip()
        if '=' in item:
            key, value = item.split('=', 1)
            cookies[key.strip()] = value.strip()
    
    return cookies


def main():
    # Read curl command from stdin
    curl_command = sys.stdin.read()
    
    # Extract cookies
    cookies = parse_curl_cookies(curl_command)
    
    if not cookies:
        print("Error: Failed to extract cookies", file=sys.stderr)
        sys.exit(1)
    
    # Output as JSON
    print(json.dumps(cookies, indent=2))
    
    # Print summary to stderr
    print(f"\n✓ Extracted {len(cookies)} cookies", file=sys.stderr)
    print(f"Critical cookies found:", file=sys.stderr)
    critical = ["XSRF-TOKEN", "JSESSIONID", "_abck", "bm_sv"]
    for key in critical:
        status = "✓" if key in cookies else "✗"
        print(f"  {status} {key}", file=sys.stderr)


if __name__ == "__main__":
    main()
