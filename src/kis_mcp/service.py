import asyncio
import uuid
from datetime import datetime, timedelta

from .kis import KST, integer, number, today
from .models import BrokerRejected, SubmissionNotSent, TradingError, UncertainSubmission

PUBLIC_ORDER_FIELDS = (
    "id",
    "client_request_id",
    "mode",
    "exchange",
    "symbol",
    "side",
    "quantity",
    "price",
    "order_type",
    "date",
    "broker_id",
    "status",
    "filled",
    "cancelled",
    "remaining",
    "average_price",
    "created_at",
    "last_checked",
    "candidate_ids",
    "pending_cancel",
    "error",
)


def public_order(order):
    return {k: order[k] for k in PUBLIC_ORDER_FIELDS if k in order}


def same_id(left, right):
    return str(left).lstrip("0") == str(right).lstrip("0")


class TradingService:
    def __init__(self, settings, store, broker):
        self.settings, self.store, self.broker = settings, store, broker
        self.lock = asyncio.Lock()
        self.last_poll = None
        self.poll_error = None

    def check_account(self, order):
        if self.settings.credentials[order["mode"]].fingerprint != order["account_fingerprint"]:
            raise TradingError(
                "Configured account differs from the order's original account; restore its configuration"
            )

    def check_exchange(self, mode, exchange):
        if mode == "demo" and exchange != "KRX":
            raise TradingError(
                "Demo supports KRX only; no real-account or exchange fallback is allowed"
            )

    async def status(self):
        return {
            "mode": self.store.mode(),
            "scope": "server_global_persistent",
            "broker": self.broker.health,
            "supported_exchanges": {"real": ["KRX", "NXT"], "demo": ["KRX"]},
            "order_types": ["limit", "market"],
            "execution": "immediate",
            "cash_only": True,
            "last_poll": self.last_poll,
            "poll_error": self.poll_error,
            "pending_notifications": self.store.pending_count(),
            "unresolved_orders": [
                o["id"] for o in self.store.orders() if o["status"] in ("unknown", "submitting")
            ],
        }

    async def set_mode(self, mode):
        async with self.lock:
            self.settings.credentials[mode].validate()
            self.store.set_mode(mode)
            return {"mode": mode, "scope": "server_global_persistent"}

    async def account(self, exchange):
        mode = self.store.mode()
        self.check_exchange(mode, exchange)
        rows, summary = await self.broker.balance(mode, exchange)
        fields = (
            "pdno",
            "prdt_name",
            "hldg_qty",
            "ord_psbl_qty",
            "pchs_avg_pric",
            "prpr",
            "evlu_amt",
            "evlu_pfls_amt",
            "evlu_pfls_rt",
        )
        summary_fields = (
            "dnca_tot_amt",
            "nxdy_excc_amt",
            "prvs_rcdl_excc_amt",
            "scts_evlu_amt",
            "tot_evlu_amt",
            "evlu_pfls_smtl_amt",
        )
        return {
            "mode": mode,
            "exchange": exchange,
            "holdings": [{k: r.get(k) for k in fields} for r in rows],
            "summary": [{k: r.get(k) for k in summary_fields} for r in summary],
        }

    async def quote(self, symbol, exchange):
        mode = self.store.mode()
        self.check_exchange(mode, exchange)
        return {"mode": mode} | await self.broker.quote(mode, symbol, exchange)

    async def capacity(self, symbol, price, exchange):
        mode = self.store.mode()
        self.check_exchange(mode, exchange)
        return {"mode": mode, "symbol": symbol, "exchange": exchange} | await self.broker.capacity(
            mode, symbol, price, exchange
        )

    async def place(self, request):
        payload = request.model_dump(mode="json")
        # Decimal's lexical form must not change retry identity.
        payload["price"] = str(int(request.price))
        async with self.lock:
            previous = self.store.request(request.client_request_id, "place", payload)
            if previous is not None:
                return public_order(self.store.get(previous["id"]))
            mode = self.store.mode()
            self.check_exchange(mode, request.exchange)
            if any(
                o["mode"] == mode and o["status"] in ("unknown", "submitting")
                for o in self.store.orders()
            ):
                raise TradingError(
                    "Resolve the account's uncertain submission before creating another order"
                )
            capacity = await self.broker.capacity(
                mode, request.symbol, request.price, request.exchange
            )
            if request.side == "buy":
                if request.quantity > capacity["cash_buy_quantity"] or (
                    request.order_type == "limit"
                    and request.price * request.quantity > number(capacity["cash_buy_amount"])
                ):
                    raise TradingError("Quantity exceeds non-margin cash buying capacity")
            elif request.quantity > capacity["sell_quantity"]:
                raise TradingError("Quantity exceeds cash holdings available to sell")
            date = today()
            baseline = await self.broker.daily(mode, date, date, request.exchange)
            order = payload | {
                "id": str(uuid.uuid4()),
                "mode": mode,
                "account_fingerprint": self.settings.credentials[mode].fingerprint,
                "date": date,
                "created_at": datetime.now(KST).isoformat(),
                "broker_id": None,
                "org": None,
                "baseline_ids": [str(r.get("odno", "")) for r in baseline],
                "status": "submitting",
                "filled": 0,
                "cancelled": 0,
                "remaining": request.quantity,
                "average_price": "0",
                "filled_amount": "0",
                "pending_cancel": None,
            }
            with self.store.transaction():
                self.store.put(order)
                self.store.save_request(
                    request.client_request_id, "place", payload, {"id": order["id"]}
                )
            try:
                receipt = await self.broker.place(order)
                order.update(receipt)
                order["status"] = "accepted"
            except (BrokerRejected, SubmissionNotSent) as exc:
                order.update(status="rejected", remaining=0, error=str(exc))
            except (UncertainSubmission, TradingError):
                order.update(
                    status="unknown",
                    error="Submission outcome needs reconciliation; do not resubmit",
                )
            except Exception:
                order.update(
                    status="unknown",
                    error="Submission outcome needs reconciliation; do not resubmit",
                )
            self.store.put(order)
            return public_order(order)

    def candidates(self, order, rows):
        result = []
        for row in rows:
            if any(same_id(row.get("odno", ""), old) for old in order["baseline_ids"]):
                continue
            if row.get("pdno") != order["symbol"] or row.get("sll_buy_dvsn_cd") != (
                "02" if order["side"] == "buy" else "01"
            ):
                continue
            if row.get("rvse_cncl_dvsn_cd", "00") not in ("", "00"):
                continue
            if integer(row.get("ord_qty", "0")) != order["quantity"] or number(
                row.get("ord_unpr", "0")
            ) != number(order["price"]):
                continue
            if row.get("ord_dt", order["date"]) != order["date"]:
                continue
            result.append(row)
        return result

    async def refresh(self, order):
        self.check_account(order)
        rows = await self.broker.daily(
            order["mode"], order["date"], order["date"], order["exchange"]
        )
        if not order["broker_id"]:
            order["status"] = "unknown"
            order["candidate_ids"] = [str(r["odno"]) for r in self.candidates(order, rows)]
            order["last_checked"] = datetime.now(KST).isoformat()
            self.store.put(order)
            # KIS has no client idempotency key: even one matching external order is
            # not proof of ownership. Only an explicit resolve_order may bind it.
            return order
        matches = [r for r in rows if same_id(r.get("odno", ""), order["broker_id"])]
        if len(matches) != 1:
            raise TradingError("Original broker order not uniquely found; state retained")
        row = matches[0]
        filled = integer(row["tot_ccld_qty"])
        remaining = integer(row["rmn_qty"])
        rejected = integer(row.get("rjct_qty") or "0")
        if filled < order["filled"] or filled + remaining + rejected > order["quantity"]:
            raise TradingError("Inconsistent broker quantities; state retained")
        cancelled = order["cancelled"]
        if row.get("cncl_yn") == "Y":
            cancelled = max(cancelled, order["quantity"] - filled - remaining - rejected)
        if row.get("cncl_qty") not in (None, ""):
            cancelled = max(cancelled, integer(row["cncl_qty"]))
        # Partial cancellations may be represented only as successful child orders.
        # A cancellation receipt is an authoritative link when KIS omits the
        # parent-link/classification fields from that child in daily history.
        pending_cancel_broker_id = (order.get("pending_cancel") or {}).get("broker_id")
        cancellation_broker_ids = {
            *self.store.cancellation_broker_ids(order["id"]),
            *([pending_cancel_broker_id] if pending_cancel_broker_id else []),
        }
        legacy_cancel_quantity = order["quantity"] - filled - rejected
        legacy_cancel_children = [
            child
            for child in rows
            if order["status"] == "needs_review"
            and child.get("cncl_yn") == "Y"
            and integer(child.get("rjct_qty") or "0") == 0
            and child.get("pdno") == order["symbol"]
            and child.get("sll_buy_dvsn_cd") == ("02" if order["side"] == "buy" else "01")
            and integer(child.get("ord_qty") or "0") == legacy_cancel_quantity
        ]
        for child in rows:
            is_linked_cancel_child = (
                same_id(child.get("orgn_odno", ""), order["broker_id"])
                and child.get("rvse_cncl_dvsn_cd") == "02"
            )
            is_receipted_cancel_child = any(
                same_id(child.get("odno", ""), broker_id)
                for broker_id in cancellation_broker_ids
            )
            if (
                (
                    is_linked_cancel_child
                    or is_receipted_cancel_child
                    or (len(legacy_cancel_children) == 1 and child is legacy_cancel_children[0])
                )
                and integer(child.get("rjct_qty") or "0") == 0
                and child.get("cncl_yn") == "Y"
            ):
                cancelled = max(cancelled, order["quantity"] - filled - remaining - rejected)
        if filled + cancelled > order["quantity"]:
            raise TradingError("Inconsistent cancellation quantities; state retained")
        average = number(row.get("avg_prvs") or "0")
        amount = (
            number(row["tot_ccld_amt"])
            if row.get("tot_ccld_amt") not in (None, "")
            else average * filled
        )
        old_filled, old_cancelled = order["filled"], order["cancelled"]
        old_amount = number(order["filled_amount"])
        if filled > old_filled and (amount < old_amount or amount <= 0):
            raise TradingError("Invalid fill amount; state retained")
        order.update(
            filled=filled,
            cancelled=cancelled,
            remaining=remaining,
            average_price=str(average),
            filled_amount=str(amount),
            last_checked=datetime.now(KST).isoformat(),
        )
        order["status"] = (
            "filled"
            if filled == order["quantity"]
            else "cancelled"
            if filled + cancelled == order["quantity"]
            else "rejected"
            if rejected == order["quantity"]
            else "partial"
            if filled
            else "accepted"
        )
        if remaining == 0 and filled + cancelled + rejected != order["quantity"]:
            order["status"] = "needs_review"
        with self.store.transaction():
            if filled > old_filled:
                delta = filled - old_filled
                event_price = (amount - old_amount) / delta
                event_id = f"{order['id']}:fill:{filled}"
                self.store.event(
                    event_id,
                    {
                        "order_id": order["id"],
                        "type": "fill",
                        "quantity": delta,
                        "price": str(event_price),
                    },
                    self.message(
                        order, f"체결 {delta}주 / 평균 {event_price:,.2f}원 (누적 {filled}주)"
                    ),
                )
            if cancelled > old_cancelled:
                delta = cancelled - old_cancelled
                self.store.event(
                    f"{order['id']}:cancel:{cancelled}",
                    {"order_id": order["id"], "type": "cancel", "quantity": delta},
                    self.message(order, f"취소 완료 {delta}주"),
                )
            pending = order.get("pending_cancel")
            if pending and (
                cancelled - pending["baseline_cancelled"] >= pending["quantity"] or remaining == 0
            ):
                result = {
                    "order_id": order["id"],
                    "mode": order["mode"],
                    "exchange": order["exchange"],
                    "status": "cancelled"
                    if cancelled > pending["baseline_cancelled"]
                    else "not_cancelled",
                    "cancelled_quantity": cancelled - pending["baseline_cancelled"],
                }
                if pending.get("broker_id"):
                    result["cancellation_broker_id"] = pending["broker_id"]
                self.store.update_request(pending["request_id"], result)
                order["pending_cancel"] = None
            self.store.put(order)
        return order

    @staticmethod
    def message(order, event):
        mode = "실전" if order["mode"] == "real" else "모의"
        side = "매수" if order["side"] == "buy" else "매도"
        return f"[{mode}] {order['symbol']} {order['exchange']} {side}\n{event}\n주문 {order['broker_id']}"

    async def get_order(self, order_id):
        async with self.lock:
            order = self.store.get(order_id)
            if order["status"] != "rejected":
                try:
                    order = await self.refresh(order)
                except TradingError as exc:
                    return public_order(order) | {"refresh_error": str(exc)}
            return public_order(order)

    async def list_orders(self, start_date, end_date):
        try:
            start, end = (datetime.strptime(s, "%Y-%m-%d").date() for s in (start_date, end_date))
        except ValueError:
            raise TradingError("Dates must be YYYY-MM-DD") from None
        if (
            start > end
            or end > datetime.now(KST).date()
            or start < datetime.now(KST).date() - timedelta(days=89)
        ):
            raise TradingError("Date range must be within the last 90 calendar days")
        mode = self.store.mode()
        output = []
        for exchange in ["KRX"] if mode == "demo" else ["KRX", "NXT"]:
            rows = await self.broker.daily(
                mode, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), exchange
            )
            allowed = (
                "ord_dt",
                "odno",
                "orgn_odno",
                "pdno",
                "prdt_name",
                "sll_buy_dvsn_cd",
                "ord_qty",
                "ord_unpr",
                "tot_ccld_qty",
                "avg_prvs",
                "rmn_qty",
                "cncl_yn",
                "rjct_qty",
            )
            output.extend({"exchange": exchange, **{k: r.get(k) for k in allowed}} for r in rows)
        return {
            "mode": mode,
            "broker_orders": output,
            "mcp_orders": [
                public_order(o)
                for o in self.store.orders()
                if o["mode"] == mode
                and start.strftime("%Y%m%d") <= o["date"] <= end.strftime("%Y%m%d")
            ],
        }

    async def cancel(self, request):
        payload = request.model_dump(mode="json")
        async with self.lock:
            previous = self.store.request(request.client_request_id, "cancel", payload)
            if previous is not None:
                return previous
            order = self.store.get(request.order_id)
            self.check_account(order)
            if not order["broker_id"] or order["status"] in ("rejected", "unknown", "submitting"):
                raise TradingError("Order has no confirmed broker identity")
            order = await self.refresh(order)
            if order.get("pending_cancel"):
                raise TradingError("Previous cancellation is still being reconciled")
            capacity = min(order["remaining"], await self.broker.cancel_capacity(order))
            quantity = request.quantity if request.quantity is not None else capacity
            if quantity <= 0 or quantity > capacity:
                raise TradingError("Quantity exceeds currently cancellable remainder")
            result = {
                "order_id": order["id"],
                "mode": order["mode"],
                "exchange": order["exchange"],
                "status": "unknown",
                "requested_quantity": quantity,
            }
            order["pending_cancel"] = {
                "request_id": request.client_request_id,
                "quantity": quantity,
                "baseline_cancelled": order["cancelled"],
            }
            with self.store.transaction():
                self.store.put(order)
                self.store.save_request(request.client_request_id, "cancel", payload, result)
            try:
                receipt = await self.broker.cancel(order, quantity)
                result.update(status="accepted", cancellation_broker_id=receipt["broker_id"])
                order["pending_cancel"]["broker_id"] = receipt["broker_id"]
            except (BrokerRejected, SubmissionNotSent) as exc:
                result.update(status="rejected", error=str(exc))
                order["pending_cancel"] = None
            except Exception:
                result["status"] = "unknown"
            with self.store.transaction():
                self.store.put(order)
                self.store.update_request(request.client_request_id, result)
            return result

    async def resolve_order(self, order_id, broker_id):
        """Operator explicitly attests ownership after checking KIS order history."""
        async with self.lock:
            order = self.store.get(order_id)
            self.check_account(order)
            if order["broker_id"]:
                if same_id(order["broker_id"], broker_id):
                    return public_order(order)
                raise TradingError("Order already bound to another broker ID")
            rows = await self.broker.daily(
                order["mode"], order["date"], order["date"], order["exchange"]
            )
            matched = [
                r for r in self.candidates(order, rows) if same_id(r.get("odno", ""), broker_id)
            ]
            if len(matched) != 1:
                raise TradingError("Broker ID does not uniquely match the submitted order")
            if any(
                o["mode"] == order["mode"]
                and o["date"] == order["date"]
                and o["broker_id"]
                and same_id(o["broker_id"], broker_id)
                for o in self.store.orders()
            ):
                raise TradingError("Broker ID is already bound to another MCP order")
            row = matched[0]
            org = row.get("ord_gno_brno") or row.get("krx_fwdg_ord_orgno")
            if not org:
                raise TradingError("Broker order organization number missing")
            order.update(
                broker_id=str(row["odno"]),
                org=str(org),
                status="accepted",
                resolution="explicit_operator_binding",
            )
            order.pop("error", None)
            order.pop("candidate_ids", None)
            self.store.put(order)
            return public_order(await self.refresh(order))

    async def poll(self):
        errors = 0
        for snapshot in self.store.orders():
            if snapshot["status"] in ("filled", "cancelled", "rejected") and not snapshot.get(
                "pending_cancel"
            ):
                continue
            async with self.lock:
                try:
                    await self.refresh(self.store.get(snapshot["id"]))
                except Exception:
                    errors += 1
        self.last_poll = datetime.now(KST).isoformat()
        self.poll_error = "Some orders could not be refreshed; state retained" if errors else None

    async def supervise(self):
        while True:
            try:
                await self.poll()
            except Exception:
                self.poll_error = "Supervisor iteration failed; retrying"
            await asyncio.sleep(self.settings.poll_seconds)
