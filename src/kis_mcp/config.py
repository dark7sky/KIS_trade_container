import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True, repr=False)
class Credentials:
    key: str
    secret: str
    account: str
    product: str

    def validate(self):
        if (
            not self.key
            or not self.secret
            or not re.fullmatch(r"\d{8}", self.account)
            or not re.fullmatch(r"\d{2}", self.product)
        ):
            raise ValueError(
                "KIS credentials must include key, secret, 8-digit account and 2-digit product code"
            )

    @property
    def fingerprint(self):
        return hashlib.sha256(f"{self.account}:{self.product}".encode()).hexdigest()


@dataclass(repr=False)
class Settings:
    data_dir: Path
    credentials: dict[str, Credentials]
    public_url: str
    issuer: str
    allowed_subject: str
    telegram_token: str = ""
    telegram_chat: str = ""
    poll_seconds: float = 10
    trusted_proxies: str = "127.0.0.1"
    allowed_origins: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls):
        def required(name):
            value = os.environ.get(name, "").strip()
            if not value:
                raise ValueError(f"Missing required configuration: {name}")
            return value

        credentials = {}
        for mode in ("real", "demo"):
            prefix = f"KIS_{mode.upper()}_"
            credentials[mode] = Credentials(
                *(
                    required(prefix + key)
                    for key in ("APP_KEY", "APP_SECRET", "ACCOUNT_NO", "ACCOUNT_PRODUCT_CODE")
                )
            )
            credentials[mode].validate()
        public_url = required("MCP_PUBLIC_URL").rstrip("/")
        issuer = required("OAUTH_ISSUER").rstrip("/")
        for url in (public_url, issuer):
            parsed = urlparse(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Public MCP URL and OAuth issuer must be HTTPS URLs")
        if urlparse(public_url).path:
            raise ValueError("MCP_PUBLIC_URL must be an origin without a path; endpoint is /mcp")
        interval = float(os.environ.get("ORDER_SUPERVISOR_INTERVAL_SECONDS") or "10")
        if not 1 <= interval <= 3600:
            raise ValueError("Polling interval must be between 1 and 3600 seconds")
        return cls(
            Path(required("DATA_DIR")),
            credentials,
            public_url,
            issuer,
            required("OAUTH_ALLOWED_SUB"),
            required("TELEGRAM_BOT_TOKEN"),
            required("TELEGRAM_ALLOWED_CHAT_ID"),
            interval,
            required("TRUSTED_PROXY_IPS"),
            [
                s.strip()
                for s in os.environ.get("MCP_ALLOWED_ORIGINS", "https://chatgpt.com").split(",")
                if s.strip()
            ],
        )
