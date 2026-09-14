# 扩展数据契约 `astock.datasets`

职责：集中定义扩展数据类的固定AKShare接口、物理表名、字段、主键、请求作用域及覆盖限制；不联网、不入库、不做接口切换。

## API

- 冻结 `Dataset(table,interface,fields,keys,scope,date_field=None,coverage=...)`。
- `DATASETS`：kind到Dataset的固定映射。
- 冻结 `ValidatedBatch(rows,warnings)`：校验器输出，rows为标准字段+选定来源raw_json，warnings为来源限制/异常描述。

| kind | 固定接口 | 作用域 | 表 |
|---|---|---|---|
| dividends | stock_fhps_detail_em | 单代码全部可用事件 | dividend_events |
| profile | stock_individual_info_em | 单代码当前资料 | security_profiles |
| valuation | stock_value_em | 单代码可用日历史，应用再按区间筛选 | valuation_daily |
| suspension | stock_tfp_em | 指定一天全市场返回的停牌条目 | suspension_daily |
| delisting | stock_info_sz_delist(终止上市公司) | 深市终止上市名单，不是暂停上市名单 | delisting_events |

不存在可配置备用源或自动降级。`delisting`覆盖仅SZ，SH/BJ无确认日期时保持未知，不通过名单缺席补日期。估值单页最多5000条，有来源日期但不保证完整历史或当天已经收盘。

调用示例：`DATASETS['valuation'].interface` 返回 `stock_value_em`；字段/主键用于后续迁移、审计和导出统一校验。完整字段和状态见 [数据字典](schema.md)。本模块没有业务异常转换，未知字典键是KeyError；外部输入通过适配器/校验器的ValueError处理。

`ALL_DATASETS`包含bars及五类扩展，供CLI all显式展开。`select_datasets(values)`验证非空list/tuple、固定名称且无重复，返回tuple；非法选择ValueError。TOML和CLI共用此契约，不动态接受任意AKShare函数名。

契约、适配器、校验器、扩展存储、宽表、自动同步/refresh/retry及CLI选项均已实现。重试按原kind调用固定接口，不受当前默认dataset选择影响，不会误走日线接口。
