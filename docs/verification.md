# 首版 0.1.0 验收记录（历史基线）

本文保留首次交付时的测试、安装和网络探测结果，不代表当前扩展版本。当前版本及schema v4的最新验收见 [扩展验收记录](extension-verification.md)。

## 环境与依赖

- macOS，Python 3.13.2；项目声明 Python >=3.11。
- 已执行 `python3 -m venv .venv` 和 `.venv/bin/python -m pip install -e '.[test,parquet]'`，安装成功。
- 实际依赖：AKShare 1.18.94、pandas 3.0.5、pytest 9.1.1、pyarrow 25.0.1。
- 测试是离线 fixture，不使用在线股票数据冒充固定预期。

## 测试命令

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/verify_commands.py
.venv/bin/python -m pip wheel . --no-deps --wheel-dir dist
```

首版基线回归：**106 passed**；`verify_commands.py`：**15 条命令通过、22 个本地文档链接通过**，临时数据库已清理，无网络请求。`pip wheel` 构建成功，`pip check` 返回 No broken requirements found；源码及脚本 compileall 通过。

首版 wheel `dist/astock_raw-0.1.0-py3-none-any.whl` 已用 `pip install --no-deps --target` 安装至临时独立目录，从临时工作目录验证 import 确实来自 wheel 安装目录，而非源码；该安装的 `--help`、`status`、空表 CSV export 三条命令通过，临时安装和数据库清理完毕。依赖复用已验证的虚拟环境，不声称全新机器无网络安装。

曾尝试 `--no-build-isolation`，因 Python 3.13 虚拟环境未自带 setuptools 而失败；恢复默认 PEP 517 构建隔离后成功。常规文档命令无需手动安装构建后端。

覆盖：幂等、全旧值修订、数据/修订/状态/成功日志回滚、强退出恢复、失败链补偿、未知空响应、普通进度与历史 refresh 隔离、限频与熔断、单实例锁、严格字段/日期/主键、数值异常保留、上海跨日、spawn 超时/崩溃/半包与中断清理、五类 CLI、CSV/Parquet 导出。

## 接口验证：契约通过，实时请求未成功

已核对 [AKShare 官方股票文档](https://akshare.akfamily.xyz/data/stock/stock.html) 与安装源码。名单 `stock_info_a_code_name()` 返回 code/name；日线 `stock_zh_a_hist` 的 symbol、period、start_date、end_date、adjust、timeout 参数已通过 `inspect.signature` 核实；成交量手、成交额元，不复权 `adjust=""`。

运行 `.venv/bin/python scripts/probe_interfaces.py`，每接口只试一次，整体截止时间20秒，日线仅 000001/2024-01-02～2024-01-05：

- 名单：`AdapterTimeout: stock_info_a_code_name: total invocation exceeded 20s`。
- 日线：`AdapterError: stock_zh_a_hist: ConnectionError: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))`。
- 探测退出码1，`environment_unchanged=true`，没有修改系统或进程代理，没有换源。

因此本次**没有成功获取真实行情样本，也没有完成全市场历史采集**；实现与离线数据一致性已经验证，实时连通性仍取决于当前网络及上游。失败不能解释为无证券/无交易。

补充：尝试 Python urllib 读取官方文档源以截取名单章节，收到 HTTP 403；官方网页工具和安装源码已提供契约证据，不绕过403或禁用证书/代理。在线故障不通过伪造空表掩盖。
