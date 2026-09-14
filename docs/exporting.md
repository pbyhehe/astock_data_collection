# 按需导出 `astock.exporting`

职责：从 SQLite 快照导出，不参与采集、不成为主存储。

`export(store, output, *, table='bars', format='csv', symbol=None, start=None, end=None) -> dict`，输出路径、表、格式、行数和CSV空值标记。库函数默认bars以兼容旧调用；CLI默认daily_data统一宽表，可显式--table bars。调用者应持InstanceLock。

```bash
astock --config config.example.toml export --table bars --output exports/bars.csv
astock --config config.example.toml export --table bars --format parquet --output exports/bars.parquet
astock --config config.example.toml export --table bar_revisions --symbol 000001 --output exports/revisions.csv
```

13张物理表和daily_data视图均允许导出，完整名单与字段见 [数据字典](schema.md)。统一日线使用 `export --table daily_data --output exports/daily.csv`，包含各类当前已存值，尚未采集的关联字段保持NULL/unknown；不因导出触发网络补采。代码过滤可用于全部表，dataset_state按scope代码过滤；日期闭区间支持bars/bar_revisions/valuation_daily/suspension_daily/daily_data的trade_date及delisting_events的delisting_date，其余快照/审计表拒绝日期过滤。日线排序 symbol/trade_date，修订再按 id 排序。CSV UTF-8 带表头，NULL 为 `\N`，数值文本原样输出、不转换成交单位；导入方必须指定 symbol 为字符串以保留前导零。CSV 是交付视图，不作为无损数据库备份（例如业务文本恰为 `\N` 会与空标记冲突），需要完整无歧义值时用 Parquet 或 SQLite 备份。Parquet 保留真实 null，数值列仍是文本而非主动转换 float，并带来源、表名、成交单位、市值单位元、百分数尺度%及PE(TTM)/PE(静)/PB(MRQ)口径metadata。

先写同目录临时文件，再原子 link 到新目标；拒绝覆盖已有文件，包括数据库、回滚日志和锁侧文件。错误时清理临时文件。无 CSV/Parquet 自动日常落盘。当前实现一次读入选定结果，超大导出建议按代码/日期拆分。

错误：非法表/格式/日期 ValueError；目标存在 FileExistsError；缺 pyarrow 为 RuntimeError（安装 `pip install 'astock-raw[parquet]'`）；数据库及文件 IO 错误传播。
