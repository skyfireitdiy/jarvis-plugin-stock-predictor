# A股个股数据（stock-predictor）

为 Agent 提供 A 股个股的**完整原始数据**：日K与技术指标、盘中实时走势、主力资金流、
涨停池情绪、大盘环境、两融余额、北向资金、解禁信息与个股新闻。

> **本插件不做预测。** 它只负责抓数据、算客观指标（MA/MACD/RSI/BOLL/ATR/量比/换手），
> 不输出方向、不输出涨跌幅区间、不输出置信度。所有分析结论由 Agent（大模型）基于这些数据自行给出。

> ⚠️ 股市有风险，入市需谨慎，据此操作盈亏自负。

## 特性

- **纯数据层**：`plugin/api.py` 只做数据获取与指标计算，零判断逻辑，结论完全由 Agent 生成
- **一次拿全**：`get_snapshot` 一次返回九大数据块（行情/指标/盘中/资金/大盘/情绪/新闻/两融/解禁）
- **盘中/非盘中分离**：交易时段自动补充实时走势（现价/VWAP/日内位置/盘中量比/30分钟动量/振幅），
  非交易时段直接跳过网络请求；返回 `session_state` 让 Agent 判断分析口径
- **数据可信度透明**：`data_quality` 逐项标注每个数据源 `ok` / `none` / `error`，Agent 需如实告知缺失
- **多源降级**：东财主源 + 新浪/腾讯备用源，接口限流自动降级；内置缓存与重试
- **侧边栏数据面板**：K线图、盘中快照、技术指标、资金流、新闻可视化，一键"让 Agent 分析"

## 安装

```bash
jarvis --install-plugin <repo 或本地路径>
```

依赖：`akshare`（数据源）、`pandas`、`numpy`。

## 使用

对话中直接说：

```
分析一下 600519
600519 明天怎么看
000858 的资金面怎么样
出一份自选股日报
```

或在侧边栏「A股数据」面板输入代码，点「🤖 让 Agent 分析这只股票」。

## 工具

| 工具 | 说明 |
|------|------|
| `stock_predict` | 完整数据快照：九大数据块结构化输出，供 Agent 做多维度分析 |
| `stock_analyze` | 单维度数据（tech/fund/sentiment/market/news） |
| `stock_quote` | 轻量行情：价格/涨跌幅/均线位置/RSI |
| `stock_daily_report` | 批量数据摘要：关注列表一次拉取（不调 LLM，零成本） |

## 侧边栏数据面板

`frontend/sidebar_view.js` 提供可视化面板：

- 搜索 + 自选股（localStorage 持久化，显示涨跌色）
- 盘中快照卡片（仅交易时段显示）：现价 / VWAP / 日内位置 / 盘中量比 / 30分钟动量 / 振幅
- 技术指标：MA5/10/20/60（带价格上下方配色）、MACD、RSI14、BOLL、ATR14、量比、换手
- 日K线 SVG 自绘（蜡烛 + MA5/MA20 + 成交量）
- 主力资金近 10 日、市场情绪（涨停家数/最高连板/行业分布）、大盘环境、相关新闻
- 数据可用性指示（✅ / ❌）
- 「🤖 让 Agent 分析这只股票」：把结构化数据摘要发给当前 Agent，结论在对话中给出

## 定时盘后复盘

在 Jarvis 中创建定时任务（如每交易日 17:30）：

- 动作：创建 Agent 或调用 `stock_daily_report` 工具
- 关注列表：通过 `symbols` 参数指定，或修改 `tools/stock_daily_report.py` 的 `DEFAULT_SYMBOLS`

## 目录结构

```
stock-predictor/
├── config.yaml            # 插件声明
├── README.md
├── rules/                 # Agent 分析框架说明（不含判断逻辑）
├── tools/                 # Agent 工具（4个）
├── frontend/              # 侧边栏数据面板
└── plugin/api.py          # 私有功能：数据接入 + 指标计算（纯数据层）
```

## 数据源

| 数据 | 主源 | 降级 |
|---|---|---|
| 日K线 | 东财 `stock_zh_a_hist` | 新浪 `stock_zh_a_daily` |
| 盘中分时 | 新浪 `stock_zh_a_minute(period=1)` | 腾讯 `stock_zh_a_tick_tx_js` |
| 主力资金 | `stock_individual_fund_flow` | — |
| 大盘指数 | `stock_zh_index_daily` | — |
| 涨停池 | `stock_zt_pool_em` | — |
| 个股新闻 | `stock_news_em` | — |
| 北向资金 | `stock_hsgt_fund_flow_summary_em` | — |
| 两融余额 | `macro_china_market_margin_sh` | — |
| 限售解禁 | `stock_restricted_release_detail_em` | — |

**已知限制**：东财实时接口（`stock_zh_a_spot_em` / `stock_bid_ask_em`）当前不可用（限流/接口失效），
故盘中走势走新浪分时；同花顺全市场资金流 `stock_fund_flow_individual` 单次 60s+，不作为降级源。

## 开发

- 本地安装后即可在 `~/.jarvis/plugins/stock-predictor/` 修改，改完需重启 Jarvis 或重载插件生效。
- 缓存 TTL、重试次数等常量在 `plugin/api.py` 顶部常量区，可调。
- 分析口径（Agent 该怎么看这些数据）写在 `rules/stock_predict_rule.md`。

## 变更历史

- **v0.2.0**（破坏性变更）：移除全部规则打分引擎（五维权重、方向判定、涨跌幅区间、置信度、LLM 双轨研判），
  `api.py` 退化为纯数据层；工具改为输出结构化原始数据；侧边栏从"预测结果展示"改为"数据面板 + 让 Agent 分析"。
- v0.1.4：盘中实时预测（盘中维度、区间按剩余时间折算）
- v0.1.3：修复侧边栏无法滚动
- v0.1.2：修复侧边栏空白（模板引用模块顶层函数）
- v0.1.1：新增前端侧边栏界面
- v0.1.0：首个版本
