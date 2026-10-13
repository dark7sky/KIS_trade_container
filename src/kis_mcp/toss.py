"""Toss REST adapter. Contract: official OpenAPI 1.2.24, reviewed 2026-10-13.

No submission is replayed, even though clientOrderId has a 10 minute TTL.
Credentials and broker error bodies never enter public errors or persisted state.
"""

import asyncio
import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import httpx

from .kis import KST
from .models import BrokerRejected, SubmissionNotSent, TradingError, UncertainSubmission


def decimal(value):
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise TradingError("Invalid Toss numeric response; state retained") from None


def money(value):
    return format(decimal(value).normalize(), "f")


def observation():
    return {"received_at": datetime.now(KST).isoformat(), "time_basis": "server_received_at"}


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", value):
        raise TradingError("Invalid Toss order identity")
    return value


class Toss:
    def __init__(self, credentials, *, client=None, interval=1.1):
        credentials.validate()
        self.credentials = credentials
        self.client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)
        self.interval = interval
        self.lock = asyncio.Lock()
        self.next_call = 0
        self.auth_retry_at = 0
        self.access_token = None
        self.expires_at = 0
        self.health = {"real": "not_checked"}

    async def close(self):
        await self.client.aclose()

    async def token(self):
        if self.access_token and time.monotonic() < self.expires_at:
            return self.access_token
        if time.monotonic() < self.auth_retry_at:
            raise SubmissionNotSent("Toss authentication cooling down; request not sent")
        self.auth_retry_at = time.monotonic() + 60
        try:
            response = await self.client.post(
                "https://openapi.tossinvest.com/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.credentials.client_id,
                    "client_secret": self.credentials.secret,
                },
            )
            response.raise_for_status()
            body = response.json()
            token, expires = body["access_token"], body["expires_in"]
            if not isinstance(token, str) or not token or body["token_type"] != "Bearer":
                raise ValueError
            if isinstance(expires, bool) or not isinstance(expires, int) or expires <= 60:
                raise ValueError
            self.access_token = token
            self.expires_at = time.monotonic() + expires - 60
            return token
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            self.health["real"] = "authentication_failed"
            raise SubmissionNotSent("Toss authentication failed; request not sent") from None

    async def call(self, path, *, params=None, body=None, account=False):
        write = body is not None
        async with self.lock:
            token = await self.token()
            await asyncio.sleep(max(0, self.next_call - time.monotonic()))
            headers = {"Authorization": f"Bearer {token}"}
            if account:
                headers["X-Tossinvest-Account"] = self.credentials.account
            self.next_call = time.monotonic() + self.interval
            try:
                response = await self.client.request(
                    "POST" if write else "GET",
                    "https://openapi.tossinvest.com" + path,
                    headers=headers,
                    params=params,
                    **({"json": body} if write else {}),
                )
                if response.status_code == 401:
                    self.access_token = None
                if response.status_code == 429:
                    self.next_call = max(self.next_call, time.monotonic() + 60)
                if response.status_code in (400, 401, 403, 404, 422):
                    error = response.json().get("error")
                    if isinstance(error, dict) and isinstance(error.get("code"), str):
                        raise BrokerRejected(
                            "Toss rejected request; verify inputs and account settings"
                        )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or "result" not in data or "error" in data:
                    raise ValueError
                result = data["result"]
                if not isinstance(result, (dict, list)):
                    raise ValueError
                self.health["real"] = "ok"
                return result
            except BrokerRejected:
                self.health["real"] = "rejected"
                raise
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                self.health["real"] = "error"
                if write:
                    raise UncertainSubmission(
                        "Toss submission outcome unknown; do not resubmit"
                    ) from None
                raise TradingError(
                    "Toss query failed; state retained", code="broker_query_failed"
                ) from None

    async def stock(self, symbol):
        rows = await self.call("/api/v1/stocks", params={"symbols": symbol})
        matches = (
            [r for r in rows if isinstance(r, dict) and r.get("symbol") == symbol]
            if isinstance(rows, list)
            else []
        )
        if (
            len(matches) != 1
            or matches[0].get("currency") != "USD"
            or matches[0].get("market") not in ("NYSE", "NASDAQ", "AMEX", "US_ETC")
        ):
            raise TradingError("Only Toss US stocks in USD are supported")
        return matches[0]

    async def quote(self, symbol):
        stock = await self.stock(symbol)
        rows = await self.call("/api/v1/prices", params={"symbols": symbol})
        observed = observation()
        matches = (
            [r for r in rows if isinstance(r, dict) and r.get("symbol") == symbol]
            if isinstance(rows, list)
            else []
        )
        if len(matches) != 1 or matches[0].get("currency") != "USD":
            raise TradingError("Toss price unavailable")
        row = matches[0]
        return {
            "symbol": symbol,
            "market": stock["market"],
            "currency": "USD",
            "price": money(row.get("lastPrice")),
            "broker_timestamp": row.get("timestamp"),
            "price_observed_at": observed["received_at"],
            "price_time_basis": observed["time_basis"],
            "component_observations": {"price": observed},
        }

    async def capacity(self, symbol, price):
        stock = await self.stock(symbol)
        if stock.get("status") != "ACTIVE":
            raise TradingError("Toss US stock is not active")
        buying = await self.call("/api/v1/buying-power", params={"currency": "USD"}, account=True)
        if buying.get("currency") != "USD":
            raise TradingError("Toss buying power currency mismatch")
        selling = await self.call(
            "/api/v1/sellable-quantity", params={"symbol": symbol}, account=True
        )
        return {
            "cash_buy_amount": money(buying.get("cashBuyingPower")),
            "sell_quantity": money(selling.get("sellableQuantity")),
            "currency": "USD",
        }

    async def account(self):
        data = await self.call("/api/v1/holdings", account=True)
        observed = observation()
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise TradingError("Invalid Toss holdings response")
        fields = ("symbol", "name", "currency", "quantity", "lastPrice", "averagePurchasePrice")
        holdings = [
            {k: row.get(k) for k in fields}
            for row in data["items"]
            if row.get("marketCountry") == "US" and row.get("currency") == "USD"
        ]
        buying = await self.call("/api/v1/buying-power", params={"currency": "USD"}, account=True)
        if buying.get("currency") != "USD":
            raise TradingError("Toss buying power currency mismatch")
        return {
            "holdings": holdings,
            "currency": "USD",
            "cash_buy_amount": money(buying.get("cashBuyingPower")),
            "price_observed_at": observed["received_at"],
            "price_time_basis": observed["time_basis"],
            "component_observations": {"balance": observed, "buying_power": observation()},
        }

    async def calendar(self):
        data = await self.call("/api/v1/market-calendar/US")
        # Only public market session fields; never pass through an arbitrary broker envelope.
        result = {}
        for key in ("today", "previousBusinessDay", "nextBusinessDay"):
            day = data[key]
            result[key] = {"date": day["date"]}
            for session in ("dayMarket", "preMarket", "regularMarket", "afterMarket"):
                value = day[session]
                result[key][session] = (
                    None
                    if value is None
                    else {"startTime": value["startTime"], "endTime": value["endTime"]}
                )
        return result

    async def orders(self, start, end, symbol=None):
        rows = {}
        for status in ("OPEN", "CLOSED"):
            params = {"status": status, "from": start, "to": end, "limit": 100}
            if symbol:
                params["symbol"] = symbol
            seen = set()
            for _ in range(200):
                page = await self.call("/api/v1/orders", params=params, account=True)
                if (
                    not isinstance(page, dict)
                    or not isinstance(page.get("orders"), list)
                    or type(page.get("hasNext")) is not bool
                ):
                    raise TradingError("Invalid Toss pagination response")
                for row in page["orders"]:
                    order_id = identity(row.get("orderId"))
                    if order_id in rows and rows[order_id] != row:
                        raise TradingError("Toss orders changed during pagination; retry query")
                    rows[order_id] = row
                if not page["hasNext"]:
                    break
                cursor = page.get("nextCursor")
                if status == "OPEN" or not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise TradingError("Incomplete Toss pagination; state retained")
                seen.add(cursor)
                params["cursor"] = cursor
            else:
                raise TradingError("Toss pagination limit exceeded")
        return list(rows.values())

    async def detail(self, broker_id):
        return await self.call(
            "/api/v1/orders/" + quote(identity(broker_id), safe=""), account=True
        )

    @staticmethod
    def receipt(data):
        try:
            return {"broker_id": identity(data["orderId"])}
        except (TradingError, KeyError, TypeError):
            raise UncertainSubmission(
                "Toss acknowledged request without a usable order ID"
            ) from None

    async def place(self, order):
        body = {
            "clientOrderId": order["id"],
            "symbol": order["symbol"],
            "side": order["side"].upper(),
            "orderType": order["order_type"].upper(),
            "timeInForce": "DAY",
            "quantity": str(order["quantity"]),
        }
        if order["order_type"] == "limit":
            body["price"] = order["price"]
        return self.receipt(await self.call("/api/v1/orders", body=body, account=True))

    async def cancel(self, order):
        path = "/api/v1/orders/" + quote(identity(order["broker_id"]), safe="") + "/cancel"
        return self.receipt(await self.call(path, body={}, account=True))
