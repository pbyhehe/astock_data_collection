"""Validated TOML configuration and Shanghai-local date defaults."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math
from pathlib import Path
import re
import tomllib
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Config:
    database: Path = Path("data/astock.sqlite3")
    history_start: date = date(1990, 12, 19)
    overlap_days: int = 7
    min_interval: float = 1.0
    max_attempts: int = 3
    backoff_seconds: float = 1.0
    circuit_failures: int = 5
    request_timeout: float = 60.0
    datasets: tuple[str, ...] = ('bars',)

    def __post_init__(self) -> None:
        from .datasets import select_datasets
        if not isinstance(self.datasets, tuple):
            raise ValueError('Config.datasets must be a tuple')
        select_datasets(self.datasets)
        if not isinstance(self.database, Path):
            raise ValueError("database must be a pathlib.Path")
        if type(self.history_start) is not date:
            raise ValueError("history_start must be a date, not a datetime")
        for name in ("overlap_days", "max_attempts", "circuit_failures"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer (not bool)")
        for name in ("min_interval", "backoff_seconds", "request_timeout"):
            value = getattr(self, name)
            if type(value) not in (int, float):
                raise ValueError(f"{name} must be an int or float (not bool)")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
            if value < 0 or (name == "request_timeout" and value == 0):
                bound = "positive" if name == "request_timeout" else "nonnegative"
                raise ValueError(f"{name} must be {bound}")


_SYNC_KEYS = {"history_start", "overlap_days", "min_interval", "max_attempts", "backoff_seconds", "circuit_failures", "request_timeout", "datasets"}


def load_config(path: str | Path | None = None) -> Config:
    if path is None:
        return Config()
    if not isinstance(path, (str, Path)):
        raise ValueError("config path must be a string or pathlib.Path")
    config_path = Path(path).expanduser()
    with config_path.open("rb") as stream:
        document = tomllib.load(stream)
    unknown = set(document) - {"storage", "sync"}
    if unknown:
        raise ValueError(f"unknown configuration sections: {', '.join(sorted(unknown))}")
    for section, allowed in (("storage", {"database"}), ("sync", _SYNC_KEYS)):
        values = document.get(section, {})
        if not isinstance(values, dict):
            raise ValueError(f"[{section}] must be a TOML table")
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unknown [{section}] keys: {', '.join(sorted(unknown))}")
    storage = document.get("storage", {})
    database = Path("data/astock.sqlite3")
    if "database" in storage:
        raw_database = storage["database"]
        if not isinstance(raw_database, str) or not raw_database.strip():
            raise ValueError("storage.database must be a nonempty path string")
        database = Path(raw_database).expanduser()
    if not database.is_absolute():
        database = (config_path.absolute().parent / database).resolve()
    values = dict(document.get("sync", {}))
    if "history_start" in values:
        start = values["history_start"]
        if isinstance(start, str):
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", start) is None:
                raise ValueError("sync.history_start must use exact YYYY-MM-DD format")
            start = date.fromisoformat(start)
        if type(start) is not date:
            raise ValueError("sync.history_start must be a date, not a datetime")
        values["history_start"] = start
    if 'datasets' in values:
        from .datasets import select_datasets
        if not isinstance(values['datasets'], list):
            raise ValueError('sync.datasets must be a TOML array of dataset names')
        values['datasets'] = select_datasets(values['datasets'])
    return Config(database=database, **values)


def default_end() -> date:
    """Yesterday's calendar date in Asia/Shanghai, not a trading date."""
    return datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
