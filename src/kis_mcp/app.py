import asyncio
import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Annotated
from urllib.parse import urlparse

import uvicorn
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl, Field
from starlette.responses import JSONResponse

from .auth import KeycloakVerifier
from .config import Settings
from .kis import KIS
from .models import CancelInput, Exchange, Mode, OrderInput, OrderType, Side, TradingError
from .service import TradingService
from .store import Store
from .telegram import Notifications

Symbol = Annotated[str, Field(pattern=r"^(?:\d{6}|Q\d{6})$")]


def create_app(settings=None, *, broker=None, verifier=None, notifier=None):
    settings = settings or Settings.from_env()
    # HTTP request logs would expose the Telegram token in its URL.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    broker = broker or KIS(settings)
    verifier = verifier or KeycloakVerifier(settings)
    service = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal service
        store = Store(settings.data_dir)
        notifications = notifier or Notifications(settings, store)
        service = TradingService(settings, store, broker)
        app.state.service = service
        tasks = [asyncio.create_task(service.supervise()), asyncio.create_task(notifications.run())]
        try:
            # FastMCP's low-level lifespan is per request in stateless mode.
            # The database and workers instead belong to the ASGI application.
            async with mcp.session_manager.run():
                yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await broker.close()
            await notifications.close()
            await verifier.close()
            store.close()

    hostname = urlparse(settings.public_url).netloc
    mcp = FastMCP(
        "KIS Trading",
        instructions="Personal KIS cash trading. Orders execute immediately. First use get_status to see the server-global mode; set_mode changes it for every conversation and persists across restarts. Never infer a request to trade from market information. Reuse client_request_id only when retrying the same request. Accepted is not filled. Never create another order to retry an unknown submission. Resolve uncertain orders only after the user confirms either their KIS order number or that no broker order exists.",
        host="0.0.0.0",
        stateless_http=True,
        json_response=True,
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(settings.issuer),
            resource_server_url=AnyHttpUrl(settings.public_url + "/mcp"),
            required_scopes=["kis:access"],
            validate_token_resource=True,
        ),
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[hostname],
            allowed_origins=settings.allowed_origins,
        ),
    )

    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
    oauth_meta = {"securitySchemes": [{"type": "oauth2", "scopes": ["kis:access"]}]}
    write = ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True
    )

    async def safe(awaitable):
        try:
            return await awaitable
        except TradingError as exc:
            raise ValueError(str(exc)) from None
        except Exception:
            raise ValueError(
                "Operation failed; inspect server status and retained order state"
            ) from None

    @mcp.tool(annotations=read, meta=oauth_meta)
    async def get_status() -> dict:
        """현재 서버 전체 실전/모의 모드, API 연결 기록, 추적 및 알림 상태."""
        return await safe(service.status())

    @mcp.tool(annotations=write, meta=oauth_meta)
    async def set_mode(mode: Mode) -> dict:
        """모든 대화에 적용되는 거래 모드를 전환하고 재시작 후에도 유지. 기본 real."""
        return await safe(service.set_mode(mode))

    @mcp.tool(annotations=read, meta=oauth_meta)
    async def get_account(exchange: Exchange = "KRX") -> dict:
        """활성 모드 계좌의 예수금, 보유종목, 평가금액·손익 조회."""
        return await safe(service.account(exchange))

    @mcp.tool(annotations=read, meta=oauth_meta)
    async def get_quote(symbol: Symbol, exchange: Exchange = "KRX") -> dict:
        """종목코드로 거래소별 현재가와 10단계 호가 조회."""
        return await safe(service.quote(symbol, exchange))

    @mcp.tool(annotations=read, meta=oauth_meta)
    async def get_order_capacity(
        symbol: Symbol,
        price: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] = Decimal("0"),
        exchange: Exchange = "KRX",
    ) -> dict:
        """미수 없는 현금 매수가능금액·수량과 현금 보유주식 매도가능수량 조회."""
        return await safe(service.capacity(symbol, price, exchange))

    @mcp.tool(annotations=write, meta=oauth_meta)
    async def place_order(
        client_request_id: str,
        symbol: Symbol,
        side: Side,
        quantity: Annotated[int, Field(strict=True, gt=0)],
        exchange: Exchange = "KRX",
        order_type: OrderType = "limit",
        price: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] = Decimal("0"),
    ) -> dict:
        """즉시 현금 주문. 지정가는 양의 정수 원, 시장가는 price=0. 요청 ID를 생성하고 재시도에는 같은 ID 사용. accepted는 접수일 뿐 체결 아님."""
        request = OrderInput(
            client_request_id=client_request_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            exchange=exchange,
            order_type=order_type,
            price=price,
        )
        return await safe(service.place(request))

    @mcp.tool(annotations=read, meta=oauth_meta)
    async def list_orders(start_date: str, end_date: str) -> dict:
        """활성 계좌 최근 90일 내 날짜 범위(YYYY-MM-DD)의 주문·체결과 MCP 주문 조회. 외부 주문은 알림 추적 대상 아님."""
        return await safe(service.list_orders(start_date, end_date))

    @mcp.tool(annotations=read, meta=oauth_meta)
    async def get_order(order_id: str) -> dict:
        """MCP order_id로 원래 계좌에서 최신 체결·취소 상태 조회. 현재 모드와 무관."""
        return await safe(service.get_order(order_id))

    @mcp.tool(annotations=write, meta=oauth_meta)
    async def cancel_order(
        client_request_id: str,
        order_id: str,
        quantity: Annotated[int | None, Field(strict=True, gt=0)] = None,
    ) -> dict:
        """원래 계좌의 미체결 잔량 취소. quantity 생략 시 가능한 잔량 전부. accepted는 취소 접수이며 완료는 추적 확인."""
        return await safe(
            service.cancel(
                CancelInput(
                    client_request_id=client_request_id, order_id=order_id, quantity=quantity
                )
            )
        )

    @mcp.tool(annotations=write, meta=oauth_meta)
    async def resolve_order(order_id: str, broker_id: str) -> dict:
        """응답 유실 주문 복구 전용. 사용자가 KIS 내역에서 본인 주문임을 확인한 broker_id만 연결. 새 거래를 전송하지 않음."""
        return await safe(service.resolve_order(order_id, broker_id))

    @mcp.tool(annotations=write, meta=oauth_meta)
    async def resolve_order_not_submitted(order_id: str) -> dict:
        """응답 유실 주문의 미접수 확정 전용. 사용자가 KIS 주문내역에서 주문이 없음을 확인한 경우만 호출. 새 거래를 전송하지 않으며 같은 요청 ID는 not_submitted를 반환함."""
        return await safe(service.resolve_order_not_submitted(order_id))

    @mcp.custom_route("/healthz", methods=["GET"])
    async def health(request):
        return JSONResponse({"status": "ok"})

    app = mcp.streamable_http_app()
    app.router.lifespan_context = lifespan
    app.state.mcp = mcp
    return app


def main():
    settings = Settings.from_env()
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=8000,
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips=settings.trusted_proxies,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
