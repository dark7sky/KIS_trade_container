import json

import httpx
import pytest

from kis_mcp.config import TossCredentials
from kis_mcp.models import BrokerRejected, SubmissionNotSent, TradingError, UncertainSubmission
from kis_mcp.toss import Toss


def client(handler):
    return Toss(TossCredentials("test-client", "test-secret", "3"),
                client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), interval=0)


def token_response():
    return httpx.Response(200, json={"access_token": "test-token", "token_type": "Bearer",
                                    "expires_in": 86400})


def order():
    return {"id": "b4d30471-90aa-4d86-b916-50d4c8e4c9c1", "symbol": "AAPL",
            "side": "buy", "quantity": 5, "order_type": "limit", "price": "185.5"}


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
        assert body == {"clientOrderId": order()["id"], "symbol": "AAPL", "side": "BUY",
                        "orderType": "LIMIT", "timeInForce": "DAY", "quantity": "5",
                        "price": "185.5"}
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
            return httpx.Response(200, json={"result": {"orders": [], "hasNext": False,
                                                        "nextCursor": None}})
        return httpx.Response(200, json={"result": {"orders": [], "hasNext": True,
                                                    "nextCursor": "repeat"}})

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
        return httpx.Response(200, json={"result": [{"symbol": "ABC123", "market": "KOSPI",
                                                     "currency": "KRW", "status": "ACTIVE"}]})

    api = client(handler)
    try:
        with pytest.raises(TradingError, match="US"):
            await api.capacity("ABC123", "1")
    finally:
        await api.close()
