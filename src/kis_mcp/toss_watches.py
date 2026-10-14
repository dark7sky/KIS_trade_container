"""One-shot, persisted Toss market alerts. This module never submits orders."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from .kis import KST
from .models import TradingError
from .toss import decimal, money


def timestamp(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError
        return result
    except (TypeError, ValueError):
        raise TradingError("Market observation requires a timezone timestamp") from None


def evaluate(rule, *, quote=None, candles=None, now=None):
    """Pure replayable decision; only fresh observations may fire."""
    now = now or datetime.now(timezone.utc)
    kind = rule["kind"]
    if kind.startswith("price_"):
        if not isinstance(quote, dict):
            return None
        observed = timestamp(quote.get("broker_timestamp"))
        if observed < timestamp(rule["created_at"]) or not timedelta(0) <= now - observed <= timedelta(seconds=30):
            return None
        price = decimal(quote.get("price"))
        threshold = decimal(rule["threshold"])
        matched = price >= threshold if kind == "price_at_or_above" else price <= threshold
        return {"price": money(price), "observed_at": observed.isoformat()} if matched else None
    if kind != "volume_breakout_1m" or not isinstance(candles, list) or len(candles) < 6:
        return None
    bars = sorted(
        (bar for bar in candles if timestamp(bar["timestamp"]) <= now),
        key=lambda bar: timestamp(bar["timestamp"]),
        reverse=True,
    )[:6]
    if len(bars) < 6:
        return None
    newest = bars[0]
    end = timestamp(newest["timestamp"])
    if end <= timestamp(rule["created_at"]) or not timedelta(0) <= now - end <= timedelta(minutes=2):
        return None
    previous = bars[1:6]
    if any(timestamp(bars[i]["timestamp"]) - timestamp(bars[i + 1]["timestamp"]) != timedelta(minutes=1) for i in range(5)):
        return None
    average_volume = sum(decimal(bar["volume"]) for bar in previous) / 5
    if average_volume <= 0:
        return None
    if decimal(newest["close"]) <= max(decimal(bar["high"]) for bar in previous):
        return None
    if decimal(newest["volume"]) < decimal(rule["volume_multiplier"]) * average_volume:
        return None
    return {
        "close": money(newest["close"]),
        "volume": money(newest["volume"]),
        "average_previous_volume": money(average_volume),
        "bar_end": end.isoformat(),
    }


class TossWatches:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.broker = service.broker
        self.lock = service.lock
        self.last_poll = None
        self.poll_error = None
        self.stream_enabled = False

    def check_account(self, doc):
        self.service.configured()
        if doc["account_fingerprint"] != self.service.settings.toss_credentials.fingerprint:
            raise TradingError("Configured Toss account differs from watch account")

    @staticmethod
    def public(doc):
        return {k: v for k, v in doc.items() if k != "account_fingerprint"}

    def list(self):
        return [self.public(d) for d in self.store.documents("toss_watches")]

    async def create(self, request):
        payload = request.model_dump(mode="json")
        async with self.lock:
            previous = self.store.request(request.client_request_id, "toss_watch", payload)
            if previous:
                doc = self.store.document("toss_watches", previous["id"])
                self.check_account(doc)
                return self.public(doc)
            self.service.active()
            now = datetime.now(KST)
            if not timedelta(0) < request.expires_at - now <= timedelta(hours=24):
                raise TradingError("Watch expiry must be in the next 24 hours")
        # A slow reference-data call must not hold the trading state lock.
        await self.broker.stock(request.symbol)
        async with self.lock:
            previous = self.store.request(request.client_request_id, "toss_watch", payload)
            if previous:
                doc = self.store.document("toss_watches", previous["id"])
                self.check_account(doc)
                return self.public(doc)
            self.service.active()
            now = datetime.now(KST)
            if not timedelta(0) < request.expires_at - now <= timedelta(hours=24):
                raise TradingError("Watch expiry must be in the next 24 hours")
            doc = {
                "id": str(uuid.uuid4()),
                "symbol": request.symbol,
                "kind": request.kind,
                "threshold": money(request.threshold) if request.threshold is not None else None,
                "volume_multiplier": money(request.volume_multiplier),
                "expires_at": request.expires_at.isoformat(),
                "created_at": now.isoformat(),
                "status": "watching",
                "account_fingerprint": self.service.settings.toss_credentials.fingerprint,
            }
            with self.store.transaction():
                self.store.put_document("toss_watches", doc)
                self.store.save_request(request.client_request_id, "toss_watch", payload, {"id": doc["id"]})
            return self.public(doc)

    async def stop(self, doc_id):
        async with self.lock:
            doc = self.store.document("toss_watches", doc_id)
            self.check_account(doc)
            if doc["status"] == "watching":
                doc["status"] = "stopped"
                self.store.put_document("toss_watches", doc)
            return self.public(doc)

    async def observe_trade(self, symbol, quote, *, now=None):
        """Apply only validated live trades; callers never submit an order here."""
        now = now or datetime.now(KST)
        # No await in this state transition: it can run while slow REST reads
        # hold the order lock, without interleaving another SQLite mutation.
        if self.service.store.mode() != "real":
            return
        for doc in self.store.documents("toss_watches"):
            if doc["status"] != "watching" or doc["symbol"] != symbol or not doc["kind"].startswith("price_"):
                continue
            self.check_account(doc)
            if now >= timestamp(doc["expires_at"]):
                doc["status"] = "expired"
                self.store.put_document("toss_watches", doc)
                continue
            hit = evaluate(doc, quote=quote, now=now)
            if hit is not None:
                self._trigger(doc, hit, now)

    def _trigger(self, doc, hit, now):
        doc.update(status="triggered", triggered_at=now.isoformat(), evidence=hit)
        with self.store.transaction():
            self.store.put_document("toss_watches", doc)
            self.store.event(
                "toss-watch:" + doc["id"],
                {"broker": "toss", "watch_id": doc["id"], "evidence": hit},
                f"[실전 TOSS US] {doc['symbol']} {doc['kind']} 신호 발생. 관측 {hit}. 자동 주문 없음.",
            )

    async def tick(self, *, now=None):
        fixed_time = now is not None
        now = now or datetime.now(KST)
        async with self.lock:
            watches = self.store.documents("toss_watches")
            active = []
            for doc in watches:
                if doc["status"] != "watching":
                    continue
                if now >= timestamp(doc["expires_at"]):
                    doc["status"] = "expired"
                    self.store.put_document("toss_watches", doc)
                elif self.service.store.mode() == "real" and self.service.settings.toss_credentials is not None:
                    self.check_account(doc)
                    active.append(doc)
        quotes, bars = {}, {}
        failures = 0
        for symbol in {d["symbol"] for d in active if d["kind"].startswith("price_") and not self.stream_enabled}:
            try:
                quotes[symbol] = await self.broker.quote(symbol)
            except Exception:
                failures += 1
        for symbol in {d["symbol"] for d in active if d["kind"] == "volume_breakout_1m"}:
            try:
                bars[symbol] = await self.broker.candles(symbol, 7)
            except Exception:
                failures += 1
        evaluated_at = now if fixed_time else datetime.now(KST)
        async with self.lock:
            for snapshot in active:
                doc = self.store.document("toss_watches", snapshot["id"])
                if doc["status"] != "watching" or self.service.store.mode() != "real":
                    continue
                self.check_account(doc)
                if evaluated_at >= timestamp(doc["expires_at"]):
                    doc["status"] = "expired"
                    self.store.put_document("toss_watches", doc)
                    continue
                try:
                    hit = evaluate(doc, quote=quotes.get(doc["symbol"]), candles=bars.get(doc["symbol"]), now=evaluated_at)
                except Exception:
                    failures += 1
                    continue
                if hit is None:
                    continue
                self._trigger(doc, hit, evaluated_at)
            self.last_poll = evaluated_at.isoformat()
            self.poll_error = "Some Toss market observations failed; alerts may be delayed" if failures else None

    async def supervise(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.poll_error = "Toss watch poll failed; retained last state"
            await asyncio.sleep(15)

    @staticmethod
    def replay(request, observations):
        rule = {
            "kind": request.kind,
            "threshold": money(request.threshold) if request.threshold is not None else None,
            "volume_multiplier": money(request.volume_multiplier),
            "created_at": request.created_at.isoformat(),
        }
        for item in observations:
            now = timestamp(item["as_of"])
            hit = evaluate(rule, quote=item.get("quote"), candles=item.get("candles"), now=now)
            if hit is not None:
                return {"triggered": True, "at": now.isoformat(), "evidence": hit}
        return {"triggered": False}
