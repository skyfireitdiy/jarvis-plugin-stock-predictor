# stock-predictor v0.2.0 架构设计：纯 Agent 驱动

> 状态：已实现（替代 v0.1.4 的规则打分引擎架构，见 `DESIGN_v0.1.4_archive.md`）

## 1. 核心决策

**彻底删除规则打分引擎，插件退化为纯数据层。**

| 项 | v0.1.4（旧） | v0.2.0（新） |
|---|---|---|
| 分析逻辑 | `api.py` 内置五维打分 + 权重 + 阈值 + 方向判定 | 无。`api.py` 只抓数据、算客观指标 |
| 结论输出 | 方向 / 涨跌幅区间 / 综合得分 / 置信度 / 触发信号 | 不输出任何结论 |
| LLM 角色 | 可选二次研判，与规则结果双轨对照 | 唯一分析者（Agent 本体） |
| 前端 | 展示预测结果（方向徽章、得分条、区间带） | 数据面板 + "让 Agent 分析"按钮 |
| 规则文件 | "如何调用预测工具" | "Agent 拿到数据后该怎么分析" |

**为什么这样改**：规则引擎的阈值是拍脑袋定的，可解释性停留在"某指标加了几分"的层面，
无法做跨维度权衡（如"技术面强但资金面背离"）。大模型能综合权衡且能自然语言解释，
把判断权交给 Agent、插件只保证数据质量，职责更清晰，也免去维护阈值表。

## 2. 分层结构

```
┌─────────────────────────────────────────────┐
│ Agent（大模型）                               │  ← 唯一的分析判断者
│  · 读 tools 返回的结构化数据                   │
│  · 按 rules/stock_predict_rule.md 的分析框架   │
│  · 输出结论 + 依据 + 风险 + 免责声明            │
└───────────────┬─────────────────────────────┘
                │ 调用
┌───────────────▼─────────────────────────────┐
│ tools/（4 个 Agent 工具）                     │  ← 数据格式化层
│  stock_predict      完整快照（九大数据块）      │
│  stock_analyze      单维度数据                 │
│  stock_quote        轻量行情                   │
│  stock_daily_report 批量摘要                   │
└───────────────┬─────────────────────────────┘
                │ 调用
┌───────────────▼─────────────────────────────┐
│ plugin/api.py（纯数据层，13 个 PUBLIC 函数）   │  ← 数据获取 + 指标计算
│  get_snapshot / get_kline / get_intraday      │
│  get_fund_flow / get_index_kline / get_zt_pool│
│  get_news / get_margin / get_north_flow       │
│  get_unlock / watchlist / *_json              │
└───────────────┬─────────────────────────────┘
                │ akshare
┌───────────────▼─────────────────────────────┐
│ 公开行情接口（东财 / 新浪 / 腾讯）              │
└─────────────────────────────────────────────┘

┌─────────────────────────────────────────────┐
│ frontend/sidebar_view.js                     │  ← 可视化（不做判断）
│  调 get_snapshot_json → 渲染数据面板           │
│  "让 Agent 分析" → 拼数据摘要发给 Agent        │
└─────────────────────────────────────────────┘
```

## 3. `get_snapshot` 返回结构

```python
{
  "symbol", "code", "name", "updated_at",
  "session_state",   # pre_open | trading | lunch_break | closed | no_data
  "is_intraday",     # bool，是否盘中
  "disclaimer",
  "kline": [ {date, open, high, low, close, volume, amount, turnover,
              chg_pct, ma5, ma10, ma20, ma60, dif, dea, macd,
              rsi14, boll_up, boll_mid, boll_low, vol_ratio}, ... ],  # 近 days 日
  "indicators": { ...末行指标 + atr14 },   # 最新一日
  "intraday": { price, prev_close, chg_pct, vwap, pos_in_range,
                vol_ratio_intraday, mom30, amplitude, trade_minutes,
                updated_at, source, partial } | None,
  "fund_flow":    [ ...近10日主力资金 ],   # 列名为中文（akshare 原样）
  "index":        [ ...上证近60日 ],
  "zt_pool":      { count, max_board, industry_rank, list } | None,
  "news":         [ {新闻标题, 发布时间, ...} ...≤20 ],
  "margin":       [ ...全市场两融近10行 ],
  "north_flow":   [ ... ],
  "unlock":       [ ... ] | None,
  "data_quality": { kline: "ok", intraday: "ok", unlock: "none", ... }
}
```

**设计要点**：
- 每个数据源之间 `time.sleep(0.4)`，避免限流导致"九个源全部 error"。
- 任何源失败只记 `data_quality` 为 `error`，字段置 `None`/`[]`，**绝不抛异常**。
- `data_quality` 三态：`ok` 正常 / `none` 该股确实无此类数据（如无解禁，**不是故障**）/ `error` 抓取失败。

## 4. 盘中 / 非盘中分离

`is_trading_session(now)` 返回 `(is_intraday, session_state)`：

- `pre_open`：9:15 前
- `trading`：9:30–11:30、13:00–15:00
- `lunch_break`：11:30–13:00（**归为盘中**，仍展示盘中数据）
- `closed`：收盘后 / 周末
- `no_data`：分时数据末行日期与今日不符（节假日兜底）

非盘中时 `get_intraday` **直接返回 `available=False`，不发起网络请求**（省 1~2 秒）。

**为什么必须分离**：盘中日K的最后一根是**未完成**K线，基于它算出的 MA/MACD/RSI 会失真。
插件如实标注 `session_state`，由 Agent 在分析时区分口径（规则文件已明确要求）。
`indicators` 始终基于**已完成**的交易日计算，与 `intraday` 分开呈现。

## 5. 数据源选型（2026-10 实测 akshare 1.18.64）

| 数据 | 主源 | 降级 | 实测 |
|---|---|---|---|
| 日K | 东财 `stock_zh_a_hist` | 新浪 `stock_zh_a_daily` | 东财常限流，`with_retry(1, 0.5)` 快速失败降级 |
| 盘中分时 | 新浪 `stock_zh_a_minute(period=1)` | 腾讯 `stock_zh_a_tick_tx_js` | 新浪 0.9~1.9s / 1970 行；腾讯 0.85s / 仅最新价 |
| 主力资金 | 东财 `stock_individual_fund_flow` | — | 间歇限流，需重试 |
| 涨停池 | `stock_zt_pool_em` | — | 稳定 |
| 新闻 | `stock_news_em` | — | 稳定 |
| 两融 | `macro_china_market_margin_sh` | — | 全市场，无参数 |
| 北向 | `stock_hsgt_fund_flow_summary_em` | — | 稳定 |
| 解禁 | `stock_restricted_release_detail_em` | — | 多数股票无数据 → `none` |

**已排除的数据源**：
- 东财实时接口（`stock_zh_a_spot_em` 限流 / `stock_bid_ask_em` JSONDecodeError / `stock_zh_a_minute_em` 二次即限流）——**全部不可用**。
- 同花顺 `stock_fund_flow_individual`：内部翻 100+ 页爬全市场，单次 60s+ 且抢占网络，**绝不可做自动降级源**。

## 6. 实现中修复的 4 个真实 bug

1. `get_margin()` 不接受参数，`get_snapshot` 却传了 `code` → 改为无参调用。
2. `_calc_indicators(df)` 返回**全量 DataFrame**，被直接塞进 `indicators` → 改为 `_last_row()` 取末行 + 补 `atr14`。
3. `_get_prev_close_and_volma5` 假设"日K不含当日"，但东财日K盘中**已含当日未完成K线**，
   导致 `prev_close` 取成现价、`chg_pct=0` → 改为按 `_d < trade_date` 显式排除当日。
4. 新浪分时 `volume`/`amount` 列是**字符串**，`sum()` 做字符串拼接，算出量比 129.75 的荒谬值
   → 加 `pd.to_numeric` 转换，修复后量比 1.29 合理。

## 7. 前端数据面板

- 单文件 ES module，`window.Vue`，不 `import 'vue'`，`export default`。
- 模板引用的函数**必须**在 `methods`/`computed`（v0.1.2 空白 bug 教训）。
- 根 div 必须 `height:100%` + `overflow:auto` + `box-sizing:border-box`（v0.1.3 滚动 bug 教训）。
- 一次调 `get_snapshot_json` 拿全量数据（约 10 秒），不并行多次调用。
- `mounted` **不自动加载**自选股，避免每次打开侧边栏都触发 10 秒抓取。
- "让 Agent 分析"：把数据拼成结构化文本，经 `window.__jarvisSendToActiveAgent` 发给当前 Agent。

## 8. 验证方法

| 层 | 方法 |
|---|---|
| api.py | `py_compile` + 真实调用 `get_snapshot('301002')`，检查九源 `data_quality` 全 ok、JSON 可序列化、异常输入不抛错 |
| tools | `py_compile` + 逐个调用，检查输出无结论性词汇 |
| 前端 | `/tmp/vuetest` 的 jsdom + Vue 3.5 真实渲染，盘中/非盘中/空态三场景，断言 DOM 长度 >5000、零渲染错误、关键文案存在 |
| 发布后 | `GET /api/plugins?node_id=master` 看 `frontend.sidebar_views`；`POST /api/plugins/master/function-call` 实测函数 |

**jsdom 环境要点**：JSDOM 必须带 `url: 'http://127.0.0.1:8000/'`（否则 localStorage 抛 SecurityError）；
需补 `SVGElement/HTMLElement/Element/Node/CSS/MutationObserver/Document/DocumentFragment/ShadowRoot` 等全局；
mock `__jarvisFetch` 返回 `{ok:true, json: async()=>({success:true, result: payload})}`。
