"""US cash order lifecycle, isolated from KIS state and reconciliation rules."""

import asyncio
import uuid
from datetime import datetime, timedelta

from .kis import KST
from .models import BrokerRejected, CancelInput, SubmissionNotSent, TradingError
from .service import TradingService, public_order
from .toss import decimal, identity, money
from .toss_fastpath import estimate_entry

TERMINAL = ("filled", "cancelled", "rejected", "not_submitted")


def whole(value):
    number = decimal(value)
    if number != number.to_integral_value():
        raise TradingError("Unexpected fractional Toss execution; state retained")
    return int(number)


def history_range(order):
    # API history filters orderedAt in KST, not US trading date. Include midnight races.
    created = datetime.fromisoformat(order["created_at"]).astimezone(KST).date()
    return (created - timedelta(days=1)).isoformat(), (created + timedelta(days=1)).isoformat()


class TossTradingService(TradingService):
    broker_name = "toss"

    def __init__(self, settings, store, broker, *, lock=None):
        super().__init__(settings, store, broker)
        self.lock = lock or asyncio.Lock()
        self.market_stream = None

    def orders(self):
        return [o for o in self.store.orders() if o.get("broker") == "toss"]

    def configured(self):
        if self.broker is None or self.settings.toss_credentials is None:
            raise TradingError("Toss broker is not configured", code="broker_not_configured")

    def active(self):
        self.configured()
        if self.store.mode() != "real":
            raise TradingError(
                "Toss supports real mode only; demo never falls back to real", code="mode_mismatch"
            )

    def check_account(self, order):
        if order.get("broker") != "toss":
            raise TradingError("Order belongs to another broker; use its tools")
        self.configured()
        if order["account_fingerprint"] != self.settings.toss_credentials.fingerprint:
            raise TradingError("Configured Toss account differs from the order's original account")

    async def status(self):
        return {
            "enabled": self.settings.toss_credentials is not None,
            "broker": "toss",
            "market": "US",
            "currency": "USD",
            "supported_modes": ["real"],
            "mode": self.store.mode(),
            "order_types": ["limit", "market"],
            "quantity_type": "whole_shares",
            "time_in_force": "DAY",
            "cancellation": "entire_remainder",
            "health": getattr(self.broker, "health", {}),
            "last_poll": self.last_poll,
            "poll_error": self.poll_error,
            "unresolved_orders": [
                o["id"]
                for o in self.orders()
                if o["status"] in ("unknown", "submitting", "needs_review")
            ],
        }

    async def account(self):
        self.active()
        return {"broker": "toss", "mode": "real", "market": "US", **await self.broker.account()}

    async def get_order(self, order_id):
        self.check_account(self.store.get(order_id))
        return await super().get_order(order_id)

    async def quote(self, symbol):
        self.active()
        return {"broker": "toss", "mode": "real", **await self.broker.quote(symbol)}

    async def capacity(self, symbol, price):
        self.active()
        return {
            "broker": "toss",
            "mode": "real",
            "symbol": symbol,
            "currency": "USD",
            "market_buy_quantity_guaranteed": False,
            **await self.broker.capacity(symbol, price),
        }

    async def calendar(self):
        self.active()
        return {
            "broker": "toss",
            "market": "US",
            "time_zone": "Asia/Seoul",
            "sessions": await self.broker.calendar(),
        }

    async def entry_cost(self, symbol, expected_move_bps, slippage_bps, max_spread_bps="25", quantity=1):
        self.active()
        if self.market_stream is None:
            raise TradingError("Fresh Toss orderbook unavailable")
        book = self.market_stream.orderbook(symbol)
        rate = await self.broker.commission_rate()
        return {"symbol": symbol, **estimate_entry(book, rate, expected_move_bps, slippage_bps, max_spread_bps=max_spread_bps, quantity=quantity)}

    @staticmethod
    def validate_guarded_quote(request, guard, check):
        if not check["allowed"]:
            raise TradingError("Expected move does not exceed Toss entry cost or spread limit")
        if request.price < decimal(check["ask"]):
            raise TradingError("Guarded buy limit is below the observed ask; immediate execution unlikely")
        if request.price > decimal(check["ask"]) * (1 + decimal(guard["slippage_bps"]) / 10000):
            raise TradingError("Guarded buy limit exceeds the slippage allowance")

    async def place(self, request, *, guard=None):
        payload = request.model_dump(mode="json")
        payload["price"] = money(request.price)
        if guard is not None:
            payload["entry_guard"] = {key: money(guard[key]) for key in ("expected_move_bps", "slippage_bps", "max_spread_bps") if key in guard}
        async with self.lock:
            previous = self.store.request(request.client_request_id, "toss_place", payload)
            if previous is not None:
                saved = self.store.get(previous["id"])
                self.check_account(saved)
                return public_order(saved)
            self.active()
            if guard is not None:
                if request.side != "buy" or request.order_type != "limit":
                    raise TradingError("Guarded Toss entry supports limit buy orders only")
                check = await self.entry_cost(request.symbol, guard["expected_move_bps"], guard["slippage_bps"], guard.get("max_spread_bps", "25"), request.quantity)
                self.validate_guarded_quote(request, guard, check)
            if any(o["status"] in ("unknown", "submitting", "needs_review") for o in self.orders()):
                raise TradingError(
                    "Resolve Toss uncertain or needs_review orders before another order"
                )
            if any(
                d["status"] in ("unknown", "submitting", "cancel_unknown", "cancel_submitting")
                for d in self.store.documents("toss_conditions")
            ):
                raise TradingError("Resolve uncertain Toss conditional order before another order")
            capacity = await self.broker.capacity(request.symbol, request.price)
            if request.side == "buy":
                # MARKET has no guaranteed cash quantity in the API. Broker performs final check.
                if decimal(capacity["cash_buy_amount"]) <= 0 or (
                    request.order_type == "limit"
                    and request.price * request.quantity > decimal(capacity["cash_buy_amount"])
                ):
                    raise TradingError("Quantity exceeds Toss cash buying capacity")
            elif request.quantity > decimal(capacity["sell_quantity"]):
                raise TradingError("Quantity exceeds Toss holdings available to sell")
            order = payload | {
                "id": str(uuid.uuid4()),
                "broker": "toss",
                "mode": "real",
                "market": "US",
                "currency": "USD",
                "exchange": "US",
                "date": datetime.now(KST).strftime("%Y%m%d"),
                "account_fingerprint": self.settings.toss_credentials.fingerprint,
                "created_at": datetime.now(KST).isoformat(),
                "broker_id": None,
                "status": "submitting",
                "filled": 0,
                "cancelled": 0,
                "rejected_quantity": 0,
                "remaining": request.quantity,
                "average_price": "0",
                "filled_amount": "0",
                "pending_cancel": None,
            }
            baseline = await self.broker.orders(*history_range(order), symbol=request.symbol)
            order["baseline_ids"] = [identity(r.get("orderId")) for r in baseline]
            if guard is not None:
                check = {"symbol": request.symbol, **estimate_entry(
                    self.market_stream.orderbook(request.symbol), check["commission_rate"],
                    guard["expected_move_bps"], guard["slippage_bps"],
                    max_spread_bps=guard.get("max_spread_bps", "25"), quantity=request.quantity,
                )}
                self.validate_guarded_quote(request, guard, check)
                order["entry_cost_check"] = check
            with self.store.transaction():
                self.store.put(order)
                self.store.save_request(
                    request.client_request_id, "toss_place", payload, {"id": order["id"]}
                )
            try:
                receipt = await self.broker.place(order)
                order.update(broker_id=identity(receipt["broker_id"]), status="accepted")
            except (BrokerRejected, SubmissionNotSent) as exc:
                order.update(
                    status="rejected",
                    remaining=0,
                    rejected_quantity=request.quantity,
                    error=str(exc),
                )
            except Exception:
                order.update(
                    status="unknown",
                    error="Toss submission outcome needs reconciliation; do not resubmit",
                )
            self.store.put(order)
            return public_order(order)

    @staticmethod
    def matches(order, row):
        return (
            row.get("currency") == "USD"
            and row.get("symbol") == order["symbol"]
            and row.get("side") == order["side"].upper()
            and row.get("orderType") == order["order_type"].upper()
            and row.get("timeInForce") == "DAY"
            and decimal(row.get("quantity")) == order["quantity"]
            and decimal(row.get("price") or "0") == decimal(order["price"])
        )

    def candidates(self, order, rows):
        matches = []
        start = datetime.fromisoformat(order["created_at"]) - timedelta(minutes=5)
        end = start + timedelta(minutes=20)
        for row in rows:
            if row.get("orderId") in order["baseline_ids"] or not self.matches(order, row):
                continue
            try:
                timestamp = datetime.fromisoformat(row["orderedAt"])
                if timestamp.tzinfo is None:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise TradingError("Invalid Toss order timestamp; state retained") from None
            if start <= timestamp <= end:
                matches.append(row)
        return matches

    async def refresh(self, order):
        self.check_account(order)
        if not order["broker_id"]:
            rows = await self.broker.orders(*history_range(order), symbol=order["symbol"])
            order.update(
                status="unknown",
                candidate_ids=[identity(r.get("orderId")) for r in self.candidates(order, rows)],
                last_checked=datetime.now(KST).isoformat(),
            )
            self.store.put(order)
            return order
        row = await self.broker.detail(order["broker_id"])
        if row.get("orderId") != order["broker_id"] or not self.matches(order, row):
            raise TradingError("Toss order identity or terms changed; state retained")
        execution = row.get("execution")
        if not isinstance(execution, dict):
            raise TradingError("Missing Toss execution; state retained")
        filled = whole(execution.get("filledQuantity"))
        amount = decimal(execution.get("filledAmount") or "0")
        average = decimal(execution.get("averageFilledPrice") or "0")
        old_filled, old_cancelled = order["filled"], order["cancelled"]
        old_amount = decimal(order["filled_amount"])
        if (
            filled < old_filled
            or filled > order["quantity"]
            or (filled > 0 and (amount <= 0 or average <= 0))
            or amount < old_amount
            or (filled > old_filled and amount <= old_amount)
        ):
            raise TradingError("Inconsistent Toss execution quantities or amount; state retained")
        broker_status = row.get("status")
        remaining = order["quantity"] - filled
        cancelled = rejected = 0
        if broker_status == "FILLED" and remaining == 0:
            status = "filled"
        elif broker_status == "CANCELED":
            status, cancelled, remaining = "cancelled", remaining, 0
        elif broker_status == "REJECTED":
            status, rejected, remaining = "rejected", remaining, 0
        elif broker_status in ("PENDING", "PARTIAL_FILLED", "PENDING_CANCEL") and remaining > 0:
            status = "partial" if filled else "accepted"
        else:
            status = "needs_review"
        if cancelled < old_cancelled or (order["status"] in TERMINAL and status not in TERMINAL):
            raise TradingError("Toss terminal state regressed; state retained")
        pending = order.get("pending_cancel")
        cancel_result = None
        if pending:
            if status in TERMINAL:
                cancel_result = {
                    "status": "cancelled" if cancelled else "not_cancelled",
                    "cancelled_quantity": cancelled,
                }
            elif pending.get("broker_id"):
                child = await self.broker.detail(pending["broker_id"])
                if child.get("orderId") != pending["broker_id"]:
                    raise TradingError("Toss cancellation identity mismatch; state retained")
                if child.get("status") == "CANCEL_REJECTED":
                    cancel_result = {"status": "rejected", "cancelled_quantity": 0}
        order.update(
            status=status,
            broker_status=broker_status,
            filled=filled,
            cancelled=cancelled,
            rejected_quantity=rejected,
            remaining=remaining,
            filled_amount=money(amount),
            average_price=money(average),
            last_checked=datetime.now(KST).isoformat(),
        )
        with self.store.transaction():
            if filled > old_filled:
                delta = filled - old_filled
                price = (amount - old_amount) / delta
                self.store.event(
                    f"{order['id']}:fill:{filled}",
                    {
                        "order_id": order["id"],
                        "broker": "toss",
                        "currency": "USD",
                        "type": "fill",
                        "quantity": delta,
                        "price": money(price),
                    },
                    self.message(
                        order, f"체결 {delta}주 / 평균 {price:,.4f} USD (누적 {filled}주)"
                    ),
                )
            if cancelled > old_cancelled:
                self.store.event(
                    f"{order['id']}:cancel:{cancelled}",
                    {
                        "order_id": order["id"],
                        "broker": "toss",
                        "type": "cancel",
                        "quantity": cancelled - old_cancelled,
                    },
                    self.message(order, f"취소 완료 {cancelled - old_cancelled}주"),
                )
            if cancel_result is not None:
                self.store.update_request(
                    pending["request_id"],
                    {
                        "order_id": order["id"],
                        "broker": "toss",
                        "mode": "real",
                        "requested_quantity": pending["quantity"],
                        "cancellation_broker_id": pending.get("broker_id"),
                        **cancel_result,
                    },
                )
                order["pending_cancel"] = None
            self.store.put(order)
        return order

    @staticmethod
    def message(order, event):
        side = "매수" if order["side"] == "buy" else "매도"
        return f"[실전 TOSS US] {order['symbol']} {side}\n{event}\n주문 {order['broker_id']}"

    async def cancel(self, client_request_id, order_id):
        request = CancelInput(
            client_request_id=client_request_id, order_id=order_id, expected_mode="real"
        )
        payload = request.model_dump(mode="json")
        async with self.lock:
            order = self.store.get(order_id)
            self.check_account(order)
            previous = self.store.request(client_request_id, "toss_cancel", payload)
            if previous is not None:
                return previous
            if not order["broker_id"] or order["status"] in ("unknown", "submitting", *TERMINAL):
                raise TradingError("Toss order has no cancellable broker identity")
            order = await self.refresh(order)
            if order.get("pending_cancel"):
                raise TradingError("Previous Toss cancellation is still being reconciled")
            if order["remaining"] <= 0 or order["broker_status"] not in (
                "PENDING",
                "PARTIAL_FILLED",
            ):
                raise TradingError("Toss order is not currently cancellable; state retained")
            result = {
                "order_id": order_id,
                "broker": "toss",
                "mode": "real",
                "status": "unknown",
                "requested_quantity": order["remaining"],
            }
            order["pending_cancel"] = {
                "request_id": client_request_id,
                "quantity": order["remaining"],
            }
            with self.store.transaction():
                self.store.put(order)
                self.store.save_request(client_request_id, "toss_cancel", payload, result)
            try:
                receipt = await self.broker.cancel(order)
                broker_id = identity(receipt["broker_id"])
                result.update(status="accepted", cancellation_broker_id=broker_id)
                order["pending_cancel"]["broker_id"] = broker_id
            except (BrokerRejected, SubmissionNotSent) as exc:
                result.update(status="rejected", error=str(exc))
                order["pending_cancel"] = None
            except Exception:
                result["status"] = "unknown"
            with self.store.transaction():
                self.store.put(order)
                self.store.update_request(client_request_id, result)
            return result

    async def resolve_order(self, order_id, broker_id):
        identity(broker_id)
        async with self.lock:
            order = self.store.get(order_id)
            self.check_account(order)
            if order["broker_id"] == broker_id:
                return public_order(order)
            if order["broker_id"] or order["status"] not in ("unknown", "submitting"):
                raise TradingError("Toss order is not awaiting submission resolution")
            rows = await self.broker.orders(*history_range(order), symbol=order["symbol"])
            matches = [r for r in self.candidates(order, rows) if r.get("orderId") == broker_id]
            if len(matches) != 1 or any(o["broker_id"] == broker_id for o in self.orders()):
                raise TradingError("Toss broker ID does not uniquely match an unbound order")
            order.update(
                broker_id=broker_id, status="accepted", resolution="explicit_operator_binding"
            )
            order.pop("candidate_ids", None)
            order.pop("error", None)
            self.store.put(order)
            return public_order(await self.refresh(order))

    async def resolve_order_not_submitted(self, order_id):
        async with self.lock:
            order = self.store.get(order_id)
            self.check_account(order)
            if order["status"] == "not_submitted":
                return public_order(order)
            if order["broker_id"] or order["status"] not in ("unknown", "submitting"):
                raise TradingError("Toss order is not awaiting submission resolution")
            rows = await self.broker.orders(*history_range(order), symbol=order["symbol"])
            if self.candidates(order, rows):
                raise TradingError(
                    "A matching Toss order exists; verify it and use toss_resolve_order"
                )
            order.update(
                status="not_submitted",
                remaining=0,
                resolution="explicit_operator_no_broker_order",
                resolved_at=datetime.now(KST).isoformat(),
            )
            order.pop("candidate_ids", None)
            order.pop("error", None)
            self.store.put(order)
            return public_order(order)

    async def list_orders(self, start_date, end_date):
        self.active()
        try:
            start, end = [datetime.strptime(s, "%Y-%m-%d").date() for s in (start_date, end_date)]
        except ValueError:
            raise TradingError("Dates must be YYYY-MM-DD (KST)") from None
        if (
            not datetime.now(KST).date() - timedelta(days=89)
            <= start
            <= end
            <= datetime.now(KST).date()
        ):
            raise TradingError("Date range must be within the last 90 calendar days")
        rows = await self.broker.orders(start.isoformat(), end.isoformat())
        fields = (
            "orderId",
            "symbol",
            "side",
            "orderType",
            "timeInForce",
            "status",
            "quantity",
            "price",
            "currency",
            "orderedAt",
            "canceledAt",
        )
        execution_fields = (
            "filledQuantity",
            "averageFilledPrice",
            "filledAmount",
            "filledAt",
            "settlementDate",
        )
        return {
            "broker": "toss",
            "mode": "real",
            "market": "US",
            "date_basis": "orderedAt_KST",
            "broker_orders": [
                {
                    **{k: r.get(k) for k in fields},
                    "execution": {k: r.get("execution", {}).get(k) for k in execution_fields},
                }
                for r in rows
                if r.get("currency") == "USD"
            ],
            "mcp_orders": [
                public_order(o)
                for o in self.orders()
                if start.strftime("%Y%m%d") <= o["date"] <= end.strftime("%Y%m%d")
            ],
        }
