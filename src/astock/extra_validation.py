"""Strict, source-preserving validation of company events and dated valuation."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json

import pandas as pd

from .config import default_end
from .datasets import DATASETS, ValidatedBatch
from .validation import ValidationError, _day, _frame, _null, _numeric, _symbol


DIVIDEND_DATES = {
    "report_date": "报告期", "announcement_date": "预案公告日", "record_date": "股权登记日",
    "ex_dividend_date": "除权除息日", "latest_announcement_date": "最新公告日期",
}
VALUATION_FIELDS = {
    "total_market_cap": "总市值", "float_market_cap": "流通市值",
    "pe_ttm": "PE(TTM)", "pe_static": "PE(静)", "pb_mrq": "市净率",
}
SUSPENSION_FIELDS = ("代码", "名称", "停牌时间", "停牌截止时间", "停牌期限", "停牌原因", "所属市场", "预计复牌时间")


def _text(value, context):
    if _null(value):
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{context}: expected string or null")
    return value


def _optional_date(value):
    return None if _null(value) else _day(value).isoformat()


def _source_scalar(value):
    """Represent returned selected scalars without JSON NaN or additional float coercion."""
    if _null(value):
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return str(value)
    raise ValidationError(f"unsupported source scalar: {value!r}")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _raw(row, fields):
    return _json({field: _source_scalar(row[field]) for field in fields})


def _bounds(start, end):
    if type(start) is not date or type(end) is not date or start > end:
        raise ValidationError("ordered date bounds are required")


def _unique(rows, keys):
    seen = set()
    for row in rows:
        key = tuple(row[field] for field in keys)
        if key in seen:
            raise ValidationError(f"ambiguous duplicate source identity {key}; refusing to overwrite")
        seen.add(key)


def dividends(frame, symbol):
    _symbol(symbol)
    fields = (*DIVIDEND_DATES.values(), "方案进度")
    _frame(frame, fields)
    records, warnings = [], []
    for raw in frame.to_dict("records"):
        row = {field: _optional_date(raw[column]) for field, column in DIVIDEND_DATES.items()}
        if row["report_date"] is None:
            raise ValidationError("dividend report date is required for a stable local identity")
        row.update(symbol=symbol, plan_status_raw=_text(raw["方案进度"], "方案进度"))
        identity = [symbol, row["report_date"], row["announcement_date"]]
        row["event_key"] = hashlib.sha256(_json(identity).encode()).hexdigest()
        row["raw_json"] = _raw(raw, fields)
        records.append(row)
    _unique(records, DATASETS["dividends"].keys)
    if records:
        warnings.append("announcement_date is 预案公告日, not implementation announcement; nulls are not filled from 最新公告日期")
        warnings.append("dividend identity is locally generated from symbol/report_date/proposal_date; source has no durable event id")
    return ValidatedBatch(records, warnings)


def profiles(frame, symbol):
    _symbol(symbol)
    _frame(frame, ("item", "value"))
    if not len(frame):
        return ValidatedBatch([], [])
    values = {}
    for row in frame.to_dict("records"):
        item = _text(row["item"], "profile item")
        if item is None or item in values:
            raise ValidationError("missing or duplicate profile item")
        values[item] = row["value"]
    if "股票代码" not in values or "上市时间" not in values:
        raise ValidationError("profile requires 股票代码 and 上市时间")
    if _symbol(values["股票代码"]) != symbol:
        raise ValidationError("profile symbol mismatch")
    raw_date = values["上市时间"]
    listing_date = None
    warnings = []
    if not _null(raw_date):
        if isinstance(raw_date, bool):
            raise ValidationError("invalid listing date boolean")
        # API documents a YYYYMMDD numeric/string scalar, never epoch nanoseconds.
        try:
            number = Decimal(str(raw_date))
            if not number.is_finite() or number != number.to_integral_value():
                raise ValueError("nonintegral listing date")
            text = str(int(number))
            if len(text) != 8:
                raise ValueError("listing date must have eight digits")
            listing_date = datetime.strptime(text, "%Y%m%d").date().isoformat()
        except (InvalidOperation, ValueError, OverflowError) as exc:
            raise ValidationError(f"invalid listing date: {raw_date!r}") from exc
    else:
        warnings.append("listing date unavailable; not inferred from first_seen")
    row = {"symbol": symbol, "listing_date": listing_date,
           "raw_json": _raw(values, ("股票代码", "上市时间"))}
    return ValidatedBatch([row], warnings)


def valuations(frame, symbol, start, end, *, completed_through=None):
    _symbol(symbol)
    _bounds(start, end)
    cutoff = default_end() if completed_through is None else completed_through
    if type(cutoff) is not date:
        raise ValidationError("completed_through must be a date")
    fields = ("数据日期", *VALUATION_FIELDS.values())
    _frame(frame, fields)
    records, warnings, dates, seen = [], [], [], set()
    for raw in frame.to_dict("records"):
        day = _day(raw["数据日期"])
        if day in seen:
            raise ValidationError(f"duplicate valuation date: {symbol}/{day}")
        seen.add(day)
        dates.append(day)
        # Adapter returns available full history, not the requested range. Filter,
        # never relabel provider dates or ingest a potentially incomplete same day.
        if not start <= day <= min(end, cutoff):
            continue
        row = {field: _numeric(raw[column], f"{symbol}/{day}/{column}", warnings,
                               allow_negative=field in ("pe_ttm", "pe_static", "pb_mrq"))
               for field, column in VALUATION_FIELDS.items()}
        row.update(symbol=symbol, trade_date=day.isoformat(), raw_json=_raw(raw, fields))
        records.append(row)
    if dates:
        warnings.append(f"source available dates {min(dates)}..{max(dates)}; single page at most 5000 rows, not proof of complete history")
        if start < min(dates):
            warnings.append(f"requested start {start} precedes source available start {min(dates)}; earlier coverage unknown")
        if max(dates) > cutoff:
            warnings.append(f"excluded source dates after completed-session cutoff {cutoff}")
    return ValidatedBatch(records, warnings)


def suspensions(frame, query_date):
    if type(query_date) is not date:
        raise ValidationError("suspension query must specify one date")
    _frame(frame, SUSPENSION_FIELDS)
    groups = {}
    for raw in frame.to_dict("records"):
        symbol = _symbol(raw["代码"])
        # AKShare has already discarded intraday precision; preserve dates and
        # raw duration text, without fabricating all-day suspension/resumption.
        for field in ("停牌时间", "停牌截止时间", "预计复牌时间"):
            _optional_date(raw[field])
        for field in ("名称", "停牌期限", "停牌原因", "所属市场"):
            _text(raw[field], field)
        record = json.loads(_raw(raw, SUSPENSION_FIELDS))
        groups.setdefault(symbol, []).append(record)
    rows = []
    for symbol, details in sorted(groups.items()):
        # Multiple events for one symbol/day are legal. Exact repeated source
        # events are retained as evidence, rather than silently dropping rows.
        details.sort(key=_json)
        payload = _json(details)
        from .suspension import classify_suspension
        status = classify_suspension(details, query_date, default_end())
        rows.append(dict(symbol=symbol, trade_date=query_date.isoformat(), suspension_status=status,
                         suspension_details_json=payload, raw_json=payload))
    warnings = ["source suspension observation does not prove all-day suspension; predicted resumption is not confirmed",
                "future plans are announced; open-ended earlier starts are unknown, not extrapolated to the query day"] if rows else []
    return ValidatedBatch(rows, warnings)


def delistings(frame):
    fields = ("证券代码", "上市日期", "终止上市日期")
    _frame(frame, fields)
    rows = []
    for raw in frame.to_dict("records"):
        symbol = _symbol(raw["证券代码"])
        if not symbol.startswith(("000", "001", "002", "003", "300", "301")):
            # The selected exchange list can include non-A-share securities;
            # explicitly exclude rather than importing B shares into A-share data.
            continue
        day = _day(raw["终止上市日期"])
        row = dict(symbol=symbol, delisting_date=day.isoformat(), listing_date=_optional_date(raw["上市日期"]),
                   coverage="SZ", raw_json=_raw(raw, fields))
        rows.append(row)
    _unique(rows, DATASETS["delisting"].keys)
    return ValidatedBatch(rows, [DATASETS["delisting"].coverage])


def validate_dataset(kind, frame, symbol=None, start=None, end=None, *, completed_through=None):
    if kind == "dividends":
        return dividends(frame, symbol)
    if kind == "profile":
        return profiles(frame, symbol)
    if kind == "valuation":
        return valuations(frame, symbol, start, end, completed_through=completed_through)
    if kind == "suspension":
        if start != end:
            raise ValidationError("suspension request must cover exactly one query date")
        return suspensions(frame, end)
    if kind == "delisting":
        return delistings(frame)
    raise ValidationError(f"unknown dataset: {kind}")
