"""Date utilities for processing date ranges and validation"""

import datetime
from typing import List, Tuple, Union
from dateutil.parser import parse as parse_date
from dateutil.rrule import rrule, DAILY


def parse_date_or_range(date_spec: str) -> List[str]:
    """
    Parse a date specification that can be a single date or a range.
    
    Args:
        date_spec: A date in YYYY-MM-DD format or a range in YYYY-MM-DD:YYYY-MM-DD format
        
    Returns:
        List of date strings in YYYY-MM-DD format
        
    Raises:
        ValueError: If the date format is invalid or the end date is before the start date
    """
    # Handle date range format (YYYY-MM-DD:YYYY-MM-DD)
    if ":" in date_spec:
        try:
            start_date_str, end_date_str = date_spec.split(":", 1)
            start_date = parse_date(start_date_str).date()
            end_date = parse_date(end_date_str).date()
            
            if end_date < start_date:
                raise ValueError(f"End date {end_date_str} is before start date {start_date_str}")
            
            # Generate all dates in the range
            dates = [
                dt.strftime("%Y-%m-%d")
                for dt in rrule(DAILY, dtstart=start_date, until=end_date)
            ]
            
            return dates
            
        except ValueError as e:
            raise ValueError(f"Invalid date range '{date_spec}': {str(e)}")
    
    # Handle single date
    else:
        try:
            # Validate the date format
            date = parse_date(date_spec).date()
            return [date.strftime("%Y-%m-%d")]
        except ValueError as e:
            raise ValueError(f"Invalid date '{date_spec}': {str(e)}")


def parse_date_list(date_specs: List[str]) -> List[str]:
    """
    Parse a list of date specifications that can include individual dates and ranges.
    
    Args:
        date_specs: List of date strings, which can be single dates or ranges
        
    Returns:
        Sorted list of unique date strings in YYYY-MM-DD format
        
    Raises:
        ValueError: If any date format is invalid
    """
    all_dates = []
    
    for spec in date_specs:
        dates = parse_date_or_range(spec)
        all_dates.extend(dates)
    
    # Remove duplicates and sort
    unique_dates = sorted(list(set(all_dates)))
    
    if len(unique_dates) != len(all_dates):
        # Log a warning about duplicate dates
        from loguru import logger
        logger.warning(f"Removed {len(all_dates) - len(unique_dates)} duplicate dates from input")
    
    return unique_dates


def validate_date_list(dates: List[str]) -> Tuple[bool, str]:
    """
    Validate a list of dates.
    
    Args:
        dates: List of date strings in YYYY-MM-DD format
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not dates:
        return False, "No dates provided"
    
    try:
        # Try to parse each date to ensure they're valid
        for date_str in dates:
            parse_date(date_str).date()
        
        return True, ""
    except ValueError as e:
        return False, str(e)


def get_date_range_info(date_specs: List[str]) -> Tuple[int, int]:
    """
    Get information about the date range.
    
    Args:
        date_specs: List of date specifications
        
    Returns:
        Tuple of (total_dates, consecutive_days)
        
    Note:
        consecutive_days is the number of consecutive days if all dates form a
        continuous range, otherwise 0
    """
    dates = parse_date_list(date_specs)
    total_dates = len(dates)
    
    if total_dates < 2:
        return total_dates, 0
    
    try:
        # Check if dates form a continuous range
        date_objs = [parse_date(d).date() for d in dates]
        date_objs.sort()
        
        # Calculate the difference between first and last date
        first_date = date_objs[0]
        last_date = date_objs[-1]
        expected_days = (last_date - first_date).days + 1
        
        # If the number of days matches the range size, it's continuous
        consecutive_days = expected_days if len(date_objs) == expected_days else 0
        
        return total_dates, consecutive_days
        
    except:
        return total_dates, 0