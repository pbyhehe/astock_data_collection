# 校验模块 `astock.validation`

职责：阻止结构错误进入数据库，返回数值异常告警，不做异常修复或跨源对账。

## API

- `securities(frame: DataFrame) -> list[Security]`
- `bars(frame: DataFrame, symbol: str, start: date, end: date) -> (list[Bar], list[str])`
- 失败统一 `ValidationError(ValueError)`，整批失败，无部分输出。

证券必需 `code,name`，代码严格六位 ASCII 数字字符串，名称可为空；不把整数补零、不 trim、不改前缀。日线必需 `日期,开盘,最高,最低,收盘,成交量,成交额,涨跌幅,振幅,换手率`，若返回 `股票代码` 则必须匹配请求。所有重复主键均拒绝，包括相同值重复；重复列名拒绝。

日期接受 date、无时区的午夜 datetime、严格 YYYY-MM-DD 字符串；日期必须位于请求闭区间。拒绝缺失日期、错误日期、有时间/时区日期。非 DataFrame、缺字段、不可解析数值、bool 数值阻止入库。

零行零列 DataFrame 返回空记录，交给存储标记 unknown empty；不能据此证明休市、停牌或补齐。其他有列空表仍须包含必需字段。

数值经 `Decimal(str(value))` 转为确定性文本，不使用浮点再转换或 Decimal.normalize 的上下文舍入。None/pandas NA/数值 NaN 作为 SQL NULL；文本 NaN/Infinity、OHLC 次序不一致保留并告警。价格、成交量额、振幅、换手率的负值告警，**涨跌幅正常允许负值**。涨跌幅/振幅/换手率保持来源百分数尺度，不除100、不从OHLC推算。不会把缺失补零、生成缺失日、裁剪负数、补价格或转换单位。未选定的附加字段（如涨跌额）仍忽略；收集来源指标不等于计算投资收益率。

```python
from datetime import date
from astock.validation import bars
# frame 为适配器真实返回或测试构造的 DataFrame
# rows, warnings = bars(frame, "000001", date(2024, 1, 2), date(2024, 1, 5))
```

数据模型和数据库记录保留解析后的 AKShare 值，而非交易所原始报文字节。
