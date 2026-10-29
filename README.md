# A股中短线预测（stock-predictor）

综合**量价技术面 / 资金结构面 / 情绪题材面 / 大盘环境 / 消息事件面**五维信号，
预测 A 股次日走势，输出方向、涨跌幅区间、触发信号、置信度与逐项可解释证据。
支持对话问答与定时盘后日报。

> ⚠️ **免责声明**：本插件输出仅为基于公开行情数据的量化参考，**不构成任何投资建议**。
> 股市有风险，入市需谨慎，据此操作盈亏自负。

## 特性

- **五维综合打分**：技术面 40% / 资金面 25% / 情绪面 20% / 大盘 10% / 消息面 5%（权重可在 `plugin/api.py` 调整）
- **可解释**：每个结论都附逐项证据，可追溯"为什么这么判断"
- **多源降级**：东财主源 + 新浪备用源，接口限流自动降级；内置缓存与重试
- **中短线信号**：放量突破 / 缩量回踩 / 放量滞涨 / 连板情绪 / 限售解禁事件等触发信号
- **两种用法**：对话即时预测 + 定时盘后日报

## 安装

```bash
jarvis --install-plugin <repo 或本地路径>
```

依赖：`akshare`（数据源）、`pandas`、`numpy`。

## 使用

对话中直接说：

```
预测明天 600519 走势
600519 怎么看
分析一下 000858 的资金面
出一份盘后日报
```

## 工具

| 工具 | 说明 |
|------|------|
| `stock_predict` | 综合预测：方向 + 涨跌幅区间 + 触发信号 + 置信度 + 五维证据 |
| `stock_analyze` | 单维度深度分析（tech/fund/sentiment/market/news） |
| `stock_quote` | 行情快照：收盘价/涨跌幅/量比/均线/RSI |
| `stock_daily_report` | 盘后日报：批量预测关注列表 |

## 定时盘后日报

在 Jarvis 中创建定时任务（如每交易日 17:30）：

- 动作：创建 Agent 或调用 `stock_daily_report` 工具
- 关注列表：可通过 `symbols` 参数指定，或修改 `tools/stock_daily_report.py` 的 `DEFAULT_SYMBOLS`

## 目录结构

```
stock-predictor/
├── config.yaml            # 插件声明
├── README.md
├── rules/                 # 触发规则
├── tools/                 # Agent 工具（4个）
└── plugin/api.py          # 私有功能：数据接入+指标+五维打分引擎
```

## 数据源

- 日K线：东财 `stock_zh_a_hist` → 新浪 `stock_zh_a_daily`（降级）
- 主力资金：`stock_individual_fund_flow`
- 大盘指数：`stock_zh_index_daily`
- 涨停池：`stock_zt_pool_em`
- 个股新闻：`stock_news_em`
- 北向资金：`stock_hsgt_fund_flow_summary_em`
- 两融余额：`macro_china_market_margin_sh`
- 限售解禁：`stock_restricted_release_detail_em`

## 开发

- 本地安装后即可在 `~/.jarvis/plugins/stock-predictor/` 修改，改完需重启 Jarvis 或重载插件生效。
- 权重、词库、TTL 均在 `plugin/api.py` 顶部常量区，可调。
