"""Offline public CLI and scheduler integration for all requested dataset classes."""
from datetime import date
from dataclasses import replace
import csv
import json

import pandas as pd
import pytest

from astock.cli import main
from astock.config import Config, load_config
from astock.datasets import ALL_DATASETS
from astock.storage import Store
from astock.sync import Scheduler
from test_extra_validation import DAY, dividend_frame, suspension_frame
from test_sync_cli import frame


class AllFake:
    def __init__(self):
        self.calls=[]
        self.fail_kind=None

    def securities(self):
        self.calls.append(('securities',None,None,None))
        return pd.DataFrame([{'code':'000001','name':'A'}])

    def daily(self,symbol,start,end):
        self.calls.append(('bars',symbol,start,end))
        return frame(day=end.isoformat())

    def supplement(self,kind,symbol,start,end):
        self.calls.append((kind,symbol,start,end))
        if self.fail_kind==kind:
            self.fail_kind=None
            raise OSError('simulated upstream disconnect')
        if kind=='valuation':
            return pd.DataFrame([{'数据日期':end,'总市值':'123456789.125','流通市值':None,
                                  'PE(TTM)':'-2.5','PE(静)':'3','市净率':'0.81'}])
        if kind=='profile':
            return pd.DataFrame([{'item':'股票代码','value':symbol},{'item':'上市时间','value':19910403}])
        if kind=='dividends':
            df=dividend_frame(); df['预案公告日']=DAY
            return df
        if kind=='suspension':
            df=suspension_frame(); df['停牌时间']=end
            another=df.copy(); another['代码']='000003'
            return pd.concat([df,another])
        if kind=='delisting':
            return pd.DataFrame([{'证券代码':'000009','上市日期':date(1991,1,1),'终止上市日期':DAY}])
        raise AssertionError(kind)


def config_file(tmp_path, datasets='["bars"]'):
    path=tmp_path/'config.toml'
    path.write_text('[storage]\ndatabase="raw.sqlite"\n[sync]\nhistory_start="2024-01-01"\nmin_interval=0\nmax_attempts=1\ncircuit_failures=10\ndatasets='+datasets+'\n')
    return path


def test_cli_sync_all_and_default_wide_export(tmp_path,capsys):
    config=config_file(tmp_path); fake=AllFake()
    assert main(['--config',str(config),'sync','--datasets','all','--symbols','000001,000002','--end',str(DAY)],adapter=fake)==0
    result=json.loads(capsys.readouterr().out)
    assert len(result)==10 and all(r['status']=='success' for r in result)
    assert [c[0] for c in fake.calls].count('suspension')==1
    assert [c[0] for c in fake.calls].count('delisting')==1
    assert not any(c[0]=='securities' for c in fake.calls)
    output=tmp_path/'daily.csv'
    assert main(['--config',str(config),'export','--symbol','000001','--output',str(output)],adapter=fake)==0
    assert json.loads(capsys.readouterr().out)['table']=='daily_data'
    with output.open() as f: rows=list(csv.DictReader(f))
    assert len(rows)==1
    row=rows[0]
    assert row['pe']=='-2.5' and row['pe_basis']=='TTM' and row['pb']=='0.81'
    assert row['total_market_cap']=='123456789.125' and row['float_market_cap']=='\\N'
    assert row['announcement_date']=='2024-01-10' and row['listing_date']=='1991-04-03'
    assert row['suspension_status']=='suspended' and row['delisting_date']=='\\N'
    with Store(tmp_path/'raw.sqlite') as store:
        assert store.status()['pending_count']==0
        assert len(store.export_rows('daily_data'))==3  # Third code has only explicit suspension facts.
        assert store.export_rows('delisting_events')[0]['coverage']=='SZ'
        assert len(store.status()['dataset_states'])==8


def test_config_selection_and_cli_override_keep_legacy_default(tmp_path,capsys):
    assert Config().datasets==('bars',)
    path=config_file(tmp_path,'["valuation","profile"]')
    assert load_config(path).datasets==('valuation','profile')
    fake=AllFake()
    assert main(['--config',str(path),'sync','--symbols','000001','--end',str(DAY)],adapter=fake)==0
    assert [c[0] for c in fake.calls]==['valuation','profile']
    capsys.readouterr()
    fake.calls.clear()
    assert main(['--config',str(path),'sync','--datasets','bars','--symbols','000001','--end',str(DAY)],adapter=fake)==0
    assert [c[0] for c in fake.calls]==['bars']


@pytest.mark.parametrize('value',['[]','"bars"','["all"]','["other"]','["bars","bars"]','[true]'])
def test_invalid_toml_dataset_selection(tmp_path,value):
    with pytest.raises(ValueError): load_config(config_file(tmp_path,value))


def test_incremental_windows_are_independent_and_refresh_cannot_skip(tmp_path):
    cfg=Config(database=tmp_path/'db',history_start=date(2024,1,1),min_interval=0)
    fake=AllFake()
    with Store(cfg.database) as store:
        scheduler=Scheduler(cfg,store,fake)
        scheduler.sync(['000001'],date(2024,1,15),datasets=('bars',))
        scheduler.sync(['000001'],DAY,datasets=('valuation',))
        scheduler.refresh(['000001'],date(2024,1,30),date(2024,1,30),datasets=('valuation',))
        scheduler.sync(['000001'],date(2024,1,20),datasets=('bars','valuation'))
        assert fake.calls[-2:]==[('bars','000001',date(2024,1,9),date(2024,1,20)),
                                ('valuation','000001',date(2024,1,4),date(2024,1,20))]
        state=store.dataset_state('valuation','000001')
        assert state['normal_end']=='2024-01-20' and state['latest_data_date']=='2024-01-30'


def test_valuation_end_is_capped_before_collecting(monkeypatch,tmp_path):
    monkeypatch.setattr('astock.config.default_end',lambda:DAY)
    cfg=Config(database=tmp_path/'db',history_start=date(2024,1,1),min_interval=0)
    fake=AllFake()
    with Store(cfg.database) as store:
        result=Scheduler(cfg,store,fake).sync(['000001'],date(2099,1,1),datasets=('valuation',))[0]
        assert fake.calls[0][3]==DAY
        assert result['eligible_end']=='2024-01-10' and result['requested_end']=='2099-01-01'
        assert store.dataset_state('valuation','000001')['normal_end']=='2024-01-10'


def test_global_only_queries_need_no_universe_and_refresh_each_date(tmp_path):
    cfg=Config(database=tmp_path/'db',min_interval=0)
    fake=AllFake()
    with Store(cfg.database) as store:
        scheduler=Scheduler(cfg,store,fake)
        result=scheduler.sync(end=DAY,datasets=('suspension','delisting'))
        assert len(result)==2 and all(r['status']=='success' for r in result)
        assert [c[0] for c in fake.calls]==['suspension','delisting']
        fake.calls.clear()
        scheduler.refresh(start=date(2024,1,2),end=date(2024,1,4),datasets=('suspension',))
        assert fake.calls==[('suspension',None,date(2024,1,d),date(2024,1,d)) for d in (2,3,4)]


def test_cli_snapshot_refresh_without_date_bounds(tmp_path,capsys):
    path=config_file(tmp_path); fake=AllFake()
    assert main(['--config',str(path),'refresh','--datasets','profile,dividends','--symbols','000001'],adapter=fake)==0
    result=json.loads(capsys.readouterr().out)
    assert len(result)==2 and all(r['status']=='success' for r in result)
    assert all(c[2] is None and c[3] is None for c in fake.calls)
    assert all(any('not a historical as-of' in w for w in r['warnings']) for r in result)


@pytest.mark.parametrize('args',[
    ['sync','--datasets','suspension','--symbols','000001'],
    ['sync','--datasets','other'],
    ['sync','--datasets','bars,bars'],
    ['sync','--datasets','profile','--end','2024-01-10'],
    ['refresh','--datasets','profile'],
    ['refresh','--datasets','profile','--symbols','000001','--start','2024-01-01'],
    ['refresh','--datasets','suspension'],
    ['refresh','--datasets','delisting','--symbols','000001'],
    ['refresh','--securities','--datasets','profile'],
])
def test_invalid_cli_scopes_fail_before_database_creation(tmp_path,args):
    path=config_file(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(['--config',str(path),*args],adapter=AllFake())
    assert exc.value.code==2 and not (tmp_path/'raw.sqlite').exists()


def test_cli_retry_routes_new_dataset_without_default_config_override(tmp_path,capsys):
    path=config_file(tmp_path); fake=AllFake(); fake.fail_kind='valuation'
    assert main(['--config',str(path),'sync','--datasets','valuation','--symbols','000001','--end',str(DAY)],adapter=fake)==1
    failed=json.loads(capsys.readouterr().out)[0]
    assert main(['--config',str(path),'retry'],adapter=fake)==0
    success=json.loads(capsys.readouterr().out)[0]
    assert success['kind']=='valuation' and success['status']=='success'
    assert fake.calls[0]==fake.calls[1]
    with Store(tmp_path/'raw.sqlite') as store:
        assert store.conn.execute('SELECT resolved_by FROM collections WHERE id=?',(failed['id'],)).fetchone()[0]==success['id']


def test_sync_all_can_refresh_universe_then_collect_fixed_datasets(tmp_path):
    cfg=Config(database=tmp_path/'db',history_start=date(2024,1,1),min_interval=0,datasets=ALL_DATASETS)
    fake=AllFake()
    with Store(cfg.database) as store:
        results=Scheduler(cfg,store,fake).sync(end=DAY)
        assert len(results)==7 and fake.calls[0][0]=='securities'
        assert set(c[0] for c in fake.calls)=={'securities',*ALL_DATASETS}
