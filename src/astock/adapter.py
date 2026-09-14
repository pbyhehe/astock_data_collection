"""The two fixed AKShare calls, isolated behind a total deadline."""
from datetime import date
import importlib
import importlib.util
import math
import multiprocessing as mp
from queue import Queue, Empty
from threading import Thread
import time
from typing import Any
import pandas as pd


class AdapterError(RuntimeError):
    pass


class AdapterTimeout(AdapterError, TimeoutError):
    pass


def _load_module(module: str):
    if module.endswith('.py'):
        spec = importlib.util.spec_from_file_location('_astock_injected_fetch', module)
        if spec is None or spec.loader is None:
            raise AdapterError(f'cannot load module: {module}')
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        return loaded
    return importlib.import_module(module)


def _invoke(module: str, function: str, params: dict[str, Any]):
    """Spawn-safe import path/file injection for offline tests."""
    return getattr(_load_module(module), function)(**params)


def _worker(sender, module, function, params):
    try:
        sender.send(('ok', _invoke(module, function, params)))
    except BaseException as exc:
        try:
            sender.send(('error', f'{type(exc).__name__}: {exc}'))
        except (OSError, EOFError):
            pass
    finally:
        sender.close()


def _receive(receiver, outcomes):
    try:
        outcomes.put(receiver.recv())
    except BaseException as exc:
        outcomes.put(('error', f'worker receive failed: {type(exc).__name__}: {exc}'))


def invoke(function: str, params: dict[str, Any] | None = None, *, timeout: float = 60, module: str = 'akshare') -> pd.DataFrame:
    """Total deadline covers worker import, fetch and pipe transfer; cleanup <=0.4s."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be positive and finite')
    deadline = time.monotonic() + timeout
    context = mp.get_context('spawn')
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=_worker, args=(sender, module, function, params or {}), daemon=True)
    outcomes = Queue(maxsize=1)
    reader = Thread(target=_receive, args=(receiver, outcomes), daemon=True)
    reader_started = False
    try:
        worker.start()
        sender.close()
        reader.start()
        reader_started = True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AdapterTimeout(f'{function}: total invocation exceeded {timeout}s')
        try:
            status, value = outcomes.get(timeout=remaining)
        except Empty:
            raise AdapterTimeout(f'{function}: total invocation exceeded {timeout}s') from None
        if time.monotonic() > deadline:
            raise AdapterTimeout(f'{function}: total invocation exceeded {timeout}s')
        if status != 'ok':
            raise AdapterError(f'{function}: {value}')
        if not isinstance(value, pd.DataFrame):
            raise AdapterError(f'{function}: expected pandas DataFrame')
        return value
    finally:
        sender.close()
        if worker.pid is not None:
            if worker.is_alive():
                worker.kill()
            worker.join(timeout=0.2)
        receiver.close()
        if reader_started:
            reader.join(timeout=0.2)
        if worker.pid is not None and not worker.is_alive():
            worker.close()


class AkshareAdapter:
    def __init__(self, timeout: float = 60, *, module: str = 'akshare'):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout must be positive and finite')
        self.timeout = timeout
        self.module = module

    def securities(self) -> pd.DataFrame:
        return invoke('stock_info_a_code_name', timeout=self.timeout, module=self.module)

    def daily(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        from .validation import _symbol
        _symbol(symbol)
        if type(start) is not date or type(end) is not date or start > end:
            raise ValueError('start and end must be dates with start <= end')
        return invoke('stock_zh_a_hist', {'symbol':symbol, 'period':'daily', 'start_date':start.strftime('%Y%m%d'), 'end_date':end.strftime('%Y%m%d'), 'adjust':''}, timeout=self.timeout, module=self.module)

    def supplement(self, kind: str, symbol: str | None = None, start: date | None = None, end: date | None = None) -> pd.DataFrame:
        from .datasets import DATASETS
        from .validation import _symbol
        if kind not in DATASETS:
            raise ValueError(f'unknown supplemental dataset: {kind}')
        spec = DATASETS[kind]
        if spec.scope == 'symbol':
            _symbol(symbol)
            params = {'symbol': symbol}
        elif spec.scope == 'date':
            if symbol is not None or type(start) is not date or start != end:
                raise ValueError('suspension requires one global query date')
            params = {'date': start.strftime('%Y%m%d')}
        else:
            if symbol is not None:
                raise ValueError('delisting uses a fixed SZ-wide termination list')
            params = {'symbol': '终止上市公司'}
        return invoke(spec.interface, params, timeout=self.timeout, module=self.module)
