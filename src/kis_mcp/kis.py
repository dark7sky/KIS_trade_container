"""KIS REST adapter. Broker response bodies are never logged or exposed verbatim."""

import asyncio
import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import httpx

from .models import BrokerRejected, SubmissionNotSent, TradingError, UncertainSubmission

KST = ZoneInfo("Asia/Seoul")
BASE = {
    "real": "https://openapi.koreainvestment.com:9443",
    "demo": "https://openapivts.koreainvestment.com:29443",
}


def number(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise TradingError("Broker returned an invalid numeric field") from None


def integer(value):
    result = number(value)
    if result != result.to_integral_value():
        raise TradingError("Broker returned a non-integer quantity")
    return int(result)


def items(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    raise TradingError("Broker returned an unexpected response shape")


class KIS:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)
        self.tokens = {}
        self.token_locks = {mode: asyncio.Lock() for mode in BASE}
        self.rate_locks = {mode: asyncio.Lock() for mode in BASE}
        self.call_locks = {mode: asyncio.Lock() for mode in BASE}
        self.last_call = {mode: 0.0 for mode in BASE}
        self.last_token_attempt = {mode: 0.0 for mode in BASE}
        self.health = {mode: "not_checked" for mode in BASE}

    async def close(self):
        await self.client.aclose()

    def account(self, mode):
        credentials = self.settings.credentials[mode]
        return {"CANO": credentials.account, "ACNT_PRDT_CD": credentials.product}

    def tr(self, mode, value):
        return ("V" + value[1:]) if mode == "demo" else value

    async def throttle(self, mode):
        async with self.rate_locks[mode]:
            delay = (1.05 if mode == "demo" else 0.15) - (time.monotonic() - self.last_call[mode])
            if delay > 0:
                await asyncio.sleep(delay)
            self.last_call[mode] = time.monotonic()

    async def token(self, mode):
        async with self.token_locks[mode]:
            token = self.tokens.get(mode)
            if token and token[1] > time.time() + 60:
                return token[0]
            if time.monotonic() - self.last_token_attempt[mode] < 61:
                raise TradingError("KIS token issuance cooling down; retry later")
            self.last_token_attempt[mode] = time.monotonic()
            credentials = self.settings.credentials[mode]
            try:
                response = await self.client.post(
                    BASE[mode] + "/oauth2/tokenP",
                    json={
                        "grant_type": "client_credentials",
                        "appkey": credentials.key,
                        "appsecret": credentials.secret,
                    },
                )
                response.raise_for_status()
                body = response.json()
                token = str(body["access_token"])
                expiry = int(body["expires_in"])
                if not token or expiry <= 60:
                    raise ValueError
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                self.health[mode] = "token_error"
                raise TradingError("KIS token issuance failed") from None
            self.tokens[mode] = (token, time.time() + expiry)
            return token

    async def call(self, mode, path, tr_id, params, *, write=False, continuation=""):
        try:
            token = await self.token(mode)
        except TradingError:
            if write:
                raise SubmissionNotSent(
                    "Order request was not sent: KIS token unavailable"
                ) from None
            raise
        credentials = self.settings.credentials[mode]
        for attempt in range(1 if write else 3):
            headers = {
                "authorization": "Bearer " + token,
                "appkey": credentials.key,
                "appsecret": credentials.secret,
                "tr_id": tr_id,
                "custtype": "P",
                "tr_cont": continuation,
            }
            try:
                async with self.call_locks[mode]:
                    await self.throttle(mode)
                    response = await self.client.request(
                        "POST" if write else "GET",
                        BASE[mode] + path,
                        headers=headers,
                        **({"json": params} if write else {"params": params}),
                    )
                if response.status_code >= 500 or response.status_code == 429:
                    if write:
                        raise UncertainSubmission(
                            "Broker submission result is unknown; do not resubmit"
                        )
                    if attempt < 2:
                        await asyncio.sleep(2**attempt)
                        continue
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict) or "rt_cd" not in body:
                    raise ValueError
                if str(body["rt_cd"]) != "0":
                    code = str(body.get("msg_cd", ""))
                    if code in ("EGW00123", "EGW00121"):
                        self.tokens.pop(mode, None)
                        if not write and attempt < 2:
                            token = await self.token(mode)
                            continue
                    if code == "EGW00201" and not write and attempt < 2:
                        await asyncio.sleep(2**attempt)
                        continue
                    # Do not echo broker messages (they may contain an account or request).
                    safe_code = code if re.fullmatch(r"[A-Z0-9]{1,20}", code) else "UNSPECIFIED"
                    raise BrokerRejected("KIS rejected request: " + safe_code)
                self.health[mode] = "ok"
                return body, response.headers.get("tr_cont", "")
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                self.health[mode] = "request_error"
                if write:
                    raise UncertainSubmission(
                        "Broker submission result is unknown; do not resubmit"
                    ) from None
                if attempt == 2:
                    raise TradingError("KIS query failed; retry later") from None
                await asyncio.sleep(2**attempt)
        raise TradingError("KIS query retries exhausted")

    async def pages(self, mode, path, tr_id, params, output="output1", cursor_size=100):
        params = dict(params)
        fk, nk = f"CTX_AREA_FK{cursor_size}", f"CTX_AREA_NK{cursor_size}"
        params.update({fk: "", nk: ""})
        result, seen, continuation = [], set(), ""
        for _ in range(200):
            body, more = await self.call(mode, path, tr_id, params, continuation=continuation)
            result.extend(items(body.get(output, [])))
            if more not in ("M", "F"):
                return result, body
            cursor = (str(body.get(fk.lower(), "")), str(body.get(nk.lower(), "")))
            if cursor in seen or not any(s.strip() for s in cursor):
                raise TradingError("Broker pagination incomplete; refusing partial results")
            seen.add(cursor)
            params[fk], params[nk] = cursor
            continuation = "N"
        raise TradingError("Broker pagination limit exceeded; narrow the date range")

    async def balance(self, mode, exchange="KRX"):
        rows, body = await self.pages(
            mode,
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            self.tr(mode, "TTTC8434R"),
            self.account(mode)
            | {
                "AFHR_FLPR_YN": "X" if exchange == "NXT" else "N",
                "OFL_YN": "",
                "INQR_DVSN": "01",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
            },
        )
        return rows, items(body.get("output2", []))

    async def quote(self, mode, symbol, exchange):
        params = {
            "FID_COND_MRKT_DIV_CODE": "NX" if exchange == "NXT" else "J",
            "FID_INPUT_ISCD": symbol,
        }
        price, _ = await self.call(
            mode, "/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100", params
        )
        price_observed_at = datetime.now(KST).isoformat()
        book, _ = await self.call(
            mode,
            "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            "FHKST01010200",
            params,
        )
        book_observed_at = datetime.now(KST).isoformat()
        p, b = price["output"], book["output1"]
        return {
            "symbol": symbol,
            "exchange": exchange,
            "component_observations": {
                "price": {
                    "received_at": price_observed_at,
                    "time_basis": "server_received_at",
                },
                "orderbook": {
                    "received_at": book_observed_at,
                    "time_basis": "server_received_at",
                },
            },
            "price": p.get("stck_prpr"),
            "upper_limit": p.get("stck_mxpr"),
            "lower_limit": p.get("stck_llam"),
            "asks": [
                {"price": b.get(f"askp{i}"), "quantity": b.get(f"askp_rsqn{i}")}
                for i in range(1, 11)
            ],
            "bids": [
                {"price": b.get(f"bidp{i}"), "quantity": b.get(f"bidp_rsqn{i}")}
                for i in range(1, 11)
            ],
        }

    async def capacity(self, mode, symbol, price, exchange):
        # KIS explicitly recommends market classification for non-margin capacity.
        body, _ = await self.call(
            mode,
            "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            self.tr(mode, "TTTC8908R"),
            self.account(mode)
            | {
                "PDNO": symbol,
                "ORD_UNPR": str(price),
                "ORD_DVSN": "01",
                "CMA_EVLU_AMT_ICLD_YN": "N",
                "OVRS_ICLD_YN": "N",
            },
        )
        value = body["output"]
        rows, _ = await self.balance(mode, exchange)
        sell = sum(
            integer(r["ord_psbl_qty"])
            for r in rows
            if r.get("pdno") == symbol and r.get("loan_dt") in ("", "00000000")
        )
        return {
            "cash_buy_amount": str(number(value["nrcvb_buy_amt"])),
            "cash_buy_quantity": integer(value["nrcvb_buy_qty"]),
            "sell_quantity": sell,
        }

    async def daily(self, mode, start, end, exchange="KRX"):
        params = self.account(mode) | {
            "INQR_STRT_DT": start,
            "INQR_END_DT": end,
            "SLL_BUY_DVSN_CD": "00",
            "CCLD_DVSN": "00",
            "INQR_DVSN": "01",
            "INQR_DVSN_3": "01",
            "PDNO": "",
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "INQR_DVSN_1": "",
            "EXCG_ID_DVSN_CD": exchange,
        }
        rows, _ = await self.pages(
            mode,
            "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            self.tr(mode, "TTTC0081R"),
            params,
        )
        return rows

    async def place(self, order):
        mode = order["mode"]
        body, _ = await self.call(
            mode,
            "/uapi/domestic-stock/v1/trading/order-cash",
            self.tr(mode, "TTTC0012U" if order["side"] == "buy" else "TTTC0011U"),
            self.account(mode)
            | {
                "PDNO": order["symbol"],
                "ORD_DVSN": "00" if order["order_type"] == "limit" else "01",
                "ORD_QTY": str(order["quantity"]),
                "ORD_UNPR": order["price"],
                "EXCG_ID_DVSN_CD": order["exchange"],
                "SLL_TYPE": "01" if order["side"] == "sell" else "",
                "CNDT_PRIC": "",
            },
            write=True,
        )
        return self.receipt(body)

    @staticmethod
    def receipt(body):
        try:
            output = {k.lower(): v for k, v in body["output"].items()}
            broker_id, org = str(output["odno"]), str(output["krx_fwdg_ord_orgno"])
            if not broker_id.strip() or not org.strip():
                raise ValueError
            return {"broker_id": broker_id, "org": org}
        except (KeyError, TypeError, ValueError, AttributeError):
            raise UncertainSubmission(
                "Broker acknowledged request without a usable order ID"
            ) from None

    async def cancel_capacity(self, order):
        if order["mode"] == "demo":
            # Real-only cancellation-capacity endpoint has no V TR ID.
            return max(0, order["remaining"])
        rows, _ = await self.pages(
            "real",
            "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
            "TTTC0084R",
            self.account("real") | {"INQR_DVSN_1": "1", "INQR_DVSN_2": "0"},
            output="output",
        )
        found = [
            r for r in rows if str(r.get("odno", "")).lstrip("0") == order["broker_id"].lstrip("0")
        ]
        return integer(found[0]["psbl_qty"]) if len(found) == 1 else 0

    async def cancel(self, order, quantity):
        body, _ = await self.call(
            order["mode"],
            "/uapi/domestic-stock/v1/trading/order-rvsecncl",
            self.tr(order["mode"], "TTTC0013U"),
            self.account(order["mode"])
            | {
                "KRX_FWDG_ORD_ORGNO": order["org"],
                "ORGN_ODNO": order["broker_id"],
                "ORD_DVSN": "00" if order["order_type"] == "limit" else "01",
                "RVSE_CNCL_DVSN_CD": "02",
                "ORD_QTY": str(quantity),
                "ORD_UNPR": "0",
                "QTY_ALL_ORD_YN": "N",
                "EXCG_ID_DVSN_CD": order["exchange"],
                "CNDT_PRIC": "",
            },
            write=True,
        )
        return self.receipt(body)


def today():
    return datetime.now(KST).strftime("%Y%m%d")
