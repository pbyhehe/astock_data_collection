from datetime import date
from decimal import Decimal
import pandas as pd
import pytest
from astock.validation import securities, bars, ValidationError

DAY = date(2024, 1, 2)


def frame(**updates):
    row = {'日期':DAY, '开盘':'1.2300','收盘':'1.5','最高':'2','最低':'1','成交量':'120','成交额':'123.456789012345678901234567890123',
           '涨跌幅':'-1.25', '振幅':'3.50', '换手率':'0.12'}
    row.update(updates)
    return pd.DataFrame([row])


def test_empty():
    assert securities(pd.DataFrame()) == []
    assert bars(pd.DataFrame(), '000001', DAY, DAY) == ([], [])


@pytest.mark.parametrize('symbol', [1, '1', '１２３４５６', '000001 ', None, 'sz000001'])
def test_bad_symbols(symbol):
    with pytest.raises(ValidationError):
        securities(pd.DataFrame({'code':[symbol], 'name':['A']}))


@pytest.mark.parametrize('names', [['A','A'], ['A','B']])
def test_duplicate_security(names):
    with pytest.raises(ValidationError, match='duplicate'):
        securities(pd.DataFrame({'code':['000001','000001'], 'name':names}))


def test_security_null_and_required():
    assert securities(pd.DataFrame({'code':['000001'], 'name':[None]}))[0].name is None
    with pytest.raises(ValidationError):
        securities(pd.DataFrame({'code':['000001']}))


def test_exact_numeric_and_source_metrics():
    rows, warnings = bars(frame(涨跌额='ignored extra'), '000001', DAY, DAY)
    assert rows[0].open == '1.23'
    assert rows[0].volume == '120'
    assert rows[0].amount == '123.456789012345678901234567890123'
    assert warnings == []
    assert rows[0].change_pct == '-1.25'
    assert rows[0].amplitude_pct == '3.5'
    assert rows[0].turnover_rate_pct == '0.12'
    assert not hasattr(rows[0], 'return_rate')


@pytest.mark.parametrize('value', [None, float('nan'), pd.NA, Decimal('NaN')])
def test_null_preserved(value):
    rows, _ = bars(frame(成交额=value), '000001', DAY, DAY)
    assert rows[0].amount is None


@pytest.mark.parametrize('value', ['bad', '', '--', True])
def test_malformed_numeric(value):
    with pytest.raises(ValidationError):
        bars(frame(成交额=value), '000001', DAY, DAY)


@pytest.mark.parametrize('value', ['Infinity', '-Infinity', '-2', 'NaN'])
def test_anomaly_retained_and_warned(value):
    rows, warnings = bars(frame(成交额=value), '000001', DAY, DAY)
    assert rows[0].amount == value
    assert warnings


def test_inconsistent_ohlc_kept():
    rows, warnings = bars(frame(最高='0'), '000001', DAY, DAY)
    assert rows[0].high == '0'
    assert any('OHLC' in warning for warning in warnings)


@pytest.mark.parametrize('value', [None, '2024-02-30', '20240102', '2023-12-31', '2024-1-2', pd.NaT])
def test_bad_dates(value):
    with pytest.raises(ValidationError):
        bars(frame(日期=value), '000001', DAY, DAY)


def test_duplicate_bar_even_identical():
    with pytest.raises(ValidationError, match='duplicate'):
        bars(pd.concat([frame(),frame()]), '000001', DAY, DAY)


def test_symbol_mismatch_and_schema():
    with pytest.raises(ValidationError):
        bars(frame(股票代码='000002'), '000001', DAY, DAY)
    with pytest.raises(ValidationError):
        bars(frame().drop(columns=['成交额']), '000001', DAY, DAY)
    duplicate = frame()
    duplicate.columns = [*list(duplicate.columns[:-1]), '成交量']
    with pytest.raises(ValidationError):
        bars(duplicate, '000001', DAY, DAY)
