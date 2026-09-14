"""Command line interface; importing it never starts network requests."""
import argparse
from datetime import date
import json
import logging
import re
import sqlite3
import sys

from .adapter import AkshareAdapter
from .config import load_config, default_end
from .exporting import export, TABLES
from .locking import InstanceLock
from .storage import Store
from .sync import Scheduler, CircuitOpen


def _date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
        return parsed
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _symbols(value):
    values = value.split(",")
    if not all(re.fullmatch(r"[0-9]{6}", s) for s in values):
        raise argparse.ArgumentTypeError("symbols must be comma-separated six-digit codes")
    return list(dict.fromkeys(values))


def _single_symbol(value):
    if "," in value:
        raise argparse.ArgumentTypeError("one symbol required")
    return _symbols(value)[0]


def _datasets(value):
    from .datasets import ALL_DATASETS, select_datasets
    if value == 'all':
        return ALL_DATASETS
    try:
        return select_datasets(value.split(','))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parser():
    p = argparse.ArgumentParser(prog="astock", description="AKShare A股原始数据采集（不复权）")
    p.add_argument("--config", help="TOML config file; paths resolve relative to it")
    commands = p.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="refresh universe then incremental sync, or sync explicit symbols")
    sync.add_argument('--datasets', type=_datasets, help='all or comma-separated bars,valuation,dividends,profile,suspension,delisting; overrides config')
    sync.add_argument("--symbols", type=_symbols, help='applies to per-symbol datasets; suspension/delisting remain global')
    sync.add_argument("--end", type=_date, help="default: yesterday in Asia/Shanghai")
    commands.add_parser("status", help="show SQLite progress and unresolved failures (offline)")
    retry = commands.add_parser("retry", help="retry unresolved failed, interrupted, and empty requests")
    retry.add_argument("--limit", type=int, default=100)
    refresh = commands.add_parser("refresh", help="refresh stock list or explicitly re-fetch historical interval")
    refresh.add_argument('--datasets', type=_datasets, help='all or comma-separated fixed dataset names; overrides config')
    refresh.add_argument("--securities", action="store_true")
    refresh.add_argument("--symbols", type=_symbols)
    refresh.add_argument("--start", type=_date)
    refresh.add_argument("--end", type=_date)
    output = commands.add_parser("export", help="export SQLite table on demand (offline)")
    output.add_argument("--output", required=True)
    output.add_argument("--format", choices=("csv", "parquet"), default="csv")
    output.add_argument("--table", choices=TABLES, default="daily_data")
    output.add_argument("--symbol", type=_single_symbol)
    output.add_argument("--start", type=_date)
    output.add_argument("--end", type=_date)
    return p


def main(argv=None, *, adapter=None):
    p = parser()
    args = p.parse_args(argv)
    if args.command == "retry" and args.limit <= 0:
        p.error("--limit must be positive")
    if args.command == 'refresh' and args.securities:
        if args.symbols or args.start or args.end or args.datasets is not None:
            p.error('--securities cannot be combined with dataset or historical options')
    if hasattr(args, "start") and args.start and getattr(args, "end", None) and args.start > args.end:
        p.error("--start must not exceed --end")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config = load_config(args.config)
        if args.command in ('sync','refresh') and not getattr(args,'securities',False):
            from .datasets import DATASETS
            selected=args.datasets if args.datasets is not None else config.datasets
            needs_symbols=any(kind=='bars' or DATASETS[kind].scope=='symbol' for kind in selected)
            dated=any(kind in ('bars','valuation','suspension') for kind in selected)
            if not needs_symbols and args.symbols is not None:
                p.error('global-only suspension/delisting datasets do not accept --symbols')
            if args.command=='refresh':
                if needs_symbols and not args.symbols:
                    p.error('selected refresh datasets require --symbols')
                if dated and args.start is None:
                    p.error('dated refresh requires --start')
                if not dated and (args.start is not None or args.end is not None):
                    p.error('snapshot-only refresh does not accept date bounds')
                if dated and args.start > (args.end or default_end()):
                    p.error('--start must not exceed the effective --end')
            elif not dated and args.end is not None:
                p.error('snapshot-only sync does not accept --end')
        with InstanceLock(config.database), Store(config.database) as store:
            # The lock ensures an abandoned 'running' record is not a live writer.
            recovered = store.recover_interrupted()
            if recovered:
                logging.warning("recovered %s interrupted collections; use retry", recovered)
            if args.command == "status":
                result = store.status()
            elif args.command == "export":
                result = export(store, args.output, table=args.table, format=args.format,
                                symbol=args.symbol, start=args.start, end=args.end)
            else:
                scheduler = Scheduler(config, store, adapter or AkshareAdapter(timeout=config.request_timeout))
                if args.command == "sync":
                    result = scheduler.sync(args.symbols, args.end, datasets=selected)
                elif args.command == "retry":
                    result = scheduler.retry(args.limit)
                elif args.securities:
                    result = [scheduler.refresh_securities()]
                else:
                    end=(args.end or default_end()) if dated else None
                    result = scheduler.refresh(args.symbols, args.start, end, datasets=selected)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            if isinstance(result, list) and any(r.get("status") in ("failed", "empty", "interrupted") for r in result):
                return 1
        return 0
    except KeyboardInterrupt:
        print("interrupted; run retry to resume", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3 if isinstance(exc, CircuitOpen) else 1
