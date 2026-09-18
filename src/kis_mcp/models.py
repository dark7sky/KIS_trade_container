from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Mode = Literal["real", "demo"]
Exchange = Literal["KRX", "NXT"]
Side = Literal["buy", "sell"]
OrderType = Literal["limit", "market"]


class OrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    symbol: str = Field(pattern=r"^(?:\d{6}|Q\d{6})$")
    side: Side
    quantity: int = Field(gt=0, le=100000000, strict=True)
    exchange: Exchange = "KRX"
    order_type: OrderType = "limit"
    price: Decimal = Field(default=Decimal("0"), ge=0, allow_inf_nan=False)
    expected_mode: Mode | None = None

    @model_validator(mode="after")
    def price_valid(self):
        if self.order_type == "limit" and (
            self.price <= 0 or self.price != self.price.to_integral_value()
        ):
            raise ValueError("Limit price must be a positive integer KRW amount")
        if self.order_type == "market" and self.price != 0:
            raise ValueError("Market orders require price=0")
        return self


class CancelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    order_id: str
    quantity: int | None = Field(default=None, gt=0, strict=True)
    expected_mode: Mode | None = None


class TradingError(Exception):
    """Only fixed, non-sensitive messages may be exposed to clients."""

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


class BrokerRejected(TradingError):
    def __init__(self, message):
        super().__init__(message, code="broker_rejected")


class SubmissionNotSent(TradingError):
    """A failure before the write request was sent; safe to make a new request."""

    def __init__(self, message):
        super().__init__(message, code="submission_not_sent")


class UncertainSubmission(TradingError):
    def __init__(self, message):
        super().__init__(message, code="submission_unknown")
