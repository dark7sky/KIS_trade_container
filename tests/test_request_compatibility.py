import json

import pytest

from kis_mcp.models import CancelInput, OrderInput


@pytest.mark.parametrize("kind", ["place", "cancel"])
async def test_retry_pre_upgrade_request_without_expected_mode(service, broker, kind):
    request = OrderInput(client_request_id="legacy-place", symbol="005930", side="buy", quantity=1, price="1000")
    order = await service.place(request)
    if kind == "cancel":
        request = CancelInput(client_request_id="legacy-cancel", order_id=order["id"])
        await service.cancel(request)
    row = service.store.db.execute("SELECT payload FROM requests WHERE id=?", (request.client_request_id,)).fetchone()
    payload = json.loads(row["payload"])
    payload.pop("expected_mode", None)
    from kis_mcp.store import encoded
    service.store.db.execute("UPDATE requests SET payload=? WHERE id=?", (encoded(payload), request.client_request_id))
    await service.set_mode("demo")
    result = await getattr(service, kind)(request)
    assert result["mode"] == "real"
    assert len(broker.placed) == 1
    assert len(broker.cancelled) == (1 if kind == "cancel" else 0)
