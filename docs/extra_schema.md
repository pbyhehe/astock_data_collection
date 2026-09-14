# 扩展DDL与日线宽表 `astock.extra_schema`

职责：声明schema v3新增结构，集中产生确定性SQL；不自行执行DDL、不联网、不触碰生产数据库。当前0.2.0使用schema v4，沿用这些结构；v4仅审计修正派生停牌解释，见 [迁移](migrations.md)。

## API

- `EXTRA_TABLES`：五个扩展事实表、record_revisions、dataset_state的白名单。
- `statements() -> list[str]`：返回逐条CREATE TABLE/INDEX/VIEW语句，由migrations在已有事务内逐条execute，**不用会隐式提交的executescript**。

五个事实表依赖 [Dataset契约](datasets.md) 的字段及主键；非主键业务字段默认可空，报告期、停牌状态/明细、退市coverage另外NOT NULL。raw_json/is_current/source/updated_at/collection_id为所有扩展表公共必需列，collection_id外键指向collections。is_current限定0/1。

record_revisions保存完整旧行JSON；dataset_state用kind/scope联合主键，normal日期成对为空或有序。完整列、类型、键及语义见 [全量数据字典](schema.md)。不另建主存储或导出副本。

## daily_data视图

行键来自真实bars、当前valuation_daily、所有已存suspension_daily日期并集。按主键关联日线/估值/停牌/证券资料，分红通过相关子查询聚合JSON，不做会扩增行数的事件直接JOIN。

- OHLC和估值缺失NULL；has_bar指示有无真实K线。
- suspension_status透传当前记录的suspended/announced/unknown，见 [日期归属](suspension.md)；缺少当前记录为unknown，旧观察退出也不声称复牌。
- 分红公告日匹配可标1；登记/除息标记只针对核验状态“实施分配”；其他未知NULL。
- 多事件保留JSON及count；三日期标量只有恰好一事件时可取，不能任取一条。
- pe=pe_ttm，pb=pb_mrq，附basis；不计算估值。
- 各类来源、时间及请求ID独立；历史时点可知性需由调用者结合版本/公告时间处理。

migrations创建视图后执行LIMIT 0验证引用有效。SQLite必须支持JSON函数；不支持时迁移失败回滚，不降级为丢事件的宽表。测试覆盖迁移拒绝中途DDL的全步回滚、同日多事件、停牌无K线、空估值和导出。
