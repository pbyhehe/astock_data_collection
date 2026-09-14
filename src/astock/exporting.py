"""On-demand exports: SQLite stays the only authoritative store."""
import csv
import json
import os
from pathlib import Path
import tempfile

from .storage import EXPORT_TABLES as TABLES


def export(store, output, *, table="bars", format="csv", symbol=None, start=None, end=None):
    if table not in TABLES:
        raise ValueError("unsupported export table")
    if format not in ("csv", "parquet"):
        raise ValueError("unsupported export format")
    output = Path(output).expanduser().resolve()
    database = Path(store.conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    if output == database or str(output).startswith(str(database) + "-") or output == Path(str(database) + ".lock"):
        raise ValueError("export must not overwrite database or its sidecar files")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite {output}")
    rows = [dict(r) for r in store.export_rows(table, symbol=symbol, start=start, end=end)]
    columns = [r[1] for r in store.conn.execute(f'PRAGMA table_info("{table}")')]
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".astock-export-", dir=output.parent)
    os.close(fd)
    try:
        if format == "csv":
            with open(tmp, "w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=columns)
                writer.writeheader()
                for row in rows:
                    # Explicit null marker avoids conflating empty name/string with SQL NULL.
                    writer.writerow({k: "\\N" if v is None else v for k, v in row.items()})
        else:
            try:
                import pyarrow as pa
                import pyarrow.parquet as pq
            except ImportError as exc:
                raise RuntimeError("Parquet export requires: pip install 'astock-raw[parquet]'") from exc
            data = {c: [r[c] for r in rows] for c in columns}
            arrow = pa.table(data)
            meta = dict(arrow.schema.metadata or {})
            meta[b"astock"] = json.dumps({"source": "AKShare", "table": table, "adjust": "", "volume_unit": "手", "amount_unit": "元",
                                           "market_cap_unit": "元", "percent_unit": "%", "pe_ttm_basis": "TTM", "pe_static_basis": "static",
                                           "pb_mrq_basis": "MRQ", "numeric_storage": "source numeric text"}, ensure_ascii=False).encode()
            pq.write_table(arrow.replace_schema_metadata(meta), tmp)
        # Link is atomic and refuses an output created concurrently; never clobber a file.
        os.link(tmp, output)
    finally:
        os.unlink(tmp)
    return {"output": str(output), "table": table, "format": format, "rows": len(rows), "csv_null": "\\N"}
