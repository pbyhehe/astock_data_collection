from pathlib import Path
import re

from astock.storage import Store, TABLES, EXPORT_TABLES


def test_documented_tables_and_columns_cover_current_sqlite_schema(tmp_path):
    text=(Path(__file__).resolve().parents[1]/'docs/schema.md').read_text(encoding='utf-8')
    assert len(TABLES)==13 and 'daily_data' in EXPORT_TABLES
    with Store(tmp_path/'db') as store:
        for table in EXPORT_TABLES:
            assert re.search(r'\b'+re.escape(table)+r'\b',text), table
            for column in store.conn.execute(f'PRAGMA table_info({table})'):
                assert re.search(r'\b'+re.escape(column['name'])+r'\b',text), (table,column['name'])
