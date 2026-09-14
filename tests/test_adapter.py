from datetime import date
import multiprocessing as mp
import time
import pytest
import astock.adapter as adapter


@pytest.fixture
def fake(tmp_path):
    path = tmp_path / 'fake_ak.py'
    path.write_text('''import pandas as pd
import time
import os
def stock_info_a_code_name():
    return pd.DataFrame({'code':['000001'], 'name':['A']})
def stock_zh_a_hist(**kwargs):
    return pd.DataFrame([kwargs])
def slow():
    time.sleep(30)
    return pd.DataFrame()
def broken():
    raise ValueError('upstream unavailable')
def crash():
    os._exit(7)
def large():
    return pd.DataFrame({'x':['x' * 1024] * 20000})
def wrong():
    return 3
''', encoding='utf-8')
    return str(path)


def test_exact_api_calls(fake):
    client = adapter.AkshareAdapter(module=fake, timeout=10)
    assert client.securities().to_dict('records') == [{'code':'000001','name':'A'}]
    row = client.daily('000001', date(2024,1,2),date(2024,1,3)).to_dict('records')[0]
    assert row == {'symbol':'000001','period':'daily','start_date':'20240102','end_date':'20240103','adjust':''}


def test_deadline_and_cleanup(fake):
    before = {p.pid for p in mp.active_children()}
    started = time.monotonic()
    with pytest.raises(adapter.AdapterTimeout):
        adapter.invoke('slow', timeout=1, module=fake)
    assert time.monotonic() - started < 3
    assert {p.pid for p in mp.active_children()} == before


@pytest.mark.parametrize('function', ['broken','crash','wrong'])
def test_errors_not_empty_or_fallback(fake, function):
    with pytest.raises(adapter.AdapterError):
        adapter.invoke(function, module=fake, timeout=10)


def test_large_transfer_no_join_deadlock(fake):
    assert len(adapter.invoke('large', module=fake, timeout=10)) == 20000


@pytest.mark.parametrize('timeout', [0,-1,float('inf'),float('nan'),True,'60',None])
def test_timeout_parameter(timeout):
    with pytest.raises(ValueError):
        adapter.AkshareAdapter(timeout)


def test_parent_interrupt_cleans_worker(fake, monkeypatch):
    before = {p.pid for p in mp.active_children()}
    class InterruptQueue:
        def __init__(self, **kwargs):
            pass
        def get(self, **kwargs):
            raise KeyboardInterrupt
        def put(self, value):
            pass
    monkeypatch.setattr(adapter, 'Queue', InterruptQueue)
    with pytest.raises(KeyboardInterrupt):
        adapter.invoke('slow', module=fake, timeout=10)
    assert {p.pid for p in mp.active_children()} == before


def _partial_worker(sender, module, function, params):
    import struct
    sender._send(struct.pack('!i', 100000))
    sender._send(b'x')
    time.sleep(30)


def test_partial_pipe_transfer_deadline(fake, monkeypatch):
    before = {p.pid for p in mp.active_children()}
    monkeypatch.setattr(adapter, '_worker', _partial_worker)
    started = time.monotonic()
    with pytest.raises(adapter.AdapterTimeout):
        adapter.invoke('slow', module=fake, timeout=1)
    assert time.monotonic() - started < 3
    assert {p.pid for p in mp.active_children()} == before
