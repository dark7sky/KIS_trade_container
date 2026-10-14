from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from kis_mcp.config import TossCredentials
from kis_mcp.models import (
    TossOCOInput,
    TossOrderInput,
    TossReplayInput,
    TossWatchInput,
    TradingError,
)
from kis_mcp.toss_conditions import TossConditions
from kis_mcp.toss_service import TossTradingService
from kis_mcp.toss_watches import TossWatches, evaluate


class AutomationBroker:
    health = {"real": "ok"}

    def __init__(self):
        self.created = []
        self.conditional_rows = {}
        self.cancelled = []
        self.create_error = None
        self.cancel_error = None
        self.current_price = "100"
        self.quote_calls = 0
        self.generated_status = "PENDING"
        self.generated_filled = "0"
        self.bars = []

    async def capacity(self, symbol, price):
        return {"sell_quantity": "10", "cash_buy_amount": "1000"}

    async def quote(self, symbol):
        self.quote_calls += 1
        return {
            "symbol": symbol,
            "price": self.current_price,
            "broker_timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def stock(self, symbol):
        return {"symbol": symbol, "market": "NASDAQ", "currency": "USD", "status": "ACTIVE"}

    async def candles(self, symbol, count):
        return deepcopy(self.bars)

    async def create_oco(self, payload):
        self.created.append(deepcopy(payload))
        broker_id = "broker-oco-" + str(len(self.created))
        self.conditional_rows[broker_id] = {
            "conditionalOrderId": broker_id,
            "symbol": payload["symbol"],
            "market": "US",
            "type": "OCO",
            "orderType": "LIMIT",
            "quantity": payload["quantity"],
            "status": "WATCHING",
            "first": payload["first"] | {"status": "WATCHING", "triggeredOrderId": None},
            "second": payload["second"] | {"status": "WATCHING", "triggeredOrderId": None},
        }
        if self.create_error:
            raise self.create_error
        return broker_id

    async def conditional_detail(self, broker_id):
        return deepcopy(self.conditional_rows[broker_id])

    async def detail(self, order_id):
        return {
            "orderId": order_id, "symbol": "SOXL", "side": "SELL",
            "currency": "USD", "quantity": "2", "status": self.generated_status,
            "execution": {"filledQuantity": self.generated_filled},
        }

    async def conditional_orders(self, symbol):
        return [deepcopy(row) for row in self.conditional_rows.values() if row["symbol"] == symbol]

    async def cancel_conditional(self, broker_id):
        self.cancelled.append(broker_id)
        if self.cancel_error:
            raise self.cancel_error
        return {}


@pytest.fixture
def automation(service, settings):
    settings.toss_credentials = TossCredentials("test-client", "test-secret", "3")
    broker = AutomationBroker()
    toss = TossTradingService(settings, service.store, broker, lock=service.lock)
    return TossConditions(toss), TossWatches(toss), broker


def oco_request(**changes):
    return TossOCOInput(
        **(
            {
                "client_request_id": "oco-request-1",
                "symbol": "SOXL",
                "quantity": 2,
                "take_profit_trigger": "110",
                "take_profit_limit": "109",
                "stop_loss_trigger": "90",
                "stop_loss_limit": "89",
                "expire_date": (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat(),
            }
            | changes
        )
    )


async def test_oco_idempotency_trigger_does_not_claim_fill(automation):
    conditions, _, broker = automation
    request = oco_request()
    created = await conditions.create_oco(request)
    assert (await conditions.create_oco(request))["id"] == created["id"]
    assert len(broker.created) == 1
    assert broker.created[0]["first"]["orderSide"] == "SELL"
    broker.conditional_rows[created["broker_id"]]["status"] = "ORDERED"
    broker.conditional_rows[created["broker_id"]]["first"]["triggeredOrderId"] = "generated-order"
    refreshed = await conditions.refresh(created["id"])
    assert refreshed["status"] == "ORDERED"
    assert refreshed["triggered_orders"] == ["generated-order"]
    assert refreshed["generated_orders"][0]["filled_quantity"] == "0"
    assert refreshed["generated_orders"][0]["status"] == "PENDING"
    assert conditions.store.pending_count() == 2
    await conditions.refresh(created["id"])
    assert conditions.store.pending_count() == 2
    broker.generated_status = "PARTIAL_FILLED"
    broker.generated_filled = "1"
    partial = await conditions.refresh(created["id"])
    assert partial["generated_orders"][0]["filled_quantity"] == "1"
    assert conditions.store.pending_count() == 3
    with pytest.raises(TradingError, match="Refresh"):
        await conditions.cancel("cancel-oco-1", created["id"])


async def test_oco_unknown_blocks_new_creation_until_manual_match(automation):
    conditions, _, broker = automation
    broker.create_error = RuntimeError("response lost")
    uncertain = await conditions.create_oco(oco_request())
    assert uncertain["status"] == "unknown"
    with pytest.raises(TradingError, match="Resolve"):
        await conditions.create_oco(oco_request(client_request_id="oco-request-2"))
    with pytest.raises(TradingError, match="conditional"):
        await conditions.service.place(TossOrderInput(
            client_request_id="toss-order-after-unknown", symbol="SOXL",
            side="sell", quantity=1, price="100",
        ))
    broker_id = next(iter(broker.conditional_rows))
    broker.conditional_rows[broker_id]["first"]["triggerPrice"] = "120"
    with pytest.raises(TradingError, match="do not match"):
        await conditions.resolve(uncertain["id"], broker_id)
    broker.conditional_rows[broker_id]["first"]["triggerPrice"] = "110"
    assert (await conditions.resolve(uncertain["id"], broker_id))["status"] == "WATCHING"


async def test_oco_cancel_rechecks_broker_before_delete(automation):
    conditions, _, broker = automation
    created = await conditions.create_oco(oco_request())
    broker.conditional_rows[created["broker_id"]]["first"]["triggeredOrderId"] = "order-raced"
    with pytest.raises(TradingError, match="changed"):
        await conditions.cancel("cancel-oco-1", created["id"])
    assert broker.cancelled == []
    broker.conditional_rows[created["broker_id"]]["first"]["triggeredOrderId"] = None
    assert (await conditions.cancel("cancel-oco-1", created["id"]))["status"] == "cancelled"
    assert broker.cancelled == [created["broker_id"]]
    assert (await conditions.cancel("cancel-oco-1", created["id"]))["status"] == "cancelled"
    assert len(broker.cancelled) == 1


async def test_unknown_condition_requires_manual_absence_and_fresh_broker_list(automation):
    conditions, _, broker = automation
    broker.create_error = RuntimeError("response lost")
    uncertain = await conditions.create_oco(oco_request())
    with pytest.raises(TradingError, match="Confirm absence"):
        await conditions.resolve_not_submitted(uncertain["id"], False)
    with pytest.raises(TradingError, match="Possible"):
        await conditions.resolve_not_submitted(uncertain["id"], True)
    broker.conditional_rows.clear()
    assert (await conditions.resolve_not_submitted(uncertain["id"], True))["status"] == "not_submitted"


async def test_uncertain_cancel_blocks_new_condition_until_reconciled(automation):
    conditions, _, broker = automation
    created = await conditions.create_oco(oco_request())
    broker.cancel_error = RuntimeError("response lost")
    assert (await conditions.cancel("cancel-oco-2", created["id"]))["status"] == "cancel_unknown"
    with pytest.raises(TradingError, match="Resolve"):
        await conditions.create_oco(oco_request(client_request_id="oco-request-2"))
    with pytest.raises(TradingError, match="still exists"):
        await conditions.resolve_cancel(created["id"], True)
    assert (await conditions.resolve_cancel(created["id"], False))["status"] == "WATCHING"


async def test_watch_fires_once_and_survives_restart(automation):
    _, watches, broker = automation
    now = datetime.now(timezone.utc)
    request = TossWatchInput(
        client_request_id="watch-request-1",
        symbol="SOXL",
        kind="price_at_or_above",
        threshold="101",
        expires_at=now + timedelta(minutes=5),
    )
    created = await watches.create(request)
    again = TossWatches(watches.service)
    assert (await again.create(request))["id"] == created["id"]
    await again.tick()
    assert again.list()[0]["status"] == "watching"
    broker.current_price = "101"
    await again.tick()
    assert again.list()[0]["status"] == "triggered"
    assert again.list()[0]["evidence"]["price"] == "101"
    assert again.store.pending_count() == 0
    assert again.store.db.execute("SELECT count(*) FROM events WHERE id=?", ("toss-watch:" + created["id"],)).fetchone()[0] == 1
    await watches.tick()
    assert again.store.pending_count() == 0
    assert broker.quote_calls == 2


async def test_watch_expiry_does_not_query_broker(automation):
    _, watches, broker = automation
    now = datetime.now(timezone.utc)
    created = await watches.create(
        TossWatchInput(
            client_request_id="watch-request-2",
            symbol="SOXL",
            kind="price_at_or_below",
            threshold="90",
            expires_at=now + timedelta(seconds=2),
        )
    )
    await watches.tick(now=now + timedelta(seconds=3))
    assert watches.list()[0]["status"] == "expired"
    assert broker.quote_calls == 0
    assert created["id"] == watches.list()[0]["id"]


def test_price_replay_rejects_stale_and_replays_without_side_effects():
    now = datetime.now(timezone.utc)
    request = TossReplayInput(kind="price_at_or_above", threshold="101", created_at=now)
    observations = [
        {"as_of": (now + timedelta(seconds=1)).isoformat(), "quote": {"price": "102", "broker_timestamp": (now - timedelta(seconds=5)).isoformat()}},
        {"as_of": (now + timedelta(seconds=2)).isoformat(), "quote": {"price": "102", "broker_timestamp": (now + timedelta(seconds=2)).isoformat()}},
    ]
    assert TossWatches.replay(request, observations)["at"] == observations[1]["as_of"]


def test_closed_minute_breakout_requires_contiguous_bars():
    now = datetime.now(timezone.utc).replace(second=10, microsecond=0)
    end = now.replace(second=0)
    bars = [
        {"timestamp": (end - timedelta(minutes=i)).isoformat(), "close": "110" if i == 0 else "100", "high": "105" if i else "111", "volume": "300" if i == 0 else "100"}
        for i in range(6)
    ]
    rule = {"kind": "volume_breakout_1m", "threshold": None, "volume_multiplier": "2", "created_at": (end - timedelta(minutes=1)).isoformat()}
    assert evaluate(rule, candles=bars, now=now)["volume"] == "300"
    unfinished = {"timestamp": (end + timedelta(minutes=1)).isoformat(), "close": "90", "high": "90", "volume": "1"}
    assert evaluate(rule, candles=[unfinished] + bars, now=now)["volume"] == "300"
    bars[2]["timestamp"] = (end - timedelta(minutes=4)).isoformat()
    assert evaluate(rule, candles=bars, now=now) is None


async def test_minute_breakout_watch_alerts_once(automation):
    _, watches, broker = automation
    now = datetime.now(timezone.utc)
    await watches.create(TossWatchInput(
        client_request_id="watch-volume-1", symbol="SOXL", kind="volume_breakout_1m",
        expires_at=now + timedelta(minutes=10), volume_multiplier="2",
    ))
    end = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    broker.bars = [
        {"timestamp": (end - timedelta(minutes=i)).isoformat(), "close": "110" if i == 0 else "100", "high": "111" if i == 0 else "105", "volume": "300" if i == 0 else "100"}
        for i in range(6)
    ]
    await watches.tick(now=end + timedelta(seconds=10))
    assert watches.list()[0]["status"] == "triggered"
    assert watches.list()[0]["evidence"]["bar_end"] == end.isoformat()
    assert watches.store.pending_count() == 0
    await watches.tick(now=end + timedelta(seconds=11))
    assert watches.store.pending_count() == 0
