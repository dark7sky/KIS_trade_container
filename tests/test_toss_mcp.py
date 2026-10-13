import asyncio
import json

import httpx
from test_integrations import rsa_keys as rsa_keys
from test_integrations import signed
from test_toss_trading import FakeToss

from kis_mcp.app import create_app
from kis_mcp.auth import KeycloakVerifier
from kis_mcp.config import TossCredentials


async def test_toss_authenticated_mcp_lifecycle(settings, broker, rsa_keys):
    settings.toss_credentials = TossCredentials("test-client", "test-secret", "3")
    key, jwk = rsa_keys
    verifier = KeycloakVerifier(
        settings,
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"keys": [jwk]}))
        ),
    )
    toss_broker = FakeToss()
    app = create_app(settings, broker=broker, toss_broker=toss_broker, verifier=verifier)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=settings.public_url
        ) as client:
            headers = {
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            }

            async def rpc(method, params):
                return await client.post(
                    "/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )

            init = {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            }
            assert (await rpc("initialize", init)).status_code == 401
            headers["Authorization"] = "Bearer " + signed(settings, key)
            response = await rpc("initialize", init)
            assert response.json()["result"]["serverInfo"]["name"] == "Personal Trading"
            tools = {t["name"]: t for t in (await rpc("tools/list", {})).json()["result"]["tools"]}
            assert tools["toss_place_order"]["annotations"]["destructiveHint"]
            assert tools["toss_place_order"]["_meta"]["securitySchemes"][0]["type"] == "oauth2"
            assert "quantity" not in tools["toss_cancel_order"]["inputSchema"]["properties"]

            async def call(name, arguments):
                result = (await rpc("tools/call", {"name": name, "arguments": arguments})).json()[
                    "result"
                ]
                assert not result.get("isError"), result
                return json.loads(result["content"][0]["text"])

            args = {
                "client_request_id": "mcp-toss-1",
                "symbol": "AAPL",
                "side": "buy",
                "quantity": 2,
                "price": "185.50",
            }
            placed = await call("toss_place_order", args)
            assert (await call("toss_place_order", args))["id"] == placed["id"]
            assert (await call("toss_get_order", {"order_id": placed["id"]}))[
                "status"
            ] == "accepted"
            assert (
                await call(
                    "toss_cancel_order",
                    {"client_request_id": "mcp-cancel-1", "order_id": placed["id"]},
                )
            )["status"] == "accepted"
            status = await call("get_status", {})
            assert status["brokers"]["toss"]["supported_modes"] == ["real"]
            assert not broker.placed and len(toss_broker.placed) == 1
            task = app.state.worker_tasks[-1]
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert (await client.get("/healthz")).status_code == 503


async def test_toss_disabled_is_explicit(settings, broker):
    app = create_app(settings, broker=broker)
    async with app.router.lifespan_context(app):
        tools = {tool.name for tool in await app.state.mcp.list_tools()}
        assert "toss_place_order" in tools
        result = await app.state.toss_service.status()
        assert result["enabled"] is False
