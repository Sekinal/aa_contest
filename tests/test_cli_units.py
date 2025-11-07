import asyncio
from pathlib import Path
import types
import pytest

import aa_scraper.cli as cli

@pytest.mark.asyncio
async def test_scrape_flights_concurrent_monkeypatched(tmp_path: Path, monkeypatch):
    # Fake AAFlightClient to bypass network; returns simple raw dicts
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def search_flights(self, origin, destination, date, passengers, search_type):
            return {"ok": True, "type": search_type}


    # Fake parser to return 2 items for Award, 3 for Revenue
    def fake_parse(api_response, cabin_filter, search_type):
        if search_type == "Award":
            return [{"a": 1}, {"a": 2}]
        else:
            return [{"r": 1}, {"r": 2}, {"r": 3}]

    monkeypatch.setattr(cli, "AAFlightClient", FakeClient)
    monkeypatch.setattr(cli.FlightDataParser, "parse_flight_options", staticmethod(fake_parse))

    # CookieManager instance is required by signature but not used by FakeClient
    from aa_scraper.cookie_manager import CookieManager
    cm = CookieManager(cookie_file=tmp_path / "aa_cookies.json")

    results, raw = await cli.scrape_flights(
        origin="LAX", destination="JFK", date="2025-12-15", passengers=1,
        cookie_manager=cm, cabin_filter="COACH", search_types=["Award", "Revenue"], rate_limit=5.0
    )

    assert set(results.keys()) == {"Award", "Revenue"}
    assert len(results["Award"]) == 2 and len(results["Revenue"]) == 3
    assert raw["Award"]["type"] == "Award" and raw["Revenue"]["type"] == "Revenue"

@pytest.mark.asyncio
async def test_scrape_bulk_concurrent_single_combo_streaming_metrics(tmp_path: Path, monkeypatch):
    # Fake scrape_flights_with_metrics: returns small results + metrics
    async def fake_scrape_with_metrics(origin, destination, date, passengers, cookie_manager,
                                       cabin_filter, search_types, rate_limiter, rate_limit):
        results = {
            "Award": [{"_product_type": "COACH", "is_nonstop": True, "segments":[{"departure_time":"08:00","arrival_time":"09:00"}], "total_duration":"1h", "points_required":10000, "taxes_fees_usd":5.0}],
            "Revenue": [{"_product_type": "COACH", "is_nonstop": True, "segments":[{"departure_time":"08:00","arrival_time":"09:00"}], "cash_price_usd":200.0, "taxes_fees_usd":5.0}],
        }
        raw = {"Award": {"raw": "x"}, "Revenue": {"raw": "y"}}
        metrics = {"api_requests": 2, "responses_bytes": 50, "retries": 0, "response_times": [0.1, 0.1], "cookie_refreshes": 0}
        return results, raw, metrics

    # Fake save to avoid real IO cost but still plausible return
    async def fake_save(results, raw_responses, output_dir, origin, destination, date, passengers, cabin_filter):
        out = tmp_path / f"{origin}_{destination}_{date}_combined.json"
        out.write_text('{"flights": [], "total_results": 0}')
        # Return 3-tuple: (path, flights, bytes)
        return out, 0, out.stat().st_size

    monkeypatch.setattr(cli, "scrape_flights_with_metrics", fake_scrape_with_metrics)
    monkeypatch.setattr(cli, "save_results_streaming", fake_save)

    # Minimal cookie manager or pool path
    from aa_scraper.cookie_manager import CookieManager
    cm = CookieManager(cookie_file=tmp_path / "aa_cookies.json")

    stats = await cli.scrape_bulk_concurrent(
        origins=["LAX"], destinations=["JFK"], dates=["2025-12-15"], passengers=1,
        cookie_manager=cm, cookie_pool=None, cabin_filter="COACH", search_types=["Award", "Revenue"],
        rate_limit=5.0, max_concurrent=1, output_dir=tmp_path
    )

    assert stats["successful"] == 1 and stats["failed"] == 0
    assert stats["total_api_requests"] == 2
    assert stats["total_saved_bytes"] > 0
    assert isinstance(stats["average_response_times"], list)

def test_date_action_accumulates_values(monkeypatch):
    # Construct DateAction and call manually to simulate both single and plural uses into same dest
    action = cli.DateAction(["--date"], dest="dates")
    class NS: pass
    ns = NS()
    # No 'dates' initially
    action(None, ns, "2025-01-01")
    assert ns.dates == ["2025-01-01"]

    # Simulate --dates multiple values
    action(None, ns, ["2025-01-02", "2025-01-03"])
    assert ns.dates == ["2025-01-01", "2025-01-02", "2025-01-03"]
