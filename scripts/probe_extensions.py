"""Opt-in bounded contract probe for fixed extension interfaces; never writes data."""
import argparse
from datetime import date
from importlib.metadata import version
import json

from astock.adapter import invoke
from astock.config import default_end


INTERFACES = {
    "dividends": "stock_fhps_detail_em",
    "profile": "stock_individual_info_em",
    "valuation": "stock_value_em",
    "suspension": "stock_tfp_em",
    "delisting": "stock_info_sz_delist",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="000001")
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--dataset", choices=INTERFACES, action="append")
    args = parser.parse_args()
    from astock.validation import _symbol
    _symbol(args.symbol)
    result = {"akshare": version("akshare"), "symbol": args.symbol, "probes": []}
    from astock.extra_validation import validate_dataset
    for dataset in args.dataset or ["dividends", "profile", "valuation"]:
        if dataset == 'suspension':
            params = {'date': default_end().strftime('%Y%m%d')}
        elif dataset == 'delisting':
            params = {'symbol': '终止上市公司'}
        else:
            params = {'symbol': args.symbol}
        item = {"dataset": dataset, "interface": INTERFACES[dataset], "params": params}
        try:
            frame = invoke(INTERFACES[dataset], params, timeout=args.timeout)
            item.update(status="nonempty" if len(frame) else "unknown_empty", rows=len(frame), columns=list(frame.columns),
                        sample=frame.head(2).astype(object).where(frame.head(2).notna(), None).to_dict("records"))
            if "数据日期" in frame and len(frame):
                item["available_dates"] = [str(frame["数据日期"].min()), str(frame["数据日期"].max())]
            end = default_end() if dataset in ('valuation','suspension') else None
            start = date(1990,12,19) if dataset == 'valuation' else end
            batch = validate_dataset(dataset,frame,args.symbol,start,end)
            item['validated_rows'] = len(batch.rows)
            item['validation_warnings'] = batch.warnings
            if not batch.rows:
                item['status'] = 'unknown_empty'
        except Exception as exc:
            item.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        result["probes"].append(item)
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2, allow_nan=False))
    return int(any(item["status"] != "nonempty" for item in result["probes"]))


if __name__ == "__main__":
    raise SystemExit(main())
