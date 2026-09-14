# SQLite 版本迁移模块

`astock.migrations` 的职责是维护旧数据库向新结构的前向迁移，不联网、不采集或计算业务数值。

## 接口

`migrate(conn, version)`：输入sqlite3连接和已读取的user_version；成功后修改schema及user_version，无返回值。调用者必须持有同库InstanceLock，不得存在外层事务。Store构造时调用。

`SCHEMA_VERSION=4`，新库先建立v1基础结构再执行相同迁移路径，避免新/旧库字段漂移。每个版本步骤独立原子提交；某步骤失败时保留上一个完整版本，不提前推进版本号。

## v1 → v2（已实现并离线验证）

为 `bars` 和 `bar_revisions` 添加可空TEXT：

- change_pct：来源涨跌幅，百分数。
- amplitude_pct：来源振幅，百分数。
- turnover_rate_pct：来源换手率，百分数。

所有ALTER及user_version推进在一次BEGIN IMMEDIATE事务中；失败回滚。已有价格、成交值、采集ID、时间、修订和同步边界不改；新增字段留NULL，不能从历史价格推算。已接入必需字段校验、存储比较、旧值修订及导出。当前完整离线回归116项通过，覆盖v1数据/旧修订/进度保留、第二张表ALTER失败时整次迁移回滚、重复打开幂等、补入指标归档旧NULL及正常负涨跌幅不误报。此次验证仅使用临时测试库，没有主动迁移用户主数据库；下次通过CLI持锁打开时执行迁移。

异常：不支持版本或嵌套事务RuntimeError；SQLite结构/约束问题原异常抛出并回滚；BaseException也回滚。完成前做foreign_key_check。

用户升级前建议在无采集进程运行时使用SQLite备份API或sqlite3 `.backup` 保存数据库。不要复制一个正在写入的数据库文件冒充可靠备份。

## v2 → v3（已实现并离线验证）

新增五张扩展事实表、record_revisions、dataset_state及daily_data视图；扩展collections.kind固定枚举。SQLite不能直接ALTER CHECK，所以使用官方FK-off重建过程：记录原foreign_keys设置，在事务外临时关闭，BEGIN IMMEDIATE内创建新父表/复制所有行/替换名称，再恢复原索引和触发器，保留AUTOINCREMENT高水位。创建新增结构，检查视图引用和全部外键，最后推进user_version并提交；无论成功失败均恢复原foreign_keys设置。

不改采集ID或重试父子链，不改旧行情/修订引用。未知父表结构拒绝迁移，不猜测改写。任何失败回滚本步骤，包括已重建的父表、新增表及版本号。

测试覆盖：已有外键、旧重试链、用户触发器、曾删除最大ID后的自增序列保留；创建第三张新业务表被拒绝时整个v3步骤回滚且FK恢复。只使用临时测试库，不为验证主动改用户主数据库。

## v3 → v4（停牌解释修正）

实测指定日期的停牌接口也返回未来才开始的安排，v3仅凭列表成员派生suspended会误标。v4使用 [停牌日期归属](suspension.md) 重新解释已有派生status；不改表形状、不改来源数值/日期/JSON，不补确认截止日。

原请求finished_at（否则created_at）须带时区，以其上海日期的前一天及当前上海昨天中较早者作为可证明截止日；不会因为现在日期已过去，就把早先观察到的计划倒推成当时确认的停牌。每个变化先归档完整旧行至record_revisions，再更新status和处理时间。修订replaced_by沿用原证据collection_id，因此这种“同一证据的迁移重解释”不同于新API请求引起的修订；不伪造请求，不推进dataset_state，不修改collections。

整步BEGIN IMMEDIATE事务、外键检查、最后推进版本；异常回滚标记、审计和版本号。重复打开无重复修订。已有v3库也会得到修正，而不只修复新采集路径。

完整字段及实现状态见 [数据字典](schema.md)。后续新表迁移必须增加版本号及对应测试，不能重写已发布版本的语义。
