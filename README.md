# A 股原始数据采集工具

模块化 Python + AKShare 采集器。**SQLite 是唯一主存储**。已实现股票名单、不复权日线 OHLC/成交量/成交额、来源涨跌幅/振幅/换手率、分红公告/登记/除息日期、上市日期、确认退市日期（目前仅深市）、停牌观察及每日盘后市值/PE/PB。统一通过 **`daily_data` 日线宽表** 查询和导出；所有表与字段见 [数据字典](docs/schema.md)。不计算因子、投资收益率或复权数据。

“原始”指 **AKShare 返回的选定字段值**，不是交易所原始报文。空值保留，不补零、不补交易日、不裁剪异常、不转换来源单位。股票名单固定 `stock_info_a_code_name()`；日线固定 `stock_zh_a_hist(..., period="daily", adjust="")`，无应用级多源融合或自动切源。

## 安装和快速开始

Python 3.11+。下面命令在项目根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test,parquet]'
astock --help
astock --config config.example.toml status
```

运行时仅需 `pip install -e .`；Parquet 为可选 `.[parquet]`。Windows 激活命令为 `.venv\Scripts\activate`；本次测试环境为 macOS/Python 3.13。

配置集中在 [config.example.toml](config.example.toml)，可复制后修改并通过 `--config` 传入。数据库路径相对 **TOML 所在目录**；无配置参数时默认 `./data/astock.sqlite3`。

```bash
# 更新股票名单
astock --config config.example.toml refresh --securities

# 一次选择全部所需字段；建议先跑少量证券
astock --config config.example.toml sync --datasets all --symbols 000001,600000

# 旧配置默认只选bars；可在TOML的sync.datasets中永久配置数据类
astock --config config.example.toml sync --symbols 000001,600000

# 完整当前名单，串行采集；通常耗时较长
astock --config config.example.toml sync

# 查看实际最新日期、检查区间、失败/空结果与采集历史
astock --config config.example.toml status

# 补偿失败、中断和未知空结果，保持原区间与请求模式
astock --config config.example.toml retry --limit 100

# 显式重抓历史：不会让正常同步进度跳过未抓区间
astock --config config.example.toml refresh --symbols 000001 --start 2024-01-02 --end 2024-01-05

# 按需导出，默认daily_data统一宽表；目标已存在时拒绝覆盖
astock --config config.example.toml export --symbol 000001 --output exports/000001.csv
astock --config config.example.toml export --format parquet --symbol 000001 --output exports/000001.parquet
# 如只需原始物理日线表
astock --config config.example.toml export --table bars --symbol 000001 --output exports/bars.csv
```

默认结束日期是上海时区昨天（自然日）。bars/valuation各自重叠 **7个自然日，含上次正常检查截止日当天**，例如检查至1月10日，下次从1月4日开始。估值即使显式传未来end也只收上海昨天及之前的来源日期，不承诺当天盘后立即发布最终估值。停牌sync仅查询end当天，历史缺口使用 `refresh --datasets suspension --start ... --end ...` 明确逐日回查；公司事件/资料每次取当前来源快照，不是历史时点还原。具体命令见 [CLI](docs/cli.md)。

混选all时，symbols仅限定每代码类；正常sync中停牌/退市按全市场/全深市固定作用域采集，不按股票重复请求；停牌历史refresh则每个指定日期请求一次。空值和unknown不等于没有事件；停牌来源包含未来安排，按明确日期证据区分suspended/announced/unknown，不直接把列表成员当作当日已停牌。升级前备份SQLite；schema v1/v2/v3打开时逐步原子迁移至v4，详见 [迁移](docs/migrations.md)。

## 数据和可靠性

- SQLite共13张物理表及daily_data只读视图，事件多条以JSON明细保留，完整表目录见数据字典。
- 相同业务值跳过；新值插入；变化先归档完整旧行（含旧采集ID）再更新。
- 每类每次请求的数据、修订、成功日志与独立进度原子提交；另一类成功不能掩盖失败或推进其边界。
- `checked_start/checked_end`（正常请求检查范围）与 `latest_data_date`（实际最大已存日期）分开。检查过不等于证明数据完整。
- 空结果记为 `empty`，不更新成功边界，不当作补齐，也不删除证券/日线。
- 有界尝试、指数退避、外层调用限频、连续请求失败熔断、子进程整体请求截止时间、数据库路径单实例锁；不修改系统代理。
- 结构错误整批阻断；可解析的负数、非有限数值、OHLC 关系异常保留并告警。
- 数值保存为十进制文本/SQL NULL，避免再次转为二进制浮点；仅规范无意义尾零等数值表示。volume 单位手、amount 单位元，OHLC 不变更上游尺度。

## 测试和验收

```bash
python -m pytest -q
python scripts/verify_commands.py
```

测试不依赖在线行情。覆盖幂等、修订、触发器事务回滚、强制进程退出恢复、失败链重试、限频、熔断、空结果、历史重抓进度、结构错误、上海跨日、单实例锁、超时/半包管道、CLI 及 CSV/Parquet。

可选在线小范围探测（不是离线测试的一部分）：

```bash
python scripts/probe_interfaces.py
```

**首次验收的真实接口探测未成功**：名单超时、日线远端关闭连接；随后用户已在当前环境成功采集 `000001` 和 `000858` 的小范围日线，说明接口存在间歇性可用情况，不能宣称持续不可用或全市场覆盖已验证。接口契约与离线正确性分别核验，不会因失败切源。原始验收记录见 [验收记录](docs/verification.md)。

## 文档

**[数据库表与日线宽表数据字典](docs/schema.md)**：全量表目录、字段类型/单位/空值语义、来源、主键与修订规则。所有列出的表及CLI入口已实现；功能可用不等于全市场/全历史数据已取得。最新测试与在线探测记录见 [扩展验收记录](docs/extension-verification.md)。

[模块接口总览](docs/interfaces.md) · [配置](docs/config.md) · [数据模型](docs/models.md) · [AKShare 适配器与接口依据](docs/adapter.md) · [校验](docs/validation.md) · [SQLite 存储](docs/storage.md) · [同步调度](docs/sync.md) · [单实例锁](docs/locking.md) · [CLI](docs/cli.md) · [导出](docs/exporting.md)

## 已知边界

名单是当前可见名单，不是历史上市/退市全集；非空名单中的缺席只是上游本次未返回，不能证明退市。单一来源无法证明历史无缺日，也不会多源对账。AKShare 自身可能已丢失精度、强制转空或内部聚合交易所，本项目无法恢复其之前的报文。增量重叠之外的修订需手动 refresh。长时间无数据的证券可能持续产生待补偿空请求，不擅自判定停牌/退市。大范围首次采集和内存式导出建议按代码拆批。遵守上游使用条款及限频要求。
