import asyncio
import json
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
from .models import (
    CancelInput,
    Exchange,
    Mode,
    OrderInput,
    OrderType,
    Side,
    TossOrderInput,
    TradingError,
)
from .service import TradingService
from .store import Store
from .telegram import Notifications
from .toss import Toss
from .toss_service import TossTradingService

Symbol = Annotated[str, Field(pattern=r"^(?:\d{6}|Q\d{6})$")]
USSymbol = Annotated[str, Field(min_length=1, max_length=20, pattern=r"^[A-Z][A-Z0-9.\-]*$")]


def public_error(exc):
    message = str(exc)
    code = getattr(exc, "code", None)
    if code is None:
        if "mode changed" in message:
            code = "mode_mismatch"
        elif "Unknown MCP order ID" in message:
            code = "order_unknown"
        elif "rate" in message.lower() or "cooling down" in message:
            code = "broker_rate_limited"
        elif "KIS query failed" in message or "timeout" in message.lower():
            code = "broker_timeout"
        elif "rejected" in message.lower():
            code = "broker_rejected"
        elif "needs_review" in message:
            code = "needs_review"
        else:
            code = "trading_error"
    return {"error": {"code": code, "message": message}}


def runtime_health(service, tasks, broker):
    workers_ok = bool(tasks) and all(not task.done() for task in tasks)
    store_ok = False
    if service is not None:
        try:
            service.store.mode()
            store_ok = True
        except Exception:
            store_ok = False
    ready = service is not None and store_ok and workers_ok
    return (
        {
            "status": "ok" if ready else "degraded",
            "store": "ok" if store_ok else "unavailable",
            "workers": "ok" if workers_ok else "unavailable",
            "broker": getattr(broker, "health", {}),
        },
        200 if ready else 503,
    )


def create_app(settings=None, *, broker=None, toss_broker=None, verifier=None, notifier=None):
    settings = settings or Settings.from_env()
    # HTTP request logs would expose the Telegram token in its URL.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    broker = broker or KIS(settings)
    toss_broker = toss_broker or (
        Toss(settings.toss_credentials) if settings.toss_credentials else None
    )
    verifier = verifier or KeycloakVerifier(settings)
    service = None
    toss_service = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal service, toss_service
        store = Store(settings.data_dir)
        notifications = notifier or Notifications(settings, store)
        service = TradingService(settings, store, broker)
        toss_service = TossTradingService(settings, store, toss_broker, lock=service.lock)
        app.state.service = service
        app.state.toss_service = toss_service
        tasks = [
            asyncio.create_task(service.supervise()),
            asyncio.create_task(notifications.run()),
            asyncio.create_task(toss_service.supervise()),
        ]
        app.state.worker_tasks = tasks
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
            if toss_broker is not None:
                await toss_broker.close()
            await notifications.close()
            await verifier.close()
            store.close()

    hostname = urlparse(settings.public_url).netloc
    mcp = FastMCP(
        "Personal Trading",
        instructions="Personal cash trading: existing tools are KIS Korean stocks; toss_* tools are Toss US stocks in USD, whole shares, DAY limit/market orders, real mode only. Toss cancellation cancels the entire remainder. Orders execute immediately. First use get_status to see the server-global mode; set_mode changes it for every conversation and persists across restarts. Demo must never fall back to a real account. Get/cancel use the order's original account even after mode changes. Never infer a request to trade from market information. Reuse client_request_id only when retrying the same request. Accepted is not filled or cancelled. Never create another order to retry an unknown submission. Resolve uncertain orders only after the user confirms either their broker order number or that no broker order exists. Use the matching broker's tools; never switch brokers to retry. Toss market cash quantity is not guaranteed by a quote; final capacity and session eligibility are checked by the broker.",
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
            raise ValueError(json.dumps(public_error(exc), ensure_ascii=False)) from None
        except Exception:
            raise ValueError(
                json.dumps(
                    {
                        "error": {
                            "code": "operation_failed",
                            "message": "Operation failed; inspect server status and retained order state",
                        }
                    },
                    ensure_ascii=False,
                )
            ) from None

    @mcp.tool(annotations=read, meta=oauth_meta)
    async def get_status() -> dict:
        """현재 서버 전체 실전/모의 모드, API 연결 기록, 추적 및 알림 상태."""
        result = await safe(service.status())
        result["brokers"] = {
            "kis": {"enabled": True, "market": "KR", "currency": "KRW"},
            "toss": await safe(toss_service.status()),
        }
        return result

    @mcp.tool(annotations=write, meta=oauth_meta)
    async def set_mode(mode: Mode) -> dict:
        """모든 대화에 적용되는 거래 모드를 전환하고 재시작 후에도 유지. 기본 real."""
        return await safe(service.set_mode(mode))

    @mcp.tool(annotations=read, meta=oauth_meta)
    async def get_account(exchange: Exchange = "KRX") -> dict:
        """활성 모드 계좌의 예수금, 보유종목, 평가금액·손익과 가격 관측시각 조회."""
        return await safe(service.account(exchange))

    @mcp.tool(annotations=read, meta=oauth_meta)
    async def get_quote(symbol: Symbol, exchange: Exchange = "KRX") -> dict:
        """종목코드로 거래소별 현재가·10단계 호가와 가격 관측시각 조회."""
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
        expected_mode: Mode | None = None,
    ) -> dict:
        """즉시 현금 주문. 지정가는 양의 정수 원, 시장가는 price=0. 재시도에는 같은 요청 ID 사용. expected_mode가 현재 모드와 다르면 전송하지 않음. accepted는 접수이며 체결 아님."""
        request = OrderInput(
            client_request_id=client_request_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            exchange=exchange,
            order_type=order_type,
            price=price,
            expected_mode=expected_mode,
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
        expected_mode: Mode | None = None,
    ) -> dict:
        """원래 계좌의 미체결 잔량 취소. quantity 생략 시 가능한 잔량 전부. expected_mode가 원주문 모드와 다르면 전송하지 않음. accepted는 취소 접수이며 완료는 추적 확인."""
        return await safe(
            service.cancel(
                CancelInput(
                    client_request_id=client_request_id,
                    order_id=order_id,
                    quantity=quantity,
                    expected_mode=expected_mode,
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

    @mcp.tool(annotations=read, meta=oauth_meta)
    async def toss_get_account() -> dict:
        """토스 실전 계좌의 미국주식 보유수량·평균매입가·관측가격과 USD 현금 매수가능액."""
        return await safe(toss_service.account())

    @mcp.tool(annotations=read, meta=oauth_meta)
    async def toss_get_quote(symbol: USSymbol) -> dict:
        """미국 티커(AAPL 등)의 토스 현재가(USD), 브로커 시각과 서버 관측시각."""
        return await safe(toss_service.quote(symbol))

    @mcp.tool(annotations=read, meta=oauth_meta)
    async def toss_get_order_capacity(symbol: USSymbol) -> dict:
        """토스 USD 현금 매수가능금액·매도가능수량. 시장가 매수가능수량 보장은 제공하지 않음."""
        return await safe(toss_service.capacity(symbol, Decimal("0")))

    @mcp.tool(annotations=read, meta=oauth_meta)
    async def toss_get_market_calendar() -> dict:
        """미국 전일·당일·익일 영업일의 세션 시간(한국시간). 주문 가능 여부의 최종 판단은 토스가 수행."""
        return await safe(toss_service.calendar())

    @mcp.tool(annotations=write, meta=oauth_meta)
    async def toss_place_order(
        client_request_id: str,
        symbol: USSymbol,
        side: Side,
        quantity: Annotated[int, Field(strict=True, gt=0)],
        order_type: OrderType = "limit",
        price: Annotated[Decimal, Field(ge=0, le=1000000000, allow_inf_nan=False)] = Decimal("0"),
    ) -> dict:
        """토스 미국주식 실전 즉시 주문, 정수 수량·DAY만 지원. 지정가는 USD($1 이상 소수 2자리, 미만 4자리), 시장가는 price=0. demo 차단. 같은 요청 재시도는 같은 ID. unknown은 재주문 금지, accepted는 체결 아님."""
        request = TossOrderInput(
            client_request_id=client_request_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            price=price,
        )
        return await safe(toss_service.place(request))

    @mcp.tool(annotations=read, meta=oauth_meta)
    async def toss_list_orders(start_date: str, end_date: str) -> dict:
        """토스 미국주식 주문과 MCP 저장 스냅샷. YYYY-MM-DD, 최근 90일, 주문 생성일의 한국시간 기준."""
        return await safe(toss_service.list_orders(start_date, end_date))

    @mcp.tool(annotations=read, meta=oauth_meta)
    async def toss_get_order(order_id: str) -> dict:
        """토스 MCP order_id로 원래 실전 계좌의 최신 체결·취소 조회. 현재 모드와 무관."""
        return await safe(toss_service.get_order(order_id))

    @mcp.tool(annotations=write, meta=oauth_meta)
    async def toss_cancel_order(client_request_id: str, order_id: str) -> dict:
        """토스 원래 실전 계좌의 미체결 잔량 전체 취소. 부분 수량 취소 미지원. 현재 모드와 무관. 같은 요청 재시도는 같은 ID. accepted는 접수이며 실제 취소 완료는 추적 확인."""
        return await safe(toss_service.cancel(client_request_id, order_id))

    @mcp.tool(annotations=write, meta=oauth_meta)
    async def toss_resolve_order(order_id: str, broker_id: str) -> dict:
        """응답 유실 복구 전용. 사용자가 토스 내역에서 본인 주문임을 확인한 broker_id만 연결. 거래 전송 없음."""
        return await safe(toss_service.resolve_order(order_id, broker_id))

    @mcp.tool(annotations=write, meta=oauth_meta)
    async def toss_resolve_order_not_submitted(order_id: str) -> dict:
        """사용자가 토스 내역·고객지원에서 미접수를 확인한 unknown 주문만 종결. 후보 없음을 재검사하며 새 거래 전송 없음."""
        return await safe(toss_service.resolve_order_not_submitted(order_id))

    @mcp.custom_route("/healthz", methods=["GET"])
    async def health(request):
        body, status_code = runtime_health(
            getattr(request.app.state, "service", None),
            getattr(request.app.state, "worker_tasks", []),
            broker,
        )
        body["brokers"] = {
            "kis": broker.health,
            "toss": getattr(toss_broker, "health", {"status": "disabled"}),
        }
        return JSONResponse(body, status_code=status_code)

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
