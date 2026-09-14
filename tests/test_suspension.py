from datetime import date
import json
import sqlite3

import pytest

from astock.suspension import classify_suspension
from astock.extra_validation import validate_dataset
from astock.storage import Store
from astock.migrations import migrate
from test_extra_validation import DAY, suspension_frame


@pytest.mark.parametrize('start,end,expected', [
    ('2024-01-10',None,'suspended'),
    ('2024-01-15',None,'announced'),
    ('2024-01-09',None,'unknown'),
    ('2024-01-09','2024-01-10','suspended'),
    ('2024-01-08','2024-01-09','unknown'),
    (None,None,'unknown'),
    ('2024-01-10','2024-01-09','unknown'),
])
def test_explicit_date_evidence_only(start,end,expected):
    details=[{'停牌时间':start,'停牌截止时间':end,'预计复牌时间':'2024-01-11'}]
    assert classify_suspension(details,DAY,DAY)==expected


def test_planned_query_day_not_completed_at_observation():
    assert classify_suspension([{'停牌时间':DAY.isoformat()}],DAY,date(2024,1,9))=='announced'


def test_any_dated_positive_event_wins_without_losing_other_events():
    details=[{'停牌时间':'2024-01-15'}, {'停牌时间':DAY.isoformat()}]
    assert classify_suspension(details,DAY,DAY)=='suspended'


def test_future_plan_preserved_but_not_suspended_and_storage_rechecks(tmp_path):
    frame=suspension_frame(); frame['停牌时间']=date(2024,1,15)
    batch=validate_dataset('suspension',frame,start=DAY,end=DAY)
    assert batch.rows[0]['suspension_status']=='announced'
    with Store(tmp_path/'db') as store:
        cid=store.start_collection('suspension',start=DAY,end=DAY)
        store.complete_dataset(cid,batch)
        assert store.export_rows('daily_data')[0]['suspension_status']=='announced'
        assert json.loads(store.export_rows('suspension_daily')[0]['raw_json'])[0]['停牌时间']=='2024-01-15'
        bad=store.start_collection('suspension',start=DAY,end=DAY)
        batch.rows[0]['suspension_status']='suspended'
        with pytest.raises(ValueError,match='explicit source date'):
            store.complete_dataset(bad,batch)


def legacy_v3(path, *, before_query=False):
    frame=suspension_frame()
    if not before_query:
        frame['停牌时间']=date(2024,1,15)
    with Store(path) as store:
        cid=store.start_collection('suspension',start=DAY,end=DAY)
        store.complete_dataset(cid,validate_dataset('suspension',frame,start=DAY,end=DAY))
        store.conn.execute("UPDATE suspension_daily SET suspension_status='suspended'")
        if before_query:
            store.conn.execute("UPDATE collections SET finished_at='2024-01-09T12:00:00+00:00'")
        store.conn.execute('PRAGMA user_version=3')
        before=dict(store.conn.execute('SELECT * FROM suspension_daily').fetchone())
    return before


@pytest.mark.parametrize('before_query',[False,True])
def test_v4_reinterprets_legacy_flags_with_same_evidence_and_full_audit(tmp_path,before_query):
    path=tmp_path/'db'
    before=legacy_v3(path,before_query=before_query)
    with Store(path) as store:
        row=store.export_rows('suspension_daily')[0]
        assert row['suspension_status']=='announced'
        assert row['raw_json']==before['raw_json'] and row['collection_id']==before['collection_id']
        rev=store.export_rows('record_revisions')[0]
        assert json.loads(rev['old_json'])==before
        assert rev['replaced_by']==before['collection_id']
        assert store.conn.execute('SELECT count(*) FROM collections').fetchone()[0]==1
        assert store.conn.execute('PRAGMA user_version').fetchone()[0]==4
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    with Store(path) as store:
        assert len(store.export_rows('record_revisions'))==1


def test_v4_failure_rolls_back_flags_audit_and_version(tmp_path):
    path=tmp_path/'db'; before=legacy_v3(path)
    conn=sqlite3.connect(path,isolation_level=None)
    conn.execute("CREATE TRIGGER deny_reclassification BEFORE UPDATE ON suspension_daily BEGIN SELECT RAISE(ABORT,'blocked'); END")
    with pytest.raises(sqlite3.DatabaseError,match='blocked'):
        migrate(conn,3)
    assert conn.execute('PRAGMA user_version').fetchone()[0]==3
    assert conn.execute('SELECT count(*) FROM record_revisions').fetchone()[0]==0
    assert conn.execute('SELECT suspension_status,raw_json FROM suspension_daily').fetchone()==('suspended',before['raw_json'])
    conn.close()
