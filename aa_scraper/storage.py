"""Data storage with async I/O - non-blocking streaming"""

import gc
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiofiles
import asyncio
import orjson  # Much faster than json module
from loguru import logger

from .config import CABIN_CLASS_MAP
from .parser import calculate_cpp


class AsyncStreamingStorage:
    """
    Async streaming storage that never blocks the event loop.
    Uses aiofiles for async I/O and orjson for fast serialization.
    """
    
    _initialized_dirs = set()  # ✅ Class-level cache
    
    def __init__(self, output_dir: Path):
        """
        Initialize async streaming storage.
        
        Args:
            output_dir: Base output directory
        """
        self.output_dir = output_dir
        self.raw_dir = output_dir / "raw_data"
        
        # ✅ Only create directories once (thread-safe)
        if output_dir not in AsyncStreamingStorage._initialized_dirs:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            AsyncStreamingStorage._initialized_dirs.add(output_dir)
            logger.debug(f"Created output directories: {output_dir}")
    
    async def save_raw_response(
        self,
        response_data: Dict[str, Any],
        origin: str,
        destination: str,
        date: str,
        search_type: str,
        timestamp: str,
    ) -> Path:
        """
        Save raw API response asynchronously without blocking.
        
        Args:
            response_data: Raw API response
            origin: Origin airport code
            destination: Destination airport code
            date: Departure date
            search_type: Award or Revenue
            timestamp: Timestamp string
        
        Returns:
            Path to saved file
        """
        base_filename = f"{origin}_{destination}_{date}_{timestamp}"
        raw_file = self.raw_dir / f"{base_filename}_{search_type.lower()}_raw.json"
        
        # Serialize to bytes in background (orjson is C-based, very fast)
        # option=orjson.OPT_INDENT_2 for pretty JSON (optional)
        json_bytes = orjson.dumps(
            response_data, 
            option=orjson.OPT_INDENT_2
        )
        
        # Async file write - doesn't block event loop!
        async with aiofiles.open(raw_file, 'wb') as f:
            await f.write(json_bytes)
        
        logger.debug(f"💾 Saved raw {search_type} response: {raw_file.name} ({len(json_bytes)/1024:.1f}KB)")
        
        # Clear reference for GC
        del response_data
        del json_bytes
        
        return raw_file
    
    async def save_combined_results(
        self,
        award_flights: Optional[List[Dict[str, Any]]],
        revenue_flights: Optional[List[Dict[str, Any]]],
        origin: str,
        destination: str,
        date: str,
        passengers: int,
        cabin_filter: str,
        timestamp: str,
    ) -> Tuple[Path, int]:
        """
        Merge and save combined results asynchronously.
        
        Args:
            award_flights: Parsed award flights
            revenue_flights: Parsed revenue flights
            origin: Origin airport code
            destination: Destination airport code
            date: Departure date
            passengers: Number of passengers
            cabin_filter: Cabin class filter
            timestamp: Timestamp string
        
        Returns:
            Tuple of (output_file_path, num_flights)
        """
        base_filename = f"{origin}_{destination}_{date}_{timestamp}"
        output_file = self.output_dir / f"{base_filename}_combined.json"
        
        # Build revenue lookup (memory efficient)
        revenue_lookup = {}
        if revenue_flights:
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
                    
                    # Only store what we need
                    revenue_lookup[key] = {
                        "cash_price_usd": cash_price,
                        "taxes_fees_usd": flight.get("taxes_fees_usd", 0.0),
                    }
            
            logger.debug(f"Built revenue lookup: {len(revenue_lookup)} flights")
            
            # Free memory
            del revenue_flights
            gc.collect()
        
        # Merge flights
        merged_count = 0
        merged_flights = []
        
        if award_flights:
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
                        revenue_data = revenue_lookup[key]
                        
                        # Create merged flight
                        merged_flight = {
                            "is_nonstop": award_flight["is_nonstop"],
                            "segments": award_flight["segments"],
                            "total_duration": award_flight["total_duration"],
                            "points_required": award_flight["points_required"],
                            "cash_price_usd": revenue_data["cash_price_usd"],
                            "taxes_fees_usd": award_flight["taxes_fees_usd"],
                        }
                        
                        # Calculate CPP
                        if merged_flight["points_required"] > 0:
                            merged_flight["cpp"] = calculate_cpp(
                                revenue_data["cash_price_usd"],
                                award_flight["taxes_fees_usd"],
                                merged_flight["points_required"],
                            )
                        else:
                            merged_flight["cpp"] = 0.0
                        
                        merged_flights.append(merged_flight)
                        merged_count += 1
            
            # Free memory
            del award_flights
            gc.collect()
        
        # Create final result
        result = {
            "search_metadata": {
                "origin": origin.upper(),
                "destination": destination.upper(),
                "date": date,
                "passengers": passengers,
                "cabin_class": CABIN_CLASS_MAP.get(cabin_filter, cabin_filter.lower()),
            },
            "flights": merged_flights,
            "total_results": merged_count,
        }
        
        # Serialize with orjson (fast!)
        json_bytes = orjson.dumps(result, option=orjson.OPT_INDENT_2)
        
        # Async write - no blocking!
        async with aiofiles.open(output_file, 'wb') as f:
            await f.write(json_bytes)
        
        if merged_count > 0:
            logger.success(f"💾 Saved {merged_count} merged flights: {output_file.name} ({len(json_bytes)/1024:.1f}KB)")
        else:
            logger.warning(f"⚠️ Saved empty results: {output_file.name}")
        
        # Cleanup
        del merged_flights
        del revenue_lookup
        del result
        del json_bytes
        gc.collect()
        
        return output_file, merged_count


async def save_results_streaming(
    results: Dict[str, Optional[List[Dict[str, Any]]]],
    raw_responses: Dict[str, Optional[Dict[str, Any]]],
    output_dir: Path,
    origin: str,
    destination: str,
    date: str,
    passengers: int,
    cabin_filter: str,
) -> Tuple[Path, int]:
    """
    Save scraping results using async streaming storage.
    Completely non-blocking - uses async I/O throughout.
    
    Args:
        results: Parsed flight results by search type
        raw_responses: Raw API responses  
        output_dir: Output directory path
        origin: Origin airport code
        destination: Destination airport code
        date: Departure date
        passengers: Number of passengers
        cabin_filter: Cabin class filter
    
    Returns:
        Tuple of (output_file_path, num_flights)
    """
    storage = AsyncStreamingStorage(output_dir)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    
    # Save raw responses asynchronously (in parallel!)
    raw_save_tasks = []
    for search_type, raw_data in raw_responses.items():
        if raw_data is not None:
            task = storage.save_raw_response(
                raw_data, origin, destination, date, search_type, timestamp
            )
            raw_save_tasks.append(task)
            # Clear reference
            raw_responses[search_type] = None
    
    # Wait for all raw saves to complete (parallel!)
    if raw_save_tasks:
        await asyncio.gather(*raw_save_tasks)
    
    # Clear dict
    del raw_responses
    gc.collect()
    
    # Save combined results (async)
    output_file, num_flights = await storage.save_combined_results(
        award_flights=results.get("Award"),
        revenue_flights=results.get("Revenue"),
        origin=origin,
        destination=destination,
        date=date,
        passengers=passengers,
        cabin_filter=cabin_filter,
        timestamp=timestamp,
    )
    
    # Clear results
    results.clear()
    gc.collect()
    
    return output_file, num_flights


# Backwards compatibility wrapper
async def save_results(
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
    Legacy save function - redirects to async streaming version.
    """
    await save_results_streaming(
        results, raw_responses, output_dir,
        origin, destination, date, passengers, cabin_filter
    )