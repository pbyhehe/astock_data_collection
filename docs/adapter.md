# AKShare 适配器 `astock.adapter`

职责：每类数据仅封装一个固定 AKShare 接口；返回 DataFrame。适配器本身无重试、切源、融合、交易日填补或单位转换，调度器负责外层可靠性策略；AKShare 某些接口内部可能另有重试，整体调用仍受进程截止时间控制。

```python
from datetime import date
from astock.adapter import AkshareAdapter
# Python 脚本调用须置于 if __name__ == "__main__": 中（spawn 要求）。
# client = AkshareAdapter(timeout=60)
# frame = client.daily("000001", date(2024, 1, 2), date(2024, 1, 5))
```

- `securities()` → `ak.stock_info_a_code_name()`，无参数，返回 code/name。
- `daily(symbol,start,end)` → `ak.stock_zh_a_hist(symbol=...,period="daily",start_date="YYYYMMDD",end_date="YYYYMMDD",adjust="")`。

- `supplement(kind,symbol=None,start=None,end=None)` → [固定扩展接口](datasets.md)：dividends/profile/valuation使用代码，suspension要求全市场单日查询，delisting固定深市终止上市分类。未知kind或非法作用域在联网前ValueError。接口原生不支持日期范围的情况下不传伪造参数，由扩展校验器筛选；适配器、校验、存储及CLI均已接入，详见 [命令说明](cli.md)。

每次调用新建 spawn 子进程。单调时钟截止时间覆盖启动后的导入、请求、序列化和管道传输；接收线程读完整消息，主线程有界等待，避免仅 poll 后 recv 在半包时永久阻塞。清理 kill 子进程，最多增加两次 0.2 秒 join 和 OS 调度时间；不是实时系统保证（进程启动/终止系统调用仍由 OS 管理）。不修改环境变量或系统代理；沿用调用者环境。

`AdapterTimeout` 同时继承 AdapterError 和 TimeoutError；请求、导入、工作进程崩溃、错误返回类型为 `AdapterError`，不会伪装为空表；父进程中断清理后传播。`invoke(...,module=...)` 和 `AkshareAdapter(module=...)` 是离线测试注入点，生产 CLI 不开放多源配置。

## 实施前契约核对

参考 [AKShare 官方股票文档](https://akshare.akfamily.xyz/data/stock/stock.html) 和 [文档源](https://akshare.akfamily.xyz/_sources/data/stock/stock.md.txt)，并核查安装的 AKShare 1.18.94：`stock/stock_info.py` 的 `stock_info_a_code_name`、`stock_feature/stock_hist_em.py` 的 `stock_zh_a_hist`。

| 数据 | 固定接口 | 返回字段/参数核对 |
|---|---|---|
| 名单 | stock_info_a_code_name() | code,name；无参数 |
| 日线 | stock_zh_a_hist | symbol,period,start_date,end_date,adjust,timeout=None |
| 日线字段 | 同上 | 日期、股票代码、开盘、收盘、最高、最低、成交量、成交额、涨跌幅、振幅、换手率；其他字段忽略 |
| 成交量 | 同上 | 官方单位手，不乘100 |
| 成交额 | 同上 | 官方单位元，不换算万元 |
| OHLC | 同上 | 官方输出表未明确标注价格单位；保持返回尺度，不推断或换算 |

名单接口内部由 AKShare 汇总沪深京交易所名单，并有缓存和补零；这是选定接口自身行为，应用不另行融合/切源/补零。子进程每次新建，避免复用过期进程缓存。

AKShare 日线内部已做数值/date 转换、可能把源 token 转 NaN，浮点也可能已损精度。本项目原始值的边界是 AKShare 返回值，不声称保留交易所/东方财富 HTTP 原始响应。来源历史覆盖、是否缺日、退市股票可用性无法通过结构检查证明。

真实连通性测试与离线结果见 [验收记录](verification.md)。源码/文档核对不是在线可用性保证。
