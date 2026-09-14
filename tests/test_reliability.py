"""Cross-process recovery, rate timing, and structural-error regressions."""
from datetime import date
from dataclasses import replace
import os
import subprocess
import sys
import textwrap

import pandas as pd
import pytest

from astock.config import Config
from astock.models import Bar
from astock.storage import Store
from astock.sync import Scheduler
from astock.locking import InstanceLock
from test_sync_cli import Fake, frame


def test_rate_limit_and_backoff(tmp_path):
    class Clock:
        value = 0.0
        sleeps = []
        def now(self):
            return self.value
        def sleep(self, duration):
            self.sleeps.append(duration)
            self.value += duration
    clock = Clock()
    calls = []
    class TimedFake(Fake):
        def daily(self, *args):
            calls.append(clock.now())
            return super().daily(*args)
    config = Config(database=tmp_path / 'db', min_interval=2, backoff_seconds=1)
    fake = TimedFake([OSError('x'), frame(), frame()])
    with Store(config.database) as store:
        Scheduler(config, store, fake, sleep=clock.sleep, clock=clock.now).sync(['000001','000002'], date(2024,1,10))
    assert calls == [0,2,4]
    assert clock.sleeps == [1,1,2]


def test_structure_error_no_writes_no_immediate_retries(tmp_path):
    fake = Fake([pd.concat([frame(),frame()])])
    config = Config(database=tmp_path / 'db', min_interval=0)
    with Store(config.database) as store:
        results = Scheduler(config,store,fake).sync(['000001'],date(2024,1,10))
        assert results[0]['status'] == 'failed'
        assert len(fake.calls) == 1
        assert not store.export_rows() and not store.state('000001')
        assert 'duplicate' in store.pending()[0]['error']


def test_hard_exit_rolls_back_and_releases_lock(tmp_path):
    path = tmp_path / 'raw.sqlite'
    # Hard process exit skips all Python cleanup, including rollback/unlock.
    code = textwrap.dedent('''
        import os,sys
        from datetime import date
        from astock.locking import InstanceLock
        from astock.storage import Store
        with InstanceLock(sys.argv[1]), Store(sys.argv[1]) as store:
            cid = store.start_collection('bars','000001',date(2024,1,1),date(2024,1,10))
            store.note_attempt(cid)
            store.conn.execute('BEGIN IMMEDIATE')
            store.conn.execute("INSERT INTO bars (symbol,trade_date,open,high,low,close,volume,amount,source,updated_at,collection_id) VALUES ('000001','2024-01-10','1','1','1','1','1',NULL,'stock_zh_a_hist','before crash',?)",(cid,))
            os._exit(9)
    ''')
    child = subprocess.run([sys.executable,'-c',code,str(path)], capture_output=True, text=True, timeout=15)
    assert child.returncode == 9, child.stderr
    with InstanceLock(path), Store(path) as store:
        assert not store.export_rows()
        assert store.recover_interrupted() == 1
        assert store.pending()[0]['attempts'] == 1
        cfg = Config(database=path,min_interval=0)
        assert Scheduler(cfg,store,Fake()).retry()[0]['status'] == 'success'
        assert len(store.export_rows()) == 1 and not store.pending()


def test_interrupt_after_commit_keeps_success(tmp_path, monkeypatch):
    config = Config(database=tmp_path / 'raw.sqlite',min_interval=0)
    with Store(config.database) as store:
        complete = store.complete_bars
        def commit_then_interrupt(*args):
            complete(*args)
            raise KeyboardInterrupt
        monkeypatch.setattr(store,'complete_bars',commit_then_interrupt)
        with pytest.raises(KeyboardInterrupt):
            Scheduler(config,store,Fake()).sync(['000001'],date(2024,1,10))
        assert store.conn.execute('SELECT status FROM collections').fetchone()[0] == 'success'
        assert len(store.export_rows()) == 1 and not store.pending()


def test_old_universe_retry_fetches_new_current_snapshot(tmp_path):
    class Universe:
        snapshots = [OSError('down'), [('000001','A'),('000002','B')], [('000001','New'),('000002','B'),('000003','C')]]
        def securities(self):
            value = self.snapshots.pop(0)
            if isinstance(value,Exception):
                raise value
            return pd.DataFrame(value,columns=['code','name'])
    cfg = Config(database=tmp_path / 'db',max_attempts=1,min_interval=0)
    with Store(cfg.database) as store:
        s = Scheduler(cfg,store,Universe())
        assert s.refresh_securities()['status'] == 'failed'
        assert s.refresh_securities()['status'] == 'success'
        assert s.retry()[0]['status'] == 'success'
        assert store.symbols() == ['000001','000002','000003']
        assert store.export_rows('securities')[0]['name'] == 'New'
        assert not store.pending()


def test_direct_storage_rejects_bad_requests(tmp_path):
    with Store(tmp_path/'db') as store:
        root = store.start_collection('bars','000001',date(2024,1,1),date(2024,1,10))
        store.fail_collection(root,'x')
        with pytest.raises(ValueError,match='exact'):
            store.start_collection('bars','000001',date(2024,1,2),date(2024,1,10),parent_id=root)
        cid = store.start_collection('bars','000001',date(2024,1,1),date(2024,1,10))
        for rows in ([Bar('000002','2024-01-02')], [Bar('000001','2024-02-02')], [Bar('000001','2024-01-02')]*2):
            with pytest.raises(ValueError):
                store.complete_bars(cid,rows)
            assert not store.export_rows()
            assert store.conn.execute('SELECT status FROM collections WHERE id=?',(cid,)).fetchone()[0] == 'running'


def test_parent_proxy_environment_unchanged(tmp_path,monkeypatch):
    monkeypatch.setenv('HTTP_PROXY','http://example.invalid:1234')
    monkeypatch.setenv('HTTPS_PROXY','http://example.invalid:5678')
    before = dict(os.environ)
    cfg = Config(database=tmp_path/'db',min_interval=0)
    with Store(cfg.database) as store:
        Scheduler(cfg,store,Fake()).sync(['000001'],date(2024,1,10))
    assert dict(os.environ) == before
