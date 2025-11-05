#!/usr/bin/env python3
import json

def read_json_file(filepath):
    """Read and parse a JSON file."""
    with open(filepath, 'r') as f:
        return json.load(f)

def read_text_file(filepath):
    """Read a text file."""
    with open(filepath, 'r') as f:
        return f.read().strip()

def cookies_to_string(cookies_dict):
    """Convert cookies dictionary to cookie header string."""
    return '; '.join([f"{key}={value}" for key, value in cookies_dict.items()])

def build_curl_command(url, cookies, headers, data=None):
    """Build a curl command from cookies, headers, and optional data."""
    
    # Convert cookies to string
    cookie_string = cookies_to_string(cookies)
    
    # Start building curl command
    lines = [
        f"curl '{url}' \\",
        "  --compressed \\",
        "  -X POST \\",
    ]
    
    # Add headers
    for key, value in headers.items():
        # Escape single quotes in value
        escaped_value = str(value).replace("'", "'\\''")
        lines.append(f"  -H '{key}: {escaped_value}' \\")
    
    # Add cookie header
    escaped_cookies = cookie_string.replace("'", "'\\''")
    lines.append(f"  -H 'Cookie: {escaped_cookies}' \\")
    
    # Add data if provided
    if data:
        escaped_data = data.replace("'", "'\\''")
        lines.append(f"  --data-raw '{escaped_data}'")
    else:
        # Remove trailing backslash from last line
        lines[-1] = lines[-1].rstrip(' \\')
    
    return '\n'.join(lines)

def main():
    # Read input files
    print("Reading files...")
    cookies = read_json_file('cookies/aa_cookies.json')
    headers = read_json_file('cookies/aa_cookies_headers.json')
    referer = read_text_file('cookies/aa_cookies_referer.txt')
    
    # Update referer in headers if not present or override
    headers['referer'] = referer
    
    # API endpoint
    url = 'https://www.aa.com/booking/api/search/itinerary'
    
    # Example data payload
    data = {
        "metadata": {
            "selectedProducts": [],
            "tripType": "OneWay",
            "udo": {"search_method": "Lowest"}
        },
        "passengers": [{"type": "adult", "count": 1}],
        "requestHeader": {"clientId": "AAcom"},
        "slices": [{
            "allCarriers": True,
            "cabin": "",
            "departureDate": "2025-12-15",
            "destination": "JFK",
            "destinationNearbyAirports": False,
            "maxStops": None,
            "origin": "LAX",
            "originNearbyAirports": False
        }],
        "tripOptions": {
            "corporateBooking": False,
            "fareType": "Lowest",
            "locale": "en_US",
            "pointOfSale": None,
            "searchType": "Revenue"
        },
        "loyaltyInfo": None,
        "version": "cfr",
        "queryParams": {
            "sliceIndex": 0,
            "sessionId": "",
            "solutionSet": "",
            "solutionId": "",
            "sort": "CARRIER"
        }
    }
    
    # Convert data to JSON string (compact format)
    data_json = json.dumps(data, separators=(',', ':'))
    
    # Build and print curl command
    print("\nGenerated curl command:\n")
    curl_command = build_curl_command(url, cookies, headers, data=data_json)
    print(curl_command)
    
    # Optionally save to file
    with open('curl_command.sh', 'w') as f:
        f.write('#!/bin/bash\n\n')
        f.write(curl_command + '\n')
    print("\n\nCurl command saved to: curl_command.sh")

if __name__ == '__main__':
    main()