# 日线接口连接排查

## 结论

失败已定位到 `stock_zh_a_hist` 内部访问东方财富日线 API 的 HTTP 响应阶段：TCP/TLS 可以建立，但请求 `/api/qt/stock/kline/get` 后，对端在返回 HTTP 状态行之前关闭连接，触发 `RemoteDisconnected`。

可以排除“仅因选择周末”“本项目调度/SQLite 导致”“普通 DNS 或证书校验失败”等解释。尚不能仅凭当前机器的结果判定是上游接口异常、当前网络出口限制、API 风控，还是客户端兼容性变化；没有返回 403/429 或明确业务错误可证明具体策略。

## 本机证据

| 检查 | 实测结果 |
|---|---|
| Python / OpenSSL | 3.13.2 / OpenSSL 3.4.1 |
| AKShare | 安装 1.18.94；查询 PyPI 最新也是 1.18.94 |
| requests / urllib3 | 2.34.2 / 2.7.0 |
| 进程代理环境变量 | 未设置 HTTP_PROXY、HTTPS_PROXY、ALL_PROXY、NO_PROXY（含小写形式） |
| urllib 有效代理 | 空字典 |
| macOS `scutil --proxy` | 空字典 |
| 目标路由 | 当前检查经 en0；未发现该目标走 utun 的证据。不能由此排除网关侧处理 |
| DNS | 正常解析；不同时间解析到不同地址，不据此判定 DNS 错误 |
| 日线域名 TCP 443 | 成功，约126ms |
| 日线域名 TLS | TLSv1.3，证书按 certifi 校验成功，签发链 GeoTrust/DigiCert |
| 日线域名 `HEAD /` | HTTP 404，约327ms；只是根路径不存在，不是无法连接 |
| 东方财富首页 `HEAD /` | HTTP 200，约199ms |
| PyPI 首页 `HEAD /` | HTTP 200，约428ms |

以上为本次单次观测耗时，不是性能统计，也不能证明整个上游一直可用。

数据库只读检查：collection 2 为 2026-09-05～06；collection 3、4 已改用 2026-09-03～04，均尝试3次后同样断连。交易日请求也失败，因此不能归因于周末。

## 独立复现与对照

绕过项目 CLI、Scheduler、SQLite、spawn，直接执行安装的 AKShare：

```bash
.venv/bin/python -c 'import akshare as ak; print(ak.stock_zh_a_hist(symbol="000001", period="daily", start_date="20240102", end_date="20240105", adjust="", timeout=8))'
```

诊断拦截点仅记录 `requests.Session.send` 元信息，没有改变该次请求行为：

- GET `https://push2his.eastmoney.com/api/qt/stock/kline/get`
- timeout=8，proxies 空，User-Agent=python-requests/2.34.2。
- 约443ms 后同样 `ConnectionError / RemoteDisconnected`；异常栈定位 AKShare `stock_feature/stock_hist_em.py:992` 的 requests.get，底层在读取 HTTP 状态行时 EOF。
- 没有到日线解析或本项目校验、入库步骤；不是等满超时才失败。

另做一次同接口、同参数的临时请求头兼容性对照，使用浏览器样式 User-Agent、Referer、Accept、Connection: close，约935ms 后仍相同断连。这个简单请求头变更无效；它不是完整浏览器/TLS 指纹对照，不能据此排除全部客户端兼容性问题。

所有行情请求均仍调用同一 AKShare 接口，无替代源、无自动切源、无数据入库；临时方法替换仅在诊断 Python 进程中存在并恢复；环境变量前后相同。未修改生产代码、依赖或系统代理。

## 下一步

1. 不继续高频 retry，不盲目提高 timeout；当前是快速 EOF，不是慢响应。
2. 在你有权使用的另一网络环境（例如允许的个人热点）上运行同一条单次独立复现命令。不要绕过组织的网络安全限制，也不要关闭 TLS 校验。
3. 若原网络失败、对照网络成功，进一步定位原网络出口/路径及上游对出口的限制；仍不能只据此断言具体封禁策略。
4. 若两边失败，再对照同一网络浏览器中的东方财富个股日K是否正常，以及 AKShare 上游公告/问题记录。浏览器页面能打开不等于日K接口可用，需确认实际行情加载。
5. 必须获得同一接口的成功响应后，再考虑将可验证的兼容性修正纳入实现；当前没有证据支持更换日期、加请求头或升级已为最新的 AKShare 就能解决。

## 外部参考

- [AKShare PyPI](https://pypi.org/project/akshare/)：本次版本查询依据。
- [AKShare issue #6987](https://github.com/akfamily/akshare/issues/6987)：历史上同名接口有相同断连报告，但该条没有根因/修复讨论，不能据此证明本次被封 IP 或全局宕机。
