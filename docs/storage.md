# SQLite 存储 `astock.storage`

职责：SQLite 为唯一主存储，管理当前值、历史变化、采集日志与同步进度；无网络依赖。`Store(path)` 创建父目录/数据库，`with Store(path) as store:` 管理连接。`conn` 是返回 sqlite3.Row 的连接；版本4，拒绝更高user_version；v1/v2/v3自动逐版本原子迁移，原数据和修订保留，新增指标不计算回填，详情见 [版本迁移](migrations.md)。使用 SQLite 默认回滚日志和完整同步，不引入额外主存储。

## 表与数据约定

- `securities`：symbol 主键、可空 name、present、首次/最近出现时间。非空完整名单中消失只标 absent，**不是宣称退市**；空名单不移除任何证券。
- `security_changes`：新增、名称/成员状态变化的 old_json/new_json、采集 ID、时间。重现不重写首次出现时间。
- `bars`：symbol/trade_date 联合主键，OHLC/volume/amount/change_pct/amplitude_pct/turnover_rate_pct 九个可空 TEXT，固定 source=stock_zh_a_hist、updated_at、collection_id。
- `bar_revisions`：旧日线的全部字段和旧 collection_id，再加 replaced_at/replaced_by。先留旧值再更新；相同九个数值则完全跳过当前记录，不改变来源时间或制造修订。
- `collections`：kind/symbol、请求 start_date/end_date、mode、running/success/empty/failed/interrupted、尝试次数、错误、开始/完成时间、计数 JSON、告警 JSON、重试 parent_id/root_id/resolved_by。
- `sync_state`：checked_start/checked_end 是正常模式连续请求检查边界；latest_data_date 是实际已存最大日期。

数字文本保持数值精度与非有限值，NULL 保留缺失。单位与 AKShare 返回一致（volume 手，amount 元），无自动缩放。业务值输入应来自 validation；存储再次检查代码、日期、重复键及字段类型。没有日历补齐、缺日删除、复权、因子或投资收益率计算。schema v3引入、当前v4沿用的五类扩展事实、统一旧值修订、独立状态及daily_data见 [全量表说明](schema.md) 和 [扩展存储API](extra_storage.md)。Store继承complete_dataset/dataset_state供调度接入，原bars状态不和估值混用。

## 状态不变量

v3→v4仅重新解释旧停牌派生status：先存完整旧行审计，再修正status/处理时间；原始JSON、进度及同一证据collection_id不变。不伪造API请求，按原请求观察时间限制可证明的已结束日期。详见 [停牌日期归属](suspension.md) 及迁移文档。

1. 首次非空 normal 请求以请求开始日锚定 checked_start，结束日成为 checked_end，而不是以最后一条数据日期代替。
2. 后续非空 normal 请求仅在 start <= checked_end+1 时可推进 checked_end；不后退，checked_start 不变。断开的区间可落库但不能跨过未检查的间隙，之后不会自动拼接已断开的区间。
3. refresh **从不初始化或推进检查边界**，包括一个证券首次出现于历史重抓的情形；正常 sync 仍从配置 history_start 开始。
4. latest_data_date 始终为所有当前已存行的 MAX(trade_date)，包含 refresh，不因旧响应后退。
5. 空结果记录为 empty 并可重试，不改数据、不推进状态、不宣称区间无交易或已经补齐。

checked 表示“接口已请求并获得过非空有效响应”，**不是完整性证明**：单源无法证明缺日、长停牌、上市前日期或上游截断。两种日期必须结合查看。

## API 和事务

```python
from astock.storage import Store
with Store("data/astock.sqlite3") as store:
    summary = store.status()
```

- `start_collection(kind,symbol=None,start=None,end=None,mode='normal',parent_id=None) -> id`：先提交 running 日志。
- `note_attempt(id)`：每次外层 AKShare 请求前单独提交计数。
- `complete_securities(id, rows)` / `complete_bars(id, rows, warnings=None)`：返回 inserted/updated/unchanged，名单另含 removed。
- `fail_collection(id,error,status='failed')`、`recover_interrupted()`：失败/中断恢复；后者仅在持有全程单实例锁后调用。
- `state(symbol)`、`symbols()`、`status()`、`pending()`：状态、当前出现代码、汇总、待补偿叶节点。
- `export_rows(table='bars',symbol=None,start=None,end=None)`：仅13张物理表及daily_data视图白名单；日期过滤支持bars/bar_revisions/valuation_daily/suspension_daily/daily_data的trade_date及delisting_events的delisting_date，其余快照/审计表拒绝日期过滤。dataset_state的symbol过滤对应scope代码。

每次 completion 用 BEGIN IMMEDIATE；数据、所有修订/变化、状态、成功日志和重试祖先解决状态 **原子提交**。任何异常（包括中断）全部回滚，之前提交的 running/attempt 记录仍在。不得在 `conn` 上开启外层事务后调用 Store 写方法。

重试必须匹配原请求的 kind/symbol/start/end/mode，只调度尚未解决的链尾；成功后设置祖先 resolved_by，保留祖先原始 failed/empty/interrupted 结果。独立新成功不擅自注销旧失败；retry 必须取回新的响应，不复用旧名单快照。

非法状态、边界、重试父节点抛 ValueError；错误字段类型 TypeError；数据库失败 sqlite3.Error；新版本或嵌套事务 RuntimeError。调用者负责记录失败；日志也写不入时保留 running 供下次恢复。测试通过 SQLite RAISE(ABORT) 触发器验证“更新、插入、修订、进度、成功状态”一起回滚。
