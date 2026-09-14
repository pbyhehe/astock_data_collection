"""Immutable selected AKShare fields; numeric values retain decimal precision."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Security:
    symbol: str
    name: str | None = None


@dataclass(frozen=True)
class Bar:
    symbol: str
    trade_date: str
    open: str | None = None
    high: str | None = None
    low: str | None = None
    close: str | None = None
    volume: str | None = None
    amount: str | None = None
    change_pct: str | None = None
    amplitude_pct: str | None = None
    turnover_rate_pct: str | None = None
