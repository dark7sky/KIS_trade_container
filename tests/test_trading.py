import asyncio

import pytest
from filelock import Timeout
from pydantic import ValidationError

from kis_mcp.config import Credentials
from kis_mcp.models import (
    BrokerRejected,
    CancelInput,
    OrderInput,
    SubmissionNotSent,
    TradingError,
    UncertainSubmission,
)
from kis_mcp.store import Store


def buy(**changes):
    return OrderInput(
        **(
            {
                "client_request_id": "buy-request-1",
                "symbol": "005930",
                "side": "buy",
                "quantity": 10,
                "price": "1000",
            }
            | changes
        )
    )


async def test_mode_persistence_and_corruption(settings):
    store = Store(settings.data_dir)
    assert store.mode() == "real"
    store.set_mode("demo")
    with pytest.raises(Timeout):
        Store(settings.data_dir)
    store.close()
    restored = Store(settings.data_dir)
    assert restored.mode() == "demo"
    restored.db.execute("UPDATE meta SET value='broken' WHERE key='mode'")
    restored.close()
    with pytest.raises(RuntimeError, match="Persisted mode"):
        Store(settings.data_dir)


async def test_deleted_mode_not_defaulted(settings):
    store = Store(settings.data_dir)
    store.db.execute("DELETE FROM meta WHERE key='mode'")
    store.close()
    with pytest.raises(RuntimeError):
        Store(settings.data_dir)


async def test_idempotency_concurrency_and_mode_binding(service, broker):
    orders = await asyncio.gather(service.place(buy()), service.place(buy()))
    assert orders[0]["id"] == orders[1]["id"]
    assert len(broker.placed) == 1
    await service.set_mode("demo")
    retry = await service.place(buy(price="1000.00"))
    assert retry["mode"] == "real"
    cancelled = await service.cancel(
        CancelInput(client_request_id="cancel-request-1", order_id=retry["id"])
    )
    assert cancelled["mode"] == "real"
    assert broker.cancelled[0][0]["mode"] == "real"
    with pytest.raises(TradingError, match="different input"):
        await service.place(buy(quantity=9))


async def test_demo_nxt_rejected_without_io(service, broker):
    await service.set_mode("demo")
    with pytest.raises(TradingError, match="Demo supports KRX"):
        await service.place(buy(exchange="NXT"))
    assert not broker.placed


async def test_real_nxt_is_explicit(service, broker):
    order = await service.place(buy(exchange="NXT"))
    assert order["exchange"] == "NXT"
    assert broker.placed[0]["exchange"] == "NXT"


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity": 0},
        {"quantity": True},
        {"quantity": 1.5},
        {"price": "NaN"},
        {"price": "1.2"},
        {"price": "0"},
        {"order_type": "market", "price": "10"},
        {"symbol": "INVALID"},
    ],
)
def test_invalid_order(changes):
    with pytest.raises(ValidationError):
        buy(**changes)


@pytest.mark.parametrize(
    "capacity",
    [
        {"cash_buy_amount": "9000", "cash_buy_quantity": 100, "sell_quantity": 100},
        {"cash_buy_amount": "100000", "cash_buy_quantity": 9, "sell_quantity": 100},
    ],
)
async def test_cash_only_capacity(service, broker, capacity):
    broker.capacity_result = capacity
    with pytest.raises(TradingError, match="cash buying"):
        await service.place(buy())
    assert not broker.placed


async def test_sell_limit(service, broker):
    broker.capacity_result["sell_quantity"] = 1
    with pytest.raises(TradingError, match="cash holdings"):
        await service.place(buy(side="sell"))


async def test_uncertain_order_not_resubmitted_or_auto_bound(service, broker):
    broker.place_error = UncertainSubmission("test")
    order = await service.place(buy())
    assert order["status"] == "unknown"
    assert (await service.place(buy()))["id"] == order["id"]
    assert len(broker.placed) == 1
    polled = await service.get_order(order["id"])
    assert polled["candidate_ids"] == ["1"]
    assert polled["broker_id"] is None
    with pytest.raises(TradingError, match="uncertain submission"):
        await service.place(buy(client_request_id="another-request"))
    resolved = await service.resolve_order(order["id"], "1")
    assert resolved["broker_id"] == "1"
    assert len(broker.placed) == 1


async def test_operator_can_resolve_unknown_order_as_not_submitted(service, broker):
    broker.place_error = UncertainSubmission("test")
    order = await service.place(buy())
    broker.rows["real"].clear()

    resolved = await service.resolve_order_not_submitted(order["id"])

    assert resolved["status"] == "not_submitted"
    assert resolved["remaining"] == 0
    assert resolved["broker_remaining"] == 0
    assert resolved["resolution"] == "explicit_operator_no_broker_order"
    assert "error" not in resolved
    assert (await service.status())["unresolved_orders"] == []
    assert (await service.place(buy()))["status"] == "not_submitted"
    assert len(broker.placed) == 1

    broker.place_error = None
    fresh = await service.place(buy(client_request_id="new-request-2"))
    assert fresh["status"] == "accepted"
    assert len(broker.placed) == 2


async def test_not_submitted_resolution_refuses_matching_broker_candidate(service, broker):
    broker.place_error = UncertainSubmission("test")
    order = await service.place(buy())

    with pytest.raises(TradingError, match="matching broker order"):
        await service.resolve_order_not_submitted(order["id"])

    assert service.store.get(order["id"])["status"] == "unknown"


async def test_not_submitted_resolution_requires_uncertain_state(service, broker):
    order = await service.place(buy())

    with pytest.raises(TradingError, match="not awaiting submission resolution"):
        await service.resolve_order_not_submitted(order["id"])


async def test_not_submitted_resolution_preserves_unknown_on_query_failure(service, broker):
    broker.place_error = UncertainSubmission("test")
    order = await service.place(buy())
    broker.rows["real"].clear()
    broker.query_error = TradingError("query unavailable")

    with pytest.raises(TradingError, match="query unavailable"):
        await service.resolve_order_not_submitted(order["id"])

    assert service.store.get(order["id"])["status"] == "unknown"


async def test_not_submitted_resolution_is_terminal_and_idempotent(service, broker):
    broker.place_error = UncertainSubmission("test")
    order = await service.place(buy())
    broker.rows["real"].clear()
    await service.resolve_order_not_submitted(order["id"])
    broker.query_error = TradingError("must not query")

    assert (await service.resolve_order_not_submitted(order["id"]))["status"] == "not_submitted"
    assert (await service.get_order(order["id"]))["status"] == "not_submitted"
    await service.poll()
    assert service.poll_error is None


async def test_not_submitted_resolution_survives_restart(settings, broker):
    from kis_mcp.service import TradingService

    store = Store(settings.data_dir)
    service = TradingService(settings, store, broker)
    broker.place_error = UncertainSubmission("test")
    order = await service.place(buy())
    broker.rows["real"].clear()
    await service.resolve_order_not_submitted(order["id"])
    store.close()

    restored_store = Store(settings.data_dir)
    try:
        restored = TradingService(settings, restored_store, broker)
        broker.query_error = TradingError("must not query")
        assert (await restored.get_order(order["id"]))["status"] == "not_submitted"
        await restored.poll()
        assert restored.poll_error is None
    finally:
        restored_store.close()


async def test_rejected_order_no_notification(service, broker):
    broker.place_error = BrokerRejected("Rejected")
    order = await service.place(buy())
    assert order["status"] == "rejected"
    assert service.store.pending_count() == 0


async def test_definitely_unsent_order_does_not_lock_account(service, broker):
    broker.place_error = SubmissionNotSent("Order was not sent")
    order = await service.place(buy())
    assert order["status"] == "rejected"
    broker.place_error = None
    assert (await service.place(buy(client_request_id="new-request-2")))["status"] == "accepted"


async def test_fills_cancels_and_restart_no_duplicates(service, broker):
    order = await service.place(buy())
    assert service.store.pending_count() == 0
    row = broker.rows["real"][0]
    row.update(tot_ccld_qty="3", tot_ccld_amt="3000", avg_prvs="1000", rmn_qty="7")
    assert (await service.get_order(order["id"]))["status"] == "partial"
    assert service.store.pending_count() == 1
    await service.poll()
    assert service.store.pending_count() == 1
    # External HTS cancellation of the remaining quantity of an MCP order.
    row.update(cncl_yn="Y", rmn_qty="0")
    final = await service.get_order(order["id"])
    assert final["status"] == "cancelled" and final["cancelled"] == 7
    assert service.store.pending_count() == 2
    await service.poll()
    assert service.store.pending_count() == 2


async def test_state_and_outbox_recover(settings, broker):
    from kis_mcp.service import TradingService

    store = Store(settings.data_dir)
    service = TradingService(settings, store, broker)
    await service.set_mode("demo")
    order = await service.place(buy())
    store.close()
    store = Store(settings.data_dir)
    try:
        restored = TradingService(settings, store, broker)
        broker.rows["demo"][0].update(
            tot_ccld_qty="10", rmn_qty="0", avg_prvs="1000", tot_ccld_amt="10000"
        )
        await restored.poll()
        assert store.get(order["id"])["status"] == "filled"
        assert store.mode() == "demo"
        assert store.pending_count() == 1
        await restored.poll()
        assert store.pending_count() == 1
    finally:
        store.close()


async def test_cancel_timeout_dedup_and_fill_race(service, broker):
    order = await service.place(buy())
    broker.cancel_error = UncertainSubmission("test")
    cancel = CancelInput(client_request_id="cancel-request-1", order_id=order["id"])
    assert (await service.cancel(cancel))["status"] == "unknown"
    await service.cancel(cancel)
    assert len(broker.cancelled) == 1
    with pytest.raises(TradingError, match="still being reconciled"):
        await service.cancel(cancel.model_copy(update={"client_request_id": "cancel-request-2"}))
    broker.rows["real"][0].update(
        tot_ccld_qty="10", rmn_qty="0", avg_prvs="1000", tot_ccld_amt="10000"
    )
    await service.poll()
    assert (await service.cancel(cancel))["status"] == "not_cancelled"
    assert service.store.pending_count() == 1  # fill only


async def test_cancel_success_is_not_notified_until_observed(service, broker):
    order = await service.place(buy())
    cancel = CancelInput(client_request_id="cancel-request-1", order_id=order["id"], quantity=4)
    assert (await service.cancel(cancel))["status"] == "accepted"
    assert service.store.pending_count() == 0
    broker.rows["real"][0].update(cncl_yn="Y", rmn_qty="6")
    await service.poll()
    assert (await service.cancel(cancel))["cancelled_quantity"] == 4
    assert service.store.pending_count() == 1


async def test_cancel_child_receipt_confirms_cancellation_when_parent_flag_is_unset(service, broker):
    order = await service.place(buy(side="sell"))
    cancel = CancelInput(client_request_id="cancel-request-1", order_id=order["id"])
    assert (await service.cancel(cancel))["status"] == "accepted"

    # KIS can mark only the cancellation child as cancelled. The receipt binds
    # that child to this request even when the daily-history relation fields are absent.
    broker.rows["real"][0].update(cncl_yn="N", rmn_qty="0")
    broker.rows["real"].append(
        {
            "odno": "cancel-1",
            "orgn_odno": "",
            "rvse_cncl_dvsn_cd": "",
            "cncl_yn": "Y",
            "rjct_qty": "0",
        }
    )

    final = await service.get_order(order["id"])

    assert final["status"] == "cancelled"
    assert final["cancelled"] == 10
    assert (await service.cancel(cancel))["cancellation_broker_id"] == "cancel-1"
    assert (await service.cancel(cancel))["cancelled_quantity"] == 10


async def test_refresh_reuses_persisted_cancel_receipt_for_legacy_needs_review_order(
    service, broker
):
    order = await service.place(buy(side="sell"))
    cancel = CancelInput(client_request_id="cancel-request-1", order_id=order["id"])
    assert (await service.cancel(cancel))["status"] == "accepted"

    legacy = service.store.get(order["id"])
    legacy.update(status="needs_review", cancelled=0, remaining=0, pending_cancel=None)
    service.store.put(legacy)
    broker.rows["real"][0].update(cncl_yn="N", rmn_qty="0")
    broker.rows["real"].append(
        {
            "odno": "cancel-1",
            "orgn_odno": "",
            "rvse_cncl_dvsn_cd": "",
            "cncl_yn": "Y",
            "rjct_qty": "0",
        }
    )

    final = await service.get_order(order["id"])

    assert final["status"] == "cancelled"
    assert final["cancelled"] == 10


async def test_refresh_resyncs_unique_legacy_cancel_child_without_receipt(service, broker):
    order = await service.place(buy(side="sell"))
    legacy = service.store.get(order["id"])
    legacy.update(status="needs_review", cancelled=0, remaining=0, pending_cancel=None)
    service.store.put(legacy)
    broker.rows["real"][0].update(cncl_yn="N", rmn_qty="0")
    broker.rows["real"].append(
        {
            "odno": "historic-cancel",
            "orgn_odno": "",
            "rvse_cncl_dvsn_cd": "",
            "cncl_yn": "Y",
            "rjct_qty": "0",
            "pdno": "005930",
            "sll_buy_dvsn_cd": "01",
            "ord_qty": "10",
        }
    )

    final = await service.get_order(order["id"])

    assert final["status"] == "cancelled"
    assert final["cancelled"] == 10


async def test_refresh_keeps_ambiguous_legacy_cancel_child_in_review(service, broker):
    order = await service.place(buy(side="sell"))
    legacy = service.store.get(order["id"])
    legacy.update(status="needs_review", cancelled=0, remaining=0, pending_cancel=None)
    service.store.put(legacy)
    broker.rows["real"][0].update(cncl_yn="N", rmn_qty="0")
    for broker_id in ("historic-cancel-1", "historic-cancel-2"):
        broker.rows["real"].append(
            {
                "odno": broker_id,
                "orgn_odno": "",
                "rvse_cncl_dvsn_cd": "",
                "cncl_yn": "Y",
                "rjct_qty": "0",
                "pdno": "005930",
                "sll_buy_dvsn_cd": "01",
                "ord_qty": "10",
            }
        )

    final = await service.get_order(order["id"])

    assert final["status"] == "needs_review"
    assert final["cancelled"] == 0


async def test_query_error_preserves_state(service, broker):
    order = await service.place(buy())
    broker.query_error = TradingError("query unavailable")
    result = await service.get_order(order["id"])
    assert result["status"] == "accepted" and "refresh_error" in result
    assert service.store.pending_count() == 0


async def test_account_change_blocks_old_orders(service, settings):
    order = await service.place(buy())
    settings.credentials["real"] = Credentials("k", "s", "33333333", "01")
    result = await service.get_order(order["id"])
    assert "original account" in result["refresh_error"]
    with pytest.raises(TradingError):
        await service.cancel(
            CancelInput(client_request_id="cancel-request-1", order_id=order["id"])
        )


async def test_no_account_secrets_in_public_order(service, settings):
    result = await service.place(buy())
    text = str(result)
    assert settings.credentials["real"].account not in text
    assert "account_fingerprint" not in result and "baseline_ids" not in result
