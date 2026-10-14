from datetime import datetime
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


class TossOrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    symbol: str = Field(min_length=1, max_length=20, pattern=r"^[A-Z][A-Z0-9.\-]*$")
    side: Side
    quantity: int = Field(gt=0, le=100000000, strict=True)
    order_type: OrderType = "limit"
    price: Decimal = Field(default=Decimal("0"), ge=0, le=1000000000, allow_inf_nan=False)
    expected_mode: Literal["real"] = "real"

    @model_validator(mode="after")
    def price_valid(self):
        if self.order_type == "market":
            if self.price != 0:
                raise ValueError("Market orders require price=0")
        else:
            tick = Decimal("0.0001") if self.price < 1 else Decimal("0.01")
            if self.price <= 0 or self.price % tick:
                raise ValueError("USD limit price requires 2 decimals, or 4 below $1")
        return self


class TossOCOInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    symbol: str = Field(min_length=1, max_length=20, pattern=r"^[A-Z][A-Z0-9.\-]*$")
    quantity: int = Field(gt=0, le=100000000, strict=True)
    take_profit_trigger: Decimal = Field(gt=0, allow_inf_nan=False)
    take_profit_limit: Decimal = Field(gt=0, allow_inf_nan=False)
    stop_loss_trigger: Decimal = Field(gt=0, allow_inf_nan=False)
    stop_loss_limit: Decimal = Field(gt=0, allow_inf_nan=False)
    expire_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def prices_valid(self):
        if self.take_profit_trigger <= self.stop_loss_trigger:
            raise ValueError("Take-profit trigger must exceed stop-loss trigger")
        for value in (
            self.take_profit_trigger,
            self.take_profit_limit,
            self.stop_loss_trigger,
            self.stop_loss_limit,
        ):
            tick = Decimal("0.0001") if value < 1 else Decimal("0.01")
            if value % tick:
                raise ValueError("USD conditional prices require 2 decimals, or 4 below $1")
        if self.take_profit_limit > self.take_profit_trigger:
            raise ValueError("Take-profit sell limit must not exceed its trigger")
        if self.stop_loss_limit > self.stop_loss_trigger:
            raise ValueError("Stop-loss sell limit must not exceed its trigger")
        return self


class TossWatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    symbol: str = Field(min_length=1, max_length=20, pattern=r"^[A-Z][A-Z0-9.\-]*$")
    kind: Literal["price_at_or_above", "price_at_or_below", "volume_breakout_1m"]
    threshold: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    volume_multiplier: Decimal = Field(default=Decimal("2"), ge=1, le=20, allow_inf_nan=False)
    expires_at: datetime

    @model_validator(mode="after")
    def rule_valid(self):
        if self.expires_at.tzinfo is None:
            raise ValueError("expires_at requires a timezone offset")
        if self.kind == "volume_breakout_1m" and self.threshold is not None:
            raise ValueError("Volume breakout has no price threshold")
        if self.kind != "volume_breakout_1m" and self.threshold is None:
            raise ValueError("Price watch requires a threshold")
        return self


class TossReplayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["price_at_or_above", "price_at_or_below", "volume_breakout_1m"]
    threshold: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    volume_multiplier: Decimal = Field(default=Decimal("2"), ge=1, le=20, allow_inf_nan=False)
    created_at: datetime

    @model_validator(mode="after")
    def rule_valid(self):
        if self.created_at.tzinfo is None:
            raise ValueError("created_at requires a timezone offset")
        if (self.kind == "volume_breakout_1m") != (self.threshold is None):
            raise ValueError("Price watches require threshold; volume breakout does not")
        return self


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
