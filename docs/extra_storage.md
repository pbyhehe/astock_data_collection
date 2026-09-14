# 扩展持久化 `astock.extra_storage`

职责：作为Store的Mixin实现五类扩展记录的原子写入、旧值修订、快照成员和独立进度；不联网。通过 `Store` 使用，不单独实例化。

## API

- `complete_dataset(collection_id, batch: ValidatedBatch) -> counts`，counts含inserted/updated/unchanged/removed。
- `dataset_state(kind,scope) -> dict | None`，返回独立状态。
- 内部 `_dataset_scope / _archive_record / _advance_dataset` 不作为外部接口。

先调用Store.start_collection取得running日志，再由固定适配器获取并整批校验。complete_dataset自身再次核对精确字段集合、字符串/NULL类型、代码、日期、主键、标准JSON、请求边界及作用域；估值额外拒绝上海当天/未来日期，退市仅SZ。停牌要求非空来源事件数组，并用共享分类器复核suspended/announced/unknown；不凭列表成员、过去的开放起点或预计复牌时间推断。详细规则见 [停牌日期归属](suspension.md)。

## 原子边界

使用Store._transaction的BEGIN IMMEDIATE。批内全部验证通过后才写事实；业务值变化或is_current恢复时先完整归档旧行，再upsert。raw_json也参与当前版本比较；完全相同不制造修订、不改变行的来源请求/时间。

分红单代码、停牌单查询日的**非空**快照中，旧成员未出现则先归档再is_current=0；不物理删除，不能推断取消分红/确认复牌。重现时留档再恢复1。空快照保留所有旧记录和旧进度，只把本次请求标为empty。

数据、record_revisions、dataset_state、成功日志、重试祖先resolved_by一并提交；SQLite异常或BaseException全部回滚，之前running/attempt日志保留供调度器标记失败或恢复。

## 进度

- 估值normal_start/normal_end连续且单调；refresh不能初始化或推进。请求end超过上海昨天时状态也不能前推到未完成日期。
- latest_data_date始终查询该类该scope已存MAX，和normal边界分开；无date_field类型留NULL。
- 分红/资料每次取当前全部可用快照；停牌按查询日期记scope；退市scope固定SZ。
- last_success_id表示最近非空成功观察，可以晚于未改变行的collection_id。
- 来源早期覆盖不足或单页上限在请求warnings中保留，检查边界不证明历史完整。

## 异常与示例

错误类型/非ValidatedBatch为TypeError；字段、键、边界、JSON、作用域错误为ValueError；SQLite约束/触发器失败原样传播；嵌套事务或非法生命周期沿用Store异常。

```python
from astock.storage import Store
# with Store(path) as store:
#     cid = store.start_collection('profile', '000001')
#     counts = store.complete_dataset(cid, validated_batch)
#     state = store.dataset_state('profile', '000001')
```

[全量表说明](schema.md)列出所有物理字段与外键。`tests/test_extra_storage.py`包含成功/幂等/旧值/空结果、最后成功日志失败时整批回滚、重试链解决、快照撤销重现和宽表不重复测试。扩展网络调度及CLI采集已经接入，详细参数见 [CLI](cli.md)。
