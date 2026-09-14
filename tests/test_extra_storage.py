"""Supplemental transactions, migration safety, and unified-view regressions."""
from datetime import date
import csv
import json
import sqlite3

import pandas as pd
import pytest

from astock.datasets import ValidatedBatch
from astock.exporting import export
from astock.extra_validation import validate_dataset
from astock.migrations import DAILY_METRICS, migrate
from astock.models import Bar
from astock.storage import Store
from test_extra_validation import DAY, valuation_frame, dividend_frame, suspension_frame
from test_migrations import legacy_database


def put(store,kind,frame,symbol='000001',start=date(2024,1,1),end=DAY,mode='normal',parent_id=None):
    if kind in ('dividends','profile'): start=end=None
    if kind=='suspension': symbol=None; start=end
    if kind=='delisting': symbol=start=end=None
    batch=validate_dataset(kind,frame,symbol,start,end,completed_through=date(2024,12,31))
    cid=store.start_collection(kind,symbol,start,end,mode=mode,parent_id=parent_id)
    counts=store.complete_dataset(cid,batch)
    return cid,counts


def v2_database(path):
    legacy_database(path)
    with sqlite3.connect(path) as db:
        for table in ('bars','bar_revisions'):
            for field in DAILY_METRICS:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {field} TEXT')
        db.execute('PRAGMA user_version=2')
        db.execute("INSERT INTO collections(id,kind,mode,status,created_at) VALUES(2,'securities','normal','failed','old failed')")
        db.execute("INSERT INTO collections(id,kind,mode,status,parent_id,root_id,created_at) VALUES(3,'securities','normal','failed',2,2,'old retry')")
        db.execute("INSERT INTO collections(id,kind,mode,status,created_at) VALUES(99,'securities','normal','failed','deleted high id')")
        db.execute('DELETE FROM collections WHERE id=99')
        db.execute("CREATE TRIGGER custom_guard BEFORE UPDATE OF error ON collections WHEN NEW.error='blocked' BEGIN SELECT RAISE(ABORT,'custom guard'); END")
    return path


def test_v3_preserves_foreign_keys_retry_chain_trigger_and_sequence(tmp_path):
    with Store(v2_database(tmp_path/'old.sqlite')) as store:
        assert store.conn.execute('PRAGMA foreign_keys').fetchone()[0]==1
        assert store.conn.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert store.export_rows()[0]['collection_id']==1
        assert store.export_rows('bar_revisions')[0]['replaced_by']==1
        pending=store.pending()
        assert len(pending)==1 and pending[0]['id']==3 and pending[0]['parent_id']==2
        cid=store.start_collection('profile','000001')
        assert cid==100
        with pytest.raises(sqlite3.IntegrityError,match='custom guard'):
            store.conn.execute("UPDATE collections SET error='blocked' WHERE id=2")
        assert store.export_rows('daily_data')[0]['close']=='11'


def test_v3_retains_sequence_when_collection_table_is_empty(tmp_path):
    path=v2_database(tmp_path/'empty.sqlite')
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM bar_revisions')
        db.execute('DELETE FROM bars')
        db.execute('DELETE FROM collections')
    with Store(path) as store:
        assert store.start_collection('profile','000001')==100


def test_v3_failure_rolls_back_parent_rebuild_and_restores_fk(tmp_path):
    db=sqlite3.connect(v2_database(tmp_path/'old.sqlite'),isolation_level=None)
    db.execute('PRAGMA foreign_keys=ON')
    before=db.execute('SELECT * FROM collections ORDER BY id').fetchall()
    def deny(action,first,second,database,trigger):
        return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_CREATE_TABLE and first=='valuation_daily' else sqlite3.SQLITE_OK
    db.set_authorizer(deny)
    with pytest.raises(sqlite3.DatabaseError): migrate(db,2)
    db.set_authorizer(None)
    assert db.execute('PRAGMA user_version').fetchone()[0]==2
    assert db.execute('PRAGMA foreign_keys').fetchone()[0]==1
    assert db.execute('SELECT * FROM collections ORDER BY id').fetchall()==before
    assert db.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('dividend_events','security_profiles','collections_new')").fetchone()[0]==0
    assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    migrate(db,2)
    assert db.execute('PRAGMA user_version').fetchone()[0]==4
    db.close()


def test_valuation_idempotence_revision_and_independent_state(tmp_path):
    with Store(tmp_path/'db') as store:
        cid,counts=put(store,'valuation',valuation_frame())
        assert counts['inserted']==2
        before=store.export_rows('valuation_daily')
        assert store.state('000001') is None
        assert store.dataset_state('valuation','000001')['normal_end']=='2024-01-10'
        _,counts=put(store,'valuation',valuation_frame())
        assert counts['unchanged']==2 and store.export_rows('valuation_daily')==before
        df=valuation_frame(); df.loc[0,'PE(TTM)']='-3'
        changed,counts=put(store,'valuation',df)
        assert counts['updated']==1
        old=json.loads(store.export_rows('record_revisions')[0]['old_json'])
        assert old['pe_ttm']=='-2.5' and old['collection_id']==cid
        assert store.export_rows('record_revisions')[0]['replaced_by']==changed


def test_refresh_and_empty_do_not_skip_normal_valuation_progress(tmp_path):
    with Store(tmp_path/'db') as store:
        put(store,'valuation',valuation_frame())
        future=valuation_frame().iloc[:1].copy(); future['数据日期']=date(2024,1,20)
        put(store,'valuation',future,start=date(2024,1,20),end=date(2024,1,20),mode='refresh')
        state=store.dataset_state('valuation','000001')
        assert state['normal_end']=='2024-01-10' and state['latest_data_date']=='2024-01-20'
        put(store,'valuation',pd.DataFrame(),start=date(2024,1,11),end=date(2024,1,15))
        assert store.dataset_state('valuation','000001')==state
        future['数据日期']=date(2024,1,30)
        put(store,'valuation',future,start=date(2024,1,30),end=date(2024,1,30))
        assert store.dataset_state('valuation','000001')['normal_end']=='2024-01-10'


def test_atomic_completion_rolls_back_rows_revision_state_and_retry_resolution(tmp_path):
    with Store(tmp_path/'db') as store:
        put(store,'valuation',valuation_frame())
        before=store.export_rows('valuation_daily'); state=store.dataset_state('valuation','000001')
        parent=store.start_collection('valuation','000001',date(2024,1,1),DAY)
        store.fail_collection(parent,'simulated failure')
        retry=store.start_collection('valuation','000001',date(2024,1,1),DAY,parent_id=parent)
        df=valuation_frame(); df.loc[0,'PE(TTM)']='100'
        batch=validate_dataset('valuation',df,'000001',date(2024,1,1),DAY,completed_through=DAY)
        store.conn.execute("CREATE TRIGGER fail_final BEFORE UPDATE OF status ON collections WHEN NEW.status='success' BEGIN SELECT RAISE(ABORT,'commit failure'); END")
        with pytest.raises(sqlite3.IntegrityError,match='commit failure'): store.complete_dataset(retry,batch)
        assert store.export_rows('valuation_daily')==before
        assert store.export_rows('record_revisions')==[]
        assert store.dataset_state('valuation','000001')==state
        assert store.conn.execute('SELECT status FROM collections WHERE id=?',(retry,)).fetchone()[0]=='running'
        assert store.conn.execute('SELECT resolved_by FROM collections WHERE id=?',(parent,)).fetchone()[0] is None
        store.conn.execute('DROP TRIGGER fail_final')
        store.complete_dataset(retry,batch)
        assert store.conn.execute('SELECT resolved_by FROM collections WHERE id=?',(parent,)).fetchone()[0]==retry


def test_snapshot_withdrawal_and_reappearance_are_audited_not_inferred(tmp_path):
    with Store(tmp_path/'db') as store:
        df=dividend_frame()
        first,_=put(store,'dividends',df)
        before=store.export_rows('dividend_events')[0]
        put(store,'dividends',pd.DataFrame())
        assert store.export_rows('dividend_events')[0]==before
        # Proposal date changes the locally generated identity. Retire the old
        # snapshot membership instead of displaying both as current dividends.
        df['预案公告日']=date(2024,4,1)
        _,counts=put(store,'dividends',df)
        assert counts['inserted']==1 and counts['removed']==1
        rows=store.export_rows('dividend_events')
        assert len(rows)==2 and sum(r['is_current'] for r in rows)==1
        old=json.loads(store.export_rows('record_revisions')[0]['old_json'])
        assert old['collection_id']==first and old['is_current']==1
        _,counts=put(store,'dividends',dividend_frame())
        assert counts['updated']==1 and counts['removed']==1
        assert sum(r['is_current'] for r in store.export_rows('dividend_events'))==1


def test_daily_view_one_row_per_day_multiple_events_and_unknowns(tmp_path):
    with Store(tmp_path/'db') as store:
        cid=store.start_collection('bars','000001',date(2024,1,1),DAY)
        store.complete_bars(cid,[Bar('000001','2024-01-10',close='10')])
        put(store,'valuation',valuation_frame())
        first=dividend_frame(); first['预案公告日']=DAY
        second=first.copy(); second['报告期']=date(2023,6,30)
        put(store,'dividends',pd.concat([first,second]))
        profile=pd.DataFrame([{'item':'股票代码','value':'000001'},{'item':'上市时间','value':19910403}])
        put(store,'profile',profile)
        stop=suspension_frame(); stop['代码']='000002'
        put(store,'suspension',stop)
        rows=store.export_rows('daily_data')
        assert len(rows)==3
        bar=next(r for r in rows if r['symbol']=='000001' and r['trade_date']=='2024-01-10')
        assert bar['pe']=='2.5' and bar['pe_basis']=='TTM' and bar['pb_basis']=='MRQ'
        assert bar['listing_date']=='1991-04-03' and bar['has_bar']==1
        assert bar['dividend_event_count']==2 and len(json.loads(bar['dividend_events_json']))==2
        assert bar['announcement_date'] is None  # Ambiguous scalar, full events retained.
        assert bar['is_dividend_announcement_day']==1 and bar['is_record_day'] is None
        assert bar['suspension_status']=='unknown' and bar['delisting_date'] is None
        stopped=next(r for r in rows if r['symbol']=='000002')
        assert stopped['has_bar']==0 and stopped['close'] is None and stopped['pe'] is None
        assert stopped['suspension_status']=='suspended'
        path=tmp_path/'daily.csv'; export(store,path,table='daily_data')
        with path.open() as f: exported=list(csv.DictReader(f))
        assert len(exported)==3 and exported[-1]['close']=='\\N'
        with pytest.raises(ValueError): store.export_rows('dividend_events',start=DAY)


def test_suspension_membership_disappearance_is_unknown_not_resumed(tmp_path):
    with Store(tmp_path/'db') as store:
        put(store,'suspension',suspension_frame())
        other=suspension_frame(); other['代码']='000002'
        put(store,'suspension',other)
        row=store.export_rows('daily_data',symbol='000001')[0]
        assert row['suspension_status']=='unknown' and row['close'] is None
        assert store.export_rows('suspension_daily',symbol='000001')[0]['is_current']==0


@pytest.mark.parametrize('mutation',['symbol','type','extra_field','raw_nan','future_date'])
def test_direct_store_rejects_invalid_batch_without_partial_writes(tmp_path,mutation):
    with Store(tmp_path/'db') as store:
        start=date(2024,1,1); end=date(2099,1,1)
        cid=store.start_collection('valuation','000001',start,end)
        batch=validate_dataset('valuation',valuation_frame(),'000001',start,DAY,completed_through=DAY)
        if mutation=='symbol': batch.rows[-1]['symbol']='000002'
        if mutation=='type': batch.rows[-1]['pe_ttm']=1.5
        if mutation=='extra_field': batch.rows[-1]['hidden']='bad'
        if mutation=='raw_nan': batch.rows[-1]['raw_json']='{"x":NaN}'
        if mutation=='future_date': batch.rows[-1]['trade_date']='2099-01-01'
        with pytest.raises((ValueError,TypeError)): store.complete_dataset(cid,batch)
        assert store.export_rows('valuation_daily')==[] and store.export_rows('record_revisions')==[]
        assert store.dataset_state('valuation','000001') is None
