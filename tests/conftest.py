from copy import deepcopy

import pytest

from kis_mcp.config import Credentials, Settings
from kis_mcp.kis import today
from kis_mcp.service import TradingService
from kis_mcp.store import Store


@pytest.fixture
def settings(tmp_path):
    return Settings(
        tmp_path / "state",
        {
            "real": Credentials("test-real-key", "test-real-secret", "11111111", "01"),
            "demo": Credentials("test-demo-key", "test-demo-secret", "22222222", "01"),
        },
        "https://mcp.test",
        "https://auth.test/realms/kis",
        "test-subject",
        "test-telegram-token",
        "123",
        poll_seconds=3600,
    )


class FakeBroker:
    def __init__(self):
        self.health = {"real": "not_checked", "demo": "not_checked"}
        self.rows = {"real": [], "demo": []}
        self.placed = []
        self.cancelled = []
        self.place_error = None
        self.cancel_error = None
        self.query_error = None
        self.capacity_result = {
            "cash_buy_amount": "1000000",
            "cash_buy_quantity": 100,
            "sell_quantity": 100,
        }

    async def balance(self, *args):
        return (
            [
                {
                    "pdno": "012450",
                    "prdt_name": "한화에어로스페이스",
                    "hldg_qty": "1",
                    "ord_psbl_qty": "1",
                    "pchs_avg_pric": "1000000",
                    "prpr": "1078000",
                    "evlu_amt": "1078000",
                    "evlu_pfls_amt": "78000",
                    "evlu_pfls_rt": "7.80",
                }
            ],
            [{"tot_evlu_amt": "1078000"}],
        )

    async def quote(self, mode, symbol, exchange):
        return {
            "symbol": symbol,
            "exchange": exchange,
            "price": "1089000",
            "upper_limit": "1400000",
            "lower_limit": "750000",
            "asks": [],
            "bids": [],
        }

    async def capacity(self, *args):
        return self.capacity_result

    async def daily(self, mode, start, end, exchange="KRX"):
        if self.query_error:
            raise self.query_error
        return deepcopy(self.rows[mode])

    async def place(self, order):
        self.placed.append(deepcopy(order))
        broker_id = str(len(self.placed))
        self.rows[order["mode"]].append(
            {
                "odno": broker_id,
                "ord_gno_brno": "001",
                "ord_dt": today(),
                "pdno": order["symbol"],
                "sll_buy_dvsn_cd": "02" if order["side"] == "buy" else "01",
                "ord_qty": str(order["quantity"]),
                "ord_unpr": order["price"],
                "tot_ccld_qty": "0",
                "rmn_qty": str(order["quantity"]),
                "avg_prvs": "0",
                "tot_ccld_amt": "0",
                "cncl_yn": "N",
            }
        )
        if self.place_error:
            raise self.place_error
        return {"broker_id": broker_id, "org": "001"}

    async def cancel_capacity(self, order):
        return order["remaining"]

    async def cancel(self, order, quantity):
        self.cancelled.append((deepcopy(order), quantity))
        if self.cancel_error:
            raise self.cancel_error
        return {"broker_id": "cancel-1", "org": "001"}

    async def close(self):
        pass


@pytest.fixture
def broker():
    return FakeBroker()


@pytest.fixture
def service(settings, broker):
    store = Store(settings.data_dir)
    value = TradingService(settings, store, broker)
    yield value
    store.close()
