"""SQLite persistence with durable logs and atomic collection completion."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping

from .migrations import SCHEMA_VERSION, DAILY_METRICS, migrate
from .datasets import DATASETS
from .extra_schema import EXTRA_TABLES
from .extra_storage import ExtraStoreMixin

SOURCE = "stock_zh_a_hist"
VALUE_FIELDS = ("open", "high", "low", "close", "volume", "amount", *DAILY_METRICS)
BAR_FIELDS = ("symbol", "trade_date", *VALUE_FIELDS, "source", "updated_at", "collection_id")
TABLES = ("securities", "security_changes", "bars", "bar_revisions", "collections", "sync_state", *EXTRA_TABLES)
EXPORT_TABLES = (*TABLES, 'daily_data')
RETRYABLE = ("failed", "empty", "interrupted")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS collections (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 kind TEXT NOT NULL CHECK(kind IN ('securities','bars')),
 symbol TEXT, start_date TEXT, end_date TEXT,
 mode TEXT NOT NULL CHECK(mode IN ('normal','refresh')),
 status TEXT NOT NULL CHECK(status IN ('running','success','empty','failed','interrupted')),
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0), error TEXT,
 created_at TEXT NOT NULL, finished_at TEXT,
 counts TEXT NOT NULL DEFAULT '{}', warnings_json TEXT NOT NULL DEFAULT '[]',
 parent_id INTEGER REFERENCES collections(id),
 root_id INTEGER REFERENCES collections(id), resolved_by INTEGER REFERENCES collections(id)
);
CREATE INDEX IF NOT EXISTS collections_parent_idx ON collections(parent_id);
CREATE INDEX IF NOT EXISTS collections_pending_idx ON collections(status,resolved_by);
CREATE TABLE IF NOT EXISTS securities (
 symbol TEXT PRIMARY KEY, name TEXT, present INTEGER NOT NULL CHECK(present IN (0,1)),
 first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS security_changes (
 id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
 change_type TEXT NOT NULL CHECK(change_type IN ('inserted','updated','removed')),
 old_json TEXT, new_json TEXT NOT NULL, changed_at TEXT NOT NULL,
 collection_id INTEGER NOT NULL REFERENCES collections(id)
);
CREATE TABLE IF NOT EXISTS bars (
 symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
 open TEXT, high TEXT, low TEXT, close TEXT, volume TEXT, amount TEXT,
 source TEXT NOT NULL, updated_at TEXT NOT NULL,
 collection_id INTEGER NOT NULL REFERENCES collections(id), PRIMARY KEY(symbol,trade_date)
);
CREATE TABLE IF NOT EXISTS bar_revisions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
 open TEXT, high TEXT, low TEXT, close TEXT, volume TEXT, amount TEXT,
 source TEXT NOT NULL, updated_at TEXT NOT NULL,
 collection_id INTEGER NOT NULL REFERENCES collections(id),
 replaced_at TEXT NOT NULL, replaced_by INTEGER NOT NULL REFERENCES collections(id)
);
CREATE INDEX IF NOT EXISTS bar_revisions_key_idx ON bar_revisions(symbol,trade_date);
CREATE TABLE IF NOT EXISTS sync_state (
 symbol TEXT PRIMARY KEY, checked_start TEXT, checked_end TEXT, latest_data_date TEXT,
 CHECK((checked_start IS NULL AND checked_end IS NULL) OR
 (checked_start IS NOT NULL AND checked_end IS NOT NULL AND checked_start <= checked_end))
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _record(value: Any) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("rows must be dataclass instances or mappings")


def _day(value: date | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        raise TypeError("expected a date, not a datetime")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        parsed = date.fromisoformat(value)
        if value != parsed.isoformat():
            raise ValueError("dates must use YYYY-MM-DD")
        return value
    raise TypeError("expected a date or YYYY-MM-DD string")


def _symbol(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value):
        raise ValueError("symbol must be six ASCII digits")
    return value


class Store(ExtraStoreMixin):
    """Single-connection store; mutation methods own their transactions."""
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            path = Path(path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.execute("PRAGMA busy_timeout = 5000")
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"database schema version {version} is newer than supported version {SCHEMA_VERSION}")
            if version not in range(SCHEMA_VERSION + 1):
                raise RuntimeError(f"unsupported database schema version {version}")
            if version == 0:
                self.conn.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA + "\nPRAGMA user_version = 1;\nCOMMIT;")
                version = 1
            migrate(self.conn, version)
        except BaseException:
            if self.conn.in_transaction:
                self.conn.rollback()
            self.conn.close()
            raise

    def __enter__(self) -> Store:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def _transaction(self):
        if self.conn.in_transaction:
            raise RuntimeError("Store mutation cannot nest in an external transaction")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def _running(self, collection_id: int, kind: str | None = None):
        row = self.conn.execute("SELECT * FROM collections WHERE id = ?", (collection_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown collection {collection_id}")
        if row["status"] != "running":
            raise ValueError(f"collection {collection_id} is not running")
        if kind is not None and row["kind"] != kind:
            raise ValueError(f"collection {collection_id} is not a {kind} collection")
        return row

    def start_collection(self, kind: str, symbol: str | None = None,
                         start: date | str | None = None, end: date | str | None = None,
                         mode: str = "normal", parent_id: int | None = None) -> int:
        if kind not in ('securities', 'bars', *DATASETS):
            raise ValueError('unknown collection kind')
        if mode not in ("normal", "refresh"):
            raise ValueError("mode must be normal or refresh")
        start_date, end_date = _day(start), _day(end)
        if kind == "bars":
            symbol = _symbol(symbol)
            if start_date is None or end_date is None or start_date > end_date:
                raise ValueError("bars require an ordered, inclusive date range")
        elif kind in DATASETS:
            spec = DATASETS[kind]
            if spec.scope == 'symbol':
                symbol = _symbol(symbol)
                if kind == 'valuation':
                    if start_date is None or end_date is None or start_date > end_date:
                        raise ValueError('valuation requires an ordered date range')
                elif start_date is not None or end_date is not None:
                    raise ValueError('company snapshots do not accept date bounds')
            elif spec.scope == 'date':
                if symbol is not None or start_date is None or start_date != end_date:
                    raise ValueError('suspension requires one global query date')
            elif symbol is not None or start_date is not None or end_date is not None:
                raise ValueError('delisting is a fixed SZ-wide snapshot')
        elif symbol is not None or start_date is not None or end_date is not None:
            raise ValueError("securities collections cannot have symbol/date bounds")
        with self._transaction():
            root_id = None
            if parent_id is not None:
                parent = self.conn.execute("SELECT * FROM collections WHERE id = ?", (parent_id,)).fetchone()
                if parent is None:
                    raise ValueError("unknown retry parent")
                requested = (kind, symbol, start_date, end_date, mode)
                original = tuple(parent[key] for key in ("kind", "symbol", "start_date", "end_date", "mode"))
                if requested != original:
                    raise ValueError("retry must match its parent's exact request")
                if parent["status"] not in RETRYABLE or parent["resolved_by"] is not None:
                    raise ValueError("retry parent is not an unresolved failure")
                if self.conn.execute("SELECT 1 FROM collections WHERE parent_id = ? LIMIT 1", (parent_id,)).fetchone():
                    raise ValueError("retry parent already has a child; retry the leaf")
                root_id = parent["root_id"] or parent_id
            cursor = self.conn.execute(
                """INSERT INTO collections (kind,symbol,start_date,end_date,mode,status,created_at,parent_id,root_id)
                   VALUES (?,?,?,?,?,'running',?,?,?)""",
                (kind, symbol, start_date, end_date, mode, _now(), parent_id, root_id))
            collection_id = cursor.lastrowid
            if root_id is None:
                self.conn.execute("UPDATE collections SET root_id = ? WHERE id = ?", (collection_id, collection_id))
        return collection_id

    def note_attempt(self, collection_id: int) -> None:
        with self._transaction():
            self._running(collection_id)
            self.conn.execute("UPDATE collections SET attempts = attempts + 1 WHERE id = ?", (collection_id,))

    def fail_collection(self, collection_id: int, error: str, status: str = "failed") -> None:
        if status not in ("failed", "interrupted"):
            raise ValueError("failure status must be failed or interrupted")
        with self._transaction():
            self._running(collection_id)
            self.conn.execute("UPDATE collections SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                              (status, str(error), _now(), collection_id))

    def recover_interrupted(self) -> int:
        """Call under the application's exclusive process lock on startup."""
        with self._transaction():
            cursor = self.conn.execute(
                """UPDATE collections SET status = 'interrupted',
                   error = 'collection interrupted before completion', finished_at = ?
                   WHERE status = 'running'""", (_now(),))
            return cursor.rowcount

    def pending(self) -> list[dict[str, Any]]:
        """Unresolved leaf failures only; a running retry suppresses its parent."""
        rows = self.conn.execute(
            """SELECT c.* FROM collections c
               WHERE c.status IN ('failed','empty','interrupted') AND c.resolved_by IS NULL
               AND NOT EXISTS (SELECT 1 FROM collections child WHERE child.parent_id = c.id)
               ORDER BY c.id""")
        return [dict(row) for row in rows]

    def _finish(self, collection, counts: dict[str, int], warnings: list[str], *, empty: bool) -> None:
        self.conn.execute(
            """UPDATE collections SET status = ?, finished_at = ?, counts = ?, warnings_json = ?,
               error = NULL WHERE id = ?""",
            ("empty" if empty else "success", _now(), _json(counts), _json(warnings), collection["id"]))
        if not empty:
            parent_id = collection["parent_id"]
            while parent_id is not None:
                parent = self.conn.execute("SELECT parent_id FROM collections WHERE id = ?", (parent_id,)).fetchone()
                if parent is None:
                    raise RuntimeError("broken collection retry lineage")
                self.conn.execute("UPDATE collections SET resolved_by = ? WHERE id = ?", (collection["id"], parent_id))
                parent_id = parent["parent_id"]

    def complete_securities(self, collection_id: int, rows: Iterable[Any]) -> dict[str, int]:
        incoming: dict[str, str | None] = {}
        for value in rows:
            row = _record(value)
            symbol = _symbol(row["symbol"])
            name = row.get("name")
            if name is not None and not isinstance(name, str):
                raise TypeError("security name must be a string or None")
            if symbol in incoming:
                raise ValueError(f"duplicate security {symbol}")
            incoming[symbol] = name
        counts = dict(inserted=0, updated=0, unchanged=0, removed=0)
        with self._transaction():
            collection = self._running(collection_id, "securities")
            if not incoming:
                self._finish(collection, counts, [], empty=True)
                return counts
            now = _now()
            existing = {row["symbol"]: dict(row) for row in self.conn.execute("SELECT * FROM securities")}
            for symbol, name in incoming.items():
                old = existing.get(symbol)
                new = dict(symbol=symbol, name=name, present=1,
                           first_seen=old["first_seen"] if old else now, last_seen=now)
                if old is None:
                    change = "inserted"
                elif old["name"] != name or old["present"] != 1:
                    change = "updated"
                else:
                    change = "unchanged"
                counts[change] += 1
                self.conn.execute(
                    """INSERT INTO securities (symbol,name,present,first_seen,last_seen)
                       VALUES (:symbol,:name,:present,:first_seen,:last_seen)
                       ON CONFLICT(symbol) DO UPDATE SET
                       name=excluded.name,present=1,last_seen=excluded.last_seen""", new)
                if change != "unchanged":
                    self._security_change(symbol, change, old, new, now, collection_id)
            for symbol, old in existing.items():
                if old["present"] and symbol not in incoming:
                    new = dict(old, present=0)
                    self.conn.execute("UPDATE securities SET present = 0 WHERE symbol = ?", (symbol,))
                    counts["removed"] += 1
                    self._security_change(symbol, "removed", old, new, now, collection_id)
            self._finish(collection, counts, [], empty=False)
        return counts

    def _security_change(self, symbol, change, old, new, now, collection_id):
        self.conn.execute(
            """INSERT INTO security_changes (symbol,change_type,old_json,new_json,changed_at,collection_id)
               VALUES (?,?,?,?,?,?)""",
            (symbol, change, None if old is None else _json(old), _json(new), now, collection_id))

    def complete_bars(self, collection_id: int, rows: Iterable[Any],
                      warnings: list[str] | None = None) -> dict[str, int]:
        incoming = [_record(value) for value in rows]
        warnings = [] if warnings is None else list(warnings)
        if not all(isinstance(warning, str) for warning in warnings):
            raise TypeError("warnings must contain strings")
        counts = dict(inserted=0, updated=0, unchanged=0)
        with self._transaction():
            collection = self._running(collection_id, "bars")
            seen = set()
            for row in incoming:
                if _symbol(row["symbol"]) != collection["symbol"]:
                    raise ValueError("bar symbol does not match collection")
                trade_date = _day(row["trade_date"])
                if trade_date is None or not (collection["start_date"] <= trade_date <= collection["end_date"]):
                    raise ValueError("bar date is outside collection bounds")
                row["trade_date"] = trade_date
                if trade_date in seen:
                    raise ValueError(f"duplicate bar date {trade_date}")
                seen.add(trade_date)
                for field in VALUE_FIELDS:
                    value = row[field]
                    if value is not None and not isinstance(value, str):
                        raise TypeError(f"{field} must be a numeric string or None")
            if not incoming:
                self._finish(collection, counts, warnings, empty=True)
                return counts
            now = _now()
            field_sql = ",".join(BAR_FIELDS)
            placeholders = ",".join("?" for _ in BAR_FIELDS)
            updates = ",".join(f"{field}=excluded.{field}" for field in BAR_FIELDS[2:])
            for row in incoming:
                old = self.conn.execute("SELECT * FROM bars WHERE symbol = ? AND trade_date = ?",
                                        (row["symbol"], row["trade_date"])).fetchone()
                if old is not None and all(old[field] == row[field] for field in VALUE_FIELDS):
                    counts["unchanged"] += 1
                    continue
                if old is None:
                    counts["inserted"] += 1
                else:
                    counts["updated"] += 1
                    self.conn.execute(
                        f"""INSERT INTO bar_revisions ({field_sql},replaced_at,replaced_by)
                            VALUES ({placeholders},?,?)""",
                        tuple(old[field] for field in BAR_FIELDS) + (now, collection_id))
                values = (row["symbol"], row["trade_date"], *(row[field] for field in VALUE_FIELDS),
                          SOURCE, now, collection_id)
                self.conn.execute(
                    f"""INSERT INTO bars ({field_sql}) VALUES ({placeholders})
                        ON CONFLICT(symbol,trade_date) DO UPDATE SET {updates}""", values)
            self._advance_state(collection)
            self._finish(collection, counts, warnings, empty=False)
        return counts

    def _advance_state(self, collection) -> None:
        symbol = collection["symbol"]
        old = self.state(symbol)
        checked_start = old["checked_start"] if old else None
        checked_end = old["checked_end"] if old else None
        if collection["mode"] == "normal":
            if checked_end is None:
                checked_start = collection["start_date"]
                checked_end = collection["end_date"]
            elif collection["end_date"] > checked_end:
                start_ordinal = date.fromisoformat(collection["start_date"]).toordinal()
                if start_ordinal <= date.fromisoformat(checked_end).toordinal() + 1:
                    checked_end = collection["end_date"]
        latest = self.conn.execute("SELECT MAX(trade_date) FROM bars WHERE symbol = ?", (symbol,)).fetchone()[0]
        self.conn.execute(
            """INSERT INTO sync_state (symbol,checked_start,checked_end,latest_data_date)
               VALUES (?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET
               checked_start=excluded.checked_start,checked_end=excluded.checked_end,
               latest_data_date=excluded.latest_data_date""",
            (symbol, checked_start, checked_end, latest))

    def state(self, symbol: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM sync_state WHERE symbol = ?", (symbol,)).fetchone()
        return dict(row) if row is not None else None

    def symbols(self) -> list[str]:
        return [row["symbol"] for row in self.conn.execute(
            "SELECT symbol FROM securities WHERE present = 1 ORDER BY symbol")]

    def status(self) -> dict[str, Any]:
        counts = {table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in TABLES}
        counts["present_securities"] = self.conn.execute(
            "SELECT COUNT(*) FROM securities WHERE present = 1").fetchone()[0]
        recent = [dict(row) for row in self.conn.execute(
            "SELECT * FROM collections ORDER BY id DESC LIMIT 20")]
        for row in recent:
            row["counts"] = json.loads(row["counts"])
            row["warnings"] = json.loads(row.pop("warnings_json"))
        return dict(counts=counts, pending_count=len(self.pending()),
                    states=[dict(row) for row in self.conn.execute("SELECT * FROM sync_state ORDER BY symbol")],
                    dataset_states=[dict(row) for row in self.conn.execute('SELECT * FROM dataset_state ORDER BY kind,scope')],
                    coverage={kind: spec.coverage for kind,spec in DATASETS.items()},
                    recent_collections=recent)

    def export_rows(self, table: str = "bars", symbol: str | None = None,
                    start: date | str | None = None, end: date | str | None = None) -> list[dict[str, Any]]:
        """Export a whitelisted table; date filters apply to bar trade dates."""
        if table not in EXPORT_TABLES:
            raise ValueError(f"table must be one of {', '.join(EXPORT_TABLES)}")
        start_date, end_date = _day(start), _day(end)
        if start_date is not None and end_date is not None and start_date > end_date:
            raise ValueError("start must not exceed end")
        date_fields = {'bars':'trade_date','bar_revisions':'trade_date','daily_data':'trade_date',
                       'valuation_daily':'trade_date','suspension_daily':'trade_date','delisting_events':'delisting_date'}
        if (start_date is not None or end_date is not None) and table not in date_fields:
            raise ValueError('date filters are unsupported for this snapshot/audit table')
        clauses = []
        parameters = []
        if symbol is not None:
            clauses.append('scope = ?' if table == 'dataset_state' else 'symbol = ?')
            parameters.append(_symbol(symbol))
        if start_date is not None:
            clauses.append(f'{date_fields[table]} >= ?')
            parameters.append(start_date)
        if end_date is not None:
            clauses.append(f'{date_fields[table]} <= ?')
            parameters.append(end_date)
        orders = {"securities": "symbol", "security_changes": "id", "bars": "symbol,trade_date",
                  "bar_revisions": "symbol,trade_date,id", "collections": "id", "sync_state": "symbol",
                  'daily_data':'symbol,trade_date','record_revisions':'id','dataset_state':'kind,scope',
                  **{spec.table:','.join(spec.keys) for spec in DATASETS.values()}}
        order = orders[table]
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return [dict(row) for row in self.conn.execute(
            f"SELECT * FROM {table}{where} ORDER BY {order}", parameters)]
