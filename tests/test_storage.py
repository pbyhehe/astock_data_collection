from datetime import date
import json
import sqlite3

import pytest
from astock.models import Bar, Security
from astock.storage import Store


def request(store, start="2024-01-01", end="2024-01-05", mode="normal", parent_id=None):
    return store.start_collection("bars", symbol="000001", start=date.fromisoformat(start),
                                  end=date.fromisoformat(end), mode=mode, parent_id=parent_id)


def bar(day="2024-01-03", close="10.20"):
    return Bar(symbol="000001", trade_date=day, open="10", high="11", low="9", close=close, volume="1200", amount=None)


def test_atomic_rollback_and_provenance(tmp_path):
    with Store(tmp_path / "raw.sqlite") as store:
        first = request(store)
        store.complete_bars(first, [bar()])
        original = store.export_rows()
        second = request(store)
        assert store.complete_bars(second, [bar()])["unchanged"] == 1
        assert store.export_rows() == original
        third = request(store)
        store.complete_bars(third, [bar(close="10.21")])
        revision = store.export_rows("bar_revisions")[0]
        assert all(revision[k] == v for k, v in original[0].items())
        before = store.export_rows()
        revisions = store.export_rows("bar_revisions")
        state = store.state("000001")
        cid = request(store, end="2024-01-10")
        store.conn.execute("CREATE TRIGGER fail_state BEFORE UPDATE ON sync_state BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with pytest.raises(sqlite3.IntegrityError):
            store.complete_bars(cid, [bar(close="10.22"), bar("2024-01-08")])
        assert store.export_rows() == before
        assert store.export_rows("bar_revisions") == revisions
        assert store.state("000001") == state
        assert store.conn.execute("SELECT status FROM collections WHERE id=?", (cid,)).fetchone()[0] == "running"
        assert store.recover_interrupted() == 1


def test_noncontiguous_request_does_not_jump(tmp_path):
    with Store(tmp_path / "raw.sqlite") as store:
        store.complete_bars(request(store), [bar()])
        store.complete_bars(request(store, "2024-01-10", "2024-01-15"), [bar("2024-01-12")])
        assert store.state("000001")["checked_end"] == "2024-01-05"
        assert store.state("000001")["latest_data_date"] == "2024-01-12"
        store.complete_bars(request(store, "2024-01-06", "2024-01-09"), [bar("2024-01-08")])
        assert store.state("000001")["checked_end"] == "2024-01-09"


def test_security_changes_and_empty_snapshot(tmp_path):
    with Store(tmp_path / "raw.sqlite") as store:
        def snapshot(rows):
            cid = store.start_collection("securities")
            store.complete_securities(cid, rows)
            return cid
        snapshot([Security("000001", "First"), Security("600000", None)])
        before = store.export_rows("securities")
        empty = snapshot([])
        assert store.export_rows("securities") == before
        assert store.conn.execute("SELECT status FROM collections WHERE id=?", (empty,)).fetchone()[0] == "empty"
        snapshot([Security("000001", "Renamed")])
        assert store.symbols() == ["000001"]
        rows = {r["symbol"]: r for r in store.export_rows("securities")}
        assert rows["600000"]["present"] == 0
        assert len(store.export_rows("security_changes")) == 4
        snapshot([Security("000001", "Renamed"), Security("600000", None)])
        rows2 = {r["symbol"]: r for r in store.export_rows("securities")}
        assert rows2["600000"]["first_seen"] == rows["600000"]["first_seen"]
        json.dumps(store.status())


def test_newer_schema_refused(tmp_path):
    path = tmp_path / "new.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=999")
    with pytest.raises(RuntimeError):
        Store(path)


def test_retry_lineage_and_durable_log(tmp_path):
    path = tmp_path / "raw.sqlite"
    with Store(path) as store:
        root = request(store)
        store.note_attempt(root)
        with Store(path) as observer:
            assert observer.conn.execute("SELECT attempts FROM collections WHERE id=?", (root,)).fetchone()[0] == 1
        store.fail_collection(root, "offline")
        child = request(store, parent_id=root)
        store.complete_bars(child, [])
        assert [r["id"] for r in store.pending()] == [child]
        leaf = request(store, parent_id=child)
        store.complete_bars(leaf, [bar()])
        assert not store.pending()
        failed = store.conn.execute("SELECT * FROM collections WHERE id=?", (root,)).fetchone()
        assert failed["status"] == "failed" and failed["resolved_by"] == leaf
