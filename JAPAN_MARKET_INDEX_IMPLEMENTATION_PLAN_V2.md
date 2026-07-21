# 保留 A 股能力的日本及美国四大指数增量改造实施计划 V2

> 状态：`IMPLEMENTED — 技术实现完成，功能开关默认关闭，商业数据授权 Gate 未通过`
>
> 基线文档：`JAPAN_MARKET_INDEX_IMPLEMENTATION_PLAN.md`
>
> 修订依据：Claude Code 外部审核建议 + 当前代码独立核验 + 用户补充 S&P 500 + 明确保留 A 股数据能力
>
> 目标版本：V1 产品能力（保留现有 A 股能力，增量加入日本及美国四大指数）
>
> 最后更新：2026-07-17

## 1. 本轮修订结论

外部审核总体判断可靠，指出的多 provider、缓存、时间语义、组件职责和 range 边界都值得纳入。但有三项不能原样采纳：

1. 项目已经有 loader protocol、loader registry 和 fallback chain，不应再造第二套 Provider Interface。V2 只补“按 instrument 选择 provider 的策略层”。
2. 日线 bar 不应伪装成某个市场时区的午夜 timestamp。V2 将日线主键改为 `session_date`，抓取时间单独使用 UTC `as_of`。
3. `SPY`、`QQQ`、`DIA` 是合法交易标的和历史研究文本，不能做全局迁移。只对错误代码 `^N225.HK` 做精确残留检查；当前仓库和 `~/.vibe-trading` 均未发现持久化残留。

V2 还新增一个独立判断：指数 API 不返回 `volume`。指数点位的“成交量”缺少统一业务语义，Yahoo 返回的值也不应被当成可比的交易量展示；内部 loader 仍维持 OHLCV 兼容格式。

本方案是**增量扩展，不是市场替换**。现有 A 股 symbol、数据源、fallback、研究、相关性和 backtest 能力必须原样保留；新增的日经 225、S&P 500、Nasdaq Composite 和 DJIA 只进入新的 `global_index` 分支，不能挤占、重写或删除 A 股路径。

## 2. 外部审核建议逐项判断

| 编号 | 外部建议 | 独立结论 | V2 处理 |
|---|---|---|---|
| R1 | Home 为未来独立 Market 路由预留扩展性 | **接受** | 行情组件不依赖 Home；以后可原样挂载到 `/market` |
| R2 | 删除 `^N225.HK` 前检查持久化残留 | **接受，但无需迁移** | 已对仓库和 `~/.vibe-trading` 做精确搜索，只发现源码和原计划中的当前定义；实施前再跑一次 preflight |
| R3 | 设计多 provider 混合场景 | **接受** | 每个 instrument 独立 provider policy，API 每项返回真实 `source` |
| R4 | 新建可替换 Provider Interface | **不接受原实现方向** | 复用现有 `DataLoader`、registry 和 fallback；只新增 policy resolver，避免平行抽象 |
| R5 | `nasdaq` 在所有语境优先返回歧义错误 | **接受并提前到 V1** | `nasdaq/纳指/ナスダック` 不再自动解析，返回 Composite 与 Nasdaq-100 选项 |
| R6 | 明确 TTL 和并发合并机制 | **接受** | 固定 60 秒成功缓存、15 秒部分成功缓存、5 秒失败抑制；基于 `asyncio.Task` 的 per-key single-flight |
| R7 | `detect_source()` 从 catalog 读取 provider | **部分接受** | catalog 只声明 provider symbol 能力；选择优先级由单独 policy 决定，避免把“支持”误当“首选” |
| R8 | 每个指数 timestamp 使用各自市场时区 | **接受问题，改用更明确方案** | 日线返回 `session_date`，不返回虚构午夜 timestamp；`as_of` 用 UTC；instrument 仍携带市场时区 |
| R9 | 关注更大点位和 FX 精度 | **记录，不新增专门架构** | JS/Python number 足以承载此量级；补 formatter 精度测试，不引入 decimal transport |
| R10 | 相关性明确按 trade date 对齐并返回 metadata | **接受** | API 增加 `alignment` metadata，明确 session-date inner join |
| R11 | 明确四个行情组件的数据流 | **接受** | 新增单一容器 + 纯展示组件 + fetch hook 的职责表 |
| R12 | 指数点位不能使用 `formatCurrency` | **接受澄清** | 指数 UI 强制使用 `formatMarketNumber`；`currency` 只作市场上下文 |
| R13 | 明确 1M/3M/6M/1Y 跨月边界 | **接受** | 采用市场本地日期的 calendar-month/year lookback，给出闭区间定义 |
| R14 | 日志过滤完整 URL/query/cookie | **接受原则，避免无用抽象** | 现有 HTTP helper 不记录 URL；新增代码只写结构化 endpoint kind，不新增暂时无调用者的 sanitizer |
| R15 | 估算依赖跨栈熟练度 | **接受** | 工期调整为 8–12 工程日；多人并行另加 0.5–1 天接口联调预算 |
| R16 | 明确 T4 同时依赖 T1 和 T2 | **接受** | 依赖表和 ASCII 图直接标出双依赖 |

## 3. 最终产品决策清单

下列决策是 V2 的实施前审批项：

| 决策 | V2 建议 | 说明 |
|---|---|---|
| D1 数据用途 | Yahoo 仅用于开发、内部验证和小范围 beta | 免费取数不等于商用展示许可 |
| D2 数据频率 | V1 只做日线 | 不承诺实时、分钟线或盘前盘后 |
| D3 产品入口 | 在现有 `/` 首页内容之后增量加入国际指数模块 | 保留现有内容及任何 A 股模块的原顺序；组件保持 route-agnostic，未来可抽到 `/market` |
| D4 默认语言 | 日本发行版默认回退日语 | 已保存语言 > 浏览器支持语言 > `ja` |
| D5 指数交易语义 | `.INDEX` 不可直接交易或作为策略下单标的 | 只用于行情、研究、相关性和 benchmark |
| D6 指数交易代理 | V1 不静默指定 ETF/期货 | SPY、QQQ、DIA 或日经代理必须由用户显式选择 |
| D7 Nasdaq 歧义 | 所有用户输入语境都返回歧义提示 | 只有 `Nasdaq Composite/^IXIC` 才解析为综合指数 |
| D8 Provider 策略 | 每个 instrument 独立配置有序 provider 候选 | 支持日经与美股指数来自不同供应商 |
| D9 日线时间 contract | `session_date`，不是 timestamp | 消除跨时区午夜和 DST 歧义 |
| D10 商业上线 | 必须通过数据授权 Gate | 未通过时生产 feature flag 保持关闭 |

## 4. 范围

### 4.1 V1 范围内

- 完整保留现有 A 股数据能力，包括 `.SH/.SZ/.BJ` symbol、自动数据源识别、fallback、研究、相关性和 backtest 路径。
- 日经 225、S&P 500、Nasdaq Composite、DJIA 四个指数。
- 统一 canonical symbol、别名、provider symbol 和 instrument metadata。
- 按 instrument 选择 provider，允许同一响应混合数据源。
- Yahoo 日线 OHLC 读取和现有 loader fallback 复用。
- 固定四个指数的只读 REST API。
- 在首页现有内容或 A 股模块之后新增国际指数卡、范围切换、趋势图、数据来源和延迟提示。
- 日语文案、数字、日期和市场时区格式化。
- 相关性支持四个指数，并返回对齐方式 metadata。
- benchmark 显式支持四个指数。
- 交易和 backtest 对 `.INDEX` 的明确拒绝。
- 缓存、single-flight、限流、partial failure 和结构化日志。
- 完整单元、API 集成、前端组件和可选公网 smoke test。

### 4.2 NOT in scope

- 删除、降级、改名或用国际指数路径替换现有 A 股 provider、symbol、API、研究或 backtest 能力。
- 重做 A 股行情界面或另行指定一组 A 股指数；本轮只保证已有 A 股能力和已有展示不受影响。
- 实时 tick、分钟线、盘前盘后行情和 WebSocket。
- 日本个股、指数成分、权重、基本面、公司行动。
- 完整交易所日历服务和节假日预测。
- 券商、下单、撮合、CFD、期权或期货交易。
- 自动选择 SPY、QQQ、DIA、日经 ETF 或期货作为代理。
- 用户配置任意 provider URL。
- Redis、分布式锁或跨 worker single-flight。
- 把完整 provider 接口重写成新的抽象体系。
- 新闻、情绪、宏观事件和 AI 日文摘要。
- 新增独立 `/market` 路由；只保证组件未来可迁移。
- 对历史会话中的合法 `SPY`、`QQQ`、`DIA` 文本做迁移或重写。

## 5. 当前代码事实

### 5.1 已经存在，应复用

| 已有能力 | 位置 | V2 用法 |
|---|---|---|
| A 股 symbol 与首选数据源识别 | `agent/src/market_data.py:16-39` | 保留 `.SZ/.SH/.BJ -> tencent` 现有行为，不被指数 canonicalization 覆盖 |
| A 股 provider fallback | `agent/backtest/loaders/registry.py:130-140` | 保留 `tencent -> mootdx -> eastmoney -> baostock -> akshare -> tushare -> local` 链及现有测试 |
| Loader 注册和市场 fallback | `agent/backtest/loaders/registry.py:68-108,130-233` | 继续作为 provider 执行层，不新建第二套接口 |
| Loader 统一 fetch contract | `agent/backtest/loaders/base.py:595-609` | 新 provider 继续实现现有 contract |
| 可选落盘 loader cache | `agent/backtest/loaders/base.py:220-404` | 复用已结束历史区间缓存；不承担当前区间短 TTL |
| Yahoo 公共 HTTP client | `agent/backtest/loaders/yahoo_client.py:152-229` | 增加 canonical/provider symbol 边界映射 |
| Yahoo HostThrottle 和 Session 复用 | `agent/backtest/loaders/_http.py:46-152` | 继续负责 provider 请求节流 |
| API 滑动窗口限流器 | `agent/src/api/system_routes.py:60-96` | 抽到共享 helper 或在 market route 复用同模式 |
| 相关性计算 | `agent/backtest/correlation.py:87-171` | 保留计算，替换重复 symbol/market 识别并补 metadata |
| ECharts line chart | `frontend/src/lib/echarts.ts:1-36` | 新图表直接复用，无新增前端依赖 |
| i18next 和日语资源 | `frontend/src/i18n/index.ts` | 调整默认回退和新增行情 key |

### 5.2 需要修复

| 位置 | 当前问题 | 处理 |
|---|---|---|
| `agent/src/market_data.py:21-39` | `^N225/^GSPC/^IXIC/^DJI` 默认走 Tushare | 先 canonicalize，再走 instrument provider policy |
| `agent/backtest/loaders/yahoo_loader.py:41-43` | 不接受指数 | 支持 canonical `.INDEX` 并映射 Yahoo symbol |
| `agent/backtest/loaders/yahoo_loader.py:160-162` | markets 无 `global_index` | 增加 `global_index` |
| `agent/backtest/loaders/registry.py:130-140` | 无指数 fallback | 增加 `global_index` chain，但 market API 优先使用 per-instrument policy |
| `agent/backtest/engines/_market_hooks.py:25-81` | `.INDEX` 会误落到 A 股默认 | 在 A 股现有判断之前增加精确 `global_index` 分支并由 tradability guard 阻止 engine 执行；不改 A 股默认行为 |
| `agent/backtest/correlation.py:19-84` | 独立维护 symbol 归一化 | 指数部分复用 instrument catalog；避免第二份 alias 表 |
| `agent/backtest/benchmark.py:14-106` | 直接依赖 yfinance loader | 显式 benchmark 走共享 market loader |
| `agent/src/tools/autopilot_tool.py:183-205` | `^N225.HK` 错误，S&P/Nasdaq/Dow 静默映射 SPY/QQQ/DIA | 拆分研究解析和可交易选择；删除错误映射和静默代理 |
| `frontend/src/i18n/index.ts:51-55` | 默认回退英语 | 改成 env 可配，默认 `ja` |
| `frontend/src/lib/formatters.ts:3-46` | 指标标签无日语 | locale JSON 成为标签事实来源 |

### 5.3 持久化残留核验

2026-07-17 已执行精确搜索：

```text
workspace: ^N225.HK 仅存在于 autopilot 源码和原计划文档
~/.vibe-trading: 未发现 ^N225.HK
```

结论：不需要数据迁移。实施前再运行一次只读 preflight；若发现残留，只生成报告，不自动改写历史会话。`SPY`、`QQQ` 和 `DIA` 是合法证券及研究术语，不属于错误数据。

## 6. Canonical instrument model

### 6.1 四个规范标识

| Canonical symbol | 日本语名称 | 英文名称 | 区域 | 币种 | 市场时区 | Yahoo symbol |
|---|---|---|---|---|---|---|
| `NIKKEI225.INDEX` | 日経平均株価 | Nikkei 225 | JP | JPY | `Asia/Tokyo` | `^N225` |
| `SP500.INDEX` | S&P 500種株価指数 | S&P 500 | US | USD | `America/New_York` | `^GSPC` |
| `NASDAQCOMPOSITE.INDEX` | ナスダック総合指数 | Nasdaq Composite | US | USD | `America/New_York` | `^IXIC` |
| `DJIA.INDEX` | ダウ・ジョーンズ工業株価平均 | Dow Jones Industrial Average | US | USD | `America/New_York` | `^DJI` |

`SP500.INDEX` 在 V1 中专指 **S&P 500 price index（价格指数）**。它不表示 S&P 500 Total Return、Net Total Return、Equal Weight、E-mini S&P 500 futures，也不表示 ETF `SPY`；这些标的将来必须使用独立 canonical symbol，并由用户显式选择。

`.INDEX` 仅为项目内部 asset-type 后缀，不代表交易所代码。

### 6.2 Instrument 数据结构

新增 `agent/backtest/instruments.py`：

```python
@dataclass(frozen=True)
class Instrument:
    canonical_symbol: str
    display_name: str
    display_name_ja: str
    asset_type: Literal["index"]
    market: Literal["global_index"]
    region: Literal["JP", "US"]
    currency: Literal["JPY", "USD"]
    timezone: str
    tradable: Literal[False]
    provider_symbols: Mapping[str, str]
```

纯函数：

```python
normalize_symbol(value: str) -> str
get_instrument(value: str) -> Instrument | None
provider_symbol(canonical: str, provider: str) -> str
detect_instrument_market(value: str) -> str | None
is_tradable(value: str) -> bool
```

约束：

- `normalize_symbol()` 幂等。
- 未知标的不改写，保持既有股票/期货兼容逻辑。
- provider symbol 只在 adapter 边界使用。
- catalog 只表达“provider 是否有映射”，不表达运行时优先级。
- V1 不把所有股票代码迁入 catalog，避免扩大重构面。

### 6.3 Alias 与歧义

明确 alias：

```text
^N225 / N225 / nikkei 225 / 日経平均 / 日经225
  -> NIKKEI225.INDEX

^GSPC / GSPC / s&p 500 / s&p500 / sp500 / S&P 500種株価指数 / 标普500 / 標普500
  -> SP500.INDEX

^IXIC / IXIC / nasdaq composite / ナスダック総合 / 纳斯达克综合指数
  -> NASDAQCOMPOSITE.INDEX

^DJI / DJI / DJIA / dow jones industrial average / ダウ平均
  -> DJIA.INDEX
```

歧义 alias：

```text
nasdaq / 纳指 / ナスダック
  -> AMBIGUOUS_SYMBOL
  -> options: NASDAQCOMPOSITE.INDEX, NASDAQ100.INDEX（后者仅为解释选项，V1 不取数）
```

同一规则用于研究、Agent、相关性、benchmark 和回测入口，避免语境切换造成不同结果。

## 7. 多 provider 路由

### 7.1 不新增第二套 Provider Interface

现有 loader 已经提供：

```text
name + markets + is_available() + fetch()
```

registry 已能注册、检查和 fallback。V2 在独立纯配置模块
`agent/backtest/index_provider_policy.py` 中新增 provider selection policy，避免
catalog 承担运行时优先级，也避免 `market_data.py` 反向依赖 API service：

```python
INDEX_PROVIDER_POLICY = {
    "NIKKEI225.INDEX": ("yahoo", "local"),
    "SP500.INDEX": ("yahoo", "local"),
    "NASDAQCOMPOSITE.INDEX": ("yahoo", "local"),
    "DJIA.INDEX": ("yahoo", "local"),
}

def provider_candidates(canonical_symbol: str) -> tuple[str, ...]: ...
```

V1 policy 固定在受测试的配置模块中。未来商用 provider 上线时，配置可以变为：

```text
NIKKEI225.INDEX          -> nikkei_official
SP500.INDEX              -> sp_dji_vendor
NASDAQCOMPOSITE.INDEX    -> nasdaq_gids
DJIA.INDEX               -> sp_dji_vendor
```

是否允许 fallback 也必须按 instrument 显式配置。生产环境不能因为官方源失败就自动切到未经授权的 Yahoo。

### 7.2 混合来源执行流

```text
GET /market/indices
        |
        v
固定 canonical list
        |
        +--> NIKKEI225.INDEX ------- policy --> provider A
        +--> SP500.INDEX ----------- policy --> provider B
        +--> NASDAQCOMPOSITE.INDEX - policy --> provider C
        +--> DJIA.INDEX ------------ policy --> provider B
                                      |
                                      v
                          按 provider 分组批量 fetch
                                      |
                          恢复 canonical symbol key
                                      |
                     每个 item 写入自己的 source 字段
```

`detect_source()` 不再通过 `.INDEX -> yahoo` 写死 provider。对指数，它先归一化，再调用 policy resolver；对现有股票、crypto 等继续保留旧 pattern，控制回归范围。

### 7.3 数据源与商用 Gate

V1 的 Yahoo 公共 endpoint 仅用于开发/内部验证，无官方商业 SLA。生产前仍需分别确认：

- Nikkei Index Licensing: <https://indexes.nikkei.co.jp/nkave/license/index.en.html>
- Nasdaq Global Index Data Service: <https://www.nasdaq.com/solutions/global-indexes/data/gids>
- S&P DJI Data & Index Licensing（需分别确认 S&P 500 与 DJIA 权限）: <https://www.spglobal.com/spdji/en/about-us/data-index-licensing/>
- JPX J-Quants API: <https://www.jpx.co.jp/english/markets/other-data-services/j-quants-api/index.html>
- JPX J-Quants Pro: <https://www.jpx.co.jp/english/markets/other-data-services/j-quants-pro/index.html>
- Twelve Data Indices: <https://twelvedata.com/indices>

Gate 必须确认展示、缓存、再分发、AI 使用、历史存储、attribution、延迟标识和终止后删除义务。

## 8. 时间、range 和数值语义

### 8.1 日线不用 timestamp

当前 Yahoo loader 会把日线 index 归一到 tz-naive midnight。这个值适合 DataFrame 日期对齐，但不是“真实发生在午夜的市场事件”。因此 V2 API 采用：

- `session_date`: provider 对应交易日，格式 `YYYY-MM-DD`。
- `as_of`: 服务器完成本次数据快照的 UTC 时间。
- `timezone`: 该 instrument 的市场时区，仅用于解释 session 和前端显示。
- 日线 `series` 不返回 `timestamp`。
- 未来如果增加 intraday，另行增加 UTC `timestamp` contract，不复用日线字段猜测。

这避免：

```text
tz-naive 2026-07-17 00:00
  -> 当成 UTC 转到 New York
  -> 2026-07-16 20:00
  -> session date 被错误减一天
```

### 8.2 Range 定义

请求参数固定为 `1m | 3m | 6m | 1y`，这里的 `m` 表示 calendar month，不是 minute；interval 固定 `1D`。

每个 instrument 独立按其市场时区计算：

```text
window_end   = 请求时刻在 instrument.timezone 的本地日期
1m start     = window_end - 1 calendar month
3m start     = window_end - 3 calendar months
6m start     = window_end - 6 calendar months
1y start     = window_end - 1 calendar year
provider window = [window_start, window_end]，两端包含
```

例：`window_end=2026-03-31` 时，`1m` 起点为 `2026-02-28`；闰年规则由 pandas `DateOffset` 处理。API 每个 item 返回自己的 `window_start/window_end`，测试覆盖月末、年末、闰日和 JST/ET 日期不同的时刻。

### 8.3 数值

- OHLC 和 change 使用 JSON number/Python float，足以覆盖当前指数点位。
- `change = latest.close - previous.close`。
- `change_percent = change / previous.close`；previous close 为 0 或缺失时返回 `null`。
- 指数点位用 `formatMarketNumber()`，不带 JPY/USD 货币符号。
- `currency` 表示指数计价上下文，不表示点位是可支付金额。
- 指数 API 不返回 volume。
- V1 不引入 Decimal 字符串 transport；未来外汇/高精度产品另行设计。

## 9. 缓存、并发和限流

### 9.1 两层缓存，不重复职责

| 层 | 已有/新增 | 作用 | 规则 |
|---|---|---|---|
| Loader disk cache | 已有 | 已结束历史区间的可选复用 | 继续使用 `cached_loader_fetch()`，只缓存 `end_date < today` |
| Market snapshot cache | 新增 | 当前 range 的短期 API 快照 | 进程内、按 provider policy + range 分 key |

Market snapshot cache：

- 完整成功：TTL 60 秒。
- 部分成功：TTL 15 秒，较快重试缺失项。
- 全部失败：5 秒失败抑制，只防止瞬时惊群；之后重新请求。
- 不采用“根据最后一根 bar 动态 TTL”。provider 发布时间和延迟未知，动态推断会制造虚假精确度；V1 日线下固定 TTL 更可解释。
- cache key 包含 `range`、canonical list、provider policy version 和 interval。
- 只缓存标准化后的 service result，不缓存 FastAPI Response 对象。
- feature flag/provider policy 变化时 key version 变化，自然失效。

### 9.2 Single-flight

行情 loader 是同步 I/O；FastAPI route 不应直接阻塞 event loop。实现流：

```text
request A --+
request B --+--> cache key K --> existing in-flight task? -- yes --> await shield(task)
request C --+                         |
                                      no
                                      |
                         create asyncio.Task(
                           asyncio.to_thread(sync_fetch)
                         )
                                      |
                             all waiters share result
```

约束：

- 一个模块级 `asyncio.Lock` 只保护 cache/in-flight 字典的短操作，不包住网络 I/O。
- 首个请求创建 task，后续同 key 请求复用。
- waiter 取消时使用 `asyncio.shield()`，不能取消其他用户共享的 fetch。
- task 完成后在 `finally` 中只删除同一个 task identity，避免旧 task 删除新 task。
- single-flight 仅进程内。V1 默认本地单进程，不引入 Redis。
- 不同 range/provider key 可并行。

### 9.3 限流

- `/market/indices`: 每 client IP 60 次/分钟。
- 复用 `_SlidingWindowRateLimiter` 的实现模式；若移到共享模块，必须保持 `/correlation` 行为和测试兼容。
- cache hit 仍计入 API 请求限流。
- provider 侧继续由 `HostThrottle` 控制真实出站请求节奏。

## 10. 后端 API contract

### 10.1 Endpoint

```http
GET /market/indices?range=1m
```

- 固定返回四大指数，不接受任意 symbols。
- `items` 固定排序：`NIKKEI225.INDEX`、`SP500.INDEX`、`NASDAQCOMPOSITE.INDEX`、`DJIA.INDEX`；单项失败时保留其余成功项的相对顺序，并把失败项写入 `errors`。
- 这是新增的指数专用 endpoint，不替代、合并或下线现有 A 股 market-data tool、loader 或其他 API；A 股 symbol 不通过本 endpoint 获取。
- `range` allowlist：`1m | 3m | 6m | 1y`。
- 使用现有 `require_auth` 保护方式，与 `/correlation` 保持一致；本地访问行为由现有 auth 逻辑决定。
- 非法 range 返回 `422`。
- 部分成功返回 `200`；全部失败返回 `503`。

### 10.2 Response

```json
{
  "as_of": "2026-07-17T08:00:00Z",
  "range": "1m",
  "interval": "1D",
  "items": [
    {
      "symbol": "NIKKEI225.INDEX",
      "name": "Nikkei 225",
      "name_ja": "日経平均株価",
      "market": "global_index",
      "region": "JP",
      "currency": "JPY",
      "timezone": "Asia/Tokyo",
      "source": "yahoo",
      "delay_status": "unknown",
      "tradable": false,
      "window_start": "2026-06-17",
      "window_end": "2026-07-17",
      "latest": {
        "session_date": "2026-07-17",
        "open": 0.0,
        "high": 0.0,
        "low": 0.0,
        "close": 0.0
      },
      "previous_close": 0.0,
      "change": 0.0,
      "change_percent": 0.0,
      "series": [
        {
          "session_date": "2026-07-17",
          "open": 0.0,
          "high": 0.0,
          "low": 0.0,
          "close": 0.0
        }
      ]
    }
  ],
  "errors": []
}
```

`delay_status` 枚举：`unknown | eod | delayed | realtime`。Yahoo V1 固定 `unknown`，不能根据更新时间猜测为实时。

错误项：

```json
{
  "symbol": "NIKKEI225.INDEX",
  "provider": "yahoo",
  "code": "PROVIDER_UNAVAILABLE",
  "message": "Market data is temporarily unavailable."
}
```

错误 code allowlist：

- `AMBIGUOUS_SYMBOL`
- `UNSUPPORTED_SYMBOL`
- `PROVIDER_UNAVAILABLE`
- `PROVIDER_RATE_LIMITED`
- `PROVIDER_SCHEMA_ERROR`
- `NO_DATA`
- `INVALID_RANGE`

不得把 provider 原始 body、cookie、crumb、URL query 或堆栈返回给浏览器。

## 11. 后端实施阶段

### Phase A：Instrument 与 provider policy

涉及：

- 新增 `agent/backtest/instruments.py`
- 新增 `agent/backtest/index_provider_policy.py`
- 修改 `agent/src/market_data.py`
- 修改 `agent/backtest/loaders/yahoo_client.py`
- 修改 `agent/backtest/loaders/yahoo_loader.py`
- 修改 `agent/backtest/loaders/registry.py`
- 修改 `agent/backtest/engines/_market_hooks.py`

工作：

1. 建立四个 instrument、明确 alias 和歧义 alias。
2. provider symbol 映射由 catalog 提供。
3. provider policy 独立于 catalog capability。
4. Yahoo loader 输入/输出保持 canonical key，调用 client 前才转换。
5. `global_index` market 和 fallback chain 加入 registry。
6. `.INDEX` 可识别，但由 tradability guard 阻止进入交易 engine。
7. Yahoo loader 使用已有 `cached_loader_fetch()`，不重复实现历史 disk cache。
8. A 股 `.SH/.SZ/.BJ` 继续走原有识别和 fallback；为 A 股与 `global_index` 的分流补回归测试。

### Phase B：Market index service 与 API

涉及：

- 新增 `agent/src/market_index_service.py`
- 新增 `agent/src/api/market_routes.py`
- 修改 `agent/api_server.py`
- 新增 `agent/tests/test_market_index_service.py`
- 新增 `agent/tests/test_market_routes.py`

Service 职责：

- range 计算。
- provider policy resolution 和按 provider 分组。
- 同步 loader fetch。
- canonical key 恢复。
- partial error 分类。
- `session_date` contract 映射。
- change/percent 计算。
- snapshot cache 和 async single-flight。

Route 只负责 query validation、auth、rate limit、HTTP status 和 response model，不直接实现 provider 逻辑。

### Phase C：Correlation、benchmark 和 autopilot

涉及：

- 修改 `agent/backtest/correlation.py`
- 修改 `agent/backtest/benchmark.py`
- 修改 `agent/src/agent/context.py`
- 修改 `agent/src/tools/autopilot_tool.py`

规则：

- correlation 接受 canonical 和明确 alias。
- `nasdaq` 等歧义 alias 直接报错并给出候选。
- 对齐字段是 `session_date`，按 inner join。
- response 增加：

  ```json
  {
    "alignment": {
      "basis": "session_date",
      "join": "inner",
      "interpretation": "Same exchange session label; not simultaneous close time and not a lead-lag model."
    }
  }
  ```

- benchmark 显式 index 走共享 loader，不再硬编码 yfinance。
- autopilot 删除 `^N225.HK`，并停止把 S&P 500、Nasdaq、Dow 静默替换为 SPY、QQQ、DIA。
- 研究型固定 UI 可直接使用 canonical index；用户自然语言中的歧义词返回选择提示。
- `.INDEX` 进入策略/交易路径时返回 `INDEX_NOT_TRADABLE`。
- A 股证券仍按现有 symbol 和 tradability 规则进入研究/backtest；不得因新增 `.INDEX` guard 被误判为不可交易指数。

## 12. 前端职责与数据流

### 12.1 首页布局

当前代码中的 Home 只有通用研究 CTA、功能说明和使用步骤，没有独立 A 股行情卡区。本轮不虚构或重做 A 股 UI；布局采用“保留在前、国际指数追加在后”的原则：

1. 原有标题、研究 CTA、功能说明和使用步骤保持原顺序与功能。
2. 如果实施时的目标分支已经存在 A 股行情模块，原样保留在国际指数模块之前，不删除、不合并。
3. 在上述现有内容之后新增“日本用户视角的全球市场概览”。
4. 概览内部依次展示日经 225、S&P 500、Nasdaq Composite、DJIA 四张卡。
5. 四张国际指数卡内部默认选中日经 225。
6. 展示所选指数的 close line chart 和 1M / 3M / 6M / 1Y range。
7. 展示数据源、session date、市场时区和延迟提示。

这里的“放在后面”指页面区块顺序；指数 API 的固定 item 顺序以及四张卡内部顺序仍保持日经、S&P 500、Nasdaq Composite、DJIA。

### 12.2 组件职责

| 组件/Hook | 职责 | 明确不做 |
|---|---|---|
| `Home.tsx` | 保留现有页面结构，把 `IndexOverview` 追加在现有内容或 A 股模块之后 | 不 fetch、不保存指数选择状态，不删除或重排 A 股模块 |
| `useMarketIndices.ts` | API fetch、AbortController、range 变化、stale response 防护 | 不渲染、不管理选中卡 |
| `IndexOverview.tsx` | 唯一状态容器：selected symbol、range、loading/error | 不直接拼 fetch URL，不创建 ECharts 实例 |
| `IndexCard.tsx` | 纯 button，显示名称、close、change、selected/disabled | 不 fetch、不展开详情、不维护本地选中状态 |
| `IndexChart.tsx` | 纯图表，props 输入 series/range/locale | 不 fetch、不计算 provider policy |
| `MarketDataNotice.tsx` | 纯展示 source、as_of、delay 和 disclaimer | 不推断实时状态 |

数据流：

```text
Home
  ├── existing research/A-share content [unchanged]
  └── IndexOverview [selectedSymbol, range]
        ├── useMarketIndices(range) --> api.getMarketIndices()
        ├── IndexCard x4 --onSelect--> setSelectedSymbol
        ├── IndexChart(selectedItem.series)
        └── MarketDataNotice(response metadata)
```

这些组件只依赖 props/API type。未来新增 `/market` 时可复用 `IndexOverview`，Home 不会因指数增加而继续膨胀；A 股现有组件或数据流不依赖、也不导入新的指数 service。

### 12.3 格式化与 i18n

- `VITE_DEFAULT_LANGUAGE` 默认 `ja`。
- 语言优先级：localStorage 显式选择 > 支持的 browser locale > `ja`。
- 新行情和指标 key 写入所有 locale JSON，日语为主审版本。
- 指数点位：`formatMarketNumber(value, locale)`。
- ETF/股票货币价格：未来使用 `formatCurrency(value, currency, locale)`。
- 更新时间：`formatMarketDateTime(asOfUtc, locale, displayTimeZone)`。
- session：直接格式化 `session_date`，不得先解析为本地 midnight 再跨时区转换。
- 正负值同时使用符号、文本和 aria-label，颜色不是唯一表达。

## 13. 日志与安全

- API 只接受固定 range，不接受 provider URL 或任意 symbol。
- provider 名称来自 allowlist policy。
- 使用现有 auth/CORS/host 防护。
- structured log 字段限于：canonical symbol、provider name、endpoint kind、duration、cache result、error code。
- 不记录 `requests.PreparedRequest.url`、params、headers、cookie、crumb 或原始 response body。
- 当前 `_http.py` 没有记录 URL，因此不新增无调用者的 URL sanitizer；若未来引入 HTTP debug logging，必须同时实现 redaction 并通过安全测试。
- SSRF 约束未来若要开放自定义 provider endpoint，必须作为独立安全设计重新审核。
- 单次固定四个指数，每个指数最多 260 根日线。
- schema parse error 不能显示为 0；返回明确错误状态。

## 14. 测试计划

### 14.1 代码路径和用户路径图

```text
CODE PATHS                                      USER FLOWS

normalize input                                Home opens
  +-- A-share symbol --> existing path --------> existing research/A-share content unchanged
  +-- index canonical --------------------------> global-index loading skeleton later on page
  +-- explicit index alias ---------------------> four cards + Nikkei selected
  +-- ambiguous alias --> AMBIGUOUS_SYMBOL      partial item failure
  +-- unknown --> preserve/unsupported          total failure + retry

resolve provider per instrument                switch card
  +-- preferred available ---------------------> chart changes, no refetch
  +-- preferred missing --> next allowed        switch range
  +-- all fail --> item error -----------------> abort old request
                                                 ignore stale response

snapshot cache
  +-- valid full hit --> return 200             rapid same-range clicks
  +-- valid partial hit --> return 200          share in-flight request
  +-- miss --> create single-flight             navigate away
  +-- waiter cancelled --> shield shared task   remaining waiter succeeds
  +-- all fail --> 5s suppression               retry after suppression

map daily bars
  +-- >=2 bars --> change calculation           display session date
  +-- 1 bar --> previous/change null            display no false currency
  +-- malformed schema --> provider error       clear recoverable message
```

### 14.2 后端测试文件

- 新增 `agent/tests/test_instruments.py`
- 修改 `agent/tests/test_market_data.py`
- 修改 `agent/tests/test_yahoo_loader.py`
- 修改 `agent/tests/test_registry.py`
- 修改 `agent/tests/test_market_detection.py`
- 新增 `agent/tests/test_market_index_service.py`
- 新增 `agent/tests/test_market_routes.py`
- 修改 `agent/tests/test_correlation.py`
- 新增 `agent/tests/test_benchmark.py`
- 修改 `agent/tests/test_autopilot_tool.py`

必测：

- 四个 canonical、明确 alias、大小写、空白、幂等。
- `.SH/.SZ/.BJ` A 股 symbol 继续匹配现有 source；不被 canonicalize 为 `global_index`，现有 fallback 顺序不变。
- `^GSPC/GSPC/S&P 500/sp500/标普500` 全部归一化为 `SP500.INDEX`。
- `nasdaq/纳指/ナスダック` 全部产生同一歧义结果。
- 每 instrument 不同 provider，按 provider 分组 fetch，item source 正确。
- provider A 失败、B 允许/禁止 fallback 两种情况。
- Yahoo provider symbol 不泄漏到业务 response。
- fixed TTL：59 秒 hit、60 秒后 miss，使用 monotonic fake clock。
- full/partial/failure 三种 TTL。
- 同 key 10 个并发请求只调用一次 loader。
- 一个 waiter cancel 不取消共享 fetch。
- 不同 range 可并行。
- 月末、年末、闰日、JST 已跨日但 ET 未跨日。
- daily `session_date` 不因时区转换减一天。
- 1 bar、2 bars、previous close 0、NaN、空数据、schema error。
- partial response 200、all failure 503、invalid range 422、rate limit 429。
- correlation alignment metadata 与 session-date inner join。
- `.INDEX` 研究允许、交易/backtest 拒绝。
- S&P 500 研究使用 `SP500.INDEX`；只有用户显式选择时才使用 `SPY.US`。
- 原有市场 detection/fallback 回归测试保持通过。

### 14.3 前端测试文件

- 修改 `frontend/src/i18n/__tests__/i18n.test.ts`
- 修改 `frontend/src/lib/__tests__/formatters.test.ts`
- 新增 `frontend/src/hooks/__tests__/useMarketIndices.test.ts`
- 新增 `frontend/src/components/market/__tests__/IndexOverview.test.tsx`
- 新增 `frontend/src/components/market/__tests__/IndexCard.test.tsx`
- 新增 `frontend/src/components/market/__tests__/IndexChart.test.tsx`

必测：

- 日本发行版默认 ja，保存语言和支持的浏览器语言优先。
- 原有 Home 研究内容以及目标分支中已有的 A 股模块继续渲染，并位于国际指数模块之前。
- 点位不出现 `¥` 或 `$`。
- JPY/USD metadata 不影响指数 number formatting。
- loading、success、partial、empty、503、retry。
- card 单击只改变 selected item，不额外 fetch。
- range 改变取消旧请求，旧 response 晚到不覆盖新数据。
- 组件卸载取消 waiter，但共享后端任务语义由后端测试覆盖。
- 键盘选择、aria-selected、正负 aria-label。
- 1M 跨月图表 x-axis 正确使用 session date。

### 14.4 网络 smoke

非阻塞 CI，手动/定时运行：

- `^N225`、`^GSPC`、`^IXIC`、`^DJI` 最近 10 个 session。
- 至少 2 根有效 bar、close > 0、session date 递增。
- response 不含 provider cookie、crumb 或原始 URL。
- 网络失败只报告 provider 状态，不归类为代码单测失败。

### 14.5 验证命令

```bash
cd agent
pytest tests/test_instruments.py tests/test_market_data.py tests/test_yahoo_loader.py tests/test_registry.py tests/test_market_detection.py tests/test_market_index_service.py tests/test_market_routes.py tests/test_correlation.py tests/test_benchmark.py tests/test_autopilot_tool.py -q

cd ../frontend
npm test -- --run
npm run build
```

新增测试通过后运行完整 Python 测试集；未执行项必须写入交付说明。

## 15. Failure modes

| 故障 | 处理 | 测试 | 用户体验 |
|---|---|---|---|
| Yahoo timeout/429 | provider error；partial 或 503 | 有 | 显示该指数暂不可用，可重试 |
| Provider schema 变化 | parse error，不输出 0 | 有 | 明确数据不可用，不显示假行情 |
| 多个并发请求惊群 | per-key single-flight | 有 | 用户只感知一次正常 loading |
| 首个 waiter 离开页面 | shield shared task | 有 | 其他请求继续完成 |
| 旧 range response 晚到 | AbortController + request identity | 有 | 不覆盖用户当前选择 |
| 日美本地日期不同 | 每 instrument 独立 window | 有 | item 展示自己的 window/session |
| 单根 bar | change 字段 null | 有 | 显示 `—`，不显示 0% |
| provider policy 配错 | startup/preflight validation | 有 | feature disabled/503，日志给运维原因 |
| 官方源失败后非法 fallback | policy 明确禁止 | 有 | 不偷偷切未经授权的数据源 |
| 全部数据 stale | V1 不作交易日历推断；显示 delay unknown | 有文案测试 | 不宣称实时或最新 |

没有“无测试 + 无错误处理 + 静默失败”的已知路径。

## 16. 验收标准

- [ ] 四个 canonical symbol 能通过统一 service 获取日线。
- [ ] 现有 `.SH/.SZ/.BJ` A 股 symbol、source detection、fallback、研究、相关性和 backtest 行为保持不变。
- [ ] 同一 API 响应允许四个 item 使用不同 provider。
- [ ] API 成功项始终按日经、S&P 500、Nasdaq Composite、DJIA 排序，partial failure 不打乱其余项。
- [ ] API 每项 source 与实际 provider 一致。
- [ ] 日线只返回 `session_date`，不返回伪市场午夜 timestamp。
- [ ] `as_of` 是 UTC，市场 `timezone` 作为 metadata。
- [ ] 1m/3m/6m/1y 的 calendar lookback 通过月末、闰日测试。
- [ ] `nasdaq/纳指/ナスダック` 返回歧义，不静默选择 Composite 或 QQQ。
- [ ] 首页原有内容及已有 A 股模块在前，国际指数模块在后；模块内部日经第一、默认选中，四卡和图表可用。
- [ ] card 切换不重复 fetch；range 切换才请求新数据。
- [ ] 点位无货币符号，正负信息不只依赖颜色。
- [ ] partial failure 不隐藏成功指数；全部失败可恢复重试。
- [ ] 同 key 并发只触发一次 provider fetch。
- [ ] provider URL/query/cookie/crumb 不进入日志或浏览器错误。
- [ ] 四大指数可用于相关性和显式 benchmark。
- [ ] 相关性 response 明确 `session_date + inner join` 对齐语义。
- [ ] `.INDEX` 不能进入直接交易/backtest。
- [ ] 不迁移合法 SPY/QQQ/DIA 历史内容。
- [ ] 现有 A/HK/US/India/crypto/futures/forex 测试无回归。
- [ ] 前端全量 test 和 production build 通过。
- [ ] 商业生产前数据授权 Gate 通过；否则 feature flag 关闭。

## 17. 发布、监控和回滚

### 17.1 Flags

```text
VIBE_MARKET_INDICES_ENABLED=false
VITE_DEFAULT_LANGUAGE=ja
```

V1 provider policy 和 `INDEX_PROVIDER_POLICY_VERSION = 1` 写在受测试的 Python
配置模块中；version 只参与 snapshot cache key，不需要暴露为环境变量。V1 不使用请求参数或任意 JSON URL 配置。将来接商用 provider 时再把 policy 配置化，并增加 schema validation。

### 17.2 发布顺序

1. mock/fixture 开发。
2. 内部环境接 Yahoo smoke。
3. staging 连续观察至少 5 个日美交易日。
4. 小范围日语 beta。
5. 数据授权 Gate 通过后才进入商业生产。

### 17.3 指标

- `market_data_requests_total{provider,symbol,status}`
- `market_data_request_duration_ms{provider}`
- `market_data_snapshot_cache_total{result}`
- `market_data_singleflight_waiters{key}`，key 必须是低基数枚举/哈希，不能放 URL
- `market_data_last_success_age_seconds{symbol}`
- `market_data_partial_response_total`
- `market_data_schema_error_total{provider}`

### 17.4 回滚

- 关闭 `VIBE_MARKET_INDICES_ENABLED`，只移除新增国际指数模块，Home 与 A 股能力恢复/保持当前状态。
- V1 无数据库 schema，无迁移回滚。
- snapshot cache 在内存，重启清除。
- provider policy 切换只能指向已实现、已测试且已授权的 loader。
- 禁止临时抓取未知网站作为线上替代。

## 18. 实施任务、依赖和工作量

| Task | 内容 | 依赖 | 预计 |
|---|---|---|---|
| T1 | Instrument catalog、alias/ambiguity、独立 provider policy 模块 | 无 | 1.0–1.5 天 |
| T2 | Yahoo index adapter、registry、market detection、disk cache 复用 | T1 | 1.0–1.5 天 |
| T3 | Market index service、range、mixed provider、snapshot cache/single-flight | T1、T2 | 1.5–2.0 天 |
| T4 | REST API、response models、auth/rate limit/error contract | T3 | 1.0–1.5 天 |
| T5 | Correlation、benchmark、autopilot tradability | T1、T2 | 1.0–1.5 天 |
| T6 | 在 Home 现有内容/A 股模块之后追加国际指数组件、hook、ECharts | T4 | 1.5–2.0 天 |
| T7 | 日语默认、locale、number/session formatter | T4，可与 T6 部分并行 | 1.0–1.5 天 |
| T8 | 全量测试、smoke、flags、发布文档 | T1–T7 | 1.0–1.5 天 |
| T9 | 商业数据许可与 provider 采购 | 可并行，阻塞商业生产 | 外部 1–6+ 周 |

工程合计约 **8–12 工程日**。多人并行时，为 API contract 冻结、fixture 共享和最终集成另预留 **0.5–1 天**。

依赖：

```text
T1 --> T2 --> T3 --> T4 --> T6 --> T8
 |      |            |      T7 ----^
 |      +--> T5 -------------------^
 +----------> T5

T9 --------------------------------> 商业生产 Gate
```

T5 显式依赖 T1 的 catalog/policy 和 T2 的可用 loader。

### 可并行 lane

```text
Lane A: T1 -> T2 -> T3 -> T4              核心后端，顺序
Lane B: 等 T1+T2 后执行 T5                 研究/backtest，可与 T3/T4 并行
Lane C: 等 T4 contract 冻结后 T6 与 T7 并行 前端 UI / i18n
Lane D: T9                                  商业许可，全程并行
最终: merge A+B+C -> T8
```

冲突提示：T6 和 T7 都可能改 Home/locale/formatter，必须事先划清 owner；T7 不修改 market 组件结构，T6 不直接编辑全局语言初始化逻辑。

## 19. 商业数据采购支线

1. 写明用户规模、区域、刷新频率、历史长度、缓存、导出和 AI 使用。
2. 向 Nikkei、Nasdaq、S&P DJI 或授权聚合商确认四个具体指数；S&P DJI 合同必须分别覆盖 S&P 500 和 DJIA。
3. 比较分别采购与统一聚合商的价格、合同和混合 provider 运维成本。
4. 确认名称、数值、图表、derived analytics、缓存和 artifact 保存权限。
5. 确认 fallback 是否允许，以及 fallback 数据是否也要展示 attribution。
6. 新 provider 实现现有 DataLoader contract。
7. staging 双源对账至少 10 个交易日。
8. 按 instrument 切换 provider policy，不修改前端 API contract。

## 20. 实施纪律

- V2 审核通过前不修改业务代码。
- 原 V1 文档保留为审计历史，V2 不覆盖原文件。
- 先提交 contract/fixture，再接真实网络。
- 不新建重复 Provider Interface。
- 不顺手迁移所有证券到 instrument catalog。
- 不删除、替换、降级或重排现有 A 股数据源、symbol、fallback、研究/backtest 路径及已有 UI。
- 不修改当前未提交的 Docker、Compose 和依赖锁改动。
- 不自动重写历史会话。
- API contract、canonical symbol 或歧义规则变化必须先更新本文件和测试。
- 数据授权与技术假设冲突时，暂停 production，不用技术手段绕过合同。

## 21. V2 复审核对表

- [x] 同意不新建第二套 Provider Interface，复用现有 loader/registry。
- [x] 同意新增 per-instrument provider policy 支持混合来源。
- [x] 同意日线使用 `session_date`，不返回市场午夜 timestamp。
- [x] 同意所有语境下 `nasdaq/纳指/ナスダック` 都返回歧义提示。
- [x] 同意 `SP500.INDEX` 专指价格指数，不能静默替换为全收益/等权指数、期货或 `SPY`。
- [x] 同意 fixed TTL + per-key single-flight 方案。
- [x] 同意指数 API 不返回 volume。
- [x] 同意指数点位只使用 number formatter，不使用 currency formatter。
- [x] 同意完整保留 A 股数据能力，四个国际指数只做增量扩展。
- [x] 同意 Home 仍是 V1 入口；原有内容/已有 A 股模块在前，国际指数模块在后，且组件可迁移到未来 `/market`。
- [x] 同意不迁移历史 SPY/QQQ/DIA，只精确处理错误 `^N225.HK`。
- [x] 同意 8–12 工程日及多人联调缓冲。
- [x] 同意商业上线仍由数据授权 Gate 阻塞。

实际实施顺序遵循 `T1 -> T2 -> T3/T5 -> T4 -> T6/T7 -> T8`；T9 商业许可继续作为独立 Gate 推进。

## 22. 2026-07-17 实施结果

已完成：

- 四个 canonical instrument、明确 alias、Nasdaq 歧义和 per-instrument provider policy。
- Yahoo adapter 边界映射、`global_index` registry/fallback、A 股 detection/fallback 回归保护。
- `session_date` 日线 contract、calendar range、四指数 service、60/15/5 秒 snapshot cache 与 per-key single-flight。
- 带 auth、60 次/分钟限流、partial success 和 503 all-failure 的 `GET /market/indices`。
- correlation alignment metadata、显式指数 benchmark、autopilot 静默 ETF 代理删除和 `INDEX_NOT_TRADABLE` 防线。
- Home 原内容之后的四指数卡片、range、折线图、partial/error/retry、数据源与延迟说明。
- 日语默认 fallback、五种 locale 文案、无货币符号指数 formatter。
- `VIBE_MARKET_INDICES_ENABLED` 默认关闭；关闭时前端不展示新增模块，A 股能力不受影响。

验证结果：

- 前端 Vitest：`29 files / 255 tests` 全部通过。
- 前端 production build：通过。
- Python `compileall` 与 `git diff --check`：通过。
- 生产镜像只读挂载无网络验证：通过，覆盖 canonical、A 股 fallback、Yahoo provider boundary、service cache/single-flight 和 FastAPI 200/422 contract。
- Yahoo 公网 smoke：四个指数全部成功，日经 23 根、S&P 500/Nasdaq/DJIA 各 20 根日线，无 error item。
- 运行镜像不包含 pytest；安全策略禁止在带项目状态的 root 容器临时下载测试包，因此新增 Python pytest 文件已提交但本轮未执行完整 pytest suite。此项必须在 CI/dev 依赖环境补跑。

仍被 Gate 阻塞：

- 商业生产展示前的 Nikkei、Nasdaq、S&P 500、DJIA 数据授权。
- staging 连续 5 个日美交易日观察。
