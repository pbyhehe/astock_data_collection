# 扩展版本验证记录（持续更新）

本文仅记录本次扩展实际执行结果，不把尚未接入的接口探测结果说成已入库功能。

## 已完成：来源日指标与schema v2

已接入 `change_pct / amplitude_pct / turnover_rate_pct`，均来自固定 `stock_zh_a_hist`，不计算派生收益，不换算百分比尺度。

实际执行：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/verify_commands.py
.venv/bin/python -m compileall -q src scripts
```

- 完整离线回归 **116 passed**。
- 文档/CLI校验 **15 commands verified，30 local links verified**。
- Python编译检查通过。
- 新增迁移测试：旧行情/旧修订/采集ID/时间/同步边界保留；第二张表ALTER被拒绝时全部回滚；迁移重复打开幂等；新字段初始NULL；真实来源指标补入后归档旧NULL；负涨跌幅不误报异常。
- 以上使用临时数据库。没有为验证主动迁移或采集到用户主库；用户下次运行CLI持锁打开时会迁移。

## 新接口的实际有界探测

命令：

```bash
.venv/bin/python scripts/probe_extensions.py --dataset valuation --dataset dividends --dataset profile
```

每类固定一个AKShare接口，每次整体截止20秒；只探测，不写业务库。AKShare=1.18.94，股票000001。

| 接口 | 结果 | 实际观察 |
|---|---|---|
| stock_value_em | 非空成功 | 2109行，来源数据日期2018-01-02～2026-09-09；具有总/流通市值、PE(TTM)、PE(静)、市净率 |
| stock_fhps_detail_em | 非空成功 | 29行；样本方案进度“实施分配”；早期事件预案公告日或登记日可为空，不能用最新公告日期替代 |
| stock_individual_info_em | 失败 | RemoteDisconnected，不能解释为没有上市日期 |

整体探测退出码1是因为profile失败；估值/分红请求本身成功。没有切源、改代理、关闭TLS校验或向主库写入探测结果。

重要：估值返回了2026-09-09当日数据，**接口本身不保证过滤盘中值**；实施时必须按上海已结束自然日筛选，默认只收昨天及之前。样本始于2018年，而不是000001上市日，不能宣称覆盖完整上市历史。源码还固定单页最多5000行。

## 已完成：扩展适配器与校验器

`datasets.py`、`extra_validation.py` 和 `AkshareAdapter.supplement` 已实现；完整离线回归 **143 passed**。固定接口参数、来源日期过滤、百分数/金额原值、正常负PE/PB、公告日不替代、YYYYMMDD上市日期、同日多停牌事件、深市确认退市范围和空响应均有离线覆盖。此阶段不创建扩展业务表，不声称CLI已接入这些数据。

固定数据映射详见 [扩展契约](datasets.md) 与 [扩展校验](extra_validation.md)。后续存储需要处理分红快照中事件身份变化/消失的可追溯性，不能直接把旧方案当作当前事件。

## 已完成：schema v3、扩展存储与统一宽表

新增五张事实表、record_revisions、dataset_state及daily_data只读视图。完整离线回归 **161 passed**；文档命令 **15** 条、链接 **47** 个校验通过，compileall通过。

验证包含：v2→v3父表重建保留外键、重试链、索引/触发器及自增高水位（包括已清空记录表）；中途DDL失败回滚；数据/旧值/独立状态/成功日志/重试解决状态原子提交；相同值幂等；空结果不推进；分红/停牌快照成员退出和重现可追溯；停牌无K线时OHLC为空；同日多事件不会复制日线；宽表CSV导出及日期过滤。

Scheduler底层_collect和retry已识别扩展kind，使用同一限频/有界重试/熔断/中断保护，不误调用日线接口。已测一个数据类失败时，其他类成功不会推进它的状态或注销失败；中断发生在成功提交之后不会覆盖成功。

以上是当时的阶段性结果，只使用临时测试库，未主动迁移用户主库。后续正常同步和CLI选择已接入，最终记录如下。

## 最终集成：0.2.0 / schema v4

公共sync/refresh支持明确datasets选择，旧配置默认bars不变；all包含六类业务数据，CLI默认导出daily_data。估值独立历史/重叠窗口，截止最多上海昨天；停牌sync单个日期，历史回查显式refresh逐日请求；公司事件和资料为当前快照而非历史时点还原。retry保留原kind/范围/mode，不受当前选择改写。来源值及单位不计算、不补齐、不换源。

- 接通CLI后184项通过；补上存储生成警告回传和日志断言后185项通过。
- 实测停牌接口包含未来安排，加入共享日期归属分类器和v3→v4原证据审计迁移后198项通过；再加入13表及宽表全部列名的数据字典覆盖检查，最终 **199 passed**。
- 最终源码命令验证：22条命令、66个本地文档链接通过；0.2.0 editable重新安装成功，依赖检查无冲突。
- `scripts/verify_commands.py`实际执行 **22条命令**，验证默认宽表CSV/Parquet、保留前导零、NULL、无虚构K线、市值/比例口径及Parquet元数据。
- `dist/astock_raw-0.2.0-py3-none-any.whl`构建成功。`scripts/verify_wheel.py`用--no-deps --no-index安装至临时独立目录，从非项目工作目录确认import确实来自wheel，再通过全部22条命令；依赖复用现有虚拟环境，不声称新机器无需安装依赖。
- `pip check`与compileall通过。临时安装及验证库清理，不修改用户主数据库，不修改DSH。

可复现命令：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/verify_commands.py
.venv/bin/python -m pip wheel . --no-deps --wheel-dir dist
.venv/bin/python scripts/verify_wheel.py dist/astock_raw-0.2.0-py3-none-any.whl
.venv/bin/python -m pip check
.venv/bin/python -m compileall -q src scripts
```

## 最后一次五类真实探测

```bash
.venv/bin/python scripts/probe_extensions.py --dataset valuation --dataset dividends --dataset profile --dataset suspension --dataset delisting --timeout 20
```

每接口总截止20秒，固定接口调用并执行实际入库校验器，但不写业务库。

| 数据类 | 实际结果 | 已验证的限制 |
|---|---|---|
| valuation | 返回2110行，2018-01-02～2026-09-10；仅2109行通过已结束日筛选 | 自动排除了2026-09-10当日行；2018年前未知，单页最多5000不能证明全部历史 |
| dividends | 返回29行，29行通过校验 | 公告采用预案公告日；空日期保留，不以最新公告日期替代 |
| profile | RemoteDisconnected，失败 | 没有成功验证本次真实上市日期；离线解析已覆盖，失败不能当作未上市或日期不存在 |
| suspension | 查询2026-09-09返回17行并通过校验；部分开始日为2026-09-15 | 这说明列表也包含未来安排，不能全标当日已停牌；修正后单独复测17行通过 |
| delisting | 深市来源208行，过滤后187行A股通过校验 | 仅SZ，SH/BJ日期仍未知，不拼接其他接口 |

五类探测整体退出码1因profile失败，其他四类有非空响应；这不是全市场/全历史完整性证明。修正停牌分类后单独探测退出码0。完整日期归属规则见 [停牌日期归属](suspension.md)。

## 交付边界

- 所有13张物理表及统一宽表、类型、单位、空值、来源和审计规则见 [数据字典](schema.md)。
- 公告日不是统一的“实施公告日”；多分红事件保留JSON，不任取单条。
- 缺停牌记录或过去开始但没有确认截止日保持unknown；未来安排announced；suspended也不保证全天停牌。
- 保守估值策略是次日及以后收取前一已结束日，不承诺当天收盘后立即发布最终值。
- 当前关联视图不是PIT回测数据，上市/退市等当前已知资料不可假装历史上早已可知。
- 首版0.1.0 wheel只是历史产物，本次使用0.2.0；升级前备份数据库，首次持锁打开自动逐版本迁移至v4。
