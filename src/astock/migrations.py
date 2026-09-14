"""Small forward-only SQLite migrations, each atomic and data preserving."""

SCHEMA_VERSION = 4
DAILY_METRICS = ("change_pct", "amplitude_pct", "turnover_rate_pct")


def migrate(conn, version):
    """Migrate a validated existing schema; do not own an outer transaction."""
    if version > SCHEMA_VERSION or version < 1:
        raise RuntimeError(f"unsupported database schema version {version}")
    if conn.in_transaction:
        raise RuntimeError("migration cannot nest in an external transaction")
    if version == 1:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table in ("bars", "bar_revisions"):
                for field in DAILY_METRICS:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {field} TEXT")
            # Historical rows stay NULL: no invented metrics or retrospective calculations.
            conn.execute("PRAGMA user_version = 2")
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("foreign key violation during schema migration")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        version = 2
    if version == 2:
        _migrate_v3(conn)
        version = 3
    if version == 3:
        _migrate_v4(conn)


def _migrate_v4(conn):
    import json
    from datetime import date, datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    from .config import default_end
    from .suspension import classify_suspension
    conn.execute('BEGIN IMMEDIATE')
    try:
        cursor = conn.execute('''SELECT s.*, COALESCE(c.finished_at,c.created_at) AS observed_at
                                FROM suspension_daily s JOIN collections c ON c.id=s.collection_id''')
        names = [column[0] for column in cursor.description]
        records = [dict(zip(names, row)) for row in cursor.fetchall()]
        now = datetime.now(timezone.utc).isoformat()
        encode = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        for old in records:
            observed = datetime.fromisoformat(old.pop('observed_at'))
            if observed.tzinfo is None:
                raise RuntimeError('suspension observation timestamp lacks timezone')
            cutoff = min(default_end(), observed.astimezone(ZoneInfo('Asia/Shanghai')).date() - timedelta(days=1))
            status = classify_suspension(json.loads(old['suspension_details_json']), date.fromisoformat(old['trade_date']), cutoff)
            if status == old['suspension_status']:
                continue
            # Same evidence collection, new interpretation; no fabricated API call.
            conn.execute('''INSERT INTO record_revisions(kind,symbol,row_key,old_json,replaced_at,replaced_by)
                            VALUES('suspension',?,?,?,?,?)''',
                         (old['symbol'], encode([old['symbol'],old['trade_date']]), encode(old), now, old['collection_id']))
            conn.execute('''UPDATE suspension_daily SET suspension_status=?,updated_at=?
                            WHERE symbol=? AND trade_date=?''', (status,now,old['symbol'],old['trade_date']))
        if conn.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('foreign key violation during v4 migration')
        conn.execute('PRAGMA user_version=4')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _migrate_v3(conn):
    from .datasets import DATASETS
    from .extra_schema import statements
    foreign_keys = conn.execute('PRAGMA foreign_keys').fetchone()[0]
    # SQLite cannot ALTER an existing CHECK constraint. Rebuild its parent
    # table using the documented FK-off transaction procedure, then validate.
    conn.execute('PRAGMA foreign_keys=OFF')
    try:
        conn.execute('BEGIN IMMEDIATE')
        original = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='collections'").fetchone()[0]
        marker = "CHECK(kind IN ('securities','bars'))"
        if marker not in original or 'CREATE TABLE collections (' not in original:
            raise RuntimeError('unsupported collections schema; refusing unsafe migration')
        kinds = ','.join(repr(k) for k in ('securities','bars',*DATASETS))
        replacement = original.replace('CREATE TABLE collections (','CREATE TABLE collections_new (',1).replace(marker,f'CHECK(kind IN ({kinds}))',1)
        indexes_and_triggers = [r[0] for r in conn.execute("SELECT sql FROM sqlite_master WHERE tbl_name='collections' AND type IN ('index','trigger') AND sql IS NOT NULL")]
        sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='collections'").fetchone()
        conn.execute(replacement)
        conn.execute('INSERT INTO collections_new SELECT * FROM collections')
        conn.execute('DROP TABLE collections')
        conn.execute('ALTER TABLE collections_new RENAME TO collections')
        if sequence:
            updated = conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='collections'",(sequence[0],))
            if updated.rowcount == 0:
                conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('collections',?)",(sequence[0],))
        for sql in indexes_and_triggers:
            conn.execute(sql)
        for sql in statements():
            conn.execute(sql)
        conn.execute('SELECT * FROM daily_data LIMIT 0')
        if conn.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('foreign key violation during v3 migration')
        conn.execute('PRAGMA user_version=3')
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.execute(f'PRAGMA foreign_keys={foreign_keys}')
