import asyncio
from pathlib import Path

import pytest

from aa_scraper.storage import AsyncStreamingStorage, save_results_streaming

@pytest.mark.asyncio
async def test_save_raw_response_writes_file(tmp_path: Path):
    storage = AsyncStreamingStorage(tmp_path)
    data = {"hello": "world"}
    p = await storage.save_raw_response(
        response_data=data,
        origin="LAX",
        destination="JFK",
        date="2025-12-15",
        search_type="Award",
        timestamp="20250101_000000",
    )
    assert p.exists(), "Raw file should be created"
    assert p.stat().st_size > 0, "Raw file should have non-zero size"

@pytest.mark.asyncio
async def test_save_combined_results_merges_and_cpp(tmp_path: Path):
    storage = AsyncStreamingStorage(tmp_path)

    # Matching by (dep_time, arr_time, nonstop) for COACH only
    award = [
        {
            "_product_type": "COACH",
            "is_nonstop": True,
            "segments": [
                {"departure_time": "08:00", "arrival_time": "16:30"},
            ],
            "total_duration": "8h 30m",
            "points_required": 12500,
            "taxes_fees_usd": 45.60,
        },
        # Non-matching (different times)
        {
            "_product_type": "COACH",
            "is_nonstop": True,
            "segments": [
                {"departure_time": "10:00", "arrival_time": "18:30"},
            ],
            "total_duration": "8h 30m",
            "points_required": 15000,
            "taxes_fees_usd": 50.0,
        },
        # Filtered out (not COACH)
        {
            "_product_type": "BUSINESS",
            "is_nonstop": True,
            "segments": [
                {"departure_time": "08:00", "arrival_time": "16:30"},
            ],
            "total_duration": "8h 30m",
            "points_required": 60000,
            "taxes_fees_usd": 5.0,
        },
    ]
    revenue = [
        {
            "_product_type": "COACH",
            "is_nonstop": True,
            "segments": [
                {"departure_time": "08:00", "arrival_time": "16:30"},
            ],
            "cash_price_usd": 350.50,
            "taxes_fees_usd": 45.60,
        },
        # Filtered out (cash <= 0)
        {
            "_product_type": "COACH",
            "is_nonstop": True,
            "segments": [
                {"departure_time": "10:00", "arrival_time": "18:30"},
            ],
            "cash_price_usd": 0.0,
            "taxes_fees_usd": 0.0,
        },
    ]

    out, count = await storage.save_combined_results(
        award_flights=award,
        revenue_flights=revenue,
        origin="LAX",
        destination="JFK",
        date="2025-12-15",
        passengers=1,
        cabin_filter="COACH",
        timestamp="20250101_000001",
    )
    assert out.exists(), "Combined output should be written"
    assert count == 1, "Only one merged flight should be produced"
    data = out.read_bytes()
    assert b'"total_results": 1' in data, "Total results should reflect merged count"

    # Validate cpp approx: (cash - taxes)/points * 100 = (350.5 - 45.6)/12500*100 ≈ 2.44
    import json
    doc = json.loads(out.read_text())
    cpp = doc["flights"][0]["cpp"]
    assert 2.43 < cpp < 2.45, f"CPP should be approx 2.44, got {cpp}"

@pytest.mark.asyncio
async def test_save_results_streaming_3tuple_and_sizes(tmp_path: Path):
    results = {
        "Award": [
            {
                "_product_type": "COACH",
                "is_nonstop": True,
                "segments": [
                    {"departure_time": "08:00", "arrival_time": "16:30"},
                ],
                "total_duration": "8h 30m",
                "points_required": 12500,
                "taxes_fees_usd": 45.60,
            }
        ],
        "Revenue": [
            {
                "_product_type": "COACH",
                "is_nonstop": True,
                "segments": [
                    {"departure_time": "08:00", "arrival_time": "16:30"},
                ],
                "cash_price_usd": 350.50,
                "taxes_fees_usd": 45.60,
            }
        ],
    }
    raw_responses = {
        "Award": {"raw": "a"},
        "Revenue": {"raw": "b"},
    }

    out_path, num_flights, total_bytes = await save_results_streaming(
        results=results,
        raw_responses=raw_responses,
        output_dir=tmp_path,
        origin="LAX",
        destination="JFK",
        date="2025-12-15",
        passengers=1,
        cabin_filter="COACH",
    )

    assert isinstance(out_path, Path) and out_path.exists(), "Combined file should exist"
    assert num_flights == 1, "One merged flight expected"
    # Count actual bytes in raw_data/*.json + combined file
    files = list((tmp_path / "raw_data").glob("*.json")) + [out_path]
    actual = sum(p.stat().st_size for p in files if p.exists())
    assert total_bytes == actual, "Reported total bytes should equal files written"
