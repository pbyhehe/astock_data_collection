# CLI `astock.cli`

职责：参数解析、配置装配、全程单实例锁、恢复中断请求、调用调度/存储/导出并输出JSON。入口 `astock` 或 `python -m astock`；`main(argv=None,adapter=None)->int` 支持离线注入测试。

## 选择数据类

`sync` 和 `refresh` 都接受 `--datasets all` 或逗号分隔的固定名称：

| 名称 | 内容 | 请求方式 |
|---|---|---|
| bars | 不复权OHLCV、涨跌幅/振幅/换手率 | 每代码增量日区间 |
| valuation | 盘后总/流通市值、PE(TTM)/PE(静)、PB(MRQ) | 每代码取来源可用历史，独立增量筛选 |
| dividends | 分红三日期、方案状态 | 每代码当前全部可用事件快照 |
| profile | 上市日期 | 每代码当前资料快照 |
| suspension | 停牌记录与未来安排 | 全市场指定日；按日期证据区分suspended/announced/unknown |
| delisting | 有明确日期的确认退市事件 | 固定深市终止上市名单；沪/北未知 |

CLI选择覆盖TOML的sync.datasets；旧配置未提供该键时仍只采bars，避免静默扩大请求量。`all` 的顺序为bars,valuation,dividends,profile,suspension,delisting；证券名单不是业务dataset选项，另用refresh --securities。

## 命令示例

全局 `--config` 放在子命令前；不指定时不自动查找TOML。

```bash
astock --help
astock --config config.example.toml status
astock --config config.example.toml refresh --securities

# 一次选择全部新增字段。省略end默认上海昨天。
astock --config config.example.toml sync --datasets all --symbols 000001,600000
# 明确小区间截止日（公司资料/分红/退市仍为当前快照，不是历史时点还原）。
astock --config config.example.toml sync --datasets all --symbols 000001 --end 2024-01-05

# 日线原用法仍有效，默认配置只选bars。
astock --config config.example.toml sync --symbols 000001,600000 --end 2024-01-05
astock --config config.example.toml sync
astock --config config.example.toml refresh --symbols 000001 --start 2024-01-02 --end 2024-01-05

# 估值历史刷新不推进估值正常边界，更不会推进日线边界。
astock --config config.example.toml refresh --datasets valuation --symbols 000001 --start 2024-01-02 --end 2024-01-05
# 无历史区间参数的来源按快照刷新。
astock --config config.example.toml refresh --datasets profile,dividends --symbols 000001
astock --config config.example.toml refresh --datasets delisting
# 明确逐日回查停牌，不从缺K线猜标记。范围大时请求会很多。
astock --config config.example.toml refresh --datasets suspension --start 2024-01-02 --end 2024-01-03

# 无须再指定dataset，原kind/代码/区间/mode随失败请求重放。
astock --config config.example.toml retry --limit 100

# 默认导出统一宽表daily_data；可以明确选物理bars保留旧导出形式。
astock --config config.example.toml export --symbol 000001 --output exports/daily.csv
astock --config config.example.toml export --format parquet --symbol 000001 --output exports/daily.parquet
astock --config config.example.toml export --table bars --symbol 000001 --output exports/bars.csv
```

## 作用域与边界

- 需要每代码数据而未传symbols时，sync先刷新名单；名单失败/空时不使用旧名单冒充有效新名单。只选全市场数据类时不需要请求证券名单。
- 混选all时，symbols只限定每代码类；停牌/退市接口仍是全市场/全深市快照；正常sync各采集一次，停牌历史refresh每个查询日期采集一次，而不是每代码重复。只选全市场类时拒绝symbols，避免假装已按代码过滤来源。
- bars和valuation各自按正常检查边界重叠7个日历日。估值不允许当前/未来日值，end最多为上海昨天；更大的显式end会截到可接收日期并在结果记录requested_end/eligible_end。**目前采用次日及以后采集前一已结束日的保守策略，不承诺当晚即时发布最终估值。**
- suspension正常sync仅查询指定/default end当天。首次不静默发起几十年逐日请求，也不宣称已覆盖历史；漏采或更早日期使用明确refresh区间。失败/未知空请求仍可retry。source缺少可证明的空结果/交易日覆盖时，不自动标全体未停牌。
- refresh若含bars/valuation/suspension，必须start，end默认上海昨天；含每代码类必须symbols。纯快照refresh不接受日期区间；只选快照的sync也不接受显式end。混合刷新时事件/资料仍是当前快照，warnings明确不是历史as-of。
- refresh --securities不搭配datasets/symbols/start/end。
- skipped表示截止日在增量起点之前或估值范围没有已结束日期，不倒退/伪造状态。

## 状态与导出

status离线显示13表计数、当前名单数、pending_count、bars states、独立dataset_states、各类coverage及最近20次采集。日线成功不能隐藏估值失败，unknown不是0。

export默认daily_data，另支持13张物理表。字段/空值/过滤规则见 [数据字典](schema.md) 和 [导出模块](exporting.md)。不联网补采，不覆盖现有输出。

代码严格六位ASCII；多代码/类名用英文逗号；日期严格YYYY-MM-DD。所有命令（含status/export）获取锁并将遗留running标interrupted，因此离线命令仍可能创建/迁移/更新数据库；升级前先备份。

## 返回值与异常

stdout为UTF-8 JSON，结果标明kind；告警/错误在stderr。退出码0=本次成功/无待办/跳过，1=请求失败/未知空/配置或IO问题，2=参数错误，3=熔断，130=中断。status的0不等于没有待补偿。熔断前完成的数据已提交，后续通过status查看；不承诺熔断后输出完整批次JSON。

锁失败不强制解锁；Ctrl-C保存可恢复状态。Python脚本嵌入spawn适配器须有 `if __name__ == '__main__':` 保护，CLI已处理。新旧命令的离线端到端覆盖见tests/test_dataset_cli.py及scripts/verify_commands.py。
