"""Execute documented CLI commands offline in a disposable database."""
from contextlib import redirect_stdout
from datetime import date
from io import StringIO
import csv
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

import pandas as pd
from astock.cli import main


class OfflineAdapter:
    """Fixture, not a configurable production data source."""
    def securities(self):
        return pd.DataFrame([{"code": "000001", "name": "离线验证"}])

    def daily(self, symbol, start, end):
        return pd.DataFrame([{"日期": end, "股票代码": symbol, "开盘": 10, "最高": 11,
                              "最低": 9, "收盘": 10, "成交量": 100, "成交额": None,
                              "涨跌幅": 0, "振幅": 2, "换手率": 0.1}])

    def supplement(self, kind, symbol, start, end):
        if kind == 'valuation':
            return pd.DataFrame([{'数据日期':end,'总市值':'1000000','流通市值':None,
                                  'PE(TTM)':'8.5','PE(静)':'9','市净率':'0.8'}])
        if kind == 'profile':
            return pd.DataFrame([{'item':'股票代码','value':symbol},{'item':'上市时间','value':19910403}])
        if kind == 'dividends':
            return pd.DataFrame([{'报告期':date(2023,12,31),'预案公告日':date(2024,1,5),
                                  '股权登记日':None,'除权除息日':None,'最新公告日期':date(2024,1,5),'方案进度':'预案'}])
        if kind == 'suspension':
            return pd.DataFrame([{'代码':'000001','名称':'离线验证','停牌时间':end,'停牌截止时间':None,
                                  '停牌期限':'盘中停牌','停牌原因':'离线样例','所属市场':'深市','预计复牌时间':None}])
        if kind == 'delisting':
            return pd.DataFrame([{'证券代码':'000009','上市日期':date(1991,1,1),'终止上市日期':date(2024,1,1)}])
        raise AssertionError(kind)


def verify():
    root = Path(__file__).resolve().parents[1]
    # Verify local markdown links, including every module's corresponding doc.
    checked_links = 0
    for doc in [root / "README.md", *sorted((root / "docs").glob("*.md"))]:
        text = doc.read_text(encoding="utf-8")
        for link in re.findall(r"\]\(([^)]+)\)", text):
            if "://" in link or link.startswith("#"):
                continue
            target = (doc.parent / link.split("#")[0]).resolve()
            assert target.exists(), f"broken link: {doc}: {link}"
            checked_links += 1
    for module in (root / "src/astock").glob("*.py"):
        if not module.name.startswith("__"):
            assert (root / "docs" / (module.stem + ".md")).exists(), module
    count = 0
    with TemporaryDirectory(prefix="astock-command-check-") as tmp:
        directory = Path(tmp)
        config = directory / "settings.toml"
        config.write_text('[storage]\ndatabase="raw.sqlite3"\n[sync]\nhistory_start="2024-01-01"\nmin_interval=0\nbackoff_seconds=0\n', encoding="utf-8")
        prefix = ["--config", str(config)]
        # Real installed entrypoint and module entrypoint, without network.
        executable = Path(sys.executable).parent / ("astock.exe" if sys.platform == "win32" else "astock")
        assert executable.exists(), "install project before running this script"
        help_commands = [[], *[[sub] for sub in ("sync", "status", "retry", "refresh", "export")]]
        for sub in help_commands:
            result = subprocess.run([str(executable), *sub, "--help"], cwd=tmp, capture_output=True, text=True, timeout=15)
            assert result.returncode == 0, result.stderr
            count += 1
        result = subprocess.run([sys.executable, "-m", "astock", *prefix, "status"], cwd=tmp, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["counts"]["bars"] == 0
        count += 1
        fixture = OfflineAdapter()
        commands = [
            ["refresh", "--securities"],
            ["sync", "--symbols", "000001,600000", "--end", "2024-01-05"],
            ["sync", "--end", "2024-01-05"],
            ["status"], ["retry", "--limit", "100"],
            ["refresh", "--symbols", "000001", "--start", "2024-01-02", "--end", "2024-01-05"],
            ["sync", "--datasets", "all", "--symbols", "000001", "--end", "2024-01-05"],
            ["refresh", "--datasets", "valuation", "--symbols", "000001", "--start", "2024-01-02", "--end", "2024-01-05"],
            ["refresh", "--datasets", "profile,dividends", "--symbols", "000001"],
            ["refresh", "--datasets", "suspension", "--start", "2024-01-02", "--end", "2024-01-03"],
            ["refresh", "--datasets", "delisting"],
            ["export", "--table", "bars", "--symbol", "000001", "--output", str(directory / "bars.csv")],
            ["export", "--table", "bars", "--format", "parquet", "--symbol", "000001", "--output", str(directory / "bars.parquet")],
            ["export", "--symbol", "000001", "--output", str(directory / "daily.csv")],
            ["export", "--format", "parquet", "--symbol", "000001", "--output", str(directory / "daily.parquet")],
        ]
        for args in commands:
            output = StringIO()
            with redirect_stdout(output):
                code = main(prefix + args, adapter=fixture)
            assert code == 0, (args, code, output.getvalue())
            result = json.loads(output.getvalue())
            if args[0] == 'export':
                expected = args[args.index('--table')+1] if '--table' in args else 'daily_data'
                assert result['table'] == expected
            count += 1
        assert "\\N" in (directory / "bars.csv").read_text()
        import pyarrow.parquet as pq
        rows = pq.read_table(directory / "bars.parquet").to_pylist()
        assert rows[0]["symbol"] == "000001" and rows[0]["amount"] is None
        with (directory / 'daily.csv').open(encoding='utf-8', newline='') as stream:
            csv_rows = list(csv.DictReader(stream))
        assert len(csv_rows) == 3 and all(row['symbol'] == '000001' for row in csv_rows)
        csv_last = next(row for row in csv_rows if row['trade_date'] == '2024-01-05')
        assert csv_last['pe'] == '8.5' and csv_last['pe_basis'] == 'TTM'
        assert csv_last['amount'] == '\\N' and csv_last['float_market_cap'] == '\\N'
        assert csv_rows[0]['has_bar'] == '0' and csv_rows[0]['close'] == '\\N'
        table = pq.read_table(directory / 'daily.parquet')
        metadata = json.loads(table.schema.metadata[b'astock'])
        assert metadata['table'] == 'daily_data'
        assert metadata['market_cap_unit'] == '元' and metadata['percent_unit'] == '%'
        assert metadata['pe_ttm_basis'] == 'TTM' and metadata['pe_static_basis'] == 'static'
        assert metadata['pb_mrq_basis'] == 'MRQ'
        daily = table.to_pylist()
        assert {r['trade_date'] for r in daily} == {'2024-01-02','2024-01-03','2024-01-05'}
        last = next(r for r in daily if r['trade_date']=='2024-01-05')
        assert last['pe']=='8.5' and last['pe_basis']=='TTM' and last['total_market_cap']=='1000000'
        assert last['is_record_day'] is None and last['float_market_cap'] is None
        assert daily[0]['has_bar']==0 and daily[0]['close'] is None
    print(json.dumps({"commands_verified": count, "local_links_verified": checked_links,
                      "network": "none; fixture injection only", "temporary_database_removed": True}, ensure_ascii=False))


if __name__ == "__main__":
    verify()
