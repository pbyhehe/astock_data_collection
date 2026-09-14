# 数据库表与日线宽表数据字典

> 本文是所有表的统一入口。当前列出的13张物理表及daily_data视图均已实现，并已接入CLI。**结构/功能已实现不等于全部证券和全部历史已采集；来源缺失、失败及覆盖限制仍需保留。** 后续变更须同步本文、迁移脚本、导出白名单和测试。
>
> 最新需求：用户日常查询和导出只需一份 **`daily_data` 日线宽表**；市值/PE/PB 按日保存盘后数据，不做盘中快照。内部保留原始事实、事件和修订，是为避免信息丢失，不要求用户手工关联多张表。

## 1. 全量目录及实现状态

| 名称 | 类型 | 用途 | 当前状态 |
|---|---|---|---|
| securities | 表 | 当前名单及出现时间 | 已实现 |
| security_changes | 表 | 名称/名单成员变化记录 | 已实现 |
| bars | 表 | 不复权日线及来源日指标 | OHLCV与三个来源指标均已实现，v1→v2迁移已测试 |
| bar_revisions | 表 | 被替换的日线完整旧值 | 已实现，含三个新指标及原子迁移 |
| collections | 表 | 请求、结果、尝试次数与重试链 | 已实现，包含全部固定数据类 |
| sync_state | 表 | 正常日线检查边界、实际最大日期 | 已实现，仅日线，不共用为估值/事件进度 |
| dividend_events | 表 | 分红方案及三类日期 | 存储、校验、修订已实现，CLI已接入 |
| security_profiles | 表 | 上市日期等来源证券资料 | 存储、校验、修订已实现，CLI已接入 |
| delisting_events | 表 | 有明确终止上市日期的事件 | 已实现深市存储/校验，SH/BJ未知，CLI已接入 |
| suspension_daily | 表 | 指定日期观察到的停复牌事实 | 存储、校验、修订已实现，CLI已接入 |
| valuation_daily | 表 | 来源按日期归属的盘后估值 | 存储、校验、修订已实现，CLI已接入 |
| record_revisions | 表 | 五类扩展记录的完整旧值和快照成员变化 | 已实现 |
| dataset_state | 表 | 各类各作用域的独立进度、最近成功请求 | 已实现 |
| daily_data | 只读宽表/视图 | 用户统一查询/导出入口 | 已实现，可用export --table daily_data导出 |

当前schema版本4。扩展表、审计、独立状态、宽表及CLI集成均已离线验证。`sync --datasets all`选择全部数据类；默认export导出daily_data。旧配置默认仍只选bars；已入库范围、未知空及失败以status/collections为准，详见 [CLI](cli.md)。

## 2. 通用存储约定

- 股票代码为六位ASCII文本，前导零保留；不能把数字主键当作代码。
- 日期为 `TEXT YYYY-MM-DD`；采集/修订时间为UTC ISO8601字符串，保留时区。
- 业务数值为十进制文本 `TEXT` 或SQL `NULL`，不转SQLite REAL，不补零。不改变来源单位。
- 百分数字段保持百分数尺度：`2.35` 表示2.35%，不除100。`change_pct` 是来源涨跌幅，不是账户收益率。
- `NULL` 表示缺失/未知，不能用来证明“没有事件”“未停牌”“未退市”。
- 来源值、来源日期、实际采集时间分开保存。当前值更新时保留旧版；采集成功、数据、修订与该数据类型的进度原子提交。
- 输入结构错误整批拒绝；空结果不推断补齐、休市或全部正常。
- 宽表关联是当前已知数据的展示，不自动保证历史时点可知性。回测仍须使用采集/公告时点和版本，不能将今天获知的方案当作过去已经知道。

## 2.1 五张扩展事实表的公共字段

除各节业务字段外，dividend_events/security_profiles/delisting_events/suspension_daily/valuation_daily **全部包含**：

| 字段 | 类型 | 可空 | 含义 |
|---|---|---|---|
| raw_json | TEXT(JSON) | 否 | 选定来源事实原值；分红等为对象，停牌为同日全部事件数组 |
| is_current | INTEGER | 否 | 0/1，默认1；0只代表未在最新非空快照继续出现，不代表取消/复牌 |
| source | TEXT | 否 | 固定接口名 |
| updated_at | TEXT | 否 | 当前业务版本写入时间 |
| collection_id | INTEGER | 否 | 当前版本请求ID，外键collections.id |

分红按代码、停牌按查询日期做快照成员对比。非空新快照缺席的旧记录先完整修订留档，再标is_current=0，不物理删除；重现可恢复为1并留档。空响应不撤销任何记录。上市资料/估值/确认退市不根据未返回行做撤销。

## 3. securities：证券名单（已实现）

固定接口：`stock_info_a_code_name()`；主键 `symbol`。

| 字段 | SQLite类型 | 可空 | 含义 |
|---|---|---|---|
| symbol | TEXT | 否 | 代码 |
| name | TEXT | 是 | 来源名称 |
| present | INTEGER | 否 | 本工具当前名单成员，0/1 |
| first_seen | TEXT | 否 | 首次在来源名单观察到的时间，不是上市日期 |
| last_seen | TEXT | 否 | 最近正向观察到的时间 |

非空名单快照可将缺席成员标 present=0；空响应不移除成员。缺席不等于退市。相同成员更新观察时间，但不制造业务变化事件。

## 4. security_changes：名单变化（已实现）

主键 `id`；外键 `collection_id → collections.id`。

| 字段 | 类型 | 可空 | 含义 |
|---|---|---|---|
| id | INTEGER | 否 | 自增ID |
| symbol | TEXT | 否 | 代码 |
| change_type | TEXT | 否 | inserted/updated/removed |
| old_json | TEXT(JSON) | 是 | 完整旧名单行；首次新增为空 |
| new_json | TEXT(JSON) | 否 | 完整新名单行 |
| changed_at | TEXT | 否 | 本工具记录变化时间 |
| collection_id | INTEGER | 否 | 观察到变化的请求 |

## 5. bars：不复权日线（已实现）

主键 `(symbol,trade_date)`；固定接口 `stock_zh_a_hist(period='daily',adjust='')`。

| 字段 | 类型 | 可空 | 来源/单位 |
|---|---|---|---|
| symbol | TEXT | 否 | 请求代码；返回代码存在时必须一致 |
| trade_date | TEXT | 否 | 日期 |
| open | TEXT | 是 | 开盘 |
| high | TEXT | 是 | 最高 |
| low | TEXT | 是 | 最低 |
| close | TEXT | 是 | 收盘 |
| volume | TEXT | 是 | 成交量，手 |
| amount | TEXT | 是 | 成交额，元 |
| change_pct | TEXT | 是 | 涨跌幅，%；新增 |
| amplitude_pct | TEXT | 是 | 振幅，%；新增 |
| turnover_rate_pct | TEXT | 是 | 换手率，%；新增 |
| source | TEXT | 否 | stock_zh_a_hist |
| updated_at | TEXT | 否 | 当前业务值最后一次落库时间 |
| collection_id | INTEGER | 否 | 当前业务值来源请求，外键 |

三项新字段与OHLC同源同频，所以直接并入本表。老库迁移后三项为NULL；不从老价格推算，也不拿最新值回填。后续真实重抓才补入来源值。

## 6. bar_revisions：日线旧值（已实现）

包含 bars 的**全部同名字段**，保留旧 collection_id/source/updated_at，另加：

| 字段 | 类型 | 可空 | 含义 |
|---|---|---|---|
| id | INTEGER | 否 | 自增修订ID，主键 |
| replaced_at | TEXT | 否 | 被替换时间 |
| replaced_by | INTEGER | 否 | 新请求ID，外键 collections.id |

任何被采集业务值变化先留旧值再更新，相同值不改来源时间。新指标变化也必须触发修订。索引 `(symbol,trade_date)`。

## 7. collections：采集及补偿记录（已实现）

| 字段 | 类型 | 可空 | 含义 |
|---|---|---|---|
| id | INTEGER | 否 | 自增主键 |
| kind | TEXT | 否 | 当前为securities/bars/dividends/profile/valuation/suspension/delisting；schema v3已迁移约束 |
| symbol | TEXT | 是 | 股票请求代码；全市场名单请求为空 |
| start_date | TEXT | 是 | 请求起点，不是数据最早日期 |
| end_date | TEXT | 是 | 请求终点，不是数据最新日期 |
| mode | TEXT | 否 | normal/refresh |
| status | TEXT | 否 | running/success/empty/failed/interrupted |
| attempts | INTEGER | 否 | 外层适配器尝试次数，初始0 |
| error | TEXT | 是 | 错误诊断 |
| created_at | TEXT | 否 | 请求创建时间 |
| finished_at | TEXT | 是 | 请求完成/失败时间 |
| counts | TEXT(JSON) | 否 | inserted/updated/unchanged等，默认{} |
| warnings_json | TEXT(JSON) | 否 | 告警列表，默认[] |
| parent_id | INTEGER | 是 | 前一次失败请求ID，自引用外键 |
| root_id | INTEGER | 是 | 重试链根ID，自引用外键 |
| resolved_by | INTEGER | 是 | 解决该失败的成功请求ID，自引用外键 |

running和每次attempt单独提交；完成事务中原子更新最终结果。重试仅原区间、原类型、原模式；仅调度失败链叶节点；成功保留祖先原结果并标resolved_by。

## 8. sync_state：正常日线进度（已实现）

主键 `symbol`。

| 字段 | 类型 | 可空 | 含义 |
|---|---|---|---|
| symbol | TEXT | 否 | 代码 |
| checked_start | TEXT | 是 | 首次非空normal请求锚点 |
| checked_end | TEXT | 是 | 已连续请求且得到有效非空响应的检查截止日 |
| latest_data_date | TEXT | 是 | 当前已存日线的真实MAX日期 |

检查边界成对为空或有序。历史refresh不初始化/推进checked边界；空响应不推进。此表不能作为其他数据类型进度；估值失败不能由日线成功掩盖。断开的区间不自动跨越；检查边界不是完整性证明。

## 9. dividend_events：分红事件（存储/校验/CLI采集已实现）

固定选择 `stock_fhps_detail_em(symbol)`。不自动切新浪接口。源结果没有稳定事件ID，event_key为symbol/report_date/announcement_date的SHA256本地标识。同批相同键重复时整批拒绝，不按报告期任意覆盖；公告日期变化导致键变化时，保留旧记录并更新快照成员状态。

| 字段 | 类型 | 可空 | 来源/含义 |
|---|---|---|---|
| event_key | TEXT | 否 | 本地生成键；与symbol联合主键 |
| symbol | TEXT | 否 | 请求代码 |
| report_date | TEXT | 否 | 报告期，用于本地事件身份 |
| announcement_date | TEXT | 是 | **预案公告日**，不是实施公告日 |
| record_date | TEXT | 是 | 股权登记日 |
| ex_dividend_date | TEXT | 是 | 除权除息日 |
| latest_announcement_date | TEXT | 是 | 最新公告日期；不冒充首次公告日 |
| plan_status_raw | TEXT | 是 | 原始方案进度，不能丢掉预案/实施区别 |
| raw_json | TEXT(JSON) | 否 | 本次选定来源事件字段与原始语义 |
| source | TEXT | 否 | stock_fhps_detail_em |
| updated_at | TEXT | 否 | 当前版本写入时间 |
| collection_id | INTEGER | 否 | 来源请求 |

公告/登记/除息日各自保留；一个事件不能拆成三个无关联布尔值后丢掉关系。同日多事件必须保留；宽表用JSON明细表示。非交易日公告仍留在事件表，不捏造OHLC。来源当前历史不是历史时点快照；最新获知事件不得伪装为当时已知。

## 10. security_profiles：上市资料（存储/校验/CLI采集已实现）

固定选择 `stock_individual_info_em(symbol)`；主键 `symbol`。

| 字段 | 类型 | 可空 | 来源/含义 |
|---|---|---|---|
| symbol | TEXT | 否 | 请求代码，核对来源股票代码 |
| listing_date | TEXT | 是 | item='上市时间'的YYYYMMDD值转日期 |
| source | TEXT | 否 | stock_individual_info_em |
| updated_at | TEXT | 否 | 观察/更新时点 |
| collection_id | INTEGER | 否 | 请求ID |

YYYYMMDD整数不能直接作为纳秒时间戳解析。接口没有确认退市日期。缺资料不推断尚未上市。

## 11. delisting_events：确认退市事件（存储/校验已实现，仅深市）

目前候选固定接口为 `stock_info_sz_delist(symbol='终止上市公司')`，**仅深市**。沪市另有接口，但在“每类固定一个接口”约束下不能默默拼接；北交所也尚无统一日期覆盖证据。

| 字段 | 类型 | 可空 | 来源/含义 |
|---|---|---|---|
| symbol | TEXT | 否 | 证券代码 |
| delisting_date | TEXT | 否 | 终止上市日期，与symbol组成主键 |
| listing_date | TEXT | 是 | 该退市资料中的上市日期，保留来源归属 |
| source | TEXT | 否 | stock_info_sz_delist |
| coverage | TEXT | 否 | SZ，不声称全A股 |
| updated_at | TEXT | 否 | 观察时点 |
| collection_id | INTEGER | 否 | 请求ID |

不使用“暂停上市公司”分类作为退市证据，不根据securities.present或日线缺失生成日期。没有记录是未获取确认信息，不是保证未退市。超出覆盖范围应明确unsupported/unknown。

## 12. suspension_daily：停牌观察（存储/校验/CLI采集已实现）

固定 `stock_tfp_em(date='YYYYMMDD')`，使用来源指定日查询；主键 `(symbol,trade_date)`。

| 字段 | 类型 | 可空 | 来源/含义 |
|---|---|---|---|
| symbol | TEXT | 否 | 代码 |
| trade_date | TEXT | 否 | 请求查询日期，不是猜测的开始日期 |
| suspension_status | TEXT | 否 | 派生状态suspended/announced/unknown：仅明确日期证据可标suspended；未来安排announced，证据不足unknown；来源明细始终保留 |
| suspension_details_json | TEXT(JSON) | 否 | 同日所有来源停牌条目：停牌时间、截止时间、期限、原因、市场、预计复牌时间 |
| source | TEXT | 否 | stock_tfp_em |
| updated_at | TEXT | 否 | 当前版本处理/写入时间，含v4迁移重解释时间 |
| collection_id | INTEGER | 否 | 证据请求ID；原始观察时间从collections读取，迁移不会伪造新请求 |

AKShare将三个时间字段转换为date，盘中时间精度已丢失，不能声称全天停牌；预计复牌时间不是确认复牌。序号是临时索引，不是事件主键。同日多条汇总为明细数组而不是丢弃。没有日线不能推断停牌，没有停牌条目也不能在覆盖未知时自动置false。

## 13. valuation_daily：每日盘后估值（存储/校验/CLI采集已实现）

固定选择 **`stock_value_em(symbol)`**，有明确 `数据日期`，无需使用盘中spot值猜日期。主键 `(symbol,trade_date)`。

| 字段 | 类型 | 可空 | 来源/单位 |
|---|---|---|---|
| symbol | TEXT | 否 | 请求代码 |
| trade_date | TEXT | 否 | 数据日期 |
| total_market_cap | TEXT | 是 | 总市值，元 |
| float_market_cap | TEXT | 是 | 流通市值，元；来源为非限售A股市值，不等同指数自由流通调整市值 |
| pe_ttm | TEXT | 是 | PE(TTM)，倍 |
| pe_static | TEXT | 是 | PE(静)，倍 |
| pb_mrq | TEXT | 是 | 市净率，源字段PB_MRQ，倍 |
| source | TEXT | 否 | stock_value_em |
| updated_at | TEXT | 否 | 当前版本采集时间 |
| collection_id | INTEGER | 否 | 来源请求ID |

相较早期讨论的pe+pe_basis，来源明确同时提供TTM/静态，因此分别命名更不含糊；用户宽表可提供明确指向pe_ttm的pe别名，pb别名指向pb_mrq。

**盘后策略**：默认只接受上海昨天及以前的来源数据日期；不因用户传未来end就收当前盘中值。不将当前快照回填历史，不填充停牌日估值。来源未提供发布时间SLA，若以后允许“今天盘后”采集，需要另外定义时间门槛与更新时间核验。

**覆盖限制**：安装源码pageSize=5000、pageNumber=1，无翻页，虽然接口说明写所有历史，不能保证完整上市历史；更早日期缺失不得记成补齐。接口自身有重试，外层整体超时必须仍生效。

**单位证据**：已解析安装包 `akshare/data/interfaces.json` 中 stock_value_em 条目，明确总/流通市值单位元；源码只做日期/数值转换，不做单位缩放。原值保留，无估值自行计算。

## 14. daily_data：对外日线宽表（已实现）

只读SQLite视图，一只股票、一个有事实依据的日级日期一行。可用 `export --table daily_data` 查询导出，不需用户手工JOIN。字段如下；数值TEXT/日期TEXT遵循前述约定：

| 字段 | 类型 | 空值/含义 |
|---|---|---|
| symbol,trade_date | TEXT | 组合唯一标识 |
| has_bar | INTEGER | 1=有真实K线；0=只有其他日级事实，不是填造K线 |
| open,high,low,close,volume,amount | TEXT | 可空，bars原值；未关联到K线时全空 |
| change_pct,amplitude_pct,turnover_rate_pct | TEXT | 可空，来源百分数 |
| bars_source,bars_updated_at,bars_collection_id | TEXT,TEXT,INTEGER | 可空，K线当前版本来源 |
| total_market_cap,float_market_cap | TEXT | 可空，元 |
| pe_ttm,pe_static,pb_mrq | TEXT | 可空，来源倍数 |
| pe,pe_basis | TEXT | pe直接别名pe_ttm；basis有估值行时TTM |
| pb,pb_basis | TEXT | pb直接别名pb_mrq；basis有估值行时MRQ |
| valuation_source,valuation_updated_at,valuation_collection_id | TEXT,TEXT,INTEGER | 可空，估值独立来源 |
| suspension_status | TEXT | suspended/announced/unknown，不根据缺行生成not_suspended；日期判定见 [停牌日期归属](suspension.md) |
| suspension_details_json | TEXT(JSON) | 可空，同日全部停牌事件数组 |
| suspension_source,suspension_updated_at,suspension_collection_id | TEXT,TEXT,INTEGER | 可空，停牌独立来源 |
| listing_date | TEXT | 可空，当前来源上市日期 |
| profile_source,profile_updated_at,profile_collection_id | TEXT,TEXT,INTEGER | 可空，上市资料来源 |
| delisting_date | TEXT | 可空，当前保留的最大确认退市日期；全部事件仍可查询原表 |
| delisting_coverage | TEXT | 固定说明SZ only; SH/BJ unknown |
| delisting_collection_id | INTEGER | 可空，该退市日期版本来源请求 |
| dividend_event_count | INTEGER | 当日关联到的当前来源事件条数；0不证明不存在分红 |
| is_dividend_announcement_day | INTEGER | 存在预案公告日正向证据为1，其余NULL |
| is_record_day,is_ex_dividend_day | INTEGER | 日期匹配且方案进度为已核验的“实施分配”时1，其余NULL，不把预案当实施 |
| announcement_date,record_date,ex_dividend_date | TEXT | 当日关联事件恰好一条时提供该事件的三日期；多个事件时标量NULL，完整信息在JSON |
| dividend_events_json | TEXT(JSON) | 全部关联事件数组，含本地键、报告期、三日期、最新公告日期、状态、source/updated_at/collection_id |

行集合为bars、当前valuation_daily及已存suspension_daily日期的并集。停牌旧观察退出当前快照后，保留其历史观察日期行，但status变unknown，不声称已复牌。不生成完整交易日历、不填充价格或估值。非交易日公告完整保留在事件表；宽表不是全部公告日历。

分红先按日聚合，避免一条日线被多事件JOIN复制。只展示is_current=1的事件；标量歧义显式NULL，不任取一条。关联当前已知资料，不是历史时点快照：回测不可忽略各类观察时间和公告可知时间。

## 15. record_revisions：扩展旧值与成员变化（已实现）

五类扩展事实共用的审计表；主键id，索引(kind,symbol,row_key)。

| 字段 | 类型 | 可空 | 含义 |
|---|---|---|---|
| id | INTEGER | 否 | 自增ID |
| kind | TEXT | 否 | DATASETS中的数据类 |
| symbol | TEXT | 否 | 代码 |
| row_key | TEXT(JSON数组) | 否 | 按该类主键字段顺序记录键值 |
| old_json | TEXT(JSON对象) | 否 | 被替换的完整旧行，含业务字段、raw_json、is_current、source、时间和旧collection_id |
| replaced_at | TEXT | 否 | 替换时间 |
| replaced_by | INTEGER | 否 | 关联证据请求ID，外键collections.id；通常为新请求，v4迁移重解释沿用旧证据请求 |

数值/日期/原始选定事实变化、快照退出及重现均留完整旧值；相同业务值且is_current=1不改当前来源时间。没有物理删除事实记录。修订和本次成功/进度同事务提交。

## 16. dataset_state：各数据类独立进度（已实现）

主键(kind,scope)，不替代原日线sync_state。

| 字段 | 类型 | 可空 | 含义 |
|---|---|---|---|
| kind | TEXT | 否 | 数据类 |
| scope | TEXT | 否 | dividends/profile/valuation用代码；suspension用查询日期；delisting固定SZ |
| normal_start | TEXT | 是 | 首个非空正常估值请求锚点，其它类型为空 |
| normal_end | TEXT | 是 | 正常估值连续检查边界，最大为上海昨天；不是完整性证明 |
| latest_data_date | TEXT | 是 | 该类该作用域已存记录实际MAX日期；无date_field的资料/分红为空 |
| last_success_id | INTEGER | 否 | 最近非空成功请求ID，外键collections.id |
| updated_at | TEXT | 否 | 状态写入时间 |

normal边界成对为空或有序。估值refresh只更新实际最新日期和最近成功，不初始化/推进正常边界；断开的正常请求不跨越空档。来源最早可用日期、5000单页限制及早期覆盖未知记入collections.warnings_json，normal_end不是“早期历史已补齐”的承诺。

空结果不改本表、不清空现有事实。分红/资料每次是来源当前快照；停牌每个查询日期单独留状态，不能用某天成功声称其他日期已检查。last_success_id不等于每行collection_id：重复响应可刷新成功观察状态而不制造业务修订。

## 17. 文档维护与验收要求

每次结构变更必须同步：本目录/字段字典、实际迁移版本、导出字段及空值说明、旧值修订字段、离线迁移/回滚测试、宽表一日一行与未知值测试。文档中不得隐去未实现字段、来源覆盖不足或实时请求失败。

现有运行细节：[SQLite存储](storage.md)、[CLI](cli.md)、[模块接口](interfaces.md)。
