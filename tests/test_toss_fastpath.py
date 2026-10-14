"""Fast path regressions: stale feeds, entry cost, and generated-order stalls."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from test_toss_automation import AutomationBroker, oco_request

from kis_mcp.config import TossCredentials
from kis_mcp.models import TossOrderInput, TossWatchInput, TradingError
from kis_mcp.toss_conditions import TossConditions
from kis_mcp.toss_fastpath import TossMarketStream, estimate_entry
from kis_mcp.toss_service import TossTradingService
from kis_mcp.toss_watches import TossWatches


@pytest.fixture
def automation(service, settings):
    settings.toss_credentials = TossCredentials("test-client", "test-secret", "3")
    broker = AutomationBroker()
    toss = TossTradingService(settings, service.store, broker, lock=service.lock)
    return TossConditions(toss), TossWatches(toss), broker


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
    with pytest.raises(TradingError, match="liquidity"):
        estimate_entry(book(), "0.001", "70", "10", quantity=21)


async def test_stream_validates_ack_and_does_not_fire_stale_or_replayed_trade(automation):
    _, watches, broker = automation

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
    assert watches.store.pending_count() == 0
    assert broker.quote_calls == 0


async def test_live_trade_is_not_blocked_by_slow_minute_candle_query(automation):
    _, watches, broker = automation
    now = datetime.now(timezone.utc)
    for suffix, kind, threshold in (("price", "price_at_or_above", "101"), ("volume", "volume_breakout_1m", None)):
        await watches.create(TossWatchInput(
            client_request_id="fast-watch-" + suffix, symbol="SOXL", kind=kind,
            threshold=threshold, expires_at=now + timedelta(minutes=5),
        ))
    watches.stream_enabled = True
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_candles(*_):
        started.set()
        await release.wait()
        return []

    broker.candles = slow_candles
    poll = asyncio.create_task(watches.tick())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(
            watches.observe_trade("SOXL", {"price": "102", "broker_timestamp": stamp()}),
            timeout=0.2,
        )
        assert next(d for d in watches.list() if d["kind"].startswith("price_"))["status"] == "triggered"
    finally:
        release.set()
        await poll


async def test_stopped_watch_is_not_retriggered_by_inflight_candle_query(automation):
    _, watches, broker = automation
    now = datetime.now(timezone.utc)
    created = await watches.create(TossWatchInput(
        client_request_id="fast-watch-stop-race", symbol="SOXL", kind="volume_breakout_1m",
        expires_at=now + timedelta(minutes=5),
    ))
    future = now + timedelta(minutes=2)
    end = future.replace(second=0, microsecond=0)
    bars = [
        {"timestamp": (end - timedelta(minutes=i)).isoformat(),
         "close": "110" if i == 0 else "100", "high": "111" if i == 0 else "105",
         "volume": "300" if i == 0 else "100"}
        for i in range(6)
    ]
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_candles(*_):
        started.set()
        await release.wait()
        return bars

    broker.candles = slow_candles
    poll = asyncio.create_task(watches.tick(now=future))
    await asyncio.wait_for(started.wait(), timeout=1)
    stop = asyncio.create_task(watches.stop(created["id"]))
    await asyncio.sleep(0.01)
    release.set()
    await asyncio.gather(poll, stop)
    assert watches.list()[0]["status"] == "stopped"
    assert watches.store.pending_count() == 0


async def test_stream_rejected_subscription_fails_closed(automation):
    _, watches, _ = automation
    stream = TossMarketStream(watches.service, watches)
    await stream.handle({"type": "subscriptions", "subscribed": [], "rejected": [{"target": "trade:us:SOXL", "code": "stock-not-found", "message": "bad"}]})
    assert stream.status()["healthy"] is False
    assert "trade:us:SOXL" not in stream.wanted()
    assert "trade:us:SOXL" in stream.status()["rejected"]
    await stream.handle({"type": "message", "topic": "orderbook:us:SOXL", "data": book()})
    with pytest.raises(TradingError, match="Fresh"):
        stream.orderbook("SOXL")


async def test_stream_declares_one_full_replacement_and_redacts_account(automation):
    _, watches, _ = automation
    stream = TossMarketStream(watches.service, watches)
    declaration = stream.declaration(stream.wanted())
    assert declaration == [
        {"type": "trade:us", "codes": ["SOXL", "SOXS"]},
        {"type": "orderbook:us", "codes": ["SOXL", "SOXS"]},
        {"type": "personal:order", "codes": ["3"]},
    ]
    await stream.handle({"type": "subscriptions", "subscribed": ["personal:order:3"], "rejected": []})
    assert stream.status()["subscribed"] == ["personal:order"]


async def test_stream_worker_subscribes_without_live_network(automation, monkeypatch):
    import json

    import websockets.asyncio.client

    _, watches, broker = automation
    stream = TossMarketStream(watches.service, watches)
    sent = []
    sent_event = asyncio.Event()

    async def token():
        return "fake-token"

    class FakeSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def send(self, message):
            sent.append(message)
            sent_event.set()

        async def recv(self):
            await asyncio.Event().wait()

    def fake_connect(url, **kwargs):
        assert url.endswith("/ws/v1")
        assert kwargs["additional_headers"] == {"Authorization": "Bearer fake-token"}
        return FakeSocket()

    broker.token = token
    monkeypatch.setattr(websockets.asyncio.client, "connect", fake_connect)
    task = asyncio.create_task(stream.run())
    try:
        await asyncio.wait_for(sent_event.wait(), timeout=2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert json.loads(sent[0]) == stream.declaration(stream.wanted())
    assert stream.status()["healthy"] is False


async def test_stream_sends_application_ping_when_market_is_quiet(automation, monkeypatch):
    import websockets.asyncio.client

    _, watches, broker = automation
    stream = TossMarketStream(watches.service, watches)
    clock = [0]
    stream.monotonic = lambda: clock[0]
    ping_sent = asyncio.Event()

    async def token():
        return "fake-token"

    class FakeSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def send(self, message):
            if message == "PING":
                ping_sent.set()

        async def recv(self):
            clock[0] = 61
            await asyncio.sleep(0)
            return '{"type":"pong"}'

    broker.token = token
    monkeypatch.setattr(websockets.asyncio.client, "connect", lambda *_args, **_kwargs: FakeSocket())
    task = asyncio.create_task(stream.run())
    try:
        await asyncio.wait_for(ping_sent.wait(), timeout=0.3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_stream_refreshes_token_after_unauthorized_handshake(automation, monkeypatch):
    import websockets.asyncio.client
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    _, watches, broker = automation
    stream = TossMarketStream(watches.service, watches)
    broker.access_token = "old-token"
    broker.auth_lock = asyncio.Lock()
    attempts = []
    subscribed = asyncio.Event()

    async def token():
        if broker.access_token is None:
            broker.access_token = "new-token"
        return broker.access_token

    class FakeSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def send(self, message):
            subscribed.set()

        async def recv(self):
            await asyncio.Event().wait()

    def fake_connect(*_args, **kwargs):
        attempts.append(kwargs["additional_headers"]["Authorization"])
        if len(attempts) == 1:
            raise InvalidStatus(Response(401, "Unauthorized", Headers()))
        return FakeSocket()

    broker.token = token
    monkeypatch.setattr(websockets.asyncio.client, "connect", fake_connect)
    monkeypatch.setattr("kis_mcp.toss_fastpath.random.uniform", lambda *_: 0)
    task = asyncio.create_task(stream.run())
    try:
        await asyncio.wait_for(subscribed.wait(), timeout=2.5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert attempts == ["Bearer old-token", "Bearer new-token"]


async def test_stream_reconnects_and_resubscribes_after_socket_loss(automation, monkeypatch):
    import json

    import websockets.asyncio.client

    _, watches, broker = automation
    stream = TossMarketStream(watches.service, watches)
    declarations = []
    second_connected = asyncio.Event()

    async def token():
        return "fake-token"

    class FakeSocket:
        def __init__(self, generation):
            self.generation = generation
            self.received = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def send(self, message):
            declarations.append((self.generation, json.loads(message)))
            if self.generation == 2:
                second_connected.set()

        async def recv(self):
            if self.generation == 1:
                self.received += 1
                if self.received == 1:
                    return json.dumps({"type": "subscriptions", "subscribed": ["orderbook:us:SOXL"], "rejected": []})
                if self.received == 2:
                    return json.dumps({"type": "message", "topic": "orderbook:us:SOXL", "data": book()})
                raise ConnectionError("socket closed")
            await asyncio.Event().wait()

    def fake_connect(*_args, **_kwargs):
        return FakeSocket(len({item[0] for item in declarations}) + 1)

    broker.token = token
    monkeypatch.setattr(websockets.asyncio.client, "connect", fake_connect)
    monkeypatch.setattr("kis_mcp.toss_fastpath.random.uniform", lambda *_: 0)
    task = asyncio.create_task(stream.run())
    try:
        await asyncio.wait_for(second_connected.wait(), timeout=2.5)
        assert stream.books == {}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(declarations) == 2
    assert declarations[0][1] == declarations[1][1]


async def test_stream_removes_rejected_topic_on_next_full_declaration(automation, monkeypatch):
    import json

    import websockets.asyncio.client

    _, watches, broker = automation
    stream = TossMarketStream(watches.service, watches)
    declarations = []
    redeclared = asyncio.Event()

    async def token():
        return "fake-token"

    class FakeSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def send(self, message):
            declarations.append(json.loads(message))
            if len(declarations) == 2:
                redeclared.set()

        async def recv(self):
            if len(declarations) == 1:
                return json.dumps({"type": "subscriptions", "subscribed": ["orderbook:us:SOXL"],
                                   "rejected": [{"target": "trade:us:SOXL", "code": "stock-not-found", "message": "bad"}]})
            await asyncio.Event().wait()

    broker.token = token
    monkeypatch.setattr(websockets.asyncio.client, "connect", lambda *_args, **_kwargs: FakeSocket())
    task = asyncio.create_task(stream.run())
    try:
        await asyncio.wait_for(redeclared.wait(), timeout=1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert "SOXL" not in next(item for item in declarations[1] if item["type"] == "trade:us")["codes"]


async def test_guarded_order_blocks_before_submission_when_cost_exceeds_edge(automation):
    conditions, watches, broker = automation
    stream = TossMarketStream(watches.service, watches)
    stream.accept_book("SOXL", book())
    await stream.handle({"type": "subscriptions", "subscribed": ["orderbook:us:SOXL"], "rejected": []})
    service = conditions.service
    service.market_stream = stream
    async def commissions():
        return "0.001"
    broker.commission_rate = commissions
    request = TossOrderInput(client_request_id="fast-guard-1", symbol="SOXL", side="buy", quantity=1, price="100.05")
    with pytest.raises(TradingError, match="cost"):
        await service.place(request, guard={"expected_move_bps": "15", "slippage_bps": "10"})
    assert service.orders() == []


async def test_guarded_order_sends_once_with_fresh_book_and_reuses_request_id(automation):
    conditions, watches, broker = automation
    stream = TossMarketStream(watches.service, watches)
    stream.accept_book("SOXL", book())
    await stream.handle({"type": "subscriptions", "subscribed": ["orderbook:us:SOXL"], "rejected": []})
    service = conditions.service
    service.market_stream = stream
    calls = []

    async def commissions():
        return "0.001"

    async def orders(*args, **kwargs):
        return []

    async def place(order):
        calls.append(order["id"])
        return {"broker_id": "broker-order-1"}

    broker.commission_rate = commissions
    broker.orders = orders
    broker.place = place
    request = TossOrderInput(client_request_id="fast-guard-2", symbol="SOXL", side="buy", quantity=1, price="100.05")
    guard = {"expected_move_bps": "70", "slippage_bps": "10"}
    first = await service.place(request, guard=guard)
    stream.disconnect()
    second = await service.place(request, guard=guard)
    assert first["status"] == second["status"] == "accepted"
    assert first["entry_cost_check"]["round_trip_cost_bps"] == "40"
    assert calls == [first["id"]]


async def test_guarded_order_rejects_limit_above_slippage_budget(automation):
    conditions, watches, broker = automation
    stream = TossMarketStream(watches.service, watches)
    stream.accept_book("SOXL", book())
    await stream.handle({"type": "subscriptions", "subscribed": ["orderbook:us:SOXL"], "rejected": []})
    conditions.service.market_stream = stream

    async def commissions():
        return "0.001"

    broker.commission_rate = commissions
    request = TossOrderInput(client_request_id="fast-guard-3", symbol="SOXL", side="buy", quantity=1, price="101")
    with pytest.raises(TradingError, match="slippage"):
        await conditions.service.place(request, guard={"expected_move_bps": "100", "slippage_bps": "10"})
    assert conditions.service.orders() == []


async def test_guarded_order_rechecks_feed_after_capacity_queries(automation):
    conditions, watches, broker = automation
    stream = TossMarketStream(watches.service, watches)
    stream.accept_book("SOXL", book())
    await stream.handle({"type": "subscriptions", "subscribed": ["orderbook:us:SOXL"], "rejected": []})
    conditions.service.market_stream = stream

    async def commissions():
        return "0.001"

    async def orders(*args, **kwargs):
        stream.disconnect()
        return []

    broker.commission_rate = commissions
    broker.orders = orders
    request = TossOrderInput(client_request_id="fast-guard-4", symbol="SOXL", side="buy", quantity=1, price="100.05")
    with pytest.raises(TradingError, match="Fresh"):
        await conditions.service.place(request, guard={"expected_move_bps": "70", "slippage_bps": "10"})
    assert conditions.service.orders() == []


async def test_stream_disconnect_invalidates_orderbook(automation):
    _, watches, _ = automation
    stream = TossMarketStream(watches.service, watches)
    stream.accept_book("SOXL", book())
    await stream.handle({"type": "subscriptions", "subscribed": ["orderbook:us:SOXL"], "rejected": []})
    assert stream.orderbook("SOXL")["currency"] == "USD"
    stream.disconnect()
    with pytest.raises(TradingError, match="Fresh"):
        stream.orderbook("SOXL")


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
