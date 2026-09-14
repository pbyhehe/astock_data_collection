# 配置模块 `astock.config`

职责：集中 TOML 配置、参数检查，以及上海时区日期默认值；不联网、不创建数据库。

## 输入输出及使用

```python
from astock.config import load_config, default_end
config = load_config("config.example.toml")
print(config.database, default_end())
```

`load_config(path=None) -> Config`；无文件参数时直接使用默认值，不隐式搜索配置。提供文件时，数据库相对路径以 TOML 所在目录为基准；未提供文件时以当前工作目录为基准。支持 `~` 展开。`Config` 为冻结 dataclass。

| TOML 字段 | 默认值 | 含义 |
|---|---|---|
| storage.database | data/astock.sqlite3 | 唯一主数据库 |
| sync.datasets | ["bars"] | 默认采集类；CLI --datasets覆盖，名称见下文 |
| sync.history_start | 1990-12-19 | bars/valuation首次正常请求起点，不代表上市日或历史完整性 |
| sync.overlap_days | 7 | 包含上次 checked_end 的重叠自然日数 |
| sync.min_interval | 1.0 | 两次 AKShare 外层调用开始时刻最小间隔，秒 |
| sync.max_attempts | 3 | 每个请求单轮最多尝试次数，含首次 |
| sync.backoff_seconds | 1.0 | 指数退避基数，秒 |
| sync.circuit_failures | 5 | 连续请求失败熔断阈值 |
| sync.request_timeout | 60.0 | 每次独立调用整体截止时间，秒 |

`default_end()` 返回 `Asia/Shanghai` 昨天的自然日期，与机器时区无关；不查询交易日历，周末不挪到周五。

datasets必须是非空、无重复的TOML字符串数组，限定bars/valuation/dividends/profile/suspension/delisting。比如 `datasets = ["bars", "valuation", "dividends", "profile", "suspension", "delisting"]` 可永久选择全部新增数据；`all`只是CLI简写，不是TOML有效名称。加载后转不可变tuple；直接构造Config也须tuple。旧配置缺少该键仍只选bars。停牌sync单日查询，历史范围用refresh；退市仅SZ，完整作用域见 [CLI](cli.md)。

## 异常

未知 section/key、错误类型、非有限时间、非法日期、非正整数均 `ValueError`。`history_start` 接受 TOML date 或严格 YYYY-MM-DD 字符串，不接受 datetime。间隔和退避允许零，超时必须正数；bool 不是合法数字。文件缺失等 `OSError` 原样抛出；格式错误 `TOMLDecodeError` 属于 ValueError。
