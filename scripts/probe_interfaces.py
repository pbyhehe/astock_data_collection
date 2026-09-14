"""Small, explicit live contract probe; not part of offline tests or default sync."""
from datetime import date
from importlib.metadata import version
import inspect
import json
import os

from astock.adapter import AkshareAdapter
from astock.validation import bars, securities


def main():
    import akshare as ak
    before = dict(os.environ)
    results = {"akshare_version": version("akshare"), "signatures": {
        name: str(inspect.signature(getattr(ak, name)))
        for name in ("stock_info_a_code_name", "stock_zh_a_hist")}, "probes": []}
    client = AkshareAdapter(timeout=20)
    for kind in ("securities", "bars"):
        try:
            if kind == "securities":
                frame = client.securities()
                rows = securities(frame)
                warnings = []
            else:
                frame = client.daily("000001", date(2024, 1, 2), date(2024, 1, 5))
                rows, warnings = bars(frame, "000001", date(2024, 1, 2), date(2024, 1, 5))
            results["probes"].append({"kind": kind, "status": "nonempty" if rows else "unknown_empty",
                                      "rows": len(rows), "columns": list(frame.columns),
                                      "dtypes": {str(k): str(v) for k, v in frame.dtypes.items()},
                                      "warnings": warnings})
        except Exception as exc:
            results["probes"].append({"kind": kind, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
    results["environment_unchanged"] = before == dict(os.environ)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return int(any(r["status"] != "nonempty" for r in results["probes"]))


if __name__ == "__main__":
    raise SystemExit(main())
