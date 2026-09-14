from datetime import date
import json

import pandas as pd
import pytest

from astock.adapter import AkshareAdapter
from astock.extra_validation import validate_dataset
from astock.validation import ValidationError

DAY = date(2024, 1, 10)


def valuation_frame():
    return pd.DataFrame([
        {'数据日期': date(2024,1,9), '总市值':'123456789.123456789', '流通市值':None, 'PE(TTM)':'-2.50', 'PE(静)':None, '市净率':'0.81'},
        {'数据日期': DAY, '总市值':'200', '流通市值':'100', 'PE(TTM)':'2.5', 'PE(静)':'2', '市净率':'1'},
        {'数据日期': date(2024,1,11), '总市值':'300', '流通市值':'200', 'PE(TTM)':'3.5', 'PE(静)':'3', '市净率':'2'},
    ])


def dividend_frame():
    return pd.DataFrame([{'报告期':date(2023,12,31), '预案公告日':None, '股权登记日':date(2024,5,1),
                          '除权除息日':date(2024,5,2), '最新公告日期':date(2024,4,1), '方案进度':'实施分配'}])


def suspension_frame():
    return pd.DataFrame([{'代码':'000001','名称':'A','停牌时间':DAY,'停牌截止时间':None,
                          '停牌期限':'盘中停牌','停牌原因':'事项','所属市场':'深市','预计复牌时间':date(2024,1,11)}])


def test_valuation_filters_source_dates_not_relabelled():
    batch=validate_dataset('valuation',valuation_frame(),'000001',date(2020,1,1),date(2030,1,1),completed_through=DAY)
    assert [r['trade_date'] for r in batch.rows] == ['2024-01-09','2024-01-10']
    row=batch.rows[0]
    assert row['total_market_cap']=='123456789.123456789'
    assert row['float_market_cap'] is None
    assert row['pe_ttm']=='-2.5' and row['pe_static'] is None
    assert row['pb_mrq']=='0.81'
    assert not any('negative' in w for w in batch.warnings)
    assert any('coverage unknown' in w for w in batch.warnings)
    assert any('excluded' in w for w in batch.warnings)
    assert json.loads(row['raw_json'])['数据日期']=='2024-01-09'


def test_default_valuation_cutoff_uses_shanghai_yesterday(monkeypatch):
    monkeypatch.setattr('astock.extra_validation.default_end',lambda:date(2024,1,9))
    batch=validate_dataset('valuation',valuation_frame(),'000001',date(2024,1,1),date(2099,1,1))
    assert len(batch.rows)==1 and batch.rows[0]['trade_date']=='2024-01-09'


def test_valuation_outside_range_empty_not_fake_history():
    batch=validate_dataset('valuation',valuation_frame(),'000001',date(2000,1,1),date(2000,1,2),completed_through=DAY)
    assert batch.rows == [] and any('coverage unknown' in w for w in batch.warnings)


@pytest.mark.parametrize('change', ['duplicate','bad_date','missing_column','bad_number'])
def test_valuation_structure_errors(change):
    df=valuation_frame()
    if change=='duplicate': df=pd.concat([df,df.iloc[:1]])
    if change=='bad_date': df.loc[0,'数据日期']='not-a-date'
    if change=='missing_column': df=df.drop(columns=['PE(TTM)'])
    if change=='bad_number': df.loc[0,'PE(TTM)']='--'
    with pytest.raises(ValidationError):
        validate_dataset('valuation',df,'000001',date(2024,1,1),DAY,completed_through=DAY)


def test_dividend_dates_status_and_nulls_are_not_substituted():
    batch=validate_dataset('dividends',dividend_frame(),'000001')
    row=batch.rows[0]
    assert row['announcement_date'] is None
    assert row['latest_announcement_date']=='2024-04-01'
    assert row['record_date']=='2024-05-01' and row['ex_dividend_date']=='2024-05-02'
    assert row['plan_status_raw']=='实施分配'
    assert len(row['event_key'])==64
    assert validate_dataset('dividends',dividend_frame(),'000001').rows == batch.rows
    assert json.loads(row['raw_json'])['预案公告日'] is None


def test_dividend_key_preserves_plan_revisions():
    df=dividend_frame()
    first=validate_dataset('dividends',df,'000001').rows[0]
    df.loc[0,'方案进度']='预案'
    changed=validate_dataset('dividends',df,'000001').rows[0]
    assert changed['event_key']==first['event_key']
    assert changed['plan_status_raw']!=first['plan_status_raw']
    with pytest.raises(ValidationError,match='duplicate'):
        validate_dataset('dividends',pd.concat([df,df]),'000001')


@pytest.mark.parametrize('value',[19910403,19910403.0,'19910403'])
def test_profile_numeric_listing_date_not_epoch(value):
    df=pd.DataFrame([{'item':'股票代码','value':'000001'},{'item':'上市时间','value':value}])
    row=validate_dataset('profile',df,'000001').rows[0]
    assert row['listing_date']=='1991-04-03'


@pytest.mark.parametrize('value',[0,'--','19910230',True,19910403.5])
def test_profile_invalid_listing_dates(value):
    df=pd.DataFrame([{'item':'股票代码','value':'000001'},{'item':'上市时间','value':value}])
    with pytest.raises(ValidationError): validate_dataset('profile',df,'000001')


def test_profile_null_mismatch_duplicate_and_missing():
    df=pd.DataFrame([{'item':'股票代码','value':'000001'},{'item':'上市时间','value':None}])
    assert validate_dataset('profile',df,'000001').rows[0]['listing_date'] is None
    with pytest.raises(ValidationError): validate_dataset('profile',df,'000002')
    with pytest.raises(ValidationError): validate_dataset('profile',pd.concat([df,df]),'000001')
    with pytest.raises(ValidationError): validate_dataset('profile',df.iloc[:1],'000001')


def test_suspension_multiple_events_preserved_without_fabricating_full_day():
    df=suspension_frame()
    second=df.copy(); second['停牌原因']='其他事项'
    row=validate_dataset('suspension',pd.concat([df,second]),start=DAY,end=DAY).rows[0]
    assert row['symbol']=='000001' and row['trade_date']=='2024-01-10'
    assert row['suspension_status']=='suspended'
    events=json.loads(row['suspension_details_json'])
    assert len(events)==2 and events[0]['停牌期限']=='盘中停牌'
    assert 'actual_resume_date' not in row
    assert events[0]['预计复牌时间']=='2024-01-11'
    empty=validate_dataset('suspension',pd.DataFrame(),start=DAY,end=DAY)
    assert empty.rows == []  # Never make a table full of 'not_suspended'.


def test_suspension_requires_exact_query_day():
    with pytest.raises(ValidationError):
        validate_dataset('suspension',suspension_frame(),start=date(2024,1,1),end=DAY)


def test_confirmed_delisting_date_only_from_fixed_termination_list():
    df=pd.DataFrame([{'证券代码':'000001','上市日期':None,'终止上市日期':DAY},
                     {'证券代码':'200001','上市日期':None,'终止上市日期':DAY}])
    batch=validate_dataset('delisting',df)
    assert len(batch.rows)==1 and batch.rows[0]['coverage']=='SZ'
    assert batch.rows[0]['delisting_date']=='2024-01-10'
    assert any('SH/BJ' in w for w in batch.warnings)
    with pytest.raises(ValidationError):
        validate_dataset('delisting',df.drop(columns=['终止上市日期']))
    df.loc[0,'终止上市日期']=None
    with pytest.raises(ValidationError): validate_dataset('delisting',df)


@pytest.mark.parametrize('kind',['dividends','profile','valuation','suspension','delisting'])
def test_empty_datasets_never_manufacture_facts(kind):
    assert validate_dataset(kind,pd.DataFrame(),symbol='000001',start=DAY,end=DAY,completed_through=DAY).rows==[]


def test_fixed_adapter_calls(monkeypatch):
    calls=[]
    def fake(function,params,**kwargs):
        calls.append((function,params,kwargs))
        return pd.DataFrame()
    monkeypatch.setattr('astock.adapter.invoke',fake)
    adapter=AkshareAdapter(timeout=17)
    adapter.supplement('dividends','000001')
    adapter.supplement('profile','000001')
    adapter.supplement('valuation','000001',date(2020,1,1),DAY)
    adapter.supplement('suspension',start=DAY,end=DAY)
    adapter.supplement('delisting')
    assert [c[:2] for c in calls]==[
        ('stock_fhps_detail_em',{'symbol':'000001'}),
        ('stock_individual_info_em',{'symbol':'000001'}),
        ('stock_value_em',{'symbol':'000001'}),
        ('stock_tfp_em',{'date':'20240110'}),
        ('stock_info_sz_delist',{'symbol':'终止上市公司'}),
    ]
    assert all(c[2]['timeout']==17 and c[2]['module']=='akshare' for c in calls)
    with pytest.raises(ValueError): adapter.supplement('other')
    with pytest.raises(ValueError): adapter.supplement('profile','1')
    with pytest.raises(ValueError): adapter.supplement('suspension',start=date(2024,1,1),end=DAY)
    with pytest.raises(ValueError): adapter.supplement('delisting','600000')
    assert len(calls)==5
