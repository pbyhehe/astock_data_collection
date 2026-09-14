# 模块接口总览

| 模块 | 责任 | 主要输入 → 输出 | 文档 |
|---|---|---|---|
| config | TOML/时区默认日期 | 路径 → Config/date | [配置](config.md) |
| models | 不可变原始字段 | 校验字段 → Security/Bar | [模型](models.md) |
| adapter | 各类固定 AKShare 调用 | 代码/日期 → DataFrame | [适配器](adapter.md) |
| datasets | 固定扩展数据契约与覆盖 | kind → Dataset | [扩展契约](datasets.md) |
| extra_validation | 事件/资料/盘后估值校验 | DataFrame → ValidatedBatch | [扩展校验](extra_validation.md) |
| migrations | 原子数据库版本升级 | conn/version → schema | [迁移](migrations.md) |
| validation | 结构阻断、异常告警 | DataFrame → records/warnings | [校验](validation.md) |
| extra_schema | 扩展表及宽表DDL | 固定契约 → SQL语句列表 | [扩展结构](extra_schema.md) |
| extra_storage | 扩展原子写入/旧值/状态 | ValidatedBatch → counts/state | [扩展存储](extra_storage.md) |
| storage | 原子入库/修订/进度 | records/request → counts/state | [存储](storage.md) |
| sync | 增量、重试、熔断 | Config+Store+Adapter → 结果列表 | [同步](sync.md) |
| locking | 单实例互斥 | 数据库 Path → context manager | [锁](locking.md) |
| exporting | SQLite 按需导出 | Store+过滤 → CSV/Parquet | [导出](exporting.md) |
| cli | 装配与命令行 | argv → JSON/exit code | [CLI](cli.md) |

边界：adapter 不持久化、不重试；validation 不修复；Store 拥有事务而不自行联网；Scheduler 不计算行情衍生值。每个模块文档说明职责、输入输出、异常和使用方式。`__init__` 仅版本，`__main__` 仅 CLI 入口，不包含采集逻辑。

全部业务数据只经 AKShare；唯一主存储 SQLite；CSV/Parquet 是用户显式请求的导出。API 测试注入 fake 不是生产数据源选择配置。
