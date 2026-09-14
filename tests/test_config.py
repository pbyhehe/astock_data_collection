from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
from pathlib import Path
import pytest
import astock.config as module
from astock.config import Config, default_end, load_config
from astock.models import Bar, Security


def test_defaults_and_sample():
    assert load_config() == Config()
    assert Config().history_start == date(1990,12,19)
    sample = load_config(Path(__file__).parents[1] / 'config.example.toml')
    assert sample.overlap_days == 7 and sample.database.is_absolute()


def test_paths_and_native_date(tmp_path):
    path = tmp_path / 'settings.toml'
    path.write_text('[storage]\ndatabase="quotes.sqlite"\n[sync]\nhistory_start=2001-02-03\n')
    cfg = load_config(path)
    assert cfg.database == tmp_path / 'quotes.sqlite'
    assert cfg.history_start == date(2001,2,3)
    path.write_text('')
    assert load_config(path).database == tmp_path / 'data/astock.sqlite3'


@pytest.mark.parametrize('text', [
    'database="wrong"', '[unknown]\nx=1', '[storage]\nunknown=1', '[sync]\nunknown=1',
    'storage=2', 'sync=[]', '[storage]\ndatabase=1', '[storage]\ndatabase=""',
    '[sync]\nhistory_start="20240101"', '[sync]\nhistory_start="2024-02-30"',
    '[sync]\nhistory_start=2024-01-01T00:00:00', '[sync]\noverlap_days=true',
    '[sync]\noverlap_days=0', '[sync]\nmax_attempts=1.5', '[sync]\ncircuit_failures=0',
    '[sync]\nmin_interval=nan', '[sync]\nbackoff_seconds=inf', '[sync]\nrequest_timeout=0',
    '[sync]\nrequest_timeout="60"', '[sync'])
def test_invalid_toml(tmp_path, text):
    path = tmp_path / 'bad.toml'
    path.write_text(text)
    with pytest.raises(ValueError):
        load_config(path)


@pytest.mark.parametrize('field,value', [('database','x'), ('history_start',datetime(2024,1,1)),
    ('overlap_days',True), ('overlap_days',0), ('max_attempts',0), ('max_attempts',2.0),
    ('circuit_failures',-1), ('min_interval',-1), ('min_interval',float('nan')),
    ('backoff_seconds',float('inf')), ('request_timeout',0), ('request_timeout','60')])
def test_invalid_direct(field, value):
    with pytest.raises(ValueError):
        Config(**{field:value})


@pytest.mark.parametrize('hour,minute,expected', [(15,59,date(2024,1,1)),(16,0,date(2024,1,2))])
def test_shanghai_boundary(monkeypatch,hour,minute,expected):
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            assert str(tz) == 'Asia/Shanghai'
            return datetime(2024,1,2,hour,minute,tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(module,'datetime',Clock)
    assert default_end() == expected


def test_models_frozen():
    value = Security('000001',None)
    with pytest.raises(FrozenInstanceError):
        value.name = 'changed'
    bar = Bar('000001','2024-01-02',open='1.2300')
    assert bar.open == '1.2300' and bar.high is None
