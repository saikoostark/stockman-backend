from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlaceOrderCommand:
    instrument_id: str
    side: str
    order_type: str
    quantity: str | None = None
    limit_price: str | None = None
    budget: str | None = None
    request_id: str = ""


@dataclass(frozen=True, slots=True)
class CancelOrderCommand:
    order_id: str
    request_id: str = ""


@dataclass(frozen=True, slots=True)
class ExecuteOrderCommand:
    order_id: str
    request_id: str = ""


@dataclass(frozen=True, slots=True)
class OrderDTO:
    id: str
    instrument_id: str
    symbol: str
    currency: str
    side: str
    order_type: str
    quantity: str | None
    limit_price: str | None
    budget: str | None
    reserved_cash: str
    reserved_quantity: str
    status: str
    created_at: str
    accepted_at: str
    expires_at: str | None
    closed_at: str | None
    close_reason: str
    source_watermark: str
    generation: str


@dataclass(frozen=True, slots=True)
class FillDTO:
    id: str
    order_id: str
    instrument_id: str
    symbol: str
    currency: str
    side: str
    quantity: str
    price: str
    cash: str
    cost_basis: str
    realized_pnl: str
    fees: str
    source_at: str
    source_watermark: str
    generation: str
    created_at: str


@dataclass(frozen=True, slots=True)
class HoldingDTO:
    instrument_id: str
    symbol: str
    currency: str
    available: str
    reserved: str
    cost_basis: str
    realized_pnl: str
    mark_price: str | None
    market_value: str | None
    unrealized_pnl: str | None
    mark_source_at: str | None
    stale: bool


@dataclass(frozen=True, slots=True)
class PortfolioCurrencyDTO:
    currency: str
    realized_pnl: str
    unrealized_pnl: str | None
    market_value: str | None
    stale: bool
    holdings: tuple[HoldingDTO, ...]


@dataclass(frozen=True, slots=True)
class MovementCommand:
    kind: str
    available_delta: int
    reserved_delta: int
    counterparty_delta: int = 0
