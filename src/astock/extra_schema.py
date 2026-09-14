"""Schema-v3 supplemental tables and the one-row-per-day public view."""
from .datasets import DATASETS

EXTRA_TABLES = tuple(spec.table for spec in DATASETS.values()) + ("record_revisions", "dataset_state")


def statements():
    result = []
    for kind,spec in DATASETS.items():
        required = set(spec.keys) | {'dividends':{'report_date'}, 'suspension':{'suspension_status','suspension_details_json'}, 'delisting':{'coverage'}}.get(kind,set())
        columns = [f'{name} TEXT' + (' NOT NULL' if name in required else '') for name in spec.fields]
        columns.extend([
            "raw_json TEXT NOT NULL", "is_current INTEGER NOT NULL DEFAULT 1 CHECK(is_current IN (0,1))",
            "source TEXT NOT NULL", "updated_at TEXT NOT NULL", "collection_id INTEGER NOT NULL REFERENCES collections(id)",
            f"PRIMARY KEY({','.join(spec.keys)})",
        ])
        result.append(f"CREATE TABLE {spec.table} ({','.join(columns)})")
    result.extend([
        """CREATE TABLE record_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, symbol TEXT NOT NULL,
            row_key TEXT NOT NULL, old_json TEXT NOT NULL, replaced_at TEXT NOT NULL,
            replaced_by INTEGER NOT NULL REFERENCES collections(id))""",
        "CREATE INDEX record_revisions_key_idx ON record_revisions(kind,symbol,row_key)",
        """CREATE TABLE dataset_state (
            kind TEXT NOT NULL, scope TEXT NOT NULL,
            normal_start TEXT, normal_end TEXT, latest_data_date TEXT,
            last_success_id INTEGER NOT NULL REFERENCES collections(id), updated_at TEXT NOT NULL,
            PRIMARY KEY(kind,scope),
            CHECK((normal_start IS NULL AND normal_end IS NULL) OR
                  (normal_start IS NOT NULL AND normal_end IS NOT NULL AND normal_start<=normal_end)))""",
        "CREATE INDEX dividends_dates_idx ON dividend_events(symbol,announcement_date,record_date,ex_dividend_date)",
    ])
    match = "d.symbol=k.symbol AND d.is_current=1 AND (d.announcement_date=k.trade_date OR d.record_date=k.trade_date OR d.ex_dividend_date=k.trade_date)"
    event_count = f"(SELECT COUNT(*) FROM dividend_events d WHERE {match})"
    projections = [
        "k.symbol", "k.trade_date", "CASE WHEN b.symbol IS NOT NULL THEN 1 ELSE 0 END AS has_bar",
        *[f"b.{name}" for name in ("open","high","low","close","volume","amount","change_pct","amplitude_pct","turnover_rate_pct")],
        "b.source AS bars_source", "b.updated_at AS bars_updated_at", "b.collection_id AS bars_collection_id",
        "v.total_market_cap", "v.float_market_cap", "v.pe_ttm", "v.pe_static", "v.pb_mrq",
        "v.pe_ttm AS pe", "CASE WHEN v.symbol IS NOT NULL THEN 'TTM' END AS pe_basis", "v.pb_mrq AS pb",
        "CASE WHEN v.symbol IS NOT NULL THEN 'MRQ' END AS pb_basis",
        "v.source AS valuation_source", "v.updated_at AS valuation_updated_at", "v.collection_id AS valuation_collection_id",
        "COALESCE(s.suspension_status,'unknown') AS suspension_status", "s.suspension_details_json",
        "s.source AS suspension_source", "s.updated_at AS suspension_updated_at", "s.collection_id AS suspension_collection_id",
        "p.listing_date", "p.source AS profile_source", "p.updated_at AS profile_updated_at", "p.collection_id AS profile_collection_id",
        "(SELECT MAX(t.delisting_date) FROM delisting_events t WHERE t.symbol=k.symbol AND t.is_current=1) AS delisting_date",
        "'SZ only; SH/BJ unknown' AS delisting_coverage",
        "(SELECT t.collection_id FROM delisting_events t WHERE t.symbol=k.symbol AND t.is_current=1 ORDER BY t.delisting_date DESC LIMIT 1) AS delisting_collection_id",
        f"{event_count} AS dividend_event_count",
    ]
    for date_field, flag in (("announcement_date","is_dividend_announcement_day"),("record_date","is_record_day"),("ex_dividend_date","is_ex_dividend_day")):
        implemented = "" if date_field == "announcement_date" else " AND d.plan_status_raw='实施分配'"
        projections.append(f"CASE WHEN EXISTS(SELECT 1 FROM dividend_events d WHERE d.symbol=k.symbol AND d.is_current=1 AND d.{date_field}=k.trade_date{implemented}) THEN 1 ELSE NULL END AS {flag}")
        projections.append(f"CASE WHEN {event_count}=1 THEN (SELECT MAX(d.{date_field}) FROM dividend_events d WHERE {match}) END AS {date_field}")
    projections.append(f"""(SELECT json_group_array(json_object(
        'event_key',d.event_key,'report_date',d.report_date,'announcement_date',d.announcement_date,
        'record_date',d.record_date,'ex_dividend_date',d.ex_dividend_date,
        'latest_announcement_date',d.latest_announcement_date,'plan_status_raw',d.plan_status_raw,
        'source',d.source,'updated_at',d.updated_at,'collection_id',d.collection_id))
        FROM dividend_events d WHERE {match}) AS dividend_events_json""")
    result.append("""CREATE VIEW daily_data AS
        WITH k AS (
          SELECT symbol,trade_date FROM bars
          UNION SELECT symbol,trade_date FROM valuation_daily WHERE is_current=1
          UNION SELECT symbol,trade_date FROM suspension_daily
        ) SELECT """ + ",\n".join(projections) + """
        FROM k
        LEFT JOIN bars b ON b.symbol=k.symbol AND b.trade_date=k.trade_date
        LEFT JOIN valuation_daily v ON v.symbol=k.symbol AND v.trade_date=k.trade_date AND v.is_current=1
        LEFT JOIN suspension_daily s ON s.symbol=k.symbol AND s.trade_date=k.trade_date AND s.is_current=1
        LEFT JOIN security_profiles p ON p.symbol=k.symbol AND p.is_current=1""")
    return result
