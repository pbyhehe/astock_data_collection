"""Fixed supplemental dataset contracts; no provider fallback selection."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Dataset:
    table: str
    interface: str
    fields: tuple[str, ...]
    keys: tuple[str, ...]
    scope: str
    date_field: str | None = None
    coverage: str = "source-returned records only; completeness not guaranteed"


DATASETS = {
    "dividends": Dataset(
        "dividend_events", "stock_fhps_detail_em",
        ("event_key", "symbol", "report_date", "announcement_date", "record_date", "ex_dividend_date",
         "latest_announcement_date", "plan_status_raw"),
        ("symbol", "event_key"), "symbol"),
    "profile": Dataset(
        "security_profiles", "stock_individual_info_em", ("symbol", "listing_date"), ("symbol",), "symbol"),
    "valuation": Dataset(
        "valuation_daily", "stock_value_em",
        ("symbol", "trade_date", "total_market_cap", "float_market_cap", "pe_ttm", "pe_static", "pb_mrq"),
        ("symbol", "trade_date"), "symbol", "trade_date",
        "source-dated history, at most one 5000-row page; Shanghai yesterday or earlier only"),
    "suspension": Dataset(
        "suspension_daily", "stock_tfp_em",
        ("symbol", "trade_date", "suspension_status", "suspension_details_json"),
        ("symbol", "trade_date"), "date", "trade_date",
        "positive source observations only; intraday precision lost upstream; absence is unknown"),
    "delisting": Dataset(
        "delisting_events", "stock_info_sz_delist",
        ("symbol", "delisting_date", "listing_date", "coverage"),
        ("symbol", "delisting_date"), "market", "delisting_date",
        "SZ only; no confirmed-date coverage for SH/BJ under the fixed-one-interface rule"),
}


ALL_DATASETS = ('bars', 'valuation', 'dividends', 'profile', 'suspension', 'delisting')


def select_datasets(values):
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError('datasets must be a nonempty list/tuple of dataset names')
    if not all(isinstance(value, str) and value in ALL_DATASETS for value in values):
        raise ValueError(f'datasets must use: {", ".join(ALL_DATASETS)}')
    if len(set(values)) != len(values):
        raise ValueError('duplicate dataset names are not allowed')
    return tuple(values)


@dataclass(frozen=True)
class ValidatedBatch:
    """Canonical rows include raw_json for selected source facts, not calculated indicators."""
    rows: list[dict]
    warnings: list[str]
