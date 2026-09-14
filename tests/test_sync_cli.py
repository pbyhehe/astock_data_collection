from datetime import date
from dataclasses import replace
import json
import sqlite3

import pandas as pd
import pytest

from astock.cli import main
from astock.config import Config
from astock.storage import Store
from astock.sync import Scheduler, CircuitOpen
from astock.locking import InstanceLock, AlreadyRunning
from astock.exporting import export


def frame(day="2024-01-10", close=10):
    return pd.DataFrame([{"日期": day, "开盘": 10, "最高": 12, "最低": 9, "收盘": close,
                          "成交量": 100, "成交额": None, "涨跌幅": 0, "振幅": 3, "换手率": 0.2}])


class Fake:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def securities(self):
        return pd.DataFrame([{"code": "000001", "name": "平安银行"}])

    def daily(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        result = self.responses.pop(0) if self.responses else frame()
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
def config(tmp_path):
    return Config(database=tmp_path / "raw.sqlite3", history_start=date(2024, 1, 1),
                  min_interval=0, backoff_seconds=0, max_attempts=2, circuit_failures=3)


def test_incremental_idempotent_revision(config):
    adapter = Fake([frame(), frame(), frame(close=11)])
    with Store(config.database) as store:
        scheduler = Scheduler(config, store, adapter)
        scheduler.sync(["000001"], date(2024, 1, 10))
        scheduler.sync(["000001"], date(2024, 1, 11))
        assert adapter.calls[1][1] == date(2024, 1, 4)
        assert store.conn.execute("SELECT COUNT(*) FROM bar_revisions").fetchone()[0] == 0
        scheduler.sync(["000001"], date(2024, 1, 12))
        assert store.conn.execute("SELECT COUNT(*) FROM bar_revisions").fetchone()[0] == 1
        assert store.state("000001")["checked_end"] == "2024-01-12"
        assert store.state("000001")["latest_data_date"] == "2024-01-10"


def test_empty_then_retry_and_interrupted_recovery(config):
    with Store(config.database) as store:
        scheduler = Scheduler(config, store, Fake([pd.DataFrame(), frame()]))
        assert scheduler.sync(["000001"], date(2024, 1, 10))[0]["status"] == "empty"
        state = store.state("000001")
        assert state is None or state["checked_end"] is None
        assert len(store.pending()) == 1
        assert scheduler.retry()[0]["status"] == "success"
        assert not store.pending()
        store.start_collection("bars", "000001", date(2024, 1, 4), date(2024, 1, 11))
    with Store(config.database) as store:
        assert store.recover_interrupted() == 1
        assert store.pending()[0]["status"] == "interrupted"
        assert Scheduler(config, store, Fake()).retry()[0]["status"] == "success"
        assert not store.pending()


def test_bounded_retry_circuit_and_compensation(config):
    with Store(config.database) as store:
        adapter = Fake([OSError("offline"), OSError("offline"), OSError("offline")])
        scheduler = Scheduler(config, store, adapter)
        with pytest.raises(CircuitOpen):
            scheduler.sync(["000001", "000002", "000003"], date(2024, 1, 10))
        assert len(adapter.calls) == 3
        assert len(store.pending()) == 2
        assert not store.state("000001")
        # A fresh invocation resets circuit; retry persists and resolves the old failure.
        good = Fake()
        assert all(r["status"] == "success" for r in Scheduler(config, store, good).retry())
        assert not store.pending()


def test_retry_failed_chain_only_one_pending_leaf(config):
    with Store(config.database) as store:
        scheduler = Scheduler(replace(config, max_attempts=1, circuit_failures=100), store,
                              Fake([OSError("x"), OSError("y"), frame()]))
        scheduler.sync(["000001"], date(2024, 1, 10))
        scheduler.retry()
        assert len(store.pending()) == 1
        assert len(scheduler.retry()) == 1
        assert not store.pending()


def test_refresh_does_not_skip_history(config):
    with Store(config.database) as store:
        adapter = Fake([frame("2024-06-01"), frame("2024-01-10")])
        scheduler = Scheduler(config, store, adapter)
        scheduler.refresh(["000001"], date(2024, 6, 1), date(2024, 6, 2))
        assert store.state("000001")["checked_end"] is None
        scheduler.sync(["000001"], date(2024, 1, 10))
        assert adapter.calls[-1][1] == config.history_start
        assert store.state("000001")["latest_data_date"] == "2024-06-01"
        assert store.state("000001")["checked_end"] == "2024-01-10"


def test_interrupt_retains_compensation(config):
    with Store(config.database) as store:
        with pytest.raises(KeyboardInterrupt):
            Scheduler(config, store, Fake([KeyboardInterrupt()])).sync(["000001"], date(2024, 1, 10))
        assert store.pending()[0]["status"] == "interrupted"
        assert not store.conn.execute("SELECT * FROM bars").fetchall()


def test_lock_released_and_canonical(tmp_path):
    db = tmp_path / "db.sqlite"
    with InstanceLock(db):
        with pytest.raises(AlreadyRunning):
            with InstanceLock(tmp_path / "." / "db.sqlite"):
                pass
    with InstanceLock(db):
        pass


def test_cli_all_commands_and_exports(tmp_path, capsys):
    config_file = tmp_path / "config.toml"
    config_file.write_text('[storage]\ndatabase="raw.sqlite3"\n[sync]\nhistory_start="2024-01-01"\nmin_interval=0\nbackoff_seconds=0\n', encoding="utf-8")
    prefix = ["--config", str(config_file)]
    assert main(prefix + ["refresh", "--securities"], adapter=Fake()) == 0
    assert main(prefix + ["sync", "--end", "2024-01-10"], adapter=Fake()) == 0
    assert main(prefix + ["refresh", "--symbols", "000001", "--start", "2024-01-01", "--end", "2024-01-10"], adapter=Fake()) == 0
    assert main(prefix + ["retry"], adapter=Fake()) == 0
    assert main(prefix + ["status"]) == 0
    csv_file = tmp_path / "bars.csv"
    assert main(prefix + ["export", "--output", str(csv_file)]) == 0
    assert "\\N" in csv_file.read_text()
    assert main(prefix + ["export", "--output", str(csv_file)]) == 1
    parquet = tmp_path / "bars.parquet"
    assert main(prefix + ["export", "--format", "parquet", "--output", str(parquet)]) == 0
    import pyarrow.parquet as pq
    rows = pq.read_table(parquet).to_pylist()
    assert rows[0]["symbol"] == "000001"
    assert rows[0]["amount"] is None
    with pytest.raises(SystemExit) as err:
        main(prefix + ["refresh"])
    assert err.value.code == 2


def test_cli_empty_exit_nonzero(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text('[storage]\ndatabase="raw.sqlite3"\n')
    assert main(["--config", str(config_file), "sync", "--symbols", "000001", "--end", "2024-01-10"],
                adapter=Fake([pd.DataFrame()])) == 1


def test_export_protects_database(config):
    with Store(config.database) as store:
        with pytest.raises(ValueError):
            export(store, config.database)
        with pytest.raises(ValueError):
            export(store, str(config.database) + "-wal")
        with pytest.raises(ValueError):
            export(store, config.database.parent / "dump", table="bars; DROP TABLE bars")
