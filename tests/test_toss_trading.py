import asyncio
from copy import deepcopy
from decimal import Decimal

import pytest
from pydantic import ValidationError

from kis_mcp.config import TossCredentials
from kis_mcp.models import OrderInput, TossOrderInput, TradingError, UncertainSubmission
from kis_mcp.store import Store
from kis_mcp.toss_service import TossTradingService


def request(**changes):
    return TossOrderInput(
        **(
            {
                "client_request_id": "toss-request-1",
                "symbol": "AAPL",
                "side": "buy",
                "quantity": 5,
                "price": "185.50",
            }
            | changes
        )
    )


class FakeToss:
    health = {"real": "not_checked"}

    def __init__(self):
        self.rows = {}
        self.placed = []
        self.cancelled = []
        self.place_error = None
        self.cancel_error = None
        self.query_error = None

    async def capacity(self, symbol, price):
        return {"cash_buy_amount": "10000", "sell_quantity": "10.5"}

    async def orders(self, start, end, symbol=None):
        if self.query_error:
            raise self.query_error
        return deepcopy(list(self.rows.values()))

    async def detail(self, broker_id):
        if self.query_error:
            raise self.query_error
        return deepcopy(self.rows[broker_id])

    async def place(self, order):
        self.placed.append(deepcopy(order))
        broker_id = "toss-order-" + str(len(self.placed))
        self.rows[broker_id] = {
            "orderId": broker_id,
            "symbol": order["symbol"],
            "side": order["side"].upper(),
            "orderType": order["order_type"].upper(),
            "timeInForce": "DAY",
            "status": "PENDING",
            "quantity": str(order["quantity"]),
            "price": order["price"],
            "currency": "USD",
            "orderedAt": order["created_at"],
            "execution": {"filledQuantity": "0", "averageFilledPrice": None, "filledAmount": None},
        }
        if self.place_error:
            raise self.place_error
        return {"broker_id": broker_id}

    async def cancel(self, order):
        self.cancelled.append(order["broker_id"])
        if self.cancel_error:
            raise self.cancel_error
        self.rows["cancel-child"] = deepcopy(self.rows[order["broker_id"]]) | {
            "orderId": "cancel-child",
            "status": "PENDING_CANCEL",
        }
        return {"broker_id": "cancel-child"}

    async def close(self):
        pass


@pytest.fixture
def toss(service, settings):
    settings.toss_credentials = TossCredentials("test-client", "test-secret", "3")
    return TossTradingService(settings, service.store, FakeToss(), lock=service.lock)


@pytest.mark.parametrize("price", ["185.501", "1.001", "0.12345", "NaN", "Infinity", "0"])
def test_reject_invalid_dollar_prices(price):
    with pytest.raises(ValidationError):
        request(price=price)


@pytest.mark.parametrize("price", ["185.50", "0.1234"])
def test_accept_dollar_prices(price):
    assert request(price=price).price == Decimal(price)


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity": 0.5},
        {"quantity": True},
        {"symbol": "005930"},
        {"symbol": "../AAPL"},
        {"order_type": "market", "price": "1"},
    ],
)
def test_reject_unsupported_orders(changes):
    with pytest.raises(ValidationError):
        request(**changes)


async def test_decimal_idempotence_and_concurrent_requests(toss):
    results = await asyncio.gather(toss.place(request()), toss.place(request(price="185.500")))
    assert results[0]["id"] == results[1]["id"]
    assert results[0]["broker"] == "toss" and results[0]["currency"] == "USD"
    assert results[0]["price"] == "185.5"
    assert len(toss.broker.placed) == 1
    with pytest.raises(TradingError, match="different input"):
        await toss.place(request(quantity=1))


async def test_demo_never_routes_to_real_toss(toss, service):
    await service.set_mode("demo")
    with pytest.raises(TradingError, match="real"):
        await toss.place(request())
    assert not toss.broker.placed


async def test_unknown_blocks_only_toss_and_never_reposts(toss, service):
    toss.broker.place_error = UncertainSubmission("unknown")
    order = await toss.place(request())
    assert order["status"] == "unknown"
    assert (await toss.place(request()))["id"] == order["id"]
    with pytest.raises(TradingError, match="uncertain"):
        await toss.place(request(client_request_id="toss-request-2"))
    kis = await service.place(
        OrderInput(
            client_request_id="kis-request-1", symbol="005930", side="buy", quantity=1, price=1000
        )
    )
    assert kis["status"] == "accepted"
    await service.poll()
    assert service.poll_error is None
    assert len(toss.broker.placed) == 1
    with pytest.raises(TradingError, match="broker"):
        await service.get_order(order["id"])
    with pytest.raises(TradingError, match="broker"):
        await toss.get_order(kis["id"])


async def test_partial_fill_cancel_race_notifications_and_restart(toss, service, settings):
    order = await toss.place(request())
    row = toss.broker.rows[order["broker_id"]]
    row.update(
        status="PARTIAL_FILLED",
        execution={"filledQuantity": "2", "averageFilledPrice": "185.25", "filledAmount": "370.50"},
    )
    partial = await toss.get_order(order["id"])
    assert (partial["filled"], partial["remaining"]) == (2, 3)
    result = await toss.cancel("toss-cancel-1", order["id"])
    assert result["status"] == "accepted"
    with pytest.raises(TradingError, match="reconciled"):
        await toss.cancel("toss-cancel-2", order["id"])
    row.update(
        status="CANCELED",
        execution={"filledQuantity": "3", "averageFilledPrice": "185.25", "filledAmount": "555.75"},
    )
    service.store.close()
    reopened = Store(settings.data_dir)
    service.store = reopened
    resumed = TossTradingService(settings, reopened, toss.broker)
    await resumed.poll()
    final = await resumed.get_order(order["id"])
    assert (final["status"], final["filled"], final["cancelled"], final["remaining"]) == (
        "cancelled",
        3,
        2,
        0,
    )
    assert (await resumed.cancel("toss-cancel-1", order["id"]))["cancelled_quantity"] == 2
    messages = reopened.pending_messages()
    assert len(messages) == 3
    assert "USD" in messages[0]["message"] and "TOSS" in messages[0]["message"]
    await resumed.poll()
    assert len(reopened.pending_messages()) == 3
    assert len(toss.broker.cancelled) == 1


async def test_cancel_rejection_child_allows_explicit_new_request(toss):
    order = await toss.place(request())
    await toss.cancel("toss-cancel-1", order["id"])
    toss.broker.rows["cancel-child"]["status"] = "CANCEL_REJECTED"
    assert (await toss.get_order(order["id"]))["pending_cancel"] is None
    assert (await toss.cancel("toss-cancel-1", order["id"]))["status"] == "rejected"
    await toss.cancel("toss-cancel-2", order["id"])
    assert len(toss.broker.cancelled) == 2


async def test_unknown_cancel_does_not_resend(toss):
    order = await toss.place(request())
    toss.broker.cancel_error = UncertainSubmission("unknown")
    assert (await toss.cancel("toss-cancel-1", order["id"]))["status"] == "unknown"
    assert (await toss.cancel("toss-cancel-1", order["id"]))["status"] == "unknown"
    with pytest.raises(TradingError):
        await toss.cancel("toss-cancel-2", order["id"])
    assert len(toss.broker.cancelled) == 1


async def test_query_failure_and_inconsistent_fill_retain_state(toss):
    order = await toss.place(request())
    toss.broker.query_error = TradingError("query failed")
    assert (await toss.get_order(order["id"]))["status"] == "accepted"
    toss.broker.query_error = None
    toss.broker.rows[order["broker_id"]]["execution"]["filledQuantity"] = "6"
    current = await toss.get_order(order["id"])
    assert current["filled"] == 0 and "refresh_error" in current


async def test_us_order_does_not_expire_at_korean_midnight(toss):
    order = await toss.place(request())
    saved = toss.store.get(order["id"])
    saved["date"] = "20200101"
    toss.store.put(saved)
    assert (await toss.get_order(order["id"]))["status"] == "accepted"


async def test_unknown_requires_operator_binding(toss):
    toss.broker.place_error = UncertainSubmission("unknown")
    order = await toss.place(request())
    current = await toss.get_order(order["id"])
    assert current["status"] == "unknown" and len(current["candidate_ids"]) == 1
    with pytest.raises(TradingError, match="matching"):
        await toss.resolve_order_not_submitted(order["id"])
    resolved = await toss.resolve_order(order["id"], current["candidate_ids"][0])
    assert resolved["status"] == "accepted"
    assert len(toss.broker.placed) == 1


async def test_not_submitted_is_terminal(toss):
    toss.broker.place_error = UncertainSubmission("unknown")
    order = await toss.place(request())
    toss.broker.rows.clear()
    assert (await toss.resolve_order_not_submitted(order["id"]))["status"] == "not_submitted"
    await toss.poll()
    assert (await toss.place(request()))["status"] == "not_submitted"


async def test_original_account_guard_and_mode_after_submission(toss, service, settings):
    order = await toss.place(request())
    await service.set_mode("demo")
    assert (await toss.get_order(order["id"]))["mode"] == "real"
    assert (await toss.cancel("toss-cancel-1", order["id"]))["mode"] == "real"
    settings.toss_credentials = TossCredentials("new-client", "secret", "3")
    with pytest.raises(TradingError, match="account"):
        await toss.get_order(order["id"])


async def test_replaced_order_is_reviewed_not_treated_as_cancelled(toss):
    order = await toss.place(request())
    toss.broker.rows[order["broker_id"]]["status"] = "REPLACED"
    current = await toss.get_order(order["id"])
    assert current["status"] == "needs_review" and current["cancelled"] == 0
    with pytest.raises(TradingError):
        await toss.cancel("toss-cancel-1", order["id"])


@pytest.mark.parametrize(
    "state,filled,expected",
    [
        ("FILLED", 5, "filled"),
        ("REJECTED", 2, "rejected"),
        ("NEW_UNKNOWN_STATUS", 0, "needs_review"),
    ],
)
async def test_broker_terminal_and_unknown_states(toss, state, filled, expected):
    order = await toss.place(request())
    row = toss.broker.rows[order["broker_id"]]
    row.update(
        status=state,
        execution={
            "filledQuantity": str(filled),
            "averageFilledPrice": "185.25",
            "filledAmount": str(Decimal("185.25") * filled),
        },
    )
    current = await toss.get_order(order["id"])
    assert current["status"] == expected
    if expected == "rejected":
        assert current["rejected_quantity"] == 3 and current["filled"] == 2


async def test_cancel_fill_race_resolves_not_cancelled(toss):
    order = await toss.place(request())
    await toss.cancel("toss-cancel-1", order["id"])
    toss.broker.rows[order["broker_id"]].update(
        status="FILLED",
        execution={"filledQuantity": "5", "averageFilledPrice": "185.25", "filledAmount": "926.25"},
    )
    await toss.get_order(order["id"])
    result = await toss.cancel("toss-cancel-1", order["id"])
    assert result["status"] == "not_cancelled" and result["cancelled_quantity"] == 0


async def test_toss_history_excludes_korean_orders_and_private_fields(toss):
    order = await toss.place(request())
    toss.broker.rows[order["broker_id"]]["accountNumber"] = "private"
    toss.broker.rows["korean"] = {"currency": "KRW"}
    date = order["created_at"][:10]
    result = await toss.list_orders(date, date)
    assert len(result["broker_orders"]) == 1 and len(result["mcp_orders"]) == 1
    assert "accountNumber" not in result["broker_orders"][0]
    with pytest.raises(TradingError):
        await toss.list_orders("not-a-date", date)
    with pytest.raises(TradingError):
        await toss.list_orders("2000-01-01", date)


async def test_disabled_service_and_no_capacity(toss, settings):
    settings.toss_credentials = None
    with pytest.raises(TradingError, match="not configured"):
        await toss.place(request())
    settings.toss_credentials = TossCredentials("test-client", "test-secret", "3")
    with pytest.raises(TradingError, match="capacity"):
        await toss.place(request(quantity=1000))
    with pytest.raises(TradingError, match="holdings"):
        await toss.place(request(side="sell", quantity=11))
