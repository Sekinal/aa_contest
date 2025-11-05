"""Data storage and persistence"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from .config import CABIN_CLASS_MAP
from .parser import calculate_cpp


def save_results(
    results: Dict[str, Optional[List[Dict[str, Any]]]],
    raw_responses: Dict[str, Optional[Dict[str, Any]]],
    output_dir: Path,
    origin: str,
    destination: str,
    date: str,
    passengers: int,
    cabin_filter: str,
) -> None:
    """
    Save scraping results to files.

    Args:
        results: Parsed flight results by search type
        raw_responses: Raw API responses
        output_dir: Output directory path
        origin: Origin airport code
        destination: Destination airport code
        date: Departure date
        passengers: Number of passengers
        cabin_filter: Cabin class filter
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw_data"
    raw_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base_filename = f"{origin}_{destination}_{date}_{timestamp}"

    # Save raw responses
    for search_type, raw_data in raw_responses.items():
        if raw_data is not None:
            raw_file = raw_dir / f"{base_filename}_{search_type.lower()}_raw.json"
            raw_file.write_text(json.dumps(raw_data, ensure_ascii=False, indent=2))
            logger.info(f"💾 Saved raw {search_type} response: {raw_file.name}")

    # Merge results
    award_flights = results.get("Award")
    revenue_flights = results.get("Revenue")
    merged_flights = []

    if award_flights and revenue_flights:
        revenue_lookup = {}
        for flight in revenue_flights:
            if flight.get("_product_type") != "COACH":
                continue
            cash_price = flight.get("cash_price_usd", 0.0)
            if cash_price <= 0:
                continue

            segments = flight["segments"]
            if segments:
                dep_time = segments[0]["departure_time"]
                arr_time = segments[-1]["arrival_time"]
                nonstop = flight["is_nonstop"]
                key = (dep_time, arr_time, nonstop)
                revenue_lookup[key] = flight

        logger.info(f"Found {len(revenue_lookup)} valid revenue flights")

        for award_flight in award_flights:
            if award_flight.get("_product_type") != "COACH":
                continue

            segments = award_flight["segments"]
            if segments:
                dep_time = segments[0]["departure_time"]
                arr_time = segments[-1]["arrival_time"]
                nonstop = award_flight["is_nonstop"]
                key = (dep_time, arr_time, nonstop)

                if key in revenue_lookup:
                    revenue_flight = revenue_lookup[key]
                    merged_flight = award_flight.copy()
                    merged_flight["cash_price_usd"] = revenue_flight["cash_price_usd"]

                    if merged_flight["points_required"] > 0:
                        merged_flight["cpp"] = calculate_cpp(
                            revenue_flight["cash_price_usd"],
                            award_flight["taxes_fees_usd"],
                            merged_flight["points_required"],
                        )

                    merged_flight.pop("_product_type", None)
                    merged_flights.append(merged_flight)

    elif not award_flights or not revenue_flights:
        logger.warning("⚠️ Cannot merge - missing Award or Revenue data")

    # Save merged results
    merged_result = {
        "search_metadata": {
            "origin": origin.upper(),
            "destination": destination.upper(),
            "date": date,
            "passengers": passengers,
            "cabin_class": CABIN_CLASS_MAP.get(cabin_filter, cabin_filter.lower()),
        },
        "flights": merged_flights,
        "total_results": len(merged_flights),
    }

    output_file = output_dir / f"{base_filename}_combined.json"
    output_file.write_text(json.dumps(merged_result, ensure_ascii=False, indent=2))

    if merged_flights:
        logger.success(f"💾 Saved {len(merged_flights)} merged flights: {output_file.name}")
    else:
        logger.warning(f"⚠️ Saved empty results: {output_file.name}")