"""Bounded Toss live feed and conservative entry-cost gate.

Market frames are lossy and have no sequence number. A disconnect or delayed
timestamp therefore invalidates the cache; no missing ticks are inferred.
"""

import asyncio
import json
import random
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .models import TradingError
from .toss import decimal, money
from .toss_watches import timestamp

WS_URL = "wss://openapi-ws.tossinvest.com/ws/v1"
MAX_AGE = timedelta(seconds=10)


def estimate_entry(orderbook, commission_rate, expected_move_bps, slippage_bps, *, now=None, max_spread_bps="25", quantity=1):
    """Round-trip estimate in basis points, with caller-supplied slippage allowance."""
    now = now or datetime.now(timezone.utc)
    observed = timestamp(orderbook.get("timestamp"))
    if not timedelta(0) <= now - observed <= MAX_AGE:
        raise TradingError("Toss orderbook is stale; entry cost cannot be checked")
    if orderbook.get("currency") != "USD":
        raise TradingError("Toss orderbook currency mismatch")
    try:
        bid = decimal(orderbook["bids"][0]["price"])
        ask = decimal(orderbook["asks"][0]["price"])
        bid_volume = decimal(orderbook["bids"][0]["volume"])
        ask_volume = decimal(orderbook["asks"][0]["volume"])
    except (IndexError, KeyError, TypeError):
        raise TradingError("Toss orderbook has no usable best quote") from None
    if bid <= 0 or ask <= bid or bid_volume <= 0 or ask_volume <= 0:
        raise TradingError("Toss orderbook spread or liquidity is invalid")
    size = decimal(quantity)
    if size <= 0 or size != size.to_integral_value() or size > bid_volume or size > ask_volume:
        raise TradingError("Toss best quote liquidity is insufficient for quantity")
    rate, expected, slippage, limit = map(decimal, (commission_rate, expected_move_bps, slippage_bps, max_spread_bps))
    if rate > 1 or expected > 100000 or slippage > 100000 or limit > 100000:
        raise TradingError("Entry cost inputs out of range")
    spread = (ask - bid) / ((ask + bid) / 2) * Decimal("10000")
    round_trip_cost = spread + 2 * rate * Decimal("10000") + slippage
    return {
        "allowed": spread <= limit and expected > round_trip_cost,
        "bid": money(bid), "ask": money(ask), "observed_at": observed.isoformat(),
        "best_bid_volume": money(bid_volume), "best_ask_volume": money(ask_volume), "quantity": money(size),
        "spread_bps": money(spread), "max_spread_bps": money(limit),
        "commission_rate": money(rate), "slippage_allowance_bps": money(slippage),
        "round_trip_cost_bps": money(round_trip_cost),
        "expected_move_bps": money(expected),
        "estimated_net_edge_bps": format((expected - round_trip_cost).normalize(), "f"),
        "basis": "best_quote_snapshot_plus_round_trip_commission_plus_user_slippage_allowance",
    }


class TossMarketStream:
    def __init__(self, service, watches, conditions=None):
        self.service = service
        self.watches = watches
        self.conditions = conditions
        self.books = {}
        self.last_trade_at = {}
        self.subscribed = set()
        self.blocked = set()
        self.connected = False
        self.last_message_at = None
        self.last_gap_at = None
        self.error = None
        self.monotonic = time.monotonic

    def status(self):
        return {
            "healthy": self.connected and self.error is None,
            "subscribed": sorted("personal:order" if key.startswith("personal:order:") else key for key in self.subscribed),
            "rejected": sorted("personal:order" if key.startswith("personal:order:") else key for key in self.blocked),
            "last_message_at": self.last_message_at,
            "last_gap_at": self.last_gap_at,
            "error": self.error,
        }

    def disconnect(self, reason="Toss stream disconnected; market cache invalidated"):
        self.connected = False
        self.subscribed.clear()
        self.books.clear()
        self.last_trade_at.clear()
        self.last_gap_at = datetime.now(timezone.utc).isoformat()
        self.error = reason

    def wanted(self):
        symbols = {"SOXL", "SOXS"}
        symbols.update(d["symbol"] for d in self.watches.list() if d["status"] == "watching")
        if self.conditions:
            symbols.update(d["symbol"] for d in self.conditions.list() if d["status"] in ("WATCHING", "ORDERING", "ORDERED"))
        if len(symbols) > 49:  # 2 channels * 49 + personal <= 100
            raise TradingError("Too many Toss stream symbols for one connection")
        symbols = sorted(symbols)
        desired = {f"{kind}:us:{symbol}" for kind in ("trade", "orderbook") for symbol in symbols} | {f"personal:order:{self.service.settings.toss_credentials.account}"}
        return desired - self.blocked

    @staticmethod
    def declaration(keys):
        result = []
        for kind in ("trade:us", "orderbook:us", "personal:order"):
            codes = sorted(key.removeprefix(kind + ":") for key in keys if key.startswith(kind + ":"))
            if codes:
                result.append({"type": kind, "codes": codes})
        return result

    def accept_book(self, symbol, data):
        if not isinstance(data, dict) or data.get("currency") != "USD":
            return
        try:
            observed = timestamp(data.get("timestamp"))
            if not timedelta(0) <= datetime.now(timezone.utc) - observed <= MAX_AGE:
                self.last_gap_at = datetime.now(timezone.utc).isoformat()
                return
            # Validate before caching; a crossed or empty book is not executable.
            estimate_entry(data, "0", "1", "0")
        except TradingError:
            self.last_gap_at = datetime.now(timezone.utc).isoformat()
            return
        previous = self.books.get(symbol)
        if previous and observed < timestamp(previous["timestamp"]):
            self.last_gap_at = datetime.now(timezone.utc).isoformat()
            return
        self.books[symbol] = data

    def orderbook(self, symbol):
        if not self.connected or f"orderbook:us:{symbol}" not in self.subscribed:
            raise TradingError("Fresh Toss orderbook unavailable")
        data = self.books.get(symbol)
        if not data:
            raise TradingError("Fresh Toss orderbook unavailable")
        if not timedelta(0) <= datetime.now(timezone.utc) - timestamp(data.get("timestamp")) <= MAX_AGE:
            self.books.pop(symbol, None)
            raise TradingError("Fresh Toss orderbook unavailable")
        return data

    async def handle(self, frame):
        if not isinstance(frame, dict):
            return
        kind = frame.get("type")
        if kind == "subscriptions":
            rejected = frame.get("rejected")
            subscribed = frame.get("subscribed")
            if not isinstance(rejected, list) or not isinstance(subscribed, list):
                self.disconnect("Invalid Toss subscription acknowledgment")
                return
            self.subscribed = {key for key in subscribed if isinstance(key, str)}
            self.blocked.update(item["target"] for item in rejected if isinstance(item, dict) and isinstance(item.get("target"), str))
            self.connected = bool(self.subscribed)
            self.error = "Toss subscription rejected; market feed incomplete" if self.blocked else None
            return
        if kind == "error":
            self.disconnect("Toss stream reported an error; reconnecting")
            return
        if kind != "message" or not self.connected:
            return
        topic, data = frame.get("topic"), frame.get("data")
        if topic not in self.subscribed or not isinstance(data, dict):
            return
        now = datetime.now(timezone.utc)
        self.last_message_at = now.isoformat()
        if topic.startswith("trade:us:"):
            symbol = topic.removeprefix("trade:us:")
            try:
                observed = timestamp(data.get("timestamp"))
                decimal(data.get("price"))
                decimal(data.get("volume"))
            except TradingError:
                self.last_gap_at = now.isoformat()
                return
            if data.get("currency") != "USD" or not timedelta(0) <= now - observed <= MAX_AGE:
                self.last_gap_at = now.isoformat()
                return
            previous = self.last_trade_at.get(symbol)
            if previous and observed < previous:
                self.last_gap_at = now.isoformat()
                return
            self.last_trade_at[symbol] = observed
            await self.watches.observe_trade(symbol, {"price": data["price"], "broker_timestamp": observed.isoformat()}, now=now)
        elif topic.startswith("orderbook:us:"):
            self.accept_book(topic.removeprefix("orderbook:us:"), data)
        elif topic.startswith("personal:order:") and self.conditions:
            # Push frames can be missed across reconnects. REST remains authority.
            self.conditions.wakeup.set()

    async def run(self):
        if self.service.settings.toss_credentials is None or not hasattr(self.service.broker, "token"):
            return
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus

        delay = 1
        while True:
            try:
                token = await self.service.broker.token()
                async with connect(WS_URL, additional_headers={"Authorization": f"Bearer {token}"}, open_timeout=15, max_size=1_000_000, ping_interval=None) as socket:
                    self.disconnect("Awaiting Toss subscription acknowledgment")
                    declared = None
                    last_ping = self.monotonic()
                    delay = 1
                    while True:
                        wanted = self.wanted()
                        if wanted != declared:
                            await socket.send(json.dumps(self.declaration(wanted)))
                            declared = wanted
                        if self.monotonic() - last_ping >= 60:
                            await socket.send("PING")
                            last_ping = self.monotonic()
                        try:
                            message = await asyncio.wait_for(socket.recv(), timeout=1)
                        except asyncio.TimeoutError:
                            continue
                        frame = json.loads(message)
                        await self.handle(frame)
                        if frame.get("type") == "error":
                            break
            except asyncio.CancelledError:
                self.disconnect()
                raise
            except InvalidStatus as exc:
                if exc.response.status_code == 401:
                    async with self.service.broker.auth_lock:
                        if self.service.broker.access_token == token:
                            self.service.broker.access_token = None
                self.disconnect("Toss stream handshake rejected; reconnecting")
            except Exception:
                self.disconnect()
            self.disconnect()
            await asyncio.sleep(delay + random.uniform(0, delay / 4))
            delay = min(delay * 2, 60)
