# 同步调度 `astock.sync`

职责：规划增量区间、限频重试、持久化补偿、熔断。输入 Config/Store/适配器；输出每次请求状态与计数列表。依赖注入 `sleep`/`clock` 便于离线验证。

- `Scheduler(config,store,adapter)`；调用者必须持有同一数据库的 InstanceLock。
- `sync(symbols=None,end=None,*,datasets=None)`：datasets显式选择或使用config.datasets（旧配置默认bars）；all选择由CLI展开。需要每代码类且未给代码才刷新名单，名单失败/空不使用旧成员。全市场停牌/退市类不需要名单，混选时只请求一次，不按每个代码重复。bars首次history_start、后续checked_end回退overlap_days-1；valuation独立使用dataset_state.normal_end，refresh后的latest_data_date不能让正常历史跳跃。默认上海昨天，估值最大也只收昨天。资料/分红/退市每次当前快照，停牌sync仅查询end日，历史缺口须显式refresh或retry，不能声称已自动补齐全部停牌历史。
- `refresh_securities()`：更新名单。
- `refresh(symbols=None,start=None,end=None,*,datasets=None)`：每代码类须显式代码；bars/valuation/suspension须有历史闭区间，纯快照类拒绝日期区间。估值截止钳制已结束日；停牌按指定每个日历日期逐条请求，不推断交易日/无停牌。全部mode=refresh，不推进正常检查边界；组合请求中的公司快照仍是当前视角而非历史as-of。
- `retry(limit=100)`：对当前 pending 叶节点快照至多重试 limit 个，每个一次，不在同一命令里无限追逐新失败。每条失败仍有配置 max_attempts 上限。

每次尝试前记日志，使用单调时钟按外层 AKShare 调用开始时刻限频。AKShare 内部可能有多次 HTTP 请求；这个限制不是每个上游 HTTP 包的精确 QPS。请求失败按 `backoff_seconds*2**attempt` 退避，连续请求失败（含重试）到 circuit_failures 后抛 CircuitOpen，停止本次批次。成功请求重置计数；熔断在下一 CLI 调用时重置。结构校验失败不重复请求同一坏响应，而是持久记录 failed 等待显式 retry。空表也不在内部立即重试，记 empty 等待后续补偿。

原子粒度为一个数据类/作用域的完整响应（某代码日期区间、某代码事件快照或全市场指定日），不是全市场或所有数据类批次。一个类成功不因另一类失败回滚，也不能代替后者推进状态或注销失败。重试沿原kind/作用域/区间/mode走固定适配器与相应校验器，不受当前config.datasets选择改写。强杀留 running；下次 CLI 持锁启动标 interrupted，retry 以原区间重新抓，幂等合并。常规 sync 则也从已提交状态继续。

异常：CircuitOpen 传播至 CLI；KeyboardInterrupt/SystemExit 清理后传播；普通采集/校验/提交异常记 failed，返回失败结果后继续其他证券。失败日志本身写不了则向上传播，原 running 供恢复。完成提交后的极窄中断窗口不把成功改写为失败。

```python
# 应用嵌入时：with InstanceLock(config.database), Store(config.database) as store:
#     store.recover_interrupted()
#     results = Scheduler(config, store, adapter).sync(["000001"])
```
