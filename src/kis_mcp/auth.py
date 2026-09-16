import asyncio
import time

import httpx
import jwt
from mcp.server.auth.provider import AccessToken


class KeycloakVerifier:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=10, follow_redirects=False)
        self.keys = {}
        self.loaded_at = 0.0
        self.last_attempt = 0.0
        self.lock = asyncio.Lock()

    async def verify_token(self, token):
        try:
            if len(token) > 16384:
                return None
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                return None
            kid = header["kid"]
            async with self.lock:
                now = time.monotonic()
                if (
                    kid not in self.keys or now - self.loaded_at > 300
                ) and now - self.last_attempt > 5:
                    self.last_attempt = now
                    response = await self.client.get(
                        self.settings.issuer + "/protocol/openid-connect/certs"
                    )
                    response.raise_for_status()
                    self.keys = {
                        jwk["kid"]: jwt.PyJWK.from_dict(jwk).key
                        for jwk in response.json()["keys"]
                        if jwk.get("kty") == "RSA" and jwk.get("use", "sig") == "sig"
                    }
                    self.loaded_at = now
            if kid not in self.keys or time.monotonic() - self.loaded_at > 600:
                return None
            resource = self.settings.public_url + "/mcp"
            claims = jwt.decode(
                token,
                self.keys[kid],
                algorithms=["RS256"],
                audience=resource,
                issuer=self.settings.issuer,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
                leeway=5,
            )
            scopes = claims.get("scope", "").split()
            if (
                claims["sub"] != self.settings.allowed_subject
                or "kis:access" not in scopes
                or claims.get("azp") != "chatgpt-kis"
            ):
                return None
            return AccessToken(
                token=token,
                client_id="chatgpt-kis",
                scopes=scopes,
                expires_at=int(claims["exp"]),
                resource=resource,
                subject=claims["sub"],
            )
        except Exception:
            return None

    async def close(self):
        await self.client.aclose()
