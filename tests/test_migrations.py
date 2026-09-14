"""Schema migration and new daily-field consistency regressions."""
from datetime import date
import json
import sqlite3

import pandas as pd
import pytest

from astock.migrations import DAILY_METRICS, SCHEMA_VERSION, migrate
from astock.models import Bar
from astock.storage import Store, _SCHEMA
from astock.validation import bars, ValidationError
from test_sync_cli import frame


def legacy_database(path):
    with sqlite3.connect(path) as db:
        db.executescript(_SCHEMA)
        db.execute("PRAGMA user_version=1")
        db.execute("INSERT INTO collections(id,kind,symbol,start_date,end_date,mode,status,created_at) VALUES(1,'bars','000001','2024-01-01','2024-01-10','normal','success','old request')")
        db.execute("INSERT INTO bars VALUES('000001','2024-01-10','10','12','9','11','100',NULL,'stock_zh_a_hist','old update',1)")
        db.execute("INSERT INTO bar_revisions(symbol,trade_date,open,high,low,close,volume,amount,source,updated_at,collection_id,replaced_at,replaced_by) VALUES('000001','2024-01-10','10','12','9','10','100',NULL,'stock_zh_a_hist','older update',1,'old update',1)")
        db.execute("INSERT INTO sync_state VALUES('000001','2024-01-01','2024-01-10','2024-01-10')")
    return path


def test_v1_migration_preserves_old_values_and_progress(tmp_path):
    path = legacy_database(tmp_path/'old.sqlite')
    with Store(path) as store:
        assert store.conn.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        row = store.export_rows()[0]
        assert row['close'] == '11' and row['updated_at'] == 'old update' and row['collection_id'] == 1
        assert row['amount'] is None
        assert all(row[field] is None for field in DAILY_METRICS)
        old = store.export_rows('bar_revisions')[0]
        assert old['close'] == '10' and old['updated_at'] == 'older update'
        assert all(old[field] is None for field in DAILY_METRICS)
        assert store.state('000001')['checked_end'] == '2024-01-10'
        assert store.conn.execute('PRAGMA foreign_key_check').fetchall() == []
    # Reopen is idempotent; never ALTER duplicate columns.
    with Store(path) as store:
        assert len(store.export_rows()) == 1


def test_migration_failure_rolls_back_schema_and_version(tmp_path):
    path = legacy_database(tmp_path/'old.sqlite')
    db = sqlite3.connect(path, isolation_level=None)
    def deny_second_table(action, first, second, database, trigger):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ALTER_TABLE and second == 'bar_revisions' else sqlite3.SQLITE_OK
    db.set_authorizer(deny_second_table)
    with pytest.raises(sqlite3.DatabaseError):
        migrate(db, 1)
    db.set_authorizer(None)
    assert db.execute('PRAGMA user_version').fetchone()[0] == 1
    for table in ('bars','bar_revisions'):
        columns = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
        assert not set(DAILY_METRICS) & columns
    assert db.execute('SELECT close FROM bars').fetchone()[0] == '11'
    migrate(db,1)
    assert db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
    db.close()


def test_enriching_old_bar_archives_old_null_fields(tmp_path):
    with Store(legacy_database(tmp_path/'old.sqlite')) as store:
        cid = store.start_collection('bars','000001',date(2024,1,1),date(2024,1,10),mode='refresh')
        value = Bar('000001','2024-01-10','10','12','9','11','100',None,'-1.2','3.5','0.2')
        assert store.complete_bars(cid,[value])['updated'] == 1
        revision = store.export_rows('bar_revisions')[-1]
        assert all(revision[f] is None for f in DAILY_METRICS)
        assert revision['collection_id'] == 1
        before = store.export_rows()[0]
        repeat = store.start_collection('bars','000001',date(2024,1,1),date(2024,1,10),mode='refresh')
        assert store.complete_bars(repeat,[value])['unchanged'] == 1
        assert store.export_rows()[0] == before
        assert store.state('000001')['checked_end'] == '2024-01-10'


@pytest.mark.parametrize('column',['涨跌幅','振幅','换手率'])
def test_new_columns_are_required_not_filled(column):
    with pytest.raises(ValidationError,match='missing required'):
        bars(frame().drop(columns=[column]),'000001',date(2024,1,1),date(2024,1,10))


@pytest.mark.parametrize('column',['涨跌幅','振幅','换手率'])
def test_new_nulls_and_bad_values(column):
    df=frame()
    df[column]=None
    records,_=bars(df,'000001',date(2024,1,1),date(2024,1,10))
    field={'涨跌幅':'change_pct','振幅':'amplitude_pct','换手率':'turnover_rate_pct'}[column]
    assert getattr(records[0],field) is None
    df[column]='not a number'
    with pytest.raises(ValidationError,match='malformed numeric'):
        bars(df,'000001',date(2024,1,1),date(2024,1,10))


def test_negative_change_not_warning_negative_amplitude_is_warning():
    df=frame()
    df['涨跌幅']=-8.12
    records,warnings=bars(df,'000001',date(2024,1,1),date(2024,1,10))
    assert records[0].change_pct == '-8.12' and not warnings
    df['振幅']=-2
    records,warnings=bars(df,'000001',date(2024,1,1),date(2024,1,10))
    assert records[0].amplitude_pct == '-2' and any('振幅' in item for item in warnings)
