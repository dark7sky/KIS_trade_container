import asyncio
import json
from datetime import date, timedelta

import httpx
import pytest

from kis_mcp.config import TossCredentials
from kis_mcp.models import BrokerRejected, SubmissionNotSent, TradingError, UncertainSubmission
from kis_mcp.toss import Toss, decimal


def client(handler):
    return Toss(
        TossCredentials("test-client", "test-secret", "3"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        interval=0,
    )


def token_response():
    return httpx.Response(
        200, json={"access_token": "test-token", "token_type": "Bearer", "expires_in": 86400}
    )


def order():
    return {
        "id": "b4d30471-90aa-4d86-b916-50d4c8e4c9c1",
        "symbol": "AAPL",
        "side": "buy",
        "quantity": 5,
        "order_type": "limit",
        "price": "185.5",
    }


async def test_current_us_commission_requires_one_active_rate():
    rows = [
        {"marketCountry": "US", "commissionRate": "0.001", "startDate": None, "endDate": None},
        {"marketCountry": "KR", "commissionRate": "0.00015", "startDate": None, "endDate": None},
    ]

    def handler(req):
        if req.url.path == "/oauth2/token":
            return token_response()
        assert req.url.path == "/api/v1/commissions"
        assert req.headers["X-Tossinvest-Account"] == "3"
        return httpx.Response(200, json={"result": rows})

    api = client(handler)
    try:
        assert await api.commission_rate() == "0.001"
        rows.append({"marketCountry": "US", "commissionRate": "0.002", "startDate": None, "endDate": None})
        with pytest.raises(TradingError, match="ambiguous"):
            await api.commission_rate()
        rows.pop()
        rows[0]["endDate"] = (date.today() - timedelta(days=2)).isoformat()
        with pytest.raises(TradingError, match="unavailable"):
            await api.commission_rate()
    finally:
        await api.close()


async def test_concurrent_rest_and_stream_token_request_issues_one_token():
    requested = []

    def handler(req):
        requested.append(req.url.path)
        return token_response()

    api = client(handler)
    try:
        assert await asyncio.gather(api.token(), api.token()) == ["test-token", "test-token"]
        assert requested == ["/oauth2/token"]
    finally:
        await api.close()


async def test_auth_and_order_contract():
    seen = []

    def handler(req):
        seen.append(req)
        if req.url.path == "/oauth2/token":
            assert b"grant_type=client_credentials" in req.content
            assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")
            return token_response()
        assert req.headers["X-Tossinvest-Account"] == "3"
        assert req.headers["authorization"] == "Bearer test-token"
        body = json.loads(req.content)
        assert body == {
            "clientOrderId": order()["id"],
            "symbol": "AAPL",
            "side": "BUY",
            "orderType": "LIMIT",
            "timeInForce": "DAY",
            "quantity": "5",
            "price": "185.5",
        }
        return httpx.Response(200, json={"result": {"orderId": "opaque-ID"}})

    api = client(handler)
    try:
        assert (await api.place(order()))["broker_id"] == "opaque-ID"
        assert len(seen) == 2
    finally:
        await api.close()


@pytest.mark.parametrize("failure", ["timeout", "bad-json", "missing-id", 500, 429, 409, 401])
async def test_post_never_retries_uncertain_outcome(failure):
    posts = []

    def handler(req):
        if req.url.path == "/oauth2/token":
            return token_response()
        posts.append(req)
        if failure == "timeout":
            raise httpx.ReadTimeout("private-response", request=req)
        if failure == "bad-json":
            return httpx.Response(200, text="private-response")
        if failure == "missing-id":
            return httpx.Response(200, json={"result": {}})
        return httpx.Response(failure, json={"error": {"message": "private-response"}})

    api = client(handler)
    try:
        with pytest.raises((UncertainSubmission, BrokerRejected)) as caught:
            await api.place(order())
        assert "private-response" not in str(caught.value)
        assert len(posts) == 1
    finally:
        await api.close()


async def test_token_failure_is_not_sent():
    seen = []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(403, json={"error_description": "private"})

    api = client(handler)
    try:
        with pytest.raises(SubmissionNotSent):
            await api.place(order())
        assert seen == ["/oauth2/token"]
    finally:
        await api.close()


async def test_market_omits_price_and_cancel_has_no_quantity():
    bodies = []

    def handler(req):
        if req.url.path == "/oauth2/token":
            return token_response()
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"result": {"orderId": "opaque-cancel-ID"}})

    api = client(handler)
    try:
        await api.place(order() | {"order_type": "market", "price": "0"})
        await api.cancel(order() | {"broker_id": "opaque-ID"})
        assert "price" not in bodies[0]
        assert bodies[1] == {}
    finally:
        await api.close()


async def test_pagination_open_closed_and_repeated_cursor_rejected():
    seen = []

    def handler(req):
        if req.url.path == "/oauth2/token":
            return token_response()
        params = req.url.params
        seen.append(dict(params))
        assert params["from"] == "2026-10-13" and params["to"] == "2026-10-13"
        if params["status"] == "OPEN":
            return httpx.Response(
                200, json={"result": {"orders": [], "hasNext": False, "nextCursor": None}}
            )
        return httpx.Response(
            200, json={"result": {"orders": [], "hasNext": True, "nextCursor": "repeat"}}
        )

    api = client(handler)
    try:
        with pytest.raises(TradingError, match="pagination"):
            await api.orders("2026-10-13", "2026-10-13")
        assert len(seen) == 3
    finally:
        await api.close()


async def test_us_stock_metadata_prevents_domestic_route():
    def handler(req):
        if req.url.path == "/oauth2/token":
            return token_response()
        return httpx.Response(
            200,
            json={
                "result": [
                    {"symbol": "ABC123", "market": "KOSPI", "currency": "KRW", "status": "ACTIVE"}
                ]
            },
        )

    api = client(handler)
    try:
        with pytest.raises(TradingError, match="US"):
            await api.capacity("ABC123", "1")
    finally:
        await api.close()


@pytest.mark.parametrize("value", [None, True, "-1", "NaN", "Infinity", "broken"])
def test_invalid_numeric_response(value):
    with pytest.raises(TradingError):
        decimal(value)


async def test_public_reads_filter_account_data_and_preserve_usd():
    seen = []

    def handler(req):
        seen.append(req.url.path)
        if req.url.path == "/oauth2/token":
            return token_response()
        data = {
            "/api/v1/stocks": [
                {"symbol": "AAPL", "market": "NASDAQ", "currency": "USD", "status": "ACTIVE"}
            ],
            "/api/v1/prices": [
                {
                    "symbol": "AAPL",
                    "currency": "USD",
                    "lastPrice": "185.50",
                    "timestamp": "2026-10-13T22:30:00+09:00",
                }
            ],
            "/api/v1/buying-power": {"currency": "USD", "cashBuyingPower": "1000.50"},
            "/api/v1/sellable-quantity": {"sellableQuantity": "1.5"},
            "/api/v1/holdings": {
                "accountNumber": "private-account",
                "items": [
                    {
                        "symbol": "AAPL",
                        "marketCountry": "US",
                        "currency": "USD",
                        "quantity": "1.5",
                        "accountNumber": "private-account",
                    },
                    {"symbol": "005930", "marketCountry": "KR", "currency": "KRW", "quantity": "2"},
                ],
            },
            "/api/v1/market-calendar/US": {
                key: {
                    "date": "2026-10-13",
                    "dayMarket": None,
                    "preMarket": None,
                    "afterMarket": None,
                    "regularMarket": {
                        "startTime": "2026-10-13T22:30:00+09:00",
                        "endTime": "2026-10-14T05:00:00+09:00",
                    },
                }
                for key in ("today", "previousBusinessDay", "nextBusinessDay")
            },
        }
        return httpx.Response(200, json={"result": data[req.url.path]})

    api = client(handler)
    try:
        quote = await api.quote("AAPL")
        assert quote["price"] == "185.5" and quote["currency"] == "USD"
        assert quote["broker_timestamp"] != quote["price_observed_at"]
        account = await api.account()
        assert len(account["holdings"]) == 1
        assert "private-account" not in json.dumps(account)
        assert account["cash_buy_amount"] == "1000.5"
        assert (await api.capacity("AAPL", "185.5"))["sell_quantity"] == "1.5"
        assert (await api.calendar())["today"]["regularMarket"]["endTime"].endswith("+09:00")
        assert seen.count("/oauth2/token") == 1
    finally:
        await api.close()


async def test_complete_pagination_and_opaque_detail_id():
    def handler(req):
        if req.url.path == "/oauth2/token":
            return token_response()
        if req.url.path == "/api/v1/orders/opaque-ID":
            return httpx.Response(200, json={"result": {"orderId": "opaque-ID"}})
        if req.url.params["status"] == "OPEN":
            result = {"orders": [{"orderId": "open-ID"}], "hasNext": False, "nextCursor": None}
        elif "cursor" not in req.url.params:
            result = {
                "orders": [{"orderId": "closed-ID-1"}],
                "hasNext": True,
                "nextCursor": "second",
            }
        else:
            result = {"orders": [{"orderId": "closed-ID-2"}], "hasNext": False, "nextCursor": None}
        return httpx.Response(200, json={"result": result})

    api = client(handler)
    try:
        assert len(await api.orders("2026-10-13", "2026-10-13", "AAPL")) == 3
        assert (await api.detail("opaque-ID"))["orderId"] == "opaque-ID"
        with pytest.raises(TradingError):
            await api.detail("../wrong-path")
    finally:
        await api.close()


async def test_oco_rest_contract_delete_204_and_minute_candles():
    seen = []

    def handler(req):
        if req.url.path == "/oauth2/token":
            return token_response()
        seen.append(req)
        if req.url.path == "/api/v1/candles":
            assert req.url.params["interval"] == "1m"
            return httpx.Response(200, json={"result": {"candles": [{
                "timestamp": "2026-10-14T22:31:00+09:00",
                "openPrice": "100", "highPrice": "102", "lowPrice": "99",
                "closePrice": "101", "volume": "300", "currency": "USD",
            }]}})
        assert req.headers["X-Tossinvest-Account"] == "3"
        if req.method == "POST":
            body = json.loads(req.content)
            assert body["type"] == "OCO" and body["orderType"] == "LIMIT"
            assert body["first"]["triggerPrice"] == "110"
            return httpx.Response(200, json={"result": {"conditionalOrderId": "condition-1"}})
        if req.method == "GET":
            return httpx.Response(200, json={"result": {"conditionalOrderId": "condition-1"}})
        return httpx.Response(204)

    api = client(handler)
    try:
        payload = {
            "symbol": "SOXL", "type": "OCO", "quantity": "2", "orderType": "LIMIT",
            "expireDate": "2026-10-15",
            "first": {"orderSide": "SELL", "triggerPrice": "110", "orderPrice": "109"},
            "second": {"orderSide": "SELL", "triggerPrice": "90", "orderPrice": "89"},
        }
        assert await api.create_oco(payload) == "condition-1"
        assert (await api.conditional_detail("condition-1"))["conditionalOrderId"] == "condition-1"
        assert await api.cancel_conditional("condition-1") == {}
        assert (await api.candles("SOXL", 6))[0]["volume"] == "300"
        assert [r.method for r in seen] == ["POST", "GET", "DELETE", "GET"]
    finally:
        await api.close()


@pytest.mark.parametrize("status", [400, 401, 403, 422])
async def test_explicit_rejection_is_sanitized(status):
    def handler(req):
        if req.url.path == "/oauth2/token":
            return token_response()
        return httpx.Response(
            status, json={"error": {"code": "invalid-request", "message": "private"}}
        )

    api = client(handler)
    try:
        with pytest.raises(BrokerRejected) as caught:
            await api.place(order())
        assert "private" not in str(caught.value)
    finally:
        await api.close()


async def test_query_failure_and_auth_cooldown():
    fail_auth = False

    def handler(req):
        if req.url.path == "/oauth2/token":
            return httpx.Response(403) if fail_auth else token_response()
        return httpx.Response(500, text="private-error")

    api = client(handler)
    try:
        with pytest.raises(TradingError, match="query failed"):
            await api.detail("opaque-ID")
        api.access_token = None
        with pytest.raises(SubmissionNotSent, match="cooling down"):
            await api.place(order())
        api.auth_retry_at = 0
        fail_auth = True
        with pytest.raises(SubmissionNotSent, match="authentication failed"):
            await api.place(order())
    finally:
        await api.close()
