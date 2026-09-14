"""Incremental scheduling with bounded calls and durable compensation."""
from datetime import date, timedelta
import json
import logging
import time

from . import validation

log = logging.getLogger(__name__)


class CircuitOpen(RuntimeError):
    pass


class Scheduler:
    def __init__(self, config, store, adapter, *, sleep=time.sleep, clock=time.monotonic):
        self.config = config
        self.store = store
        self.adapter = adapter
        self.sleep = sleep
        self.clock = clock
        self.last_call = None
        self.consecutive_failures = 0

    def _request(self, collection_id, operation):
        """Count consecutive failed network calls (including retry attempts)."""
        for attempt in range(self.config.max_attempts):
            if self.consecutive_failures >= self.config.circuit_failures:
                raise CircuitOpen("consecutive request failures reached circuit threshold")
            if self.last_call is not None:
                delay = self.config.min_interval - (self.clock() - self.last_call)
                if delay > 0:
                    self.sleep(delay)
            self.store.note_attempt(collection_id)
            self.last_call = self.clock()
            try:
                result = operation()
            except Exception as exc:
                self.consecutive_failures += 1
                if self.consecutive_failures >= self.config.circuit_failures:
                    raise CircuitOpen(f"consecutive request failures reached circuit threshold: {type(exc).__name__}: {exc}") from exc
                if attempt + 1 == self.config.max_attempts:
                    raise
                self.sleep(self.config.backoff_seconds * (2 ** attempt))
            else:
                self.consecutive_failures = 0
                return result
        raise AssertionError("invalid attempt limit")

    def _fail_if_running(self, collection_id, error, status="failed"):
        # A signal may arrive immediately after an atomic success commit. Never
        # replace that durable success or mask the original interrupt.
        row = self.store.conn.execute("SELECT status FROM collections WHERE id=?", (collection_id,)).fetchone()
        if row is not None and row["status"] == "running":
            self.store.fail_collection(collection_id, error, status=status)

    def _collect(self, kind, symbol=None, start=None, end=None, mode="normal", parent_id=None):
        collection_id = self.store.start_collection(
            kind, symbol=symbol, start=start, end=end, mode=mode, parent_id=parent_id
        )
        try:
            if kind == "securities":
                frame = self._request(collection_id, self.adapter.securities)
                rows = validation.securities(frame)
                counts = self.store.complete_securities(collection_id, rows)
                warnings = []
            elif kind == 'bars':
                frame = self._request(collection_id, lambda: self.adapter.daily(symbol, start, end))
                rows, warnings = validation.bars(frame, symbol, start, end)
                counts = self.store.complete_bars(collection_id, rows, warnings)
            else:
                from .extra_validation import validate_dataset
                frame = self._request(collection_id, lambda: self.adapter.supplement(kind, symbol, start, end))
                batch = validate_dataset(kind, frame, symbol, start, end)
                if kind in ('dividends','profile','delisting'):
                    batch.warnings.append('current source snapshot, not a historical as-of reconstruction')
                rows, warnings = batch.rows, batch.warnings
                counts = self.store.complete_dataset(collection_id, batch)
            # Completion may add warnings about withdrawn snapshot members.
            # Return/log the committed warnings, not only the validator's list.
            warnings = json.loads(self.store.conn.execute(
                'SELECT warnings_json FROM collections WHERE id=?', (collection_id,)
            ).fetchone()[0])
            for warning in warnings:
                log.warning('%s/%s: %s',kind,symbol,warning)
            return dict(id=collection_id, kind=kind, symbol=symbol, status="success" if rows else "empty",
                        counts=counts, warnings=warnings)
        except CircuitOpen as exc:
            self._fail_if_running(collection_id, str(exc))
            raise
        except (KeyboardInterrupt, SystemExit):
            self._fail_if_running(collection_id, "operation interrupted", status="interrupted")
            raise
        except Exception as exc:
            self._fail_if_running(collection_id, f"{type(exc).__name__}: {exc}")
            log.error("collection %s failed: %s", collection_id, exc)
            return dict(id=collection_id, kind=kind, symbol=symbol, status="failed", error=str(exc))

    def refresh_securities(self):
        return self._collect("securities")

    def _selection(self, datasets):
        from .datasets import select_datasets
        return select_datasets(self.config.datasets if datasets is None else datasets)

    def _needs_symbols(self, selected):
        from .datasets import DATASETS
        return any(kind=='bars' or DATASETS[kind].scope=='symbol' for kind in selected)

    def _incremental_start(self, kind, symbol):
        if kind=='bars':
            state=self.store.state(symbol)
            checked=state.get('checked_end') if state else None
        else:
            state=self.store.dataset_state(kind,symbol)
            checked=state.get('normal_end') if state else None
        start=self.config.history_start
        if checked:
            start=max(start,date.fromisoformat(checked)-timedelta(days=self.config.overlap_days-1))
        return start

    def _eligible_end(self, kind, end):
        from .config import default_end
        eligible=min(end,default_end()) if kind=='valuation' else end
        if eligible!=end:
            log.warning('valuation requested end %s capped to completed Shanghai date %s',end,eligible)
        return eligible

    def sync(self, symbols=None, end=None, *, datasets=None):
        from .config import default_end
        from .datasets import DATASETS
        selected=self._selection(datasets)
        end=end or default_end()
        if type(end) is not date:
            raise ValueError('end must be a date')
        results=[]
        if not self._needs_symbols(selected) and symbols is not None:
            raise ValueError('global-only datasets do not accept symbol filters')
        if self._needs_symbols(selected) and symbols is None:
            result=self.refresh_securities()
            results.append(result)
            if result['status']!='success':
                return results
            symbols=self.store.symbols()
        symbols=list(dict.fromkeys(symbols or []))
        for kind in selected:
            if kind in ('bars','valuation'):
                eligible=self._eligible_end(kind,end)
                for symbol in symbols:
                    start=self._incremental_start(kind,symbol)
                    if start>eligible:
                        results.append(dict(kind=kind,symbol=symbol,status='skipped',reason='end precedes incremental/completed interval; use refresh'))
                    else:
                        result=self._collect(kind,symbol,start,eligible)
                        if eligible!=end:
                            result.update(requested_end=end.isoformat(),eligible_end=eligible.isoformat())
                        results.append(result)
            elif DATASETS[kind].scope=='symbol':
                results.extend(self._collect(kind,symbol) for symbol in symbols)
            elif DATASETS[kind].scope=='date':
                # The source is a single-day suspension report, not a historical
                # interval API. Explicit refresh requests historical days; sync
                # never silently starts decades of calendar-day requests.
                results.append(self._collect(kind,None,end,end))
            else:
                results.append(self._collect(kind))
        return results

    def refresh(self, symbols=None, start=None, end=None, *, datasets=None):
        from .datasets import DATASETS
        selected=self._selection(datasets)
        dated=any(kind in ('bars','valuation','suspension') for kind in selected)
        if dated:
            if type(start) is not date or type(end) is not date or start>end:
                raise ValueError('historical refresh requires an ordered date range')
        elif start is not None or end is not None:
            raise ValueError('snapshot-only datasets do not accept historical date bounds')
        if self._needs_symbols(selected) and not symbols:
            raise ValueError('selected datasets require explicit symbols for refresh')
        if not self._needs_symbols(selected) and symbols is not None:
            raise ValueError('global-only datasets do not accept symbol filters')
        symbols=list(dict.fromkeys(symbols or []))
        results=[]
        for kind in selected:
            if kind in ('bars','valuation'):
                eligible=self._eligible_end(kind,end)
                for symbol in symbols:
                    if start>eligible:
                        results.append(dict(kind=kind,symbol=symbol,status='skipped',reason='no completed date in refresh interval'))
                    else:
                        result=self._collect(kind,symbol,start,eligible,mode='refresh')
                        if eligible!=end:
                            result.update(requested_end=end.isoformat(),eligible_end=eligible.isoformat())
                        results.append(result)
            elif DATASETS[kind].scope=='symbol':
                results.extend(self._collect(kind,symbol,mode='refresh') for symbol in symbols)
            elif DATASETS[kind].scope=='date':
                for offset in range((end-start).days+1):
                    day=start+timedelta(days=offset)
                    results.append(self._collect(kind,None,day,day,mode='refresh'))
            else:
                results.append(self._collect(kind,mode='refresh'))
        return results

    def retry(self, limit=100):
        """Snapshot only: a failure is retried at most once per retry invocation."""
        results = []
        for item in self.store.pending()[:limit]:
            start = date.fromisoformat(item["start_date"]) if item["start_date"] else None
            end = date.fromisoformat(item["end_date"]) if item["end_date"] else None
            results.append(self._collect(item["kind"], item["symbol"], start, end,
                                         item["mode"], parent_id=item["id"]))
        return results
