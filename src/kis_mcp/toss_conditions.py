"""Broker-native protective orders; local state never assumes a trigger has filled."""

import asyncio
import uuid
from datetime import date, datetime, timedelta

from .kis import KST
from .models import BrokerRejected, SubmissionNotSent, TradingError
from .toss import decimal, identity, money


def public_condition(doc):
    return {key: value for key, value in doc.items() if key not in ("account_fingerprint", "payload")}


class TossConditions:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.broker = service.broker
        self.lock = service.lock
        self.last_poll = None
        self.poll_error = None
        self.wakeup = asyncio.Event()

    def check_account(self, doc):
        self.service.configured()
        if doc["account_fingerprint"] != self.service.settings.toss_credentials.fingerprint:
            raise TradingError("Configured Toss account differs from original condition account")

    def list(self):
        return [public_condition(d) for d in self.store.documents("toss_conditions")]

    async def create_oco(self, request):
        payload = request.model_dump(mode="json")
        async with self.lock:
            previous = self.store.request(request.client_request_id, "toss_oco", payload)
            if previous:
                doc = self.store.document("toss_conditions", previous["id"])
                self.check_account(doc)
                return public_condition(doc)
            self.service.active()
            if any(
                d["status"] in ("submitting", "unknown", "cancel_unknown", "cancel_submitting")
                for d in self.store.documents("toss_conditions")
            ):
                raise TradingError("Resolve uncertain Toss conditional order before another condition")
            if any(
                order["status"] in ("unknown", "submitting", "needs_review")
                for order in self.service.orders()
            ):
                raise TradingError("Resolve uncertain Toss order before another condition")
            today = datetime.now(KST).date()
            try:
                expiry = date.fromisoformat(request.expire_date)
            except ValueError:
                raise TradingError("Invalid conditional order expiry") from None
            if expiry < today or expiry > today + timedelta(days=365):
                raise TradingError("Conditional order expiry must be within one year")
            capacity = await self.broker.capacity(request.symbol, request.stop_loss_trigger)
            if decimal(capacity["sell_quantity"]) < request.quantity:
                raise TradingError("Quantity exceeds Toss holdings available to sell")
            quote = await self.broker.quote(request.symbol)
            current = decimal(quote["price"])
            if not request.take_profit_trigger > current > request.stop_loss_trigger:
                raise TradingError("OCO requires take-profit > current price > stop-loss")
            doc_id = str(uuid.uuid4())
            broker_payload = {
                "clientOrderId": doc_id,
                "symbol": request.symbol,
                "type": "OCO",
                "quantity": str(request.quantity),
                "orderType": "LIMIT",
                "expireDate": request.expire_date,
                "first": {
                    "orderSide": "SELL",
                    "triggerPrice": money(request.take_profit_trigger),
                    "orderPrice": money(request.take_profit_limit),
                },
                "second": {
                    "orderSide": "SELL",
                    "triggerPrice": money(request.stop_loss_trigger),
                    "orderPrice": money(request.stop_loss_limit),
                },
            }
            doc = {
                "id": doc_id,
                "broker_id": None,
                "status": "submitting",
                "symbol": request.symbol,
                "quantity": request.quantity,
                "created_at": datetime.now(KST).isoformat(),
                "account_fingerprint": self.service.settings.toss_credentials.fingerprint,
                "payload": broker_payload,
                "triggered_orders": [],
            }
            with self.store.transaction():
                self.store.put_document("toss_conditions", doc)
                self.store.save_request(request.client_request_id, "toss_oco", payload, {"id": doc_id})
            try:
                doc["broker_id"] = await self.broker.create_oco(broker_payload)
                doc["status"] = "WATCHING"
            except (BrokerRejected, SubmissionNotSent) as exc:
                doc.update(status="rejected", error=str(exc))
            except Exception:
                doc.update(status="unknown", error="Conditional order outcome unknown; reconcile before retry")
            self.store.put_document("toss_conditions", doc)
            return public_condition(doc)

    async def refresh(self, doc_id, *, now=None):
        now = now or datetime.now(KST)
        async with self.lock:
            doc = self.store.document("toss_conditions", doc_id)
            self.check_account(doc)
            if not doc["broker_id"]:
                return public_condition(doc)
            row = await self.broker.conditional_detail(doc["broker_id"])
            if (
                row.get("conditionalOrderId") != doc["broker_id"]
                or row.get("market") != "US"
                or row.get("symbol") != doc["symbol"]
                or row.get("type") != "OCO"
                or row.get("orderType") != "LIMIT"
                or decimal(row.get("quantity")) != doc["quantity"]
            ):
                raise TradingError("Toss conditional identity or terms changed; state retained")
            status = row.get("status")
            if status not in ("WATCHING", "PAUSED", "ORDERING", "ORDERED", "COMPLETED", "EXPIRED"):
                raise TradingError("Unknown Toss conditional status; state retained")
            legs = []
            triggered = []
            for key in ("first", "second"):
                leg = row.get(key)
                expected = doc["payload"][key]
                if (
                    not isinstance(leg, dict)
                    or leg.get("orderSide") != "SELL"
                    or decimal(leg.get("triggerPrice")) != decimal(expected["triggerPrice"])
                    or decimal(leg.get("orderPrice")) != decimal(expected["orderPrice"])
                    or leg.get("status") not in ("WATCHING", "HOLDING", "PAUSED", "ORDERING", "ORDERED", "COMPLETED", "EXPIRED", "CANCELED")
                ):
                    raise TradingError("Invalid Toss conditional leg; state retained")
                order_id = leg.get("triggeredOrderId")
                if order_id is not None:
                    triggered.append(identity(order_id))
                legs.append({"status": leg.get("status"), "triggered_order_id": order_id})
            generated = []
            for order_id in triggered:
                order = await self.broker.detail(order_id)
                execution = order.get("execution")
                if (
                    order.get("orderId") != order_id
                    or order.get("symbol") != doc["symbol"]
                    or order.get("side") != "SELL"
                    or order.get("currency") != "USD"
                    or decimal(order.get("quantity")) != doc["quantity"]
                    or not isinstance(execution, dict)
                ):
                    raise TradingError("Triggered Toss order identity changed; state retained")
                filled = decimal(execution.get("filledQuantity"))
                if filled > doc["quantity"] or filled != filled.to_integral_value():
                    raise TradingError("Invalid triggered Toss fill quantity; state retained")
                generated.append({
                    "order_id": order_id,
                    "status": order.get("status") if order.get("status") in ("PENDING", "PARTIAL_FILLED", "FILLED", "CANCELED", "REJECTED", "PENDING_CANCEL") else "NEEDS_REVIEW",
                    "filled_quantity": money(filled),
                    "quantity": money(order.get("quantity")),
                })
            previous = doc["status"]
            previous_generated = doc.get("generated_orders", [])
            by_id = {item["order_id"]: item for item in previous_generated}
            for order in generated:
                prior = by_id.get(order["order_id"], {})
                advanced = order["filled_quantity"] != prior.get("filled_quantity")
                progress_at = now if advanced or not prior.get("progress_at") else datetime.fromisoformat(prior["progress_at"])
                order["progress_at"] = progress_at.isoformat()
                order["stalled"] = (
                    order["status"] in ("PENDING", "PARTIAL_FILLED")
                    and now - progress_at >= timedelta(seconds=90)
                )
            observed_status = status
            if previous in ("cancel_unknown", "cancel_submitting"):
                status = previous
            doc.update(
                status=status,
                broker_status=observed_status,
                triggered_orders=triggered,
                generated_orders=generated,
                legs=legs,
                last_checked=now.isoformat(),
            )
            with self.store.transaction():
                self.store.put_document("toss_conditions", doc)
                if status != previous or triggered:
                    event_id = "toss-condition:" + doc_id + ":" + status + ":" + ",".join(triggered)
                    self.store.event(
                        event_id,
                        {"broker": "toss", "condition_id": doc_id, "status": status},
                        f"[실전 TOSS US] {doc['symbol']} 조건주문 {status}; 발생 주문 {', '.join(triggered) or '없음'}. 실제 체결은 별도 확인 필요.",
                    )
                for order in generated:
                    prior = by_id.get(order["order_id"], {})
                    if (order["status"], order["filled_quantity"]) != (prior.get("status"), prior.get("filled_quantity")):
                        event_id = "toss-condition-order:" + doc_id + ":" + order["order_id"] + ":" + order["status"] + ":" + order["filled_quantity"]
                        self.store.event(
                            event_id,
                            {"broker": "toss", "condition_id": doc_id, "order": order},
                            f"[실전 TOSS US] {doc['symbol']} 조건 발생 주문 {order['order_id']} 상태 {order['status']}, 체결 {order['filled_quantity']}/{order['quantity']}주.",
                        )
                    if order["stalled"] and not prior.get("stalled"):
                        self.store.event(
                            "toss-condition-stall:" + doc_id + ":" + order["order_id"] + ":" + order["filled_quantity"],
                            {"broker": "toss", "condition_id": doc_id, "order_id": order["order_id"], "filled_quantity": order["filled_quantity"]},
                            f"[실전 TOSS US] {doc['symbol']} 조건 발생 주문 {order['order_id']} 90초 이상 추가 체결 없음. 잔량을 토스 앱에서 확인하세요. 자동 재주문·취소 없음.",
                        )
            return public_condition(doc)

    async def cancel(self, client_request_id, doc_id):
        async with self.lock:
            payload = {"condition_id": doc_id}
            previous = self.store.request(client_request_id, "toss_oco_cancel", payload)
            if previous:
                doc = self.store.document("toss_conditions", previous["id"])
                self.check_account(doc)
                return public_condition(doc)
            doc = self.store.document("toss_conditions", doc_id)
            self.check_account(doc)
            if doc["status"] != "WATCHING" or doc.get("triggered_orders") or not doc["broker_id"]:
                raise TradingError("Refresh and inspect condition before cancellation")
            current = await self.broker.conditional_detail(doc["broker_id"])
            if current.get("conditionalOrderId") != doc["broker_id"] or current.get("status") != "WATCHING" or any(
                isinstance(current.get(leg), dict) and current[leg].get("triggeredOrderId")
                for leg in ("first", "second")
            ):
                raise TradingError("Condition changed at broker; refresh before cancellation")
            with self.store.transaction():
                doc["status"] = "cancel_submitting"
                self.store.put_document("toss_conditions", doc)
                self.store.save_request(client_request_id, "toss_oco_cancel", payload, {"id": doc_id})
            try:
                await self.broker.cancel_conditional(doc["broker_id"])
                doc["status"] = "cancelled"
            except SubmissionNotSent as exc:
                doc.update(status="WATCHING", error=str(exc))
            except BrokerRejected as exc:
                doc.update(status="cancel_unknown", error=str(exc))
            except Exception:
                doc.update(status="cancel_unknown", error="Conditional cancellation outcome unknown")
            self.store.put_document("toss_conditions", doc)
            return public_condition(doc)

    async def resolve(self, doc_id, broker_id):
        async with self.lock:
            doc = self.store.document("toss_conditions", doc_id)
            self.check_account(doc)
            if doc["status"] not in ("unknown", "submitting"):
                raise TradingError("Only uncertain conditions can be resolved")
            broker_id = identity(broker_id)
            row = await self.broker.conditional_detail(broker_id)
            wanted = doc["payload"]
            if (
                row.get("conditionalOrderId") != broker_id
                or row.get("symbol") != wanted["symbol"]
                or row.get("market") != "US"
                or row.get("type") != "OCO"
                or row.get("orderType") != "LIMIT"
                or decimal(row.get("quantity")) != decimal(wanted["quantity"])
                or any(
                    not isinstance(row.get(key), dict)
                    or row[key].get("orderSide") != "SELL"
                    or decimal(row[key].get("triggerPrice")) != decimal(wanted[key]["triggerPrice"])
                    or decimal(row[key].get("orderPrice")) != decimal(wanted[key]["orderPrice"])
                    for key in ("first", "second")
                )
            ):
                raise TradingError("Broker conditional terms do not match; state retained")
            doc.update(broker_id=broker_id, status=row["status"])
            self.store.put_document("toss_conditions", doc)
            return public_condition(doc)

    async def resolve_not_submitted(self, doc_id, confirmed_in_toss_app):
        if confirmed_in_toss_app is not True:
            raise TradingError("Confirm absence in Toss app before resolving an uncertain condition")
        async with self.lock:
            doc = self.store.document("toss_conditions", doc_id)
            self.check_account(doc)
            if doc["status"] not in ("unknown", "submitting"):
                raise TradingError("Only uncertain conditions can be resolved")
            rows = await self.broker.conditional_orders(doc["symbol"])
            created = datetime.fromisoformat(doc["created_at"])
            for row in rows:
                if row.get("symbol") != doc["symbol"] or row.get("type") != "OCO":
                    continue
                try:
                    broker_created = datetime.fromisoformat(row["createdAt"])
                    close_in_time = abs((broker_created - created).total_seconds()) <= 20 * 60
                except (KeyError, TypeError, ValueError):
                    close_in_time = True
                if close_in_time:
                    raise TradingError("Possible Toss conditional order exists; inspect before resolving")
            doc["status"] = "not_submitted"
            doc["resolved_at"] = datetime.now(KST).isoformat()
            self.store.put_document("toss_conditions", doc)
            return public_condition(doc)

    async def resolve_cancel(self, doc_id, confirmed_cancelled_in_toss_app):
        async with self.lock:
            doc = self.store.document("toss_conditions", doc_id)
            self.check_account(doc)
            if doc["status"] not in ("cancel_unknown", "cancel_submitting"):
                raise TradingError("Only uncertain conditional cancellations can be resolved")
            if confirmed_cancelled_in_toss_app is True:
                rows = await self.broker.conditional_orders(doc["symbol"])
                if any(row.get("conditionalOrderId") == doc["broker_id"] for row in rows):
                    raise TradingError("Conditional order still exists at Toss; inspect before resolving")
                doc["status"] = "cancelled"
            else:
                row = await self.broker.conditional_detail(doc["broker_id"])
                if row.get("conditionalOrderId") != doc["broker_id"] or row.get("status") != "WATCHING" or any(
                    isinstance(row.get(leg), dict) and row[leg].get("triggeredOrderId")
                    for leg in ("first", "second")
                ):
                    raise TradingError("Conditional order changed at Toss; inspect before resolving")
                doc["status"] = "WATCHING"
            doc["resolved_at"] = datetime.now(KST).isoformat()
            self.store.put_document("toss_conditions", doc)
            return public_condition(doc)

    async def supervise(self):
        while True:
            try:
                failures = 0
                for doc in self.store.documents("toss_conditions"):
                    pending_generated = any(
                        item.get("status") not in ("FILLED", "CANCELED", "REJECTED")
                        for item in doc.get("generated_orders", [])
                    )
                    if doc["status"] in ("WATCHING", "PAUSED", "ORDERING", "ORDERED", "cancel_unknown") or pending_generated:
                        try:
                            await self.refresh(doc["id"])
                        except Exception:
                            failures += 1
                self.last_poll = datetime.now(KST).isoformat()
                self.poll_error = "Some conditional orders failed to refresh; retained last state" if failures else None
            except asyncio.CancelledError:
                raise
            except Exception:
                self.poll_error = "Conditional order poll failed; retained last state"
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=30)
                self.wakeup.clear()
            except asyncio.TimeoutError:
                pass
