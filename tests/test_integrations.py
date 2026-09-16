import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from kis_mcp.app import create_app
from kis_mcp.auth import KeycloakVerifier
from kis_mcp.kis import KIS
from kis_mcp.models import SubmissionNotSent, TradingError, UncertainSubmission
from kis_mcp.store import Store
from kis_mcp.telegram import Notifications


@pytest.fixture
def rsa_keys():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {
        "kid": "test-key",
        "use": "sig",
    }
    return key, jwk


def signed(settings, key, **changes):
    claims = {
        "iss": settings.issuer,
        "aud": settings.public_url + "/mcp",
        "sub": settings.allowed_subject,
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
        "azp": "chatgpt-kis",
        "scope": "kis:access",
    } | changes
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"sub": "other-user"},
        {"aud": "other-resource"},
        {"iss": "https://wrong.test"},
        {"exp": 1},
        {"scope": "profile"},
        {"azp": "other-client"},
    ],
)
async def test_oauth_verifier(settings, rsa_keys, changes):
    key, jwk = rsa_keys
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"keys": [jwk]}))
    )
    verifier = KeycloakVerifier(settings, client)
    result = await verifier.verify_token(signed(settings, key, **changes))
    assert (result is not None) == (not changes)
    assert await verifier.verify_token("garbage") is None
    await verifier.close()


def test_mcp_http_auth_initialization_tools_and_call(settings, rsa_keys, broker):
    key, jwk = rsa_keys
    verifier = KeycloakVerifier(
        settings,
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"keys": [jwk]}))
        ),
    )
    app = create_app(settings, broker=broker, verifier=verifier)
    with TestClient(app, base_url=settings.public_url) as client:
        assert client.get("/healthz").status_code == 200
        meta = client.get("/.well-known/oauth-protected-resource/mcp")
        assert meta.status_code == 200
        assert meta.json()["resource"] == settings.public_url + "/mcp"
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
        unauthorized = client.post("/mcp", headers=headers, json=request)
        assert unauthorized.status_code == 401
        assert "resource_metadata" in unauthorized.headers["www-authenticate"]
        headers["Authorization"] = "Bearer " + signed(settings, key)
        assert client.post("/mcp", headers=headers, json=request).status_code == 200
        headers["MCP-Protocol-Version"] = "2025-11-25"
        response = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        tools = {t["name"]: t for t in response.json()["result"]["tools"]}
        assert len(tools) == 10
        assert tools["place_order"]["annotations"]["destructiveHint"] is True
        assert tools["place_order"]["_meta"]["securitySchemes"][0]["type"] == "oauth2"
        response = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "set_mode", "arguments": {"mode": "demo"}},
            },
        )
        assert response.status_code == 200
        assert not response.json()["result"].get("isError"), response.json()
        assert json.loads(response.json()["result"]["content"][0]["text"])["mode"] == "demo"
        response = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "get_status", "arguments": {}},
            },
        )
        assert json.loads(response.json()["result"]["content"][0]["text"])["mode"] == "demo"
        assert (
            client.post(
                "/mcp", headers=headers | {"Origin": "https://evil.test"}, json=request
            ).status_code
            == 403
        )
        assert (
            client.post("/mcp", headers=headers | {"Host": "evil.test"}, json=request).status_code
            == 421
        )
        headers["Authorization"] = "Bearer " + signed(settings, key, sub="someone-else")
        assert client.post("/mcp", headers=headers, json=request).status_code == 401
    assert not broker.placed


async def test_kis_modes_and_no_write_retry(settings):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/oauth2/tokenP":
            payload = json.loads(request.content)
            assert payload["appkey"] == (
                "test-demo-key" if "vts" in request.url.host else "test-real-key"
            )
            return httpx.Response(200, json={"access_token": "test-token", "expires_in": 86400})
        if request.method == "POST":
            raise httpx.ReadTimeout("test timeout")
        return httpx.Response(200, json={"rt_cd": "0", "output": {}})

    broker = KIS(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    for mode in ("real", "demo"):
        await broker.call(mode, "/test", "TEST", {})
    assert len(broker.tokens) == 2
    with pytest.raises(UncertainSubmission):
        await broker.call("real", "/order", "WRITE", {}, write=True)
    assert len([r for r in seen if r.url.path == "/order"]) == 1
    await broker.close()


async def test_token_failure_before_write_is_not_unknown(settings):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(500)

    broker = KIS(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(SubmissionNotSent):
            await broker.call("real", "/order", "WRITE", {}, write=True)
        assert seen == ["/oauth2/tokenP"]
    finally:
        await broker.close()


async def test_cash_capacity_excludes_credit_and_unknown_holdings(settings):
    def handler(request):
        if request.url.path.endswith("inquire-psbl-order"):
            assert request.url.params["ORD_DVSN"] == "01"
            return httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "output": {
                        "nrcvb_buy_amt": "10000",
                        "nrcvb_buy_qty": "10",
                        "max_buy_qty": "999",
                    },
                },
            )
        assert request.url.params["INQR_DVSN"] == "01"
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output1": [
                    {"pdno": "005930", "loan_dt": "", "ord_psbl_qty": "3"},
                    {"pdno": "005930", "loan_dt": "20260901", "ord_psbl_qty": "100"},
                    {"pdno": "005930", "ord_psbl_qty": "200"},
                ],
                "output2": [],
            },
        )

    broker = KIS(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    broker.tokens["real"] = ("test-token", time.time() + 3600)
    try:
        result = await broker.capacity("real", "005930", 1000, "KRX")
        assert result == {"cash_buy_amount": "10000", "cash_buy_quantity": 10, "sell_quantity": 3}
    finally:
        await broker.close()


async def test_pagination_and_broken_cursor(settings):
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                200,
                headers={"tr_cont": "M"},
                json={
                    "rt_cd": "0",
                    "output1": [{"id": 1}],
                    "ctx_area_fk100": "a",
                    "ctx_area_nk100": "b",
                },
            )
        assert request.headers["tr_cont"] == "N"
        assert request.url.params["CTX_AREA_NK100"] == "b"
        return httpx.Response(200, json={"rt_cd": "0", "output1": [{"id": 2}]})

    broker = KIS(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    broker.tokens["real"] = ("test-token", time.time() + 3600)
    rows, _ = await broker.pages("real", "/query", "READ", {})
    assert rows == [{"id": 1}, {"id": 2}]
    await broker.close()
    broker = KIS(
        settings,
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    headers={"tr_cont": "M"},
                    json={"rt_cd": "0", "output1": [], "ctx_area_fk100": "", "ctx_area_nk100": ""},
                )
            )
        ),
    )
    broker.tokens["real"] = ("test-token", time.time() + 3600)
    with pytest.raises(TradingError, match="pagination incomplete"):
        await broker.pages("real", "/query", "READ", {})
    await broker.close()


async def test_telegram_retry_and_duplicate_suppression(settings):
    store = Store(settings.data_dir)
    seen = []

    def handler(request):
        seen.append(request)
        assert json.loads(request.content)["chat_id"] == settings.telegram_chat
        return (
            httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 30}})
            if len(seen) == 1
            else httpx.Response(200, json={"ok": True})
        )

    notifier = Notifications(
        settings, store, httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    try:
        store.event("fill:1", {"type": "fill"}, "test fill")
        await notifier.flush()
        assert store.pending_count() == 1
        await notifier.flush()
        assert len(seen) == 1
        store.db.execute("UPDATE outbox SET next_attempt=0")
        await notifier.flush()
        assert store.pending_count() == 0
        store.event("fill:1", {"type": "fill"}, "test fill")
        await notifier.flush()
        assert len(seen) == 2
    finally:
        await notifier.close()
        store.close()
