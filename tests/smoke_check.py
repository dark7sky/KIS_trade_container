"""Run only INSIDE the isolated compose.smoke.yaml MCP container.

This changes the THROWAWAY Keycloak realm to issue a fixture access token.
It refuses real KIS credentials and never calls KIS or Telegram.
"""

import asyncio
import os
from pathlib import Path

import httpx

from kis_mcp.auth import KeycloakVerifier
from kis_mcp.config import Settings


async def main():
    assert os.environ["KIS_REAL_APP_KEY"] == "fake-real-key"
    assert os.environ["MCP_PUBLIC_URL"] == "https://mcp.test"
    assert not list(Path("/app").rglob(".env"))
    async with httpx.AsyncClient(base_url="http://keycloak:8080", timeout=20) as client:
        discovery = await client.get("/realms/kis/.well-known/openid-configuration")
        discovery.raise_for_status()
        metadata = discovery.json()
        assert metadata["issuer"] == "https://auth.test/realms/kis"
        assert "S256" in metadata["code_challenge_methods_supported"]
        response = await client.post(
            "/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": "smokeadmin",
                "password": "FakeAdminPasswordForTests123!",
            },
        )
        response.raise_for_status()
        headers = {"Authorization": "Bearer " + response.json()["access_token"]}
        response = await client.get(
            "/admin/realms/kis/clients", params={"clientId": "chatgpt-kis"}, headers=headers
        )
        response.raise_for_status()
        config = response.json()[0]
        assert config["attributes"]["pkce.code.challenge.method"] == "S256"
        assert config["directAccessGrantsEnabled"] is False
        assert set(config["defaultClientScopes"]) >= {"basic", "profile", "email", "kis:access"}
        audience_mapper = next(
            m for m in config["protocolMappers"] if m["protocolMapper"] == "oidc-audience-mapper"
        )
        assert audience_mapper["config"]["included.custom.audience"] == "https://mcp.test/mcp"
        # Disposable fixture only: bypass interactive password change/OTP so
        # the real Keycloak signing key and token claim mapping can be tested.
        config["directAccessGrantsEnabled"] = True
        response = await client.put(
            "/admin/realms/kis/clients/" + config["id"], headers=headers, json=config
        )
        response.raise_for_status()
        user = "/admin/realms/kis/users/10000000-0000-4000-8000-000000000001"
        response = await client.put(
            user,
            headers=headers,
            json={
                "requiredActions": [],
                "email": "trader@example.test",
                "firstName": "Test",
                "lastName": "Trader",
            },
        )
        response.raise_for_status()
        response = await client.put(
            user + "/reset-password",
            headers=headers,
            json={
                "type": "password",
                "value": "FakeInitialPasswordForTests123!",
                "temporary": False,
            },
        )
        response.raise_for_status()
        response = await client.post(
            "/realms/kis/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "chatgpt-kis",
                "client_secret": "fake-client-secret-for-isolated-tests",
                "username": "trader",
                "password": "FakeInitialPasswordForTests123!",
                "scope": "openid profile email kis:access",
            },
        )
        response.raise_for_status()
        token = response.json()["access_token"]
        response = await client.get("/realms/kis/protocol/openid-connect/certs")
        response.raise_for_status()
        keys = response.json()
    settings = Settings.from_env()
    verifier = KeycloakVerifier(
        settings,
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=keys))),
    )
    try:
        assert await verifier.verify_token(token) is not None, (
            "Real Keycloak token failed application verification"
        )
    finally:
        await verifier.close()
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8000", headers={"Host": "mcp.test"}
    ) as client:
        assert (await client.get("/healthz")).status_code == 200
        response = await client.get("/.well-known/oauth-protected-resource/mcp")
        assert response.json()["resource"] == "https://mcp.test/mcp"
        response = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        assert response.status_code == 401
    print(
        "PASS: PostgreSQL-backed Keycloak import, PKCE config, token claims/signature, MCP health/auth, image env exclusion"
    )


if __name__ == "__main__":
    asyncio.run(main())
