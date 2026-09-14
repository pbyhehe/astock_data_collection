# 扩展校验 `astock.extra_validation`

职责：将固定AKShare扩展接口的DataFrame转换为标准记录，不推断退市/停牌，不计算估值，不混淆事件日期。无网络、无数据库写入。

## API和输入输出

`validate_dataset(kind,frame,symbol=None,start=None,end=None,*,completed_through=None) -> ValidatedBatch(rows,warnings)`。kind来自 [数据契约](datasets.md)。分类型函数为dividends/profiles/valuations/suspensions/delistings。

必需列缺失、非DataFrame、重复列、非法代码/日期/数值、身份冲突均抛 `ValidationError(ValueError)`，不会返回半批。零行零列返回空记录，含义仍是未知，不能制造无事件或已补齐结论。

`raw_json`仅保存选定来源事实字段，用于追溯日期含义等；日期为ISO，数值标量以文本表示，来源缺失保留JSON null，禁止JSON NaN。不会顺便把来源所有估值/因子字段扩充进采集范围。

## 各类规则

### 分红

必需列：报告期、预案公告日、股权登记日、除权除息日、最新公告日期、方案进度。日期可空但报告期必须有值。

`announcement_date`只映射预案公告日，不用最新公告日期或业绩披露日期替代。保留方案原始状态；它可能是预案，不代表已经实施。`event_key`为symbol/report_date/announcement_date的SHA256本地键，不声称来源ID；状态或登记日更新可关联到同键修订。若同批多行碰到相同键，拒绝入库而不是任意覆盖；需要来源身份规则改进后再处理这种情况。公告日修订可能改变键，后续存储需保留快照成员变化，不把旧事件静默混入当前方案。

### 上市日期

输入是item/value表；必须有唯一股票代码和上市时间，代码必须与请求匹配。上市时间接受整数、整数浮点或数字字符串形式YYYYMMDD，显式解析而非纳秒时间戳。空值保留并提示不可获取；错误日期/0/非整数/bool拒绝。不会将first_seen当成上市日期。

### 盘后估值

必需数据日期、总市值、流通市值、PE(TTM)、PE(静)、市净率，映射pe_ttm/pe_static/pb_mrq。总/流通市值保留元，负值保留告警；负PE/PB不作为解析失败或必然异常。来源日期必需且不能重复。

接口返回可用全历史，按[start,end]并结合`completed_through`筛选，不更改来源日期。生产默认completed_through为上海昨天；测试可注入固定日期。过滤掉当前/未来日，不能用盘中最新值填历史。记录来源实际最早/最新日期；请求早于最早可用日期时告警“earlier coverage unknown”，不声称上市以来完整覆盖。单页5000上限仍需保留告警。筛选后无行是未知空结果，不伪造旧日期。

### 停牌

必须明确单个查询日期。必需代码、名称、停牌时间、停牌截止时间、停牌期限、停牌原因、所属市场、预计复牌时间；可空来源值原样保留。AKShare已丢失盘中时间精度，不恢复/猜测。

来源同代码同日可能多事件，聚合成有序JSON数组，**保留所有条目**。共享 [停牌日期归属](suspension.md) 分类器仅对已结束查询日的明确匹配开始日或闭区间覆盖标suspended，不声称全天停牌；无此证据但有未来开始安排时标announced，较早起点缺少确认终点、缺失/矛盾日期或过期事件仍unknown。预计复牌时间不用于确认。空响应不生成not_suspended，未出现证券仍unknown，不能仅凭列表成员判定当日停牌。

### 确认退市

仅接收固定深市终止上市分类的证券代码、上市日期、终止上市日期。终止日期必须有效，不能由抓取日期/缺日/名单缺席推算。只保留已列明深市A股代码前缀，B股等不导入；覆盖明确SZ，SH/BJ未知。重复代码+终止日期拒绝覆盖。

## 使用

```python
from astock.extra_validation import validate_dataset
# batch = validate_dataset('valuation', frame, '000001', start, end)
# 持久化器只有在整批校验成功后才能开始数据完成事务。
```

测试位于 `tests/test_extra_validation.py`，包含空值、事件关联、日期失真、同日多停牌事件、盘后截止和固定接口参数检查。校验器、扩展存储/宽表及正常同步/CLI采集均已接入。
