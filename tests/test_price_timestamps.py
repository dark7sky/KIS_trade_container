from datetime import datetime

import pytest

from kis_mcp.kis import KST


def assert_server_price_observation(result):
    observed_at = datetime.fromisoformat(result["price_observed_at"])

    assert result["price_time_basis"] == "server_received_at"
    assert observed_at.tzinfo is not None
    assert observed_at.utcoffset() == KST.utcoffset(observed_at)


def assert_component_observation(value):
    observed_at = datetime.fromisoformat(value["received_at"])

    assert value["time_basis"] == "server_received_at"
    assert observed_at.tzinfo is not None
    assert observed_at.utcoffset() == KST.utcoffset(observed_at)


@pytest.mark.asyncio
async def test_account_marks_balance_price_with_observation_time(service):
    before = datetime.now(KST)
    result = await service.account("KRX")
    after = datetime.now(KST)

    assert result["holdings"][0]["prpr"] == "1078000"
    assert_server_price_observation(result)
    assert before <= datetime.fromisoformat(result["price_observed_at"]) <= after


@pytest.mark.asyncio
async def test_quote_marks_market_price_with_observation_time(service):
    before = datetime.now(KST)
    result = await service.quote("012450", "KRX")
    after = datetime.now(KST)

    assert result["price"] == "1089000"
    assert_server_price_observation(result)
    assert before <= datetime.fromisoformat(result["price_observed_at"]) <= after
    assert set(result["component_observations"]) == {"price", "orderbook"}
    for value in result["component_observations"].values():
        assert_component_observation(value)
        assert before <= datetime.fromisoformat(value["received_at"]) <= after
