"""The minute feed must expose age and continuity before an AI can use it."""

from datetime import datetime, timedelta, timezone

import pytest

from kis_mcp.config import TossCredentials
from kis_mcp.models import TradingError
from kis_mcp.toss_service import TossTradingService

NOW = datetime(2026, 10, 14, 14, 32, 20, tzinfo=timezone.utc)


def bar(minutes_ago, *, close="101", high="102"):
    return {
        "timestamp": (NOW - timedelta(minutes=minutes_ago, seconds=20)).isoformat(),
        "open": "100", "high": high, "low": "99", "close": close, "volume": "300",
    }


class CandleBroker:
    def __init__(self, bars):
        self.bars = bars
        self.calls = []

    async def candles(self, symbol, count):
        self.calls.append((symbol, count))
        return self.bars[:count]


@pytest.fixture
def minute_service(settings, service):
    settings.toss_credentials = TossCredentials("test-client", "test-secret", "3")

    def make(bars):
        broker = CandleBroker(bars)
        return TossTradingService(settings, service.store, broker, lock=service.lock), broker

    return make


async def test_latest_closed_minute_bars_exclude_incomplete_and_report_quality(minute_service):
    service, broker = minute_service([bar(-1), *[bar(i) for i in range(0, 7)]])
    result = await service.minute_bars("SOXL", count=6, now=NOW)
    assert broker.calls == [("SOXL", 7)]
    assert result["symbol"] == "SOXL"
    assert result["source"] == "toss_rest_1m"
    assert result["timestamp_basis"] == "bar_end"
    assert result["omitted_unclosed"] == 1
    assert len(result["bars"]) == 6
    assert result["bars"][0]["timestamp"] == bar(0)["timestamp"]
    assert result["last_bar_age_seconds"] == 20
    assert result["fresh"] is True
    assert result["latest_contiguous_count"] == 6
    assert result["usable_for_entry"] is True


async def test_stale_or_gapped_bars_are_not_entry_ready(minute_service):
    service, _ = minute_service([bar(i) for i in (3, 4, 5, 7, 8, 9)])
    result = await service.minute_bars("SOXS", count=6, now=NOW)
    assert result["fresh"] is False
    assert result["latest_contiguous_count"] == 3
    assert result["gaps"] == 1
    assert result["usable_for_entry"] is False


async def test_missing_just_closed_minute_is_stale_for_entry(minute_service):
    service, _ = minute_service([bar(i) for i in range(1, 7)])
    result = await service.minute_bars("SOXL", count=6, now=NOW)
    assert result["last_bar_age_seconds"] == 80
    assert result["fresh"] is False
    assert result["usable_for_entry"] is False


async def test_invalid_ohlcv_fails_closed(minute_service):
    service, _ = minute_service([bar(1, close="103")])
    with pytest.raises(TradingError, match="Invalid Toss candle"):
        await service.minute_bars("SOXL", count=1, now=NOW)


async def test_duplicate_timestamp_fails_closed(minute_service):
    service, _ = minute_service([bar(1), bar(1)])
    with pytest.raises(TradingError, match="Invalid Toss candle"):
        await service.minute_bars("SOXL", count=2, now=NOW)
