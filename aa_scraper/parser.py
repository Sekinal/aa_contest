"""Flight data parser for AA API responses"""

from typing import Any, Dict, List


class FlightDataParser:
    """Parse AA API response into structured data"""

    @staticmethod
    def parse_flight_options(
        api_response: Dict[str, Any],
        cabin_filter: str = "COACH",
        search_type: str = "Award",
    ) -> List[Dict[str, Any]]:
        """
        Parse flight options from API response.

        Args:
            api_response: Raw API response
            cabin_filter: Cabin class to filter (COACH, BUSINESS, etc.)
            search_type: Award or Revenue search

        Returns:
            List of parsed flight dictionaries
        """
        flights = []
        slices = api_response.get("slices", [])

        for slice_data in slices:
            duration_min = slice_data.get("durationInMinutes", 0)
            duration_str = format_duration(duration_min)
            is_nonstop = slice_data.get("stops", 0) == 0

            segments_data = slice_data.get("segments", [])
            parsed_segments = []

            for segment in segments_data:
                flight_info = segment.get("flight", {})
                carrier_code = flight_info.get("carrierCode", "")
                flight_num = flight_info.get("flightNumber", "")
                flight_number = f"{carrier_code}{flight_num}"

                dep_time = format_time(segment.get("departureDateTime", ""))
                arr_time = format_time(segment.get("arrivalDateTime", ""))

                parsed_segments.append(
                    {
                        "flight_number": flight_number,
                        "departure_time": dep_time,
                        "arrival_time": arr_time,
                    }
                )

            if not parsed_segments:
                continue

            pricing_detail = slice_data.get("pricingDetail", [])

            for pricing_option in pricing_detail:
                if not pricing_option.get("productAvailable", False):
                    continue

                product_type = pricing_option.get("productType", "")

                if cabin_filter == "COACH" and product_type != "COACH":
                    continue
                elif cabin_filter != "COACH" and not product_type.startswith(cabin_filter):
                    continue

                slice_pricing = pricing_option.get("slicePricing", {})
                if not slice_pricing:
                    continue

                points_str = slice_pricing.get("perPassengerAwardPoints", "0")
                if isinstance(points_str, str):
                    points_or_fare = float(points_str.replace(",", ""))
                else:
                    points_or_fare = float(points_str)

                if search_type == "Award":
                    points = int(points_or_fare)
                else:
                    points = 0

                taxes_fees = (
                    slice_pricing.get("allPassengerDisplayTaxTotal", {}).get("amount", 0.0)
                )
                cash_total = (
                    slice_pricing.get("allPassengerDisplayTotal", {}).get("amount", 0.0)
                )

                if search_type == "Award":
                    cpp = calculate_cpp(cash_total, taxes_fees, points)
                else:
                    cpp = 0.0

                flight = {
                    "is_nonstop": is_nonstop,
                    "segments": parsed_segments,
                    "total_duration": duration_str,
                    "points_required": points,
                    "cash_price_usd": cash_total,
                    "taxes_fees_usd": taxes_fees,
                    "cpp": cpp,
                    "_product_type": product_type,
                }

                flights.append(flight)

        return flights


def format_duration(minutes: int) -> str:
    """Convert minutes to 'Xh Ym' format"""
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h {mins}m"


def calculate_cpp(cash_price: float, taxes_fees: float, points: int) -> float:
    """Calculate cents per point (CPP)"""
    if points == 0:
        return 0.0
    return round((cash_price - taxes_fees) / points * 100, 2)


def format_time(datetime_str: str) -> str:
    """Extract time from ISO datetime string (HH:MM format)"""
    if "T" not in datetime_str:
        return ""
    time_part = datetime_str.split("T")[1]
    return time_part[:5]