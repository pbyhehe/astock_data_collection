# 数据模型 `astock.models`

职责：定义不可变的模块间数据，不进行采集、计算、修复或持久化。

- `Security(symbol: str, name: str | None = None)`：六位代码保留前导零；名称允许缺失。
- `Bar(symbol: str, trade_date: str, open=None, high=None, low=None, close=None, volume=None, amount=None, change_pct=None, amplitude_pct=None, turnover_rate_pct=None)`：日期 ISO YYYY-MM-DD；九个数值字段均 `str | None`。三个指标为来源涨跌幅、振幅、换手率，单位%。

```python
from astock.models import Security, Bar
security = Security("000001", None)
bar = Bar("000001", "2024-01-02", close="9.21", volume="100", amount=None)
```

Dataclass 自身不校验或转换输入，生产入口须先经过 `validation`。冻结对象赋值抛 `FrozenInstanceError`。数值文本表示避免 SQLite REAL 再次转换精度，SQL NULL 表示缺失，不等于零。解析器统一数值表示（1.2300 → 1.23），保留数值而非原字符串排版；不恢复 AKShare 已损失的精度。成交量保留手，成交额保留元；OHLC 保持上游价格尺度。没有因子、投资收益率或复权字段。估值和公司事件正在独立扩展，最新字段状态见 [数据字典](schema.md)。
