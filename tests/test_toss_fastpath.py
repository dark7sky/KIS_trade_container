"""Fast path regressions: stale feeds, entry cost, and generated-order stalls."""

from datetime import datetime, timedelta, timezone

import pytest

from kis_mcp.models import TossOrderInput, TradingError
from kis_mcp.toss_fastpath import TossMarketStream, estimate_entry

from test_toss_automation import oco_request


def stamp(seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def book(at=None, bid="99.95", ask="100.05"):
    return {"timestamp": at or stamp(), "currency": "USD", "bids": [{"price": bid, "volume": "20"}], "asks": [{"price": ask, "volume": "20"}]}


def test_entry_cost_rejects_stale_crossed_and_insufficient_edge():
    with pytest.raises(TradingError, match="stale"):
        estimate_entry(book(at=stamp(-40)), "0.001", "80", "10", now=datetime.now(timezone.utc))
    with pytest.raises(TradingError, match="spread"):
        estimate_entry(book(bid="100.10", ask="100.05"), "0.001", "80", "10")
    result = estimate_entry(book(), "0.001", "15", "10")
    assert result["allowed"] is False
    assert result["round_trip_cost_bps"] == "40"
    assert estimate_entry(book(), "0.001", "70", "10")["allowed"] is True


async def test_stream_validates_ack_and_does_not_fire_stale_or_replayed_trade(automation):
    _, watches, broker = automation
    from kis_mcp.models import TossWatchInput

    await watches.create(TossWatchInput(
        client_request_id="fast-watch-1", symbol="SOXL", kind="price_at_or_above",
        threshold="101", expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    ))
    stream = TossMarketStream(watches.service, watches)
    await stream.handle({"type": "subscriptions", "subscribed": ["trade:us:SOXL"], "rejected": []})
    await stream.handle({"type": "message", "topic": "trade:us:SOXL", "data": {"price": "102", "volume": "2", "timestamp": stamp(-45), "currency": "USD"}})
    assert watches.list()[0]["status"] == "watching"
    await stream.handle({"type": "message", "topic": "trade:us:SOXL", "data": {"price": "102", "volume": "2", "timestamp": stamp(), "currency": "USD"}})
    assert watches.list()[0]["status"] == "triggered"
    assert watches.store.pending_count() == 1
    assert broker.quote_calls == 0


async def test_stream_rejected_subscription_fails_closed(automation):
    _, watches, _ = automation
    stream = TossMarketStream(watches.service, watches)
    await stream.handle({"type": "subscriptions", "subscribed": [], "rejected": [{"target": "trade:us:SOXL", "code": "stock-not-found", "message": "bad"}]})
    assert stream.status()["healthy"] is False
    await stream.handle({"type": "message", "topic": "orderbook:us:SOXL", "data": book()})
    with pytest.raises(TradingError, match="fresh"):
        stream.orderbook("SOXL")


async def test_guarded_order_blocks_before_submission_when_cost_exceeds_edge(automation):
    conditions, watches, broker = automation
    stream = TossMarketStream(watches.service, watches)
    stream.accept_book("SOXL", book())
    service = conditions.service
    service.market_stream = stream
    async def commissions():
        return "0.001"
    broker.commission_rate = commissions
    request = TossOrderInput(client_request_id="fast-guard-1", symbol="SOXL", side="buy", quantity=1, price="100.05")
    with pytest.raises(TradingError, match="cost"):
        await service.place(request, guard={"expected_move_bps": "15", "slippage_bps": "10"})
    assert service.orders() == []


async def test_generated_order_stall_alert_survives_restart_and_partial_fill_resets_clock(automation):
    conditions, _, broker = automation
    created = await conditions.create_oco(oco_request())
    row = broker.conditional_rows[created["broker_id"]]
    row["status"] = "ORDERED"
    row["first"]["triggeredOrderId"] = "generated-order"
    start = datetime.now(timezone.utc)
    await conditions.refresh(created["id"], now=start)
    restarted = type(conditions)(conditions.service)
    await restarted.refresh(created["id"], now=start + timedelta(seconds=95))
    assert restarted.store.pending_count() == 3  # trigger, order, stall
    await restarted.refresh(created["id"], now=start + timedelta(seconds=100))
    assert restarted.store.pending_count() == 3
    broker.generated_status = "PARTIAL_FILLED"
    broker.generated_filled = "1"
    await restarted.refresh(created["id"], now=start + timedelta(seconds=105))
    assert restarted.store.pending_count() == 4
    await restarted.refresh(created["id"], now=start + timedelta(seconds=200))
    assert restarted.store.pending_count() == 5
