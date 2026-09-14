from datetime import date

import pandas as pd
import pytest

from astock.config import Config
from astock.storage import Store
from astock.sync import Scheduler
from test_extra_validation import DAY, valuation_frame
from test_sync_cli import frame


class ExtraFake:
    def __init__(self, responses):
        self.responses=list(responses)
        self.calls=[]

    def supplement(self,kind,symbol,start,end):
        self.calls.append((kind,symbol,start,end))
        value=self.responses.pop(0)
        if isinstance(value,BaseException): raise value
        return value

    def daily(self,symbol,start,end):
        return frame()


def test_retry_keeps_supplement_kind_and_exact_bounds_without_fallback(tmp_path):
    cfg=Config(database=tmp_path/'db',min_interval=0,max_attempts=1,circuit_failures=10)
    adapter=ExtraFake([OSError('offline'),valuation_frame()])
    with Store(cfg.database) as store:
        scheduler=Scheduler(cfg,store,adapter)
        failed=scheduler._collect('valuation','000001',DAY,DAY)
        assert failed['status']=='failed' and failed['kind']=='valuation'
        assert store.dataset_state('valuation','000001') is None
        # Other successful data must not advance valuation progress or resolve its failure.
        assert scheduler._collect('bars','000001',DAY,DAY)['status']=='success'
        assert store.dataset_state('valuation','000001') is None and len(store.pending())==1
        result=scheduler.retry()[0]
        assert result['status']=='success' and result['kind']=='valuation'
        assert adapter.calls==[('valuation','000001',DAY,DAY)]*2
        assert not store.pending()
        assert store.dataset_state('valuation','000001')['normal_end']=='2024-01-10'
        assert store.conn.execute('SELECT resolved_by FROM collections WHERE id=?',(failed['id'],)).fetchone()[0]==result['id']


def test_supplement_empty_is_retryable_and_cannot_advance_state(tmp_path):
    cfg=Config(database=tmp_path/'db',min_interval=0,max_attempts=1)
    with Store(cfg.database) as store:
        scheduler=Scheduler(cfg,store,ExtraFake([pd.DataFrame(),valuation_frame()]))
        assert scheduler._collect('valuation','000001',DAY,DAY)['status']=='empty'
        assert store.dataset_state('valuation','000001') is None
        assert scheduler.retry()[0]['status']=='success'


def test_snapshot_withdrawal_warnings_are_returned_and_logged(tmp_path, caplog):
    import json
    from test_extra_validation import dividend_frame
    cfg=Config(database=tmp_path/'db',min_interval=0,max_attempts=1)
    first=dividend_frame()
    changed=first.copy(); changed['预案公告日']=DAY
    with Store(cfg.database) as store:
        scheduler=Scheduler(cfg,store,ExtraFake([first,changed]))
        scheduler.sync(['000001'],datasets=('dividends',))
        result=scheduler.sync(['000001'],datasets=('dividends',))[0]
        stored=json.loads(store.conn.execute('SELECT warnings_json FROM collections WHERE id=?',(result['id'],)).fetchone()[0])
        assert result['counts']['removed']==1
        assert result['warnings']==stored
        assert any('retired' in warning for warning in result['warnings'])
        assert 'retired' in caplog.text


@pytest.mark.parametrize('after_commit',[False,True])
def test_supplement_interrupt_recovery_does_not_mask_committed_success(tmp_path,monkeypatch,after_commit):
    cfg=Config(database=tmp_path/'db',min_interval=0,max_attempts=1)
    with Store(cfg.database) as store:
        scheduler=Scheduler(cfg,store,ExtraFake([valuation_frame() if after_commit else KeyboardInterrupt()]))
        if after_commit:
            complete=store.complete_dataset
            def interrupted(*args):
                complete(*args)
                raise KeyboardInterrupt()
            monkeypatch.setattr(store,'complete_dataset',interrupted)
        with pytest.raises(KeyboardInterrupt): scheduler._collect('valuation','000001',DAY,DAY)
        status=store.conn.execute('SELECT status FROM collections').fetchone()[0]
        assert status==('success' if after_commit else 'interrupted')
        assert bool(store.export_rows('valuation_daily'))==after_commit
