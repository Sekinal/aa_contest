import sys
from pathlib import Path
import pytest

import aa_scraper.cli as cli


def _noop(*args, **kwargs):
    return None


@pytest.fixture
def no_logging(monkeypatch):
    # Avoid touching real logs/paths during tests
    monkeypatch.setattr(cli, "setup_logging", lambda *a, **k: None)


@pytest.fixture
def fake_bulk(monkeypatch, tmp_path):
    """
    Monkeypatch scrape_bulk_concurrent to capture its inputs and return a minimal stats dict.
    """
    calls = {"args": None}

    async def fake_scrape_bulk_concurrent(
        origins, destinations, dates, passengers, cookie_manager=None, cookie_pool=None,
        cabin_filter="COACH", search_types=("Award", "Revenue"), rate_limit=1.0,
        max_concurrent=5, output_dir=Path(".")
    ):
        calls["args"] = {
            "origins": origins,
            "destinations": destinations,
            "dates": dates,
            "passengers": passengers,
            "cabin_filter": cabin_filter,
            "search_types": list(search_types),
            "rate_limit": rate_limit,
            "max_concurrent": max_concurrent,
            "output_dir": Path(output_dir),
            "using_pool": cookie_pool is not None,
        }
        # Minimal plausible stats structure used by CLI summary
        return {
            "successful": len(origins) * len(destinations) * len(dates),
            "failed": 0,
            "total_flights": 0,
            "start_time": 0.0,
            "total_api_requests": 0,
            "total_responses_bytes": 0,
            "total_saved_bytes": 0,
            "failed_retries": 0,
            "average_response_times": [],
            "cookie_refreshes": 0,
        }

    monkeypatch.setattr(cli, "scrape_bulk_concurrent", fake_scrape_bulk_concurrent)
    return calls


def _run_main_with_args(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["aa_scraper"] + argv)
    # main() is synchronous and internally runs asyncio.run(...)
    cli.main()


def test_bulk_multiple_destinations_single_origin_single_date_is_bulk(no_logging, fake_bulk, monkeypatch, tmp_path):
    # Regression for the reported case: should be bulk, not single-route
    args = [
        "--origins", "LAX",
        "--destinations", "JFK", "ORL",
        "--date", "2025-12-15",
        "--output", str(tmp_path),
        "--log-file", str(tmp_path / "log.log"),
    ]
    _run_main_with_args(monkeypatch, args)

    called = fake_bulk["args"]
    assert called is not None, "Bulk path must be taken when multiple destinations are given"
    # Uppercasing is applied at call site to bulk function
    assert called["origins"] == ["LAX"]
    assert called["destinations"] == ["JFK", "ORL"]
    assert called["dates"] == ["2025-12-15"]
    assert called["output_dir"] == tmp_path


def test_bulk_single_origin_multi_destinations_multi_dates(no_logging, fake_bulk, monkeypatch, tmp_path):
    args = [
        "--origin", "lax",  # mix case on purpose
        "--destinations", "JFK", "MIA", "ORD",
        "--dates", "2025-12-15", "2025-12-16",
        "--search-type", "Award",
        "--output", str(tmp_path),
        "--log-file", str(tmp_path / "log.log"),
    ]
    _run_main_with_args(monkeypatch, args)

    called = fake_bulk["args"]
    assert called is not None
    # Origin falls back to a list when --origins not supplied, then uppercased at call
    assert called["origins"] == ["LAX"]
    assert called["destinations"] == ["JFK", "MIA", "ORD"]
    assert called["dates"] == ["2025-12-15", "2025-12-16"]
    # Only Award requested is propagated
    assert called["search_types"] == ["Award"]


def test_bulk_multi_origins_single_destination_range_dates(no_logging, fake_bulk, monkeypatch, tmp_path):
    args = [
        "--origins", "LAX", "SFO",
        "--destination", "JFK",
        "--date", "2025-12-15:2025-12-17",  # date range expands to 15,16,17
        "--output", str(tmp_path),
        "--log-file", str(tmp_path / "log.log"),
    ]
    _run_main_with_args(monkeypatch, args)

    called = fake_bulk["args"]
    assert called is not None
    assert called["origins"] == ["LAX", "SFO"]
    assert called["destinations"] == ["JFK"]
    assert called["dates"] == ["2025-12-15", "2025-12-16", "2025-12-17"]


def test_bulk_missing_destinations_errors_cleanly(no_logging, monkeypatch, tmp_path):
    # With >1 origins and valid dates, bulk mode turns on, but missing destinations must error
    args = [
        "--origins", "LAX", "SFO",
        "--date", "2025-12-15",
        "--output", str(tmp_path),
        "--log-file", str(tmp_path / "log.log"),
    ]
    monkeypatch.setattr(sys, "argv", ["aa_scraper"] + args)
    with pytest.raises(SystemExit):
        cli.main()


def test_bulk_prefers_plural_flags_when_both_provided(no_logging, fake_bulk, monkeypatch, tmp_path):
    # If user passes both --origin and --origins, the CLI prefers --origins
    args = [
        "--origin", "SEA",
        "--origins", "LAX", "SFO",
        "--destination", "JFK",
        "--date", "2025-12-15",
        "--output", str(tmp_path),
        "--log-file", str(tmp_path / "log.log"),
    ]
    _run_main_with_args(monkeypatch, args)

    called = fake_bulk["args"]
    assert called is not None
    assert called["origins"] == ["LAX", "SFO"]  # plural wins
    assert called["destinations"] == ["JFK"]
    assert called["dates"] == ["2025-12-15"]


def test_bulk_browsers_creates_cookie_pool_path(no_logging, fake_bulk, monkeypatch, tmp_path):
    # Exercise the multi-browser branch without touching real browsers/cookies
    class FakeCookiePool:
        def __init__(self, num_browsers, base_cookie_dir, max_concurrent_per_browser, test_origin, test_destination, test_days_ahead, proxy_pool=None):
            self.num_browsers = num_browsers
            self.max_concurrent_per_browser = max_concurrent_per_browser
            self.proxies = []  # no proxies
        async def initialize_all_cookies(self, force_refresh, headless, wait_time):
            return None
        def get_browser(self, task_id):
            # Minimal shape used by scrape_bulk_concurrent path
            return {"manager": object(), "semaphore": cli.asyncio.Semaphore(999), "id": 0}
        def print_stats(self):  # called at the end
            pass

    monkeypatch.setattr(cli, "CookiePool", FakeCookiePool)

    args = [
        "--origins", "LAX",
        "--destinations", "JFK", "MIA",
        "--date", "2025-12-15",
        "--browsers", "2",                 # triggers multi-browser codepath
        "--max-concurrent", "3",
        "--output", str(tmp_path),
        "--log-file", str(tmp_path / "log.log"),
    ]
    _run_main_with_args(monkeypatch, args)

    called = fake_bulk["args"]
    assert called is not None
    # scrape_bulk_concurrent should be called with a cookie_pool (multi-browser mode)
    assert called["using_pool"] is True
    assert called["max_concurrent"] == 3


def test_invalid_date_fails_validation(no_logging, monkeypatch, tmp_path):
    args = [
        "--origin", "LAX",
        "--destination", "JFK",
        "--date", "2025-13-40",  # invalid month/day
        "--output", str(tmp_path),
        "--log-file", str(tmp_path / "log.log"),
    ]
    monkeypatch.setattr(sys, "argv", ["aa_scraper"] + args)
    with pytest.raises(SystemExit):
        cli.main()
