# stock-predictor v0.1.4 设计文档：盘中实时预测 + LLM 综合研判

> 状态：设计定稿，待实现
> 前置调研：task-14（实时数据源实测，2026-10-30）
> 方案决策：用户已批准「方案 B：hybrid 双轨对照 + LLM 可选参数」

---

## 0. 设计目标与边界

### 目标
1. **盘中调用** `predict` 时，实时行情参与打分，输出反映当日盘中状态。
2. **引入 LLM 研判层**：规则给出可复现的数字结论，LLM 在规则结果基础上做自然语言综合研判，两者并列展示、冲突时告警。
3. **非盘中行为完全不变**（向后兼容 v0.1.3）。

### 明确不做的事
- 不让 LLM 计算任何数字（涨跌幅、区间、得分全部由规则算出后传入）。
- 不让 LLM 成为默认路径（默认 `mode="rule"`，避免拖慢 `daily_report` 批量场景）。
- 不引入机器学习模型（无历史样本积累，不具备训练条件）。
- 不做逐笔/盘口五档（东财盘口接口已失效，腾讯逐笔仅作降级源提供最新价）。

---

## 1. 盘中判定逻辑

### 1.1 判定依据（双条件，避免单纯依赖本地时钟）

```python
def is_trading_session(now=None) -> tuple[bool, str]:
    """返回 (是否盘中, 说明)。"""
```

判定规则：

| 条件 | 说明 |
|---|---|
| C1. 今天是工作日（周一~周五） | 周末直接判否 |
| C2. 当前时间落在 09:30–11:30 或 13:00–15:00 | 严格交易时段 |
| C3. 当前时间落在 11:30–13:00（午休） | **仍判为盘中**（见下） |
| C4. 数据校验：分时数据末行日期 == 今天 | 兜底识别节假日 |

**节假日处理**：C1–C3 只能排除周末，无法识别法定节假日。因此增加 C4 作为**最终裁决**——`get_intraday` 拿到分时数据后，若末行日期不是今天，则 `available=False, reason="非交易日或停牌"`。这样即使本地时钟认为在交易时段，节假日也会被数据事实否决。

**午休算不算盘中**：**算**。理由：11:30 收盘后数据仍是当天的，用户此时查看想看的是"今天上午走成什么样"，此时按次日预测（用昨日日K）反而是错的。因此午休期间 `is_intraday=True`，但 `session_state="lunch_break"`，前端可提示"午间休市，数据截至 11:30"。

**盘后（15:00 之后）**：**不算盘中**。此时当日已收盘，日K源（新浪）通常当天仍拿不到当日数据（实测新浪日K T+0 不含当日），但盘中分时数据是完整的。为避免"盘后还用盘中口径"造成误导，盘后按 `session_state="closed"` 处理，`is_intraday=False`，走原有次日预测。**这是已知局限**：盘后到日K更新前（通常当晚或次日凌晨），预测仍基于昨日数据。此局限在 v0.1.3 已存在，本次不扩大也不修复。

### 1.2 session_state 枚举

| 值 | 含义 | is_intraday |
|---|---|---|
| `pre_open` | 开盘前（< 09:30） | False |
| `trading` | 连续竞价（09:30–11:30, 13:00–15:00） | True |
| `lunch_break` | 午间休市（11:30–13:00） | True |
| `closed` | 收盘后（>= 15:00）或周末 | False |
| `no_data` | 数据校验失败（节假日/停牌/数据源不可用） | False |

---

## 2. 数据层：`get_intraday(symbol)`

### 2.1 函数签名

```python
def get_intraday(symbol) -> dict:
    """盘中实时快照。

    返回:
      {
        "available": bool,
        "reason": str | None,          # available=False 时说明原因
        "symbol": str,                 # 6 位代码
        "session_state": str,          # trading / lunch_break / pre_open / closed / no_data
        "trade_date": str,             # "YYYY-MM-DD"，数据所属交易日
        "updated_at": str,             # "HH:MM:SS"，分时数据末行时间
        "source": str,                 # "sina_minute" / "tencent_tick"
        "prev_close": float,           # 昨收（取自日K末行）
        "price": float,                # 最新价
        "chg_pct": float,              # 涨跌幅 %（相对昨收）
        "open": float,                 # 今开
        "high": float,                 # 日内最高
        "low": float,                  # 日内最低
        "vwap": float,                 # 成交量加权均价
        "chg_vs_open": float,          # 相对开盘 % 
        "chg_vs_vwap": float,          # 相对 VWAP %
        "pos_in_range": float,         # 日内位置 0~100（0=最低 100=最高）
        "vol_ratio_intraday": float,   # 盘中量比（时间折算）
        "mom30": float | None,         # 近 30 分钟涨幅 %
        "trade_minutes": int,          # 已交易分钟数
        "amplitude": float,            # 振幅 % = (high-low)/prev_close*100
      }
    """
```

### 2.2 数据源策略

| 优先级 | 源 | 接口 | 实测耗时 | 用途 |
|---|---|---|---|---|
| 主 | 新浪分时 | `ak.stock_zh_a_minute(symbol=exchange_prefix(code), period="1", adjust="")` | 0.9~1.9s | 全部字段 |
| 备 | 腾讯逐笔 | `ak.stock_zh_a_tick_tx_js(symbol=exchange_prefix(code))` | 0.82~0.87s | 仅 price（最新成交价） |

**降级行为**：主源失败时走腾讯逐笔，但腾讯逐笔只有逐笔成交，**无法算 VWAP/日内高低/量比**（可以算但需聚合全部逐笔，且逐笔只有当日、无历史对比基准）。因此降级时返回**精简快照**：
- 可用：`price`、`chg_pct`（需 prev_close）、`updated_at`
- 不可用：`vwap`/`pos_in_range`/`vol_ratio_intraday`/`mom30` 置 `None`
- `source="tencent_tick"`，`partial=True` 标记

打分函数 `_score_intraday` 必须能处理字段为 `None` 的情况，缺失项跳过不计分（而非当 0 分），并在 evidence 中说明"部分字段缺失，仅按可用项打分"。

### 2.3 字段计算口径

设当日分时 DataFrame `cur`（已过滤 `day` 日期 == 今天），列 `open/high/low/close/volume/amount`：

| 字段 | 公式 | 说明 |
|---|---|---|
| `prev_close` | `get_kline(code)["close"].iloc[-1]` | 新浪日K不含当日（已验证），末行即昨收 |
| `price` | `cur["close"].iloc[-1]` | 最后一分钟收盘价 |
| `chg_pct` | `(price / prev_close - 1) * 100` | |
| `open` | `cur["open"].iloc[0]` | 第一分钟开盘价 |
| `high` / `low` | `cur["high"].max()` / `cur["low"].min()` | |
| `vwap` | `cur["amount"].sum() / cur["volume"].sum()` | 成交额/成交量 |
| `chg_vs_open` | `(price / open - 1) * 100` | |
| `chg_vs_vwap` | `(price / vwap - 1) * 100` | |
| `pos_in_range` | `(price - low) / (high - low) * 100`，`high==low` 时取 50 | |
| `vol_ratio_intraday` | `vol_today / (vol_ma5 * trade_minutes / 240)` | `vol_ma5` 取日K前5日均量；`trade_minutes` 已交易分钟数 |
| `mom30` | `(price / cur["close"].iloc[-31] - 1) * 100`，不足 31 分钟为 `None` | |
| `trade_minutes` | 上午 `min(120, 距9:30分钟)` + 下午 `max(0, 距13:00分钟)` | 午休不计 |
| `amplitude` | `(high - low) / prev_close * 100` | |

**注意**：`vol_ma5` 必须用**未含当日**的前 5 日（日K末行是昨日，取 `tail(5)` 即为前5日，正确）。

### 2.4 缓存

```python
_intraday_cache = {"data": {}, "ts": {}}   # code -> dict, TTL 45s
CACHE_TTL["intraday"] = 45
```

TTL 45s 的理由：盘中 1 分钟数据本身更新粒度是 1 分钟，45s 内重复请求无意义；同时避免用户连续点击时反复打新浪（实测单次 0.9~1.9s，无缓存时体验差）。**非盘中不缓存**（`session_state` 为 `closed`/`pre_open` 时直接返回 `available=False`，无需请求）。

---

## 3. 打分层：`_score_intraday(snapshot)`

### 3.1 函数签名

```python
def _score_intraday(snap) -> dict:
    """盘中走势打分 [-100, 100]。

    返回 {"score": float, "evidence": list[str], "available": bool}
    """
```

### 3.2 打分阈值表

设计原则：沿用 `_score_tech` 的粒度（单项 ±3~±12），总分裁剪到 ±100。**分值设计逻辑**：越能反映"资金真实态度"的指标权重越高（VWAP 反映全天平均成本，最难操纵），越短期的指标权重越低（30分钟动量噪声大）。

| # | 指标 | 阈值 | 分值 | 理由 |
|---|---|---|---|---|
| 1 | **相对 VWAP** `chg_vs_vwap` | ≥ +3% | +12 | 显著站上全天均价，说明买方持续占优、成本抬升 |
| | | +1% ~ +3% | +7 | 温和站上均价，偏强 |
| | | -1% ~ +1% | 0 | 围绕均价震荡，中性 |
| | | -3% ~ -1% | -7 | 温和跌破均价，偏弱 |
| | | ≤ -3% | -12 | 显著跌破均价，卖方压制 |
| 2 | **日内位置** `pos_in_range` | ≥ 85 | +8 | 收在日内高位，尾盘强势（若在 14:30 后权重更有效） |
| | | 65 ~ 85 | +4 | 偏强 |
| | | 35 ~ 65 | 0 | 中位 |
| | | 15 ~ 35 | -4 | 偏弱 |
| | | ≤ 15 | -8 | 贴近日内低点，弱势 |
| 3 | **相对开盘** `chg_vs_open` | ≥ +3% | +6 | 低开高走/高开续涨，日内多头主导 |
| | | +1% ~ +3% | +3 | |
| | | -1% ~ +1% | 0 | |
| | | -3% ~ -1% | -3 | |
| | | ≤ -3% | -6 | 高开低走，日内空头主导 |
| 4 | **盘中量比** `vol_ratio_intraday` | ≥ 2.0 且 `chg_pct > 0` | +8 | 放量上涨，资金主动做多 |
| | | ≥ 2.0 且 `chg_pct < 0` | -8 | 放量下跌，资金出逃 |
| | | 1.5 ~ 2.0 且 `chg_pct > 0` | +5 | 温和放量上涨 |
| | | 1.5 ~ 2.0 且 `chg_pct < 0` | -5 | 温和放量下跌 |
| | | ≤ 0.6 且 `chg_pct > 0` | -3 | 缩量上涨，上涨缺乏量能支撑，警惕 |
| | | ≤ 0.6 且 `chg_pct < 0` | +2 | 缩量下跌，抛压不重 |
| | | 其余 | 0 | 量能平稳 |
| 5 | **30分钟动量** `mom30` | ≥ +2% | +5 | 近期强势拉升 |
| | | +0.8% ~ +2% | +2 | |
| | | -0.8% ~ +0.8% | 0 | |
| | | -2% ~ -0.8% | -2 | |
| | | ≤ -2% | -5 | 近期快速跳水 |
| 6 | **振幅异常** `amplitude` | ≥ 8% | -4 | 日内剧烈波动，风险高，降分 |
| | | 5% ~ 8% | -2 | 波动较大 |
| | | < 5% | 0 | 正常 |

**分数上限**：理论最高 +39（12+8+6+8+5），最低 -39（-12-8-6-8-5），再叠加振幅惩罚 -4。裁剪到 ±100 后实际范围约 ±43。**这比 `_score_tech`（±100 满幅）窄**，是有意为之：盘中数据只反映当日几小时，信息量不足以支撑极端判断，窄幅更诚实。

**字段缺失处理**：`vwap`/`pos_in_range`/`vol_ratio_intraday`/`mom30` 任一为 `None` 时，跳过该项（不计分），并在 evidence 追加"XX 数据缺失，已跳过"。若可用项 < 2 项，返回 `available=False`。

### 3.3 evidence 文案示例

```
"现价 26.71 站上全天均价 26.20（+1.95%），买方成本占优"
"日内位置 63%，处于中上部"
"低开高走，相对开盘 +2.14%，日内多头主导"
"盘中量比 0.82，缩量上涨，上攻动能不足"
"近30分钟 +1.10%，短线偏强"
```

---

## 4. `predict` 改造方案

### 4.1 签名变更

```python
def predict(symbol, days=120, mode="rule"):
    """mode:
      "rule"   —— 纯规则（默认，v0.1.3 行为）
      "llm"    —— 规则 + LLM 研判（LLM 为主展示，规则作为对照）
      "hybrid" —— 规则 + LLM 研判（两者并列，冲突告警）
    """
```

`mode="llm"` 与 `"hybrid"` 的差别仅在**前端展示侧重**，后端返回结构完全一致（都含 `rule_result` 与 `llm`）。这样后端只需实现一套逻辑，前端按 mode 决定展示样式。

### 4.2 盘中维度权重方案（三选一）

现有：`tech 0.40 / fund 0.25 / sentiment 0.20 / market 0.10 / news 0.05`

| 方案 | 盘中权重 | 优点 | 缺点 |
|---|---|---|---|
| **A. 新增 intraday 并压缩 tech** | `tech 0.25 / intraday 0.20 / fund 0.22 / sentiment 0.18 / market 0.10 / news 0.05` | 盘中信息真正参与且占比可观；tech 降权合理（日K技术指标在盘中是"背景"而非"当下"） | 权重表分两套，维护成本略增 |
| B. intraday 替换 tech | `intraday 0.40 / fund 0.25 / ...` | 简单 | 盘中完全丢弃日K技术形态（如均线位置、MACD），丢失重要背景，**不推荐** |
| C. intraday 作为 tech 的修正项 | `tech 0.40`（内部 `tech_final = tech*0.6 + intraday*0.4`） | 不新增维度，前端改动小 | 语义混淆，用户看不到盘中独立得分，可解释性差 |

**推荐方案 A**。理由：
1. 盘中数据是"当下状态"，日K技术是"中期背景"，两者都有价值，应并存而非替代。
2. tech 从 0.40 降到 0.25 是合理的——盘中时，昨天的 MACD 金叉不如今天的 VWAP 位置重要。
3. 新增独立维度，前端可单独展示盘中得分条，用户能看清"盘中表现如何"。

**权重归一化校验**：0.25+0.20+0.22+0.18+0.10+0.05 = 1.00 ✓

### 4.3 非盘中行为保证

- `mode="rule"` 且非盘中：**完全走 v0.1.3 原路径**，`total` 计算、方向判定、区间、置信度、signals、dims 全部不变。
- 新增字段始终存在但为 `None`/`False`（见 4.5），不影响旧字段。

### 4.4 盘中时的计算流程

```
1. get_kline → 日K
2. get_intraday → 盘中快照
3. 若 snap.available:
     tech = _score_tech(kline)          # 仍算，作为背景
     intra = _score_intraday(snap)      # 新增
     使用盘中权重表计算 total
     区间计算基准改为当前价 price（而非 last_close）
     置信度：可用维度数 + 盘中数据完整度
   否则:
     走原逻辑（完全不变）
4. signals 追加盘中信号（如"盘中放量突破均价"）
```

**区间计算的特殊处理**：盘中时 `last_close` 字段仍返回**昨收**（保持字段语义），但新增 `ref_price` 字段表示区间计算基准（盘中=现价，非盘中=最新收盘）。区间百分比含义变为"相对现价的预期波动"，需在返回中说明。

### 4.5 返回字段清单（新增）

| 字段 | 类型 | 非盘中 | 盘中 | 说明 |
|---|---|---|---|---|
| `is_intraday` | bool | `False` | `True` | 是否盘中模式 |
| `session_state` | str | `"closed"`/`"pre_open"` | `"trading"`/`"lunch_break"` | 交易状态 |
| `ref_price` | float | = `last_close` | = 现价 | 区间计算基准 |
| `intraday` | dict\|None | `None` | 快照 dict（同 2.1） | 盘中快照 |
| `intraday_dim` | dict\|None | `None` | `{name, score, evidence, weight}` | 盘中维度得分 |
| `llm` | dict\|None | `None` | 见 5.4 | LLM 研判结果 |
| `rule_result` | dict\|None | `None` | `{direction, score, confidence, range_low, range_high}` | 规则结论副本（mode≠rule 时） |
| `conflict` | bool | `False` | 规则与 LLM 方向冲突时 `True` | |
| `mode` | str | `"rule"` | 同请求 | 回显 |

**向后兼容保证**：v0.1.3 的所有字段（`success/symbol/trade_date/last_close/direction/range_low/range_high/score/confidence/signals/dims/disclaimer`）**全部保留且语义不变**。新增字段只增不改。

---

## 5. LLM 研判层

### 5.1 函数签名

```python
def _llm_judge(context: dict, timeout: int = 60) -> dict:
    """调用 LLM 做综合研判。

    返回:
      {
        "available": bool,
        "error": str | None,
        "direction": str | None,        # 看涨/看跌/震荡
        "confidence": int | None,       # 0-100
        "reasoning": str | None,        # 研判理由（自然语言）
        "risk_flags": list[str],        # 风险提示
        "model_group": str | None,
        "elapsed": float,
      }
    """
```

### 5.2 输入 context 构造

```python
context = {
    "symbol": code,
    "trade_date": trade_date,
    "is_intraday": bool,
    "session_state": str,
    "rule_scores": {                    # 规则五维（或六维）得分
        "tech": {"score": 12.5, "evidence": [...]},
        "fund": {...},
        ...
        "intraday": {...},              # 盘中时存在
    },
    "rule_total": 18.3,
    "rule_direction": "看涨",
    "rule_range": [-1.2, 2.4],
    "rule_confidence": 68,
    "indicators": {                     # 关键技术指标（已算好）
        "close": 26.74, "ma5": 26.1, "ma20": 25.8, "ma60": 24.9,
        "rsi14": 55.2, "macd_dif": 0.12, "macd_dea": 0.08,
        "vol_ratio": 0.82, "turnover": 3.2, "atr": 1.45,
    },
    "intraday": {...} | None,           # 盘中快照
    "news_titles": ["...", "..."],      # 最多 10 条新闻标题
    "signals": ["...", "..."],          # 规则触发的信号
}
```

### 5.3 system_prompt 全文

```
你是一名严谨的 A 股量化分析师，负责在已有量化规则结论的基础上，做综合研判。

## 你的角色定位
你不是预测机器，而是"证据审查员"。你的价值在于：
1. 识别规则结论与数据之间是否存在矛盾
2. 发现规则未覆盖的异常信号组合（如放量滞涨、缩量上涨、逆势走强）
3. 结合新闻标题做语义理解（规则只能做关键词计数，你能读懂实际含义）
4. 指出规则结论的薄弱环节与风险点

## 硬性约束（违反即视为无效输出）
1. **禁止计算任何数字**。所有数值（涨跌幅、得分、区间、均线、量比等）已由规则系统算好并提供给你。你只能引用，绝不能自己计算、推算或估算。
2. **禁止编造数据**。不得引入输入中未提供的信息（如"公司近期获得大单"、"机构调研频繁"等）。如果输入中没有相关信息，就不要提及。
3. **必须引用具体数据**。每条判断都要指明依据的是哪个字段的哪个数值，例如"相对均价 +1.95%"、"量比 0.82"。
4. **不确定就说不确定**。如果数据不足以支撑判断，明确说明"数据不足，无法判断XX"，不要强行给出结论。
5. **禁止给出投资建议**。不出现"建议买入"、"可以加仓"、"应当止损"等表述。只做客观分析。

## 输入数据说明
用户消息是一个 JSON，包含：
- rule_scores: 规则系统各维度得分（每项含 score 与 evidence）
- rule_total / rule_direction / rule_range / rule_confidence: 规则系统的综合结论
- indicators: 关键技术指标（收盘价、均线、RSI、MACD、量比、换手率、ATR）
- intraday: 盘中实时快照（若非盘中则为 null），含最新价、涨跌幅、相对均价、日内位置、盘中量比、30分钟动量
- news_titles: 近期新闻标题列表
- signals: 规则触发的信号列表

## 输出要求
只输出一个 JSON 对象，不要有任何其他文字、不要用 markdown 代码块包裹。格式：

{
  "direction": "看涨" | "看跌" | "震荡",
  "confidence": 0-100 的整数,
  "reasoning": "150-300 字的研判理由，必须引用具体数值",
  "risk_flags": ["风险点1", "风险点2"]
}

字段说明：
- direction: 你的独立研判方向。可以与规则结论一致，也可以不一致——如果数据确实指向相反方向，请如实给出并说明理由。
- confidence: 你对自身研判的置信度。数据充分且信号一致时给高分（70-85），信号矛盾或数据缺失时给低分（30-50）。不要给 90 以上。
- reasoning: 必须包含至少 3 处具体数值引用。说明你同意或不同意规则结论的理由。
- risk_flags: 0-3 条风险提示，每条不超过 30 字。若无明显风险，返回空数组。
```

### 5.4 user message 构造

```python
message = json.dumps(context, ensure_ascii=False, indent=2)
```

### 5.5 输出 JSON schema 与解析

```json
{
  "direction": "看涨",
  "confidence": 72,
  "reasoning": "现价 26.71 相对全天均价 26.20 高出 1.95%，日内位置 63% 处于中上部，显示买方成本占优。但盘中量比仅 0.82，属于缩量上涨，上攻动能不足。RSI 55.2 处于中性区间，未超买。综合判断短期偏强但空间有限。",
  "risk_flags": ["缩量上涨，量能不足", "5日累计涨幅已达 8%，短线有回调压力"]
}
```

**解析实现**：
```python
def _parse_llm_json(content: str) -> dict | None:
    """容错解析 LLM 返回的 JSON。"""
    # 1. 直接尝试 json.loads
    # 2. 剥离 ```json ... ``` 代码块后重试
    # 3. 用正则提取第一个 { 到最后一个 } 之间内容后重试
    # 4. 全部失败返回 None
```

**字段校验**：
- `direction` 必须 ∈ {"看涨", "看跌", "震荡"}，否则视为解析失败
- `confidence` 必须为 int 且在 0-100，否则裁剪/置 None
- `reasoning` 必须为非空字符串
- `risk_flags` 必须为 list，非 list 时置 `[]`

### 5.6 降级策略

| 失败场景 | 处理 |
|---|---|
| LLM 调用异常/超时 | `available=False, error="LLM 调用失败: ..."`，`predict` 仍返回成功（规则结果） |
| 返回内容为空 | `available=False, error="LLM 返回空结果"` |
| JSON 解析失败（3 次容错后） | `available=False, error="LLM 输出格式无法解析"`，**保留原始文本**到 `raw` 字段供排查 |
| 字段缺失/类型错误 | 缺失字段置 `None`，其余可用字段保留，`available=True` 但 `partial=True` |
| 平台未配置（无 API key） | `available=False, error="模型未配置"` |

**超时保护**：`ModelChatTool` 本身无超时参数，需用 `threading.Thread` + `join(timeout)` 包装，超时后放弃等待（线程无法强制终止，但主流程不再阻塞）。

### 5.7 conflict 判定

```python
conflict = (rule_direction != llm_direction) and (rule_direction != "震荡") and (llm_direction != "震荡")
```

即：只有**规则和 LLM 给出相反的明确方向**（一看涨一看跌）才算冲突。一方震荡不算冲突（震荡本身是"不确定"，与明确方向不构成对立）。

---

## 6. 前端展示方案

### 6.1 布局（自上而下）

```
┌──────────────────────────────────────┐
│ [搜索框]  [自选股 chips]              │
├──────────────────────────────────────┤
│ 状态条：                              │
│   非盘中：次日预测 · 数据截至 10-29   │
│   盘中：  🔴 盘中实时 · 26.71  +2.14% │
│          更新于 11:30                 │
│   午休：  🟡 午间休市 · 数据截至11:30 │
├──────────────────────────────────────┤
│ 模式切换：[规则] [规则+AI] [AI研判]   │
├──────────────────────────────────────┤
│ 主卡片：方向 / 区间 / 得分 / 置信度   │
│   （conflict 时顶部红色警示条）       │
├──────────────────────────────────────┤
│ K线图（60日 + 预测区间虚线带）        │
├──────────────────────────────────────┤
│ 维度得分条：                          │
│   量价技术面  ████████░░  +12.5       │
│   盘中走势    ██████░░░░  +8.2  ←新增 │
│   资金结构面  ███░░░░░░░  -5.0        │
│   ...                                 │
├──────────────────────────────────────┤
│ 触发信号（黄色高亮）                  │
├──────────────────────────────────────┤
│ 【AI 研判】卡片（仅 mode≠rule 且 llm  │
│   可用时显示）                        │
│   方向：看涨  置信度：72              │
│   理由：...                           │
│   风险：· 缩量上涨  · 短线回调压力    │
│   ⚠️ AI 生成，仅供参考                │
├──────────────────────────────────────┤
│ [问 Agent] 按钮                       │
├──────────────────────────────────────┤
│ 免责声明（常驻）                      │
└──────────────────────────────────────┘
```

### 6.2 关键实现约束（踩坑经验）

1. **模板调用的所有函数必须在 methods/computed**（v0.1.2 教训）：
   - 新增 `sessionBadgeClass()`、`sessionBadgeText()`、`modeLabel()`、`conflictText()` 全部定义为 methods
2. **根 div 必须 `height:100%; overflow:auto; box-sizing:border-box`**（v0.1.3 教训）
3. **所有新增渲染路径做空值防护**：
   - `intraday` 为 null → 不渲染盘中区块
   - `llm` 为 null → 不渲染 AI 卡片
   - `llm.reasoning` 为 null → 显示"暂无研判理由"
4. **模式切换触发重新请求**：`this.mode = m; this.doSearch()`，注意防抖（避免快速切换重复请求）
5. **单文件自包含**：不引入新依赖，SVG 图标内联

### 6.3 盘中数据展示细节

盘中区块（`v-if="result.intraday"`）展示为 2×3 网格：

| 相对均价 | 日内位置 | 盘中量比 |
|---|---|---|
| +1.95% | 63% | 0.82 |
| **相对开盘** | **振幅** | **30分钟** |
| +2.14% | 7.2% | +1.10% |

颜色规则：正值红（A股习惯）、负值绿、`None` 显示 `--`。

---

## 7. 实现清单（供 task-20/21 使用）

### 7.1 新增函数（plugin/api.py）

| 函数 | 位置建议 | 说明 |
|---|---|---|
| `is_trading_session(now=None)` | 工具函数区 | 返回 `(bool, session_state)` |
| `get_intraday(symbol)` | 数据接入层 | 盘中快照 |
| `_score_intraday(snap)` | 五维打分后 | 盘中打分 |
| `_llm_judge(context, timeout=60)` | 五维打分后 | LLM 研判 |
| `_parse_llm_json(content)` | 同上 | 容错 JSON 解析 |
| `_build_llm_context(...)` | 同上 | 构造 context |

### 7.2 修改点

| 位置 | 修改 |
|---|---|
| `WEIGHTS` | 新增 `WEIGHTS_INTRADAY` 常量（盘中权重表） |
| `DIM_NAMES` | 新增 `"intraday": "盘中走势"` |
| `CACHE_TTL` | 新增 `"intraday": 45` |
| `predict()` | 新增 `mode` 参数 + 盘中分支 + LLM 分支 |
| `PUBLIC_FUNCTIONS` | 新增 `get_intraday` |
| `config.yaml` | `version` → `0.1.4` |
| `tools/stock_predict.py` | 参数新增 `mode`（enum，默认 rule） |
| `frontend/sidebar_view.js` | 见第 6 节 |

### 7.3 测试要点

1. 非盘中 `mode="rule"` 输出与 v0.1.3 逐字段对比一致
2. 盘中 `mode="rule"` 返回 `is_intraday=True` + `intraday` 数据合理
3. `mode="hybrid"` 返回 `rule_result` + `llm`，JSON 解析成功
4. LLM 不可用时 `predict` 仍 `success=True`
5. `_score_intraday` 对 `None` 字段的容错
6. 前端 jsdom 三场景渲染（非盘中/盘中/冲突）
7. `daily_report` 性能不退化（默认 rule 不调 LLM）
