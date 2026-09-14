"""Validate returned values without filling, rounding or unit conversion."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
import pandas as pd
from .models import Security, Bar


class ValidationError(ValueError):
    pass


def _symbol(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9]{6}', value) is None:
        raise ValidationError(f'invalid symbol: {value!r}; expected six ASCII digits')
    return value


def _null(value):
    try:
        return bool(pd.isna(value))
    except (ValueError, TypeError):
        raise ValidationError(f'expected scalar value, got {value!r}') from None


def _frame(frame, required):
    if not isinstance(frame, pd.DataFrame):
        raise ValidationError('expected pandas DataFrame')
    if frame.columns.has_duplicates:
        raise ValidationError('duplicate column labels')
    if len(frame) == 0 and len(frame.columns) == 0:
        return
    missing = set(required) - set(frame.columns)
    if missing:
        raise ValidationError(f'missing required columns: {sorted(missing)}')


def securities(frame: pd.DataFrame) -> list[Security]:
    _frame(frame, ('code', 'name'))
    result, seen = [], set()
    for row in frame.to_dict('records'):
        symbol = _symbol(row['code'])
        if symbol in seen:
            raise ValidationError(f'duplicate security primary key: {symbol}')
        seen.add(symbol)
        name = row['name']
        if _null(name):
            name = None
        elif not isinstance(name, str):
            raise ValidationError(f'{symbol}: name must be string or null')
        result.append(Security(symbol=symbol, name=name))
    return result


def _day(value):
    if _null(value):
        raise ValidationError('trade date is required')
    if isinstance(value, datetime):
        if value.tzinfo is not None or value.time() != datetime.min.time():
            raise ValidationError(f'trade date contains time or timezone: {value!r}')
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value) is None:
        raise ValidationError(f'invalid trade date: {value!r}')
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f'invalid trade date: {value!r}') from exc


def _numeric(value, context, warnings, *, allow_negative=False):
    if _null(value):
        return None
    if isinstance(value, bool):
        raise ValidationError(f'{context}: malformed numeric value {value!r}')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValidationError(f'{context}: malformed numeric value {value!r}') from None
    if not number.is_finite():
        warnings.append(f'{context}: non-finite numeric value {number}')
        return str(number)
    if number < 0 and not allow_negative:
        warnings.append(f'{context}: negative numeric value {number}')
    if number == 0:
        return '0'
    text = format(number, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def bars(frame: pd.DataFrame, symbol: str, start: date, end: date) -> tuple[list[Bar], list[str]]:
    _symbol(symbol)
    if type(start) is not date or type(end) is not date or start > end:
        raise ValidationError('start and end must be dates with start <= end')
    fields = {'open':'开盘', 'high':'最高', 'low':'最低', 'close':'收盘', 'volume':'成交量', 'amount':'成交额',
              'change_pct':'涨跌幅', 'amplitude_pct':'振幅', 'turnover_rate_pct':'换手率'}
    _frame(frame, ('日期', *fields.values()))
    result, warnings, seen = [], [], set()
    for row in frame.to_dict('records'):
        day = _day(row['日期'])
        if not start <= day <= end:
            raise ValidationError(f'{symbol}: date {day} outside requested range')
        if day in seen:
            raise ValidationError(f'duplicate bar primary key: {symbol}/{day}')
        seen.add(day)
        if '股票代码' in row and _symbol(row['股票代码']) != symbol:
            raise ValidationError(f'{symbol}/{day}: returned symbol mismatch')
        values = {key: _numeric(row[column], f'{symbol}/{day}/{column}', warnings,
                                allow_negative=(key == 'change_pct')) for key, column in fields.items()}
        prices = {key: Decimal(values[key]) for key in ('open','high','low','close') if values[key] is not None}
        finite = {key: number for key, number in prices.items() if number.is_finite()}
        bad = ('high' in finite and any(finite['high'] < number for key, number in finite.items() if key != 'high')) or ('low' in finite and any(finite['low'] > number for key, number in finite.items() if key != 'low'))
        if bad:
            warnings.append(f'{symbol}/{day}: inconsistent OHLC')
        result.append(Bar(symbol=symbol, trade_date=day.isoformat(), **values))
    return result, warnings
