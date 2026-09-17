from datetime import datetime, timedelta

import pytest

from kis_mcp.kis import KST
from kis_mcp.models import CancelInput, OrderInput, TradingError
from kis_mcp.service import TradingService
from kis_mcp.store import Store


async def historical_order(service, broker, *, mode="demo", exchange="KRX", filled=0,
                           remaining=10, age=1, order_type="limit"):
    await service.set_mode(mode)
    public = await service.place(OrderInput(
        client_request_id="expiry-order-1", symbol="005930", side="sell", quantity=10,
        price="1000" if order_type == "limit" else "0", order_type=order_type,
        exchange=exchange,
    ))
    order = service.store.get(public["id"])
    date = (datetime.now(KST) - timedelta(days=age)).strftime("%Y%m%d")
    order["date"] = date
    service.store.put(order)
    row = broker.rows[mode][0]
    row.update(ord_dt=date, tot_ccld_qty=str(filled), rmn_qty=str(remaining),
               avg_prvs="1000" if filled else "0", tot_ccld_amt=str(1000 * filled))
    return order, row


@pytest.mark.parametrize("mode,exchange", [("demo", "KRX"), ("real", "KRX"), ("real", "NXT")])
@pytest.mark.parametrize("order_type", ["limit", "market"])
@pytest.mark.parametrize("filled", [0, 3])
async def test_previous_day_remainder_expires(service, broker, mode, exchange, order_type, filled):
    order, _ = await historical_order(service, broker, mode=mode, exchange=exchange,
                                     filled=filled, remaining=10-filled, order_type=order_type)
    result = await service.get_order(order["id"])
    assert result["status"] == "expired"
    assert result["filled"] == filled
    assert result["expired"] == 10 - filled
    assert result["remaining"] == 0
    assert result["broker_remaining"] == 10 - filled
    assert result["cancelled"] == 0
    assert service.store.pending_count() == bool(filled)  # no cancellation notification
    with pytest.raises(TradingError, match="remainder"):
        await service.cancel(CancelInput(client_request_id="expiry-cancel-1", order_id=order["id"]))
    assert not broker.cancelled


async def test_today_is_not_expired_from_sell_capacity(service, broker):
    order, _ = await historical_order(service, broker, age=0)
    result = await service.get_order(order["id"])
    assert result["status"] == "accepted"
    assert result["remaining"] == 10


@pytest.mark.parametrize("remaining,flag,status,expired,cancelled", [
    (0, "N", "needs_review", 0, 0),  # missing disposition is not proof of expiry
    (0, "Y", "cancelled", 0, 10),
    (6, "Y", "expired", 6, 4),
])
async def test_expiry_preserves_cancellation_evidence(service, broker, remaining, flag,
                                                     status, expired, cancelled):
    order, row = await historical_order(service, broker, remaining=remaining)
    row["cncl_yn"] = flag
    result = await service.get_order(order["id"])
    assert (result["status"], result["expired"], result["cancelled"]) == (status, expired, cancelled)


async def test_query_failure_does_not_finalize_expiry(service, broker):
    order, _ = await historical_order(service, broker)
    broker.query_error = TradingError("unavailable")
    result = await service.get_order(order["id"])
    assert result["status"] == "accepted"
    assert "refresh_error" in result


async def test_unexplained_quantity_gap_does_not_finalize_expiry(service, broker):
    order, _ = await historical_order(service, broker, remaining=6)
    result = await service.get_order(order["id"])
    assert result["status"] == "accepted"
    assert "refresh_error" in result


async def test_expired_order_reconciles_late_fill_after_restart(service, broker, settings):
    order, row = await historical_order(service, broker, filled=3, remaining=7)
    await service.poll()
    assert service.store.get(order["id"])["status"] == "expired"
    service.store.close()
    service.store = Store(settings.data_dir)
    restored = TradingService(settings, service.store, broker)
    row.update(tot_ccld_qty="4", tot_ccld_amt="4000", rmn_qty="6")
    await restored.poll()
    result = await restored.get_order(order["id"])
    assert (result["status"], result["filled"], result["expired"], result["remaining"]) == (
        "expired", 4, 6, 0)
    assert service.store.pending_count() == 2
    await restored.poll()
    assert service.store.pending_count() == 2


async def test_final_fill_takes_precedence_over_expiry(service, broker):
    order, _ = await historical_order(service, broker, filled=10, remaining=0)
    result = await service.get_order(order["id"])
    assert (result["status"], result["expired"], result["remaining"]) == ("filled", 0, 0)


async def test_unknown_identity_is_not_expired(service, broker):
    order, _ = await historical_order(service, broker)
    order.update(broker_id=None, status="unknown")
    service.store.put(order)
    assert (await service.get_order(order["id"]))["status"] == "unknown"


async def test_other_date_cannot_finalize_expiry(service, broker):
    order, row = await historical_order(service, broker)
    row["ord_dt"] = datetime.now(KST).strftime("%Y%m%d")
    result = await service.get_order(order["id"])
    assert result["status"] == "accepted"
    assert "refresh_error" in result


async def test_pending_cancel_is_not_reported_successful_due_to_expiry(service, broker):
    order, row = await historical_order(service, broker, age=0)
    request = CancelInput(client_request_id="expiry-cancel-1", order_id=order["id"])
    await service.cancel(request)
    order = service.store.get(order["id"])
    order["date"] = (datetime.now(KST) - timedelta(days=1)).strftime("%Y%m%d")
    row["ord_dt"] = order["date"]
    service.store.put(order)
    result = await service.get_order(order["id"])
    assert result["status"] == "expired"
    assert result["pending_cancel"] is not None
    assert (await service.cancel(request))["status"] == "accepted"
    assert service.store.pending_count() == 0
    row.update(cncl_yn="Y", rmn_qty="0")
    final = await service.get_order(order["id"])
    assert (final["status"], final["cancelled"], final["expired"]) == ("cancelled", 10, 0)
    assert (await service.cancel(request))["cancelled_quantity"] == 10


async def test_list_keeps_expired_snapshot(service, broker):
    order, _ = await historical_order(service, broker)
    await service.poll()
    date = datetime.strptime(order["date"], "%Y%m%d").strftime("%Y-%m-%d")
    result = await service.list_orders(date, date)
    assert result["mcp_orders"][0]["status"] == "expired"
    assert result["mcp_orders"][0]["remaining"] == 0
    assert result["broker_orders"][0]["rmn_qty"] == "10"
