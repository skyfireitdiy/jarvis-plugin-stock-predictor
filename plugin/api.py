# -*- coding: utf-8 -*-
"""
stock-predictor 插件数据层（运行于 gateway，供 Agent 工具与前端调用）。

设计原则：**本模块只负责"取数据"与"算指标"，不做任何主观判断。**
所有打分、权重、方向判定、涨跌幅区间、置信度均由 Agent（大模型）基于
本模块提供的原始数据自行分析得出。理由：
  1. 大模型能理解语义（新闻、题材、盘口异动），规则引擎只能做关键词计数；
  2. 分析口径可随市场环境调整，无需改代码；
  3. 避免"规则结论"束缚 Agent 的判断。

职责：
  1. A股数据接入（akshare 多源：东财主源 + 新浪/腾讯备用源，含缓存/重试/降级）
  2. 技术指标计算（MA/MACD/RSI/BOLL/量比/换手/ATR）——纯数学计算，不含判断
  3. 盘中实时快照（新浪分时主源 + 腾讯逐笔备用）
  4. get_snapshot() 汇总全部原始数据，一次性交给 Agent
  5. get_snapshot_json() 同上但纯 JSON 化，供前端侧边栏展示

安全边界：本文件属于插件私有功能，不直接暴露给 Agent；
仅 PUBLIC_FUNCTIONS 白名单内的函数可被 gateway 前端代理调用。
"""
import time
import math
import threading
import datetime as dt
from functools import wraps

import numpy as np
import pandas as pd

try:
    import akshare as ak
except Exception:  # pragma: no cover
    ak = None
try:
    import requests as _requests
except Exception:  # pragma: no cover
    _requests = None

# ---------------------------------------------------------------------------
# 常量与配置
# ---------------------------------------------------------------------------
DISCLAIMER = (
    "⚠️ 以上数据仅为公开行情信息的客观汇总，不构成任何投资建议。"
    "股市有风险，入市需谨慎，据此操作盈亏自负。"
)

# 缓存 TTL（秒）
CACHE_TTL = {
    "kline": 30 * 60,       # K线 30 分钟
    "fund_flow": 20 * 60,   # 主力资金 20 分钟
    "index": 20 * 60,       # 大盘指数 20 分钟
    "news": 10 * 60,        # 新闻 10 分钟
    "zt_pool": 15 * 60,     # 涨停池 15 分钟
    "margin": 60 * 60,      # 两融 1 小时
    "unlock": 60 * 60,      # 解禁 1 小时
    "north": 30 * 60,       # 北向 30 分钟
    "intraday": 45,         # 盘中分时 45 秒（1 分钟数据更新粒度，45s 内重复请求无意义）
}

_cache = {}
_cache_lock = threading.Lock()

# 注：同花顺全市场资金流快照(stock_fund_flow_individual)内部需翻 100+ 页爬全市场
# 5000+ 行，实测约 60s+，且会抢占网络拖慢其它请求，故不纳入自动降级路径。
# 东财资金流失败时直接返回空，由 Agent 自行判断"资金数据缺失"。
# 全市场类数据模块级缓存（北向/两融/涨停池/指数/解禁），避免批量场景逐只重复请求
_margin_cache = {"data": None, "ts": 0}
_north_cache = {"data": None, "ts": 0}
_zt_cache = {"data": None, "ts": 0}
_index_cache = {"data": None, "ts": 0}
_unlock_cache = {"data": None, "ts": 0}
_kline_cache = {"data": {}}  # code -> {"df": DataFrame, "ts": float}
_intraday_cache = {"data": {}, "ts": {}}  # code -> snapshot dict, TTL 45s（仅盘中缓存）
_news_cache = {"data": {}, "ts": {}}  # code -> DataFrame, TTL 5min
_name_cache = {"data": {}, "ts": {}}  # code -> 股票名称(str)，TTL 24h（名称几乎不变）

# 单只股票快照构建锁：get_snapshot 是同步阻塞函数（约 10s），而 gateway 的
# function-call 端点直接在当前事件循环里同步执行它。若前端同时发起两次快照请求
# （如连点两只自选股），第二个请求会阻塞整个网关事件循环，表现为"网关卡死"。
# 这里用锁把并发快照串行化，后来者立即返回 busy，避免长时间占用事件循环。
_snapshot_lock = threading.Lock()

# 快照分块与依赖关系（供 get_snapshot 按需取数，避免只取一个维度却拉全部数据）
SNAPSHOT_SECTIONS = (
    "kline", "intraday", "fund_flow", "index", "zt_pool",
    "news", "margin", "north_flow", "unlock",
)
# 各分块依赖的其它分块（如资金面需要北向资金）
SECTION_DEPS = {"fund": ("fund_flow", "north_flow")}


# ---------------------------------------------------------------------------
# 工具函数：缓存 / 重试 / 归一化
# ---------------------------------------------------------------------------
def cached(key_type, key_extra=""):
    """带 TTL 的内存缓存装饰器。"""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            extra = key_extra
            if not extra and len(args) > 1:
                extra = str(args[1])
            cache_key = f"{key_type}:{extra}"
            now = time.time()
            with _cache_lock:
                hit = _cache.get(cache_key)
                if hit and now - hit[0] < CACHE_TTL.get(key_type, 300):
                    return hit[1]
            result = fn(*args, **kwargs)
            if result is not None and not (hasattr(result, "empty") and result.empty):
                with _cache_lock:
                    _cache[cache_key] = (now, result)
            return result
        return wrapper
    return deco


def with_retry(times=3, delay=1.5):
    """简单重试装饰器，捕获异常后重试。"""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            last = None
            for i in range(times):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:  # noqa: BLE001
                    last = e
                    if i < times - 1:
                        time.sleep(delay)
            raise last
        return wrapper
    return deco


def _parse_money(s):
    """解析中文金额字符串为数值（元），支持 '2.02亿' / '8887.50万' / '1234'。"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip().replace(",", "")
    try:
        if t.endswith("亿"):
            return float(t[:-1]) * 1e8
        if t.endswith("万"):
            return float(t[:-1]) * 1e4
        return float(t)
    except Exception:  # noqa: BLE001
        return None


def norm_symbol(symbol):
    """把 '600519' / 'sh600519' / '600519.SH' 归一化为纯 6 位代码。"""
    s = str(symbol).strip().upper()
    for suf in (".SH", ".SZ", ".BJ"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    for pre in ("SH", "SZ", "BJ"):
        if s.startswith(pre):
            s = s[len(pre):]
    return s.zfill(6)


def exchange_prefix(symbol):
    """返回新浪源所需的交易所前缀，如 600519 -> sh600519。"""
    s = norm_symbol(symbol)
    if s.startswith(("6", "9")):
        return "sh" + s
    if s.startswith(("4", "8")):
        return "bj" + s
    return "sz" + s


def _get_stock_name(symbol):
    """获取 A 股股票名称（新浪 hq.sinajs.cn 单只实时行情，极轻量 0.02s）。

    返回格式：var hq_str_sh600519="贵州茅台,1363.000,..."，首字段即名称。
    带模块级缓存（名称几乎不变，TTL 24h）。失败返回 None，不抛异常。
    """
    code = norm_symbol(symbol)
    now = time.time()
    if code in _name_cache["data"] and now - _name_cache["ts"].get(code, 0) < 24 * 3600:
        return _name_cache["data"][code]
    if _requests is None:
        return None
    try:
        r = _requests.get(
            "https://hq.sinajs.cn/list=" + exchange_prefix(code),
            headers={
                "Referer": "https://finance.sina.com.cn",
                "User-Agent": "Mozilla/5.0",
            },
            timeout=5,
        )
        if r.status_code == 200:
            # 形如 var hq_str_sh600519="贵州茅台,1363.000,...";
            text = r.text
            start = text.find('"')
            end = text.rfind('"')
            if start != -1 and end > start:
                fields = text[start + 1 : end].split(",")
                name = fields[0].strip() if fields else ""
                if name:
                    _name_cache["data"][code] = name
                    _name_cache["ts"][code] = now
                    return name
    except Exception:  # noqa: BLE001
        pass
    return None


def _safe_float(v, ndigits=None):
    """把任意值安全转为 float，NaN/None/异常返回 None。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, ndigits) if ndigits is not None else f


# ---------------------------------------------------------------------------
# 交易时段判定
# ---------------------------------------------------------------------------
def is_trading_session(now=None):
    """判断当前是否 A 股盘中，返回 (是否盘中, session_state)。

    session_state 枚举：
      pre_open    开盘前（< 09:30）
      trading     连续竞价（09:30-11:30, 13:00-15:00）
      lunch_break 午间休市（11:30-13:00）—— 仍视为盘中，数据是当天的
      closed      收盘后（>= 15:00）或周末
      no_data     数据校验失败（节假日/停牌），由 get_intraday 兜底设置

    注意：本函数只做本地时钟判定，无法识别法定节假日；
    节假日由 get_intraday 的分时数据末行日期校验兜底。
    """
    if now is None:
        now = dt.datetime.now()
    # 周末
    if now.weekday() >= 5:
        return False, "closed"
    t = now.time()
    t_0930 = dt.time(9, 30)
    t_1130 = dt.time(11, 30)
    t_1300 = dt.time(13, 0)
    t_1500 = dt.time(15, 0)
    if t < t_0930:
        return False, "pre_open"
    if t < t_1130:
        return True, "trading"
    if t < t_1300:
        return True, "lunch_break"
    if t < t_1500:
        return True, "trading"
    return False, "closed"


def _calc_trade_minutes(now=None):
    """计算当前已交易分钟数（午休不计，上限 240）。"""
    if now is None:
        now = dt.datetime.now()
    t = now.time()
    minutes = 0
    # 上午 09:30-11:30
    if t > dt.time(9, 30):
        end_am = min(t, dt.time(11, 30))
        minutes += max(0, int((dt.datetime.combine(dt.date.today(), end_am)
                               - dt.datetime.combine(dt.date.today(), dt.time(9, 30))).total_seconds() // 60))
    # 下午 13:00-15:00
    if t > dt.time(13, 0):
        end_pm = min(t, dt.time(15, 0))
        minutes += max(0, int((dt.datetime.combine(dt.date.today(), end_pm)
                               - dt.datetime.combine(dt.date.today(), dt.time(13, 0))).total_seconds() // 60))
    return min(240, minutes)


# ---------------------------------------------------------------------------
# 盘中实时数据层
# ---------------------------------------------------------------------------
def _intraday_unavailable(code, session_state, reason):
    """构造 available=False 的盘中快照。"""
    return {
        "available": False,
        "reason": reason,
        "symbol": code,
        "session_state": session_state,
        "trade_date": None,
        "updated_at": None,
        "source": None,
        "partial": False,
        "prev_close": None,
        "price": None,
        "chg_pct": None,
        "open": None,
        "high": None,
        "low": None,
        "vwap": None,
        "chg_vs_open": None,
        "chg_vs_vwap": None,
        "pos_in_range": None,
        "vol_ratio_intraday": None,
        "mom30": None,
        "trade_minutes": 0,
        "amplitude": None,
    }


def _get_prev_close_and_volma5(code, trade_date=None):
    """从日K取昨收与 5 日均量。

    注意：不同数据源行为不一致——新浪日K盘中通常不含当日，但东财日K盘中
    可能已含当日（当日为未完成K线）。因此**不能假设末行即昨收**，必须按
    trade_date 显式排除当日，否则 prev_close 会被取成现价，导致 chg_pct≈0。
    """
    prev_close, vol_ma5 = None, None
    try:
        k = get_kline(code, 30)
        if k is not None and not k.empty:
            kk = k.copy()
            if "date" in kk.columns:
                kk["_d"] = pd.to_datetime(kk["date"], errors="coerce").dt.date
                if trade_date is not None:
                    kk = kk[kk["_d"] < trade_date]
                elif len(kk) >= 2:
                    # 未指定交易日时，若末行日期 >= 今天，视为当日未完成K线并剔除
                    kk = kk[kk["_d"] < dt.date.today()]
            if not kk.empty:
                prev_close = _safe_float(kk["close"].iloc[-1])
                if len(kk) >= 5:
                    vol_ma5 = _safe_float(kk["volume"].tail(5).mean())
    except Exception:  # noqa: BLE001
        pass
    return prev_close, vol_ma5


def _intraday_from_minute(code, df):
    """由新浪分时 DataFrame 计算完整盘中快照。"""
    today = dt.date.today()
    df = df.copy()
    df["day"] = pd.to_datetime(df["day"], errors="coerce")
    df = df.dropna(subset=["day"])
    if df.empty:
        return None
    trade_date = df["day"].dt.date.max()
    if trade_date != today:
        return None  # 非交易日/停牌，交由调用方处理
    cur = df[df["day"].dt.date == trade_date].copy()
    if cur.empty:
        return None
    # 注意：新浪分时的 volume/amount 列是**字符串**，必须先转数值，
    # 否则 sum() 会做字符串拼接，导致 VWAP / 盘中量比出现荒谬值（如量比 131）。
    for c in ("open", "high", "low", "close", "volume", "amount"):
        if c in cur.columns:
            cur[c] = pd.to_numeric(cur[c], errors="coerce")
    cur = cur.dropna(subset=["close"])
    if cur.empty:
        return None

    prev_close, vol_ma5 = _get_prev_close_and_volma5(code, trade_date)
    price = _safe_float(cur["close"].iloc[-1])
    openp = _safe_float(cur["open"].iloc[0])
    high = _safe_float(cur["high"].max())
    low = _safe_float(cur["low"].min())
    vol_sum = _safe_float(cur["volume"].sum())
    amt_sum = _safe_float(cur["amount"].sum())
    vwap = (amt_sum / vol_sum) if (vol_sum and vol_sum > 0 and amt_sum) else None

    chg_pct = ((price / prev_close - 1) * 100) if (price and prev_close) else None
    chg_vs_open = ((price / openp - 1) * 100) if (price and openp) else None
    chg_vs_vwap = ((price / vwap - 1) * 100) if (price and vwap) else None
    if high is not None and low is not None and price is not None:
        pos_in_range = 50.0 if high == low else (price - low) / (high - low) * 100
    else:
        pos_in_range = None
    amplitude = ((high - low) / prev_close * 100) if (high is not None and low is not None and prev_close) else None

    trade_minutes = _calc_trade_minutes()
    vol_ratio_intraday = None
    if vol_sum and vol_ma5 and vol_ma5 > 0 and trade_minutes > 0:
        vol_ratio_intraday = vol_sum / (vol_ma5 * trade_minutes / 240.0)

    mom30 = None
    if len(cur) >= 31:
        p30 = _safe_float(cur["close"].iloc[-31])
        if p30 and price:
            mom30 = (price / p30 - 1) * 100

    _, session_state = is_trading_session()
    return {
        "available": True,
        "reason": None,
        "symbol": code,
        "session_state": session_state,
        "trade_date": str(trade_date),
        "updated_at": cur["day"].iloc[-1].strftime("%H:%M:%S"),
        "source": "sina_minute",
        "partial": False,
        "prev_close": _safe_float(prev_close, 2),
        "price": _safe_float(price, 2),
        "chg_pct": _safe_float(chg_pct, 2),
        "open": _safe_float(openp, 2),
        "high": _safe_float(high, 2),
        "low": _safe_float(low, 2),
        "vwap": _safe_float(vwap, 3),
        "chg_vs_open": _safe_float(chg_vs_open, 2),
        "chg_vs_vwap": _safe_float(chg_vs_vwap, 2),
        "pos_in_range": _safe_float(pos_in_range, 1),
        "vol_ratio_intraday": _safe_float(vol_ratio_intraday, 2),
        "mom30": _safe_float(mom30, 2),
        "trade_minutes": trade_minutes,
        "amplitude": _safe_float(amplitude, 2),
    }


def _intraday_from_tick(code, df):
    """由腾讯逐笔 DataFrame 计算精简盘中快照（仅最新价，其余字段 None）。"""
    today = dt.date.today()
    if df is None or df.empty:
        return None
    # 腾讯逐笔列：成交时间/成交价格/价格变动/成交量/成交额/性质
    price_col = "成交价格" if "成交价格" in df.columns else None
    if price_col is None:
        return None
    price = _safe_float(pd.to_numeric(df[price_col], errors="coerce").dropna().iloc[-1]) \
        if pd.to_numeric(df[price_col], errors="coerce").notna().any() else None
    if price is None:
        return None
    updated_at = None
    if "成交时间" in df.columns:
        t = str(df["成交时间"].iloc[-1]).strip()
        updated_at = t if ":" in t else None
    prev_close, _ = _get_prev_close_and_volma5(code)
    chg_pct = ((price / prev_close - 1) * 100) if (price and prev_close) else None
    _, session_state = is_trading_session()
    return {
        "available": True,
        "reason": None,
        "symbol": code,
        "session_state": session_state,
        "trade_date": str(today),
        "updated_at": updated_at,
        "source": "tencent_tick",
        "partial": True,
        "prev_close": _safe_float(prev_close, 2),
        "price": _safe_float(price, 2),
        "chg_pct": _safe_float(chg_pct, 2),
        "open": None,
        "high": None,
        "low": None,
        "vwap": None,
        "chg_vs_open": None,
        "chg_vs_vwap": None,
        "pos_in_range": None,
        "vol_ratio_intraday": None,
        "mom30": None,
        "trade_minutes": _calc_trade_minutes(),
        "amplitude": None,
    }


def get_intraday(symbol):
    """盘中实时快照（主源新浪分时，降级腾讯逐笔）。

    非盘中（pre_open/closed）直接返回 available=False，不请求不缓存。
    盘中数据缓存 45s（1 分钟数据更新粒度，45s 内重复请求无意义）。
    """
    code = norm_symbol(symbol)
    in_session, session_state = is_trading_session()
    if not in_session:
        return _intraday_unavailable(code, session_state, "非交易时段")

    # 缓存命中
    now_ts = time.time()
    cached = _intraday_cache["data"].get(code)
    if cached is not None and now_ts - _intraday_cache["ts"].get(code, 0) < CACHE_TTL["intraday"]:
        return cached

    if ak is None:
        return _intraday_unavailable(code, "no_data", "akshare 不可用")

    prefixed = exchange_prefix(code)

    # 主源：新浪分时
    try:
        df = with_retry(1, 0.5)(ak.stock_zh_a_minute)(
            symbol=prefixed, period="1", adjust=""
        )
        snap = _intraday_from_minute(code, df) if df is not None and not df.empty else None
        if snap is not None:
            _intraday_cache["data"][code] = snap
            _intraday_cache["ts"][code] = now_ts
            return snap
        # 数据末行不是今天 → 非交易日/停牌
        return _intraday_unavailable(code, "no_data", "非交易日或停牌")
    except Exception:  # noqa: BLE001
        pass

    # 降级：腾讯逐笔（仅最新价）
    try:
        df = with_retry(1, 0.5)(ak.stock_zh_a_tick_tx_js)(symbol=prefixed)
        snap = _intraday_from_tick(code, df)
        if snap is not None:
            _intraday_cache["data"][code] = snap
            _intraday_cache["ts"][code] = now_ts
            return snap
    except Exception:  # noqa: BLE001
        pass

    return _intraday_unavailable(code, "no_data", "盘中数据源均不可用")


# ---------------------------------------------------------------------------
# 数据接入层（多源降级）
# ---------------------------------------------------------------------------
def get_kline(symbol, days=120):
    """获取日K线。优先东财源(stock_zh_a_hist)，失败降级新浪源(stock_zh_a_daily)。

    统一列：date, open, high, low, close, volume, amount, turnover
    带模块级缓存，同一次运行内对同一代码只拉一次。
    """
    if ak is None:
        return pd.DataFrame()
    code = norm_symbol(symbol)
    now = time.time()
    if _kline_cache["data"].get(code) is not None and now - _kline_cache["data"][code]["ts"] < CACHE_TTL["kline"]:
        return _kline_cache["data"][code]["df"].tail(days).reset_index(drop=True)
    end = dt.date.today()
    start = end - dt.timedelta(days=int(days * 1.7) + 30)
    s, e = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

    # 东财主源（当前东财常限流，重试1次快速失败，直接降级新浪）
    try:
        df = with_retry(1, 0.5)(ak.stock_zh_a_hist)(
            symbol=code, period="daily", start_date=s, end_date=e, adjust="qfq"
        )
        if df is not None and not df.empty:
            df = df.rename(columns={
                "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
                "最低": "low", "成交量": "volume", "成交额": "amount",
                "换手率": "turnover",
            })
            df = df[["date", "open", "close", "high", "low", "volume", "amount", "turnover"]]
            df["turnover"] = pd.to_numeric(df["turnover"], errors="coerce")
            _kline_cache["data"][code] = {"df": df, "ts": now}
            return df.tail(days).reset_index(drop=True)
    except Exception:  # noqa: BLE001
        pass

    # 新浪备用源
    try:
        df = with_retry(1, 0.5)(ak.stock_zh_a_daily)(
            symbol=exchange_prefix(code), start_date=s, end_date=e, adjust="qfq"
        )
        if df is not None and not df.empty:
            df = df.reset_index(drop=True)
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            # 新浪源 turnover 为小数（0.0031=0.31%），统一转成与东财一致的百分数口径
            if "turnover" not in df.columns:
                df["turnover"] = np.nan
            else:
                df["turnover"] = pd.to_numeric(df["turnover"], errors="coerce") * 100
            _kline_cache["data"][code] = {"df": df, "ts": now}
            return df.tail(days).reset_index(drop=True)
    except Exception:  # noqa: BLE001
        pass
    return pd.DataFrame()


def get_kline_json(symbol, days=120):
    """K线数据（JSON 版，供前端图表绘制）。

    get_kline 返回 pandas DataFrame，无法经 function-call 端点 JSON 序列化
    （后端只对 dict 返回值做处理），故此处转为纯 dict 结构。
    成功返回 records 数组，date 统一为 "YYYY-MM-DD" 字符串。
    """
    code = norm_symbol(symbol)
    df = get_kline(code, days)
    if df is None or df.empty:
        return {
            "success": False,
            "error": f"无法获取股票 {code} 的K线数据（数据源均不可用或代码无效）",
        }
    records = []
    for _, row in df.iterrows():
        records.append({
            "date": str(row.get("date", ""))[:10],
            "open": round(float(row.get("open")), 2) if pd.notna(row.get("open")) else None,
            "close": round(float(row.get("close")), 2) if pd.notna(row.get("close")) else None,
            "high": round(float(row.get("high")), 2) if pd.notna(row.get("high")) else None,
            "low": round(float(row.get("low")), 2) if pd.notna(row.get("low")) else None,
            "volume": int(row.get("volume")) if pd.notna(row.get("volume")) else None,
            "amount": round(float(row.get("amount")), 0) if pd.notna(row.get("amount")) else None,
            "turnover": round(float(row.get("turnover")), 2) if pd.notna(row.get("turnover")) else None,
        })
    return {"success": True, "symbol": code, "days": len(records), "kline": records}


def get_fund_flow(symbol, days=10):
    """主力资金流向。东财源(stock_individual_fund_flow)，失败返回空。

    东财源返回历史趋势（含主力净流入净占比）；失败时资金维度中性处理。
    注：曾尝试降级同花顺全市场快照，但该接口需翻 100+ 页爬全市场（约 60s+），
    不适合做自动降级源，故东财失败直接返回空。
    """
    if ak is None:
        return pd.DataFrame()
    code = norm_symbol(symbol)
    market = "sh" if code.startswith(("6", "9")) else "sz"

    # 东财主源（历史趋势，快速重试1次）
    try:
        df = with_retry(1, 0.5)(ak.stock_individual_fund_flow)(stock=code, market=market)
        if df is not None and not df.empty:
            return df.tail(days).reset_index(drop=True)
    except Exception:  # noqa: BLE001
        pass
    return pd.DataFrame()


def get_index_kline(symbol="sh000001", days=60):
    """大盘指数日K（默认上证指数）。模块级缓存避免 daily_report 逐只重复请求。"""
    if ak is None:
        return pd.DataFrame()
    now = time.time()
    if _index_cache["data"] is not None and now - _index_cache["ts"] < CACHE_TTL["kline"]:
        return _index_cache["data"].tail(days).reset_index(drop=True)
    try:
        df = with_retry(2, 1.0)(ak.stock_zh_index_daily)(symbol=symbol)
        if df is not None and not df.empty:
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            _index_cache["data"] = df
            _index_cache["ts"] = now
            return df.tail(days).reset_index(drop=True)
    except Exception:  # noqa: BLE001
        pass
    return pd.DataFrame()


def get_zt_pool(date=""):
    """涨停股池（当日涨停家数/强度），失败返回空。模块级缓存避免重复请求。"""
    if ak is None:
        return pd.DataFrame()
    if not date:
        date = dt.date.today().strftime("%Y%m%d")
    now = time.time()
    if _zt_cache["data"] is not None and now - _zt_cache["ts"] < CACHE_TTL["zt_pool"]:
        return _zt_cache["data"]
    try:
        df = with_retry(2, 1.0)(ak.stock_zt_pool_em)(date=date)
        if df is not None and not df.empty:
            _zt_cache["data"] = df
            _zt_cache["ts"] = now
        return df if df is not None else pd.DataFrame()
    except Exception:  # noqa: BLE001
        return pd.DataFrame()


_news_cache = {"data": {}, "ts": {}}  # code -> DataFrame, TTL 5min，避免同代码重复拉取


def get_news(symbol, limit=10):
    """个股新闻（标题+内容+来源+时间），失败返回空。模块级缓存避免重复请求。"""
    if ak is None:
        return pd.DataFrame()
    code = norm_symbol(symbol)
    now = time.time()
    if code in _news_cache["data"] and now - _news_cache["ts"].get(code, 0) < 5 * 60:
        return _news_cache["data"][code]
    try:
        df = with_retry(2, 1.0)(ak.stock_news_em)(symbol=code)
        if df is not None and not df.empty:
            df = df.head(limit)
            _news_cache["data"][code] = df
            _news_cache["ts"][code] = now
            return df
    except Exception:  # noqa: BLE001
        pass
    return pd.DataFrame()


def get_margin():
    """两市融资融券余额（沪市口径），失败返回空。模块级缓存避免重复请求。"""
    if ak is None:
        return pd.DataFrame()
    now = time.time()
    if _margin_cache["data"] is not None and now - _margin_cache["ts"] < CACHE_TTL["margin"]:
        return _margin_cache["data"]
    try:
        df = with_retry(2, 1.0)(ak.macro_china_market_margin_sh)()
        df = df.tail(10).reset_index(drop=True)
        _margin_cache["data"] = df
        _margin_cache["ts"] = now
        return df
    except Exception:  # noqa: BLE001
        return pd.DataFrame()


def get_north_flow():
    """北向资金汇总，失败返回空。模块级缓存避免重复请求。"""
    if ak is None:
        return pd.DataFrame()
    now = time.time()
    if _north_cache["data"] is not None and now - _north_cache["ts"] < CACHE_TTL["north"]:
        return _north_cache["data"]
    try:
        df = with_retry(2, 1.0)(ak.stock_hsgt_fund_flow_summary_em)()
        _north_cache["data"] = df
        _north_cache["ts"] = now
        return df
    except Exception:  # noqa: BLE001
        return pd.DataFrame()


_unlock_cache = {"data": None, "ts": 0}


def get_unlock(symbol):
    """限售解禁明细（未来60天），失败返回空。底层为全市场明细，模块级缓存避免重复拉取。"""
    if ak is None:
        return pd.DataFrame()
    now = time.time()
    if _unlock_cache["data"] is not None and now - _unlock_cache["ts"] < CACHE_TTL["unlock"]:
        df = _unlock_cache["data"]
        code = norm_symbol(symbol)
        return df[df["股票代码"].astype(str).str.zfill(6) == code] if df is not None and not df.empty else pd.DataFrame()
    try:
        start = dt.date.today().strftime("%Y%m%d")
        end = (dt.date.today() + dt.timedelta(days=60)).strftime("%Y%m%d")
        df = with_retry(2, 1.0)(ak.stock_restricted_release_detail_em)(
            start_date=start, end_date=end
        )
        if df is not None and not df.empty:
            _unlock_cache["data"] = df
            _unlock_cache["ts"] = now
            code = norm_symbol(symbol)
            return df[df["股票代码"].astype(str).str.zfill(6) == code]
    except Exception:  # noqa: BLE001
        pass
    return pd.DataFrame()


# ---------------------------------------------------------------------------
# 技术指标计算（纯数学，不含任何判断）
# ---------------------------------------------------------------------------
def _calc_indicators(df):
    """在日K基础上追加技术指标列。"""
    if df is None or df.empty:
        return df
    out = df.copy()
    for c in ("open", "close", "high", "low", "volume", "amount"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    for n in (5, 10, 20, 60):
        out[f"ma{n}"] = out["close"].rolling(n).mean()
    # 涨跌幅（百分数口径，相对前一交易日收盘）
    out["chg_pct"] = (out["close"] / out["close"].shift(1) - 1) * 100
    # MACD
    ema12 = out["close"].ewm(span=12, adjust=False).mean()
    ema26 = out["close"].ewm(span=26, adjust=False).mean()
    out["dif"] = ema12 - ema26
    out["dea"] = out["dif"].ewm(span=9, adjust=False).mean()
    out["macd"] = (out["dif"] - out["dea"]) * 2
    # RSI(14)
    delta = out["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / (loss.replace(0, np.nan))
    out["rsi14"] = 100 - 100 / (1 + rs)
    # BOLL(20, 2)
    out["boll_mid"] = out["close"].rolling(20).mean()
    out["boll_std"] = out["close"].rolling(20).std()
    out["boll_up"] = out["boll_mid"] + 2 * out["boll_std"]
    out["boll_low"] = out["boll_mid"] - 2 * out["boll_std"]
    # 量比（当日量 / 前5日均量）
    out["vol_ma5"] = out["volume"].rolling(5).mean()
    out["vol_ratio"] = out["volume"] / out["vol_ma5"].shift(1)
    if "turnover" not in out.columns:
        out["turnover"] = np.nan
    else:
        out["turnover"] = pd.to_numeric(out["turnover"], errors="coerce")
    # 换手率（百分数口径）：5日均换手，判断活跃度
    out["turnover_ma5"] = out["turnover"].rolling(5).mean()
    return out


def _last_row(df):
    """取最后一行的技术指标快照。"""
    if df is None or df.empty:
        return pd.Series(dtype=float)
    return df.iloc[-1]


def _compute_atr(df, n=14):
    """平均真实波幅，用于估计次日涨跌幅区间。"""
    if df is None or len(df) < 3:
        return 1.5
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = tr.rolling(n).mean().iloc[-1]
    return float(atr) if not pd.isna(atr) and atr > 0 else 1.5


# ---------------------------------------------------------------------------
# 数据汇总层：get_snapshot —— 一次取齐 Agent 分析所需的全部原始数据
# ---------------------------------------------------------------------------
def _df_to_records(df, limit=None, date_col=None):
    """DataFrame -> list[dict]，日期列统一转字符串，NaN 转 None，便于 JSON 序列化。"""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return []
    d = df.copy()
    if limit:
        d = d.tail(limit)
    out = []
    for _, row in d.iterrows():
        item = {}
        for k, v in row.items():
            key = str(k)
            if isinstance(v, (pd.Timestamp, dt.datetime, dt.date)):
                item[key] = str(v)[:19]
            elif v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
                item[key] = None
            elif isinstance(v, (np.integer,)):
                item[key] = int(v)
            elif isinstance(v, (np.floating,)):
                fv = float(v)
                item[key] = None if (math.isnan(fv) or math.isinf(fv)) else round(fv, 4)
            elif isinstance(v, (np.bool_,)):
                item[key] = bool(v)
            else:
                item[key] = v
        out.append(item)
    return out


def _clean(obj):
    """递归清洗：numpy 类型转原生、NaN/Inf 转 None。"""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (pd.Timestamp, dt.datetime, dt.date)):
        return str(obj)[:19]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        f = float(obj)
        return None if (math.isnan(f) or math.isinf(f)) else round(f, 4)
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else round(obj, 4)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, pd.DataFrame):
        return _df_to_records(obj)
    return obj


def _resolve_sections(sections):
    """把 sections 参数归一化为分块名元组；None/空 表示全部。"""
    if not sections:
        return SNAPSHOT_SECTIONS
    if isinstance(sections, str):
        sections = [s.strip() for s in sections.replace("，", ",").split(",")]
    wanted = []
    for s in sections:
        name = str(s or "").strip()
        if not name:
            continue
        for sec in SECTION_DEPS.get(name, (name,)):
            if sec in SNAPSHOT_SECTIONS and sec not in wanted:
                wanted.append(sec)
    return tuple(wanted)


def get_snapshot(symbol, days=120, sections=None):
    """
    汇总单只股票的全部原始数据，供 Agent 做多维度分析。

    sections: 可选，只取部分数据块（如 ["kline"] / ["fund"]），用于减少耗时与
    网络请求；不传则取全部。未取的分块在返回结构中为 None（data_quality 无对应键）。

    返回结构（各字段失败时为 None / [] / {}，不抛异常）：
    {
      "symbol", "code", "name", "updated_at", "session_state", "is_intraday",
      "disclaimer",
      "intraday": {...} | None,        # 盘中实时快照（非盘中为 None）
      "kline": [ {date,open,high,low,close,volume,turnover,...}, ... ],  # 最近 days 日
      "indicators": {...},             # 最新一日技术指标（纯计算值）
      "fund_flow": {...} | None,       # 个股主力资金流
      "index": {...} | None,           # 大盘指数行情
      "zt_pool": {...} | None,         # 涨停池统计
      "news": [ {title,time,...} ],    # 近期新闻
      "margin": {...} | None,          # 两融余额
      "north_flow": {...} | None,      # 北向资金
      "unlock": {...} | None,          # 解禁信息
      "data_quality": {字段: "ok"/"empty"/"error"}   # 各数据源可用性，供 Agent 判断可信度
    }
    """
    code = norm_symbol(symbol)
    secs = _resolve_sections(sections)
    quality = {}
    now = dt.datetime.now()
    is_intraday, session_state = is_trading_session(now)

    snap = {
        "symbol": code,
        "code": code,
        "name": None,
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "session_state": session_state,
        "is_intraday": is_intraday,
        "disclaimer": DISCLAIMER,
    }
    snap["name"] = _get_stock_name(code)

    # 1) 日K + 技术指标
    df = None
    if "kline" in secs:
        try:
            df = get_kline(code, days=days)
            quality["kline"] = "ok" if (df is not None and not df.empty) else "empty"
        except Exception:
            quality["kline"] = "error"
    if "kline" not in secs:
        snap["kline"] = None
        snap["indicators"] = None
    elif df is not None and not df.empty:
        try:
            ind_df = _calc_indicators(df)
            # kline 带上指标列，Agent 可自行判断形态（如 MA 多头排列、MACD 金叉位置）
            snap["kline"] = _df_to_records(ind_df, limit=days)
            last = _last_row(ind_df)
            latest = {}
            for k, v in last.items():
                if isinstance(k, str):
                    latest[k] = v
            latest["atr14"] = _compute_atr(ind_df, 14)
            snap["indicators"] = _clean(latest)
            quality["indicators"] = "ok"
        except Exception:
            snap["kline"] = _df_to_records(df, limit=days)
            snap["indicators"] = None
            quality["indicators"] = "error"
    else:
        snap["kline"] = []
        snap["indicators"] = None
        quality.setdefault("indicators", "empty")

    # 2) 盘中快照
    if "intraday" in secs:
        try:
            intra = get_intraday(code)
            if intra and intra.get("available"):
                snap["intraday"] = _clean(intra)
                quality["intraday"] = "ok"
            else:
                snap["intraday"] = None
                quality["intraday"] = "not_available"
        except Exception:
            snap["intraday"] = None
            quality["intraday"] = "error"
    else:
        snap["intraday"] = None

    # 3) 个股资金流（近10日主力/超大单/大单/中单/小单净流入）
    if "fund_flow" in secs:
        time.sleep(0.4)  # 数据源间隔，降低东财限流概率
        try:
            ff = get_fund_flow(code)
            snap["fund_flow"] = _df_to_records(ff, limit=10) if ff is not None and not ff.empty else None
            quality["fund_flow"] = "ok" if snap["fund_flow"] else "empty"
        except Exception:
            snap["fund_flow"] = None
            quality["fund_flow"] = "error"
    else:
        snap["fund_flow"] = None

    # 4) 大盘指数（上证近60日，供 Agent 判断大盘环境）
    if "index" in secs:
        time.sleep(0.4)
        try:
            idx = get_index_kline()
            snap["index"] = _df_to_records(idx, limit=60) if idx is not None and not idx.empty else None
            quality["index"] = "ok" if snap["index"] else "empty"
        except Exception:
            snap["index"] = None
            quality["index"] = "error"
    else:
        snap["index"] = None

    # 5) 涨停池（市场情绪面：涨停家数/连板高度/封板资金/所属行业）
    if "zt_pool" not in secs:
        snap["zt_pool"] = None
    else:
        time.sleep(0.4)
        try:
            zt = get_zt_pool()
            zt_recs = _df_to_records(zt, limit=200) if zt is not None and not zt.empty else []
            if zt_recs:
                # 统计摘要 + 明细，Agent 无需自行聚合
                industries = {}
                for r in zt_recs:
                    ind = r.get("所属行业") or "未知"
                    industries[ind] = industries.get(ind, 0) + 1
                boards = [r.get("连板数") for r in zt_recs if isinstance(r.get("连板数"), (int, float))]
                snap["zt_pool"] = {
                    "count": len(zt_recs),
                    "max_board": max(boards) if boards else None,
                    "industry_rank": sorted(
                        [{"industry": k, "count": v} for k, v in industries.items()],
                        key=lambda x: -x["count"],
                    )[:10],
                    "list": zt_recs,
                }
                quality["zt_pool"] = "ok"
            else:
                snap["zt_pool"] = None
                quality["zt_pool"] = "empty"
        except Exception:
            snap["zt_pool"] = None
            quality["zt_pool"] = "error"

    # 6) 新闻（消息面原始文本，交由 Agent 做语义理解）
    if "news" in secs:
        try:
            news = get_news(code)
            snap["news"] = _df_to_records(news, limit=20) if news is not None else []
            quality["news"] = "ok" if snap["news"] else "empty"
        except Exception:
            snap["news"] = []
            quality["news"] = "error"
    else:
        snap["news"] = None

    # 7) 两融（全市场口径，非个股）
    if "margin" in secs:
        try:
            mg = get_margin()
            snap["margin"] = _df_to_records(mg, limit=10) if mg is not None and not mg.empty else None
            quality["margin"] = "ok" if snap["margin"] else "empty"
        except Exception:
            snap["margin"] = None
            quality["margin"] = "error"
    else:
        snap["margin"] = None

    # 8) 北向资金
    if "north_flow" in secs:
        time.sleep(0.4)
        try:
            nf = get_north_flow()
            snap["north_flow"] = _df_to_records(nf, limit=10) if nf is not None and not nf.empty else None
            quality["north_flow"] = "ok" if snap["north_flow"] else "empty"
        except Exception:
            snap["north_flow"] = None
            quality["north_flow"] = "error"
    else:
        snap["north_flow"] = None

    # 9) 解禁（未来60天，该股无解禁时为空列表）
    if "unlock" in secs:
        time.sleep(0.4)
        try:
            ul = get_unlock(code)
            snap["unlock"] = _df_to_records(ul, limit=20) if ul is not None and not ul.empty else []
            quality["unlock"] = "ok" if snap["unlock"] else "none"
        except Exception:
            snap["unlock"] = []
            quality["unlock"] = "error"
    else:
        snap["unlock"] = None

    # 10) 股票名称（从新闻/资金流里取不到时留空，Agent 可用代码检索）
    snap["data_quality"] = quality
    return _clean(snap)


def get_snapshot_json(symbol, days=120, sections=None):
    """get_snapshot 的纯 JSON 版本（供前端侧边栏 function-call 使用）。

    并发保护：本函数在 gateway 事件循环中同步执行，单次约 10s。若前端连点两只
    股票，第二个请求会阻塞整个网关（表现为"网关卡死"）。因此用进程内锁串行化：
    已有快照在构建时，后来者**立即**返回 busy 标记（绝不等待），由前端稍后重试，
    避免第二个请求再次长时间独占事件循环。
    """
    if not _snapshot_lock.acquire(blocking=False):
        return {
            "success": False,
            "busy": True,
            "error": "数据正在加载中，请稍后重试",
            "symbol": norm_symbol(symbol),
        }
    try:
        return get_snapshot(symbol, days=days, sections=sections)
    finally:
        _snapshot_lock.release()


# ---------------------------------------------------------------------------
# 对外暴露函数白名单（gateway 前端可调用）
# ---------------------------------------------------------------------------
PUBLIC_FUNCTIONS = [
    "get_snapshot",
    "get_snapshot_json",
    "get_kline",
    "get_kline_json",
    "get_intraday",
    "get_fund_flow",
    "get_index_kline",
    "get_zt_pool",
    "get_news",
    "get_margin",
    "get_north_flow",
    "get_unlock",
    "watchlist",
    "get_names",
]


def get_names(symbols):
    """
    批量获取股票名称（极轻量，仅调新浪 hq.sinajs.cn 单只行情取名称，不拉行情）。

    供前端自选股列表在打开面板时快速显示名称，避免触发完整行情抓取。
    symbols: 代码列表或逗号分隔字符串。
    返回 {"names": {code: name}, "errors": [...]}
    """
    if isinstance(symbols, str):
        codes = [s.strip() for s in symbols.replace("，", ",").split(",") if s.strip()]
    elif isinstance(symbols, (list, tuple)):
        codes = [str(s).strip() for s in symbols if str(s).strip()]
    else:
        return {"names": {}, "errors": [{"error": "symbols 参数格式不支持"}]}
    names, errors = {}, []
    for c in codes:
        try:
            name = _get_stock_name(c)
            if name:
                names[norm_symbol(c)] = name
            else:
                errors.append({"symbol": c, "error": "未取到名称"})
        except Exception as e:  # noqa: BLE001
            errors.append({"symbol": c, "error": str(e)[:200]})
    return {"names": names, "errors": errors}


def watchlist(symbols):
    """
    批量获取自选股行情快照（前端自选股列表用）。
    symbols: 代码列表或逗号分隔字符串。
    返回 {"items": [ {symbol, name, price, chg_pct, session_state, ...} ], "errors": [...]}
    """
    if isinstance(symbols, str):
        codes = [s.strip() for s in symbols.replace("，", ",").split(",") if s.strip()]
    elif isinstance(symbols, (list, tuple)):
        codes = [str(s).strip() for s in symbols if str(s).strip()]
    else:
        return {"items": [], "errors": [{"error": "symbols 参数格式不支持"}]}

    items, errors = [], []
    is_intraday, session_state = is_trading_session(dt.datetime.now())
    for i, c in enumerate(codes):
        if i > 0:
            time.sleep(0.3)  # 防限流
        try:
            code = norm_symbol(c)
            item = {"symbol": code, "session_state": session_state}
            item["name"] = _get_stock_name(code)
            df = get_kline(code, days=30)
            if df is not None and not df.empty:
                last = df.iloc[-1]
                item["last_close"] = _safe_float(last.get("close"))
                item["date"] = str(last.get("date"))[:10]
                prev = df.iloc[-2] if len(df) >= 2 else None
                if prev is not None:
                    pc = _safe_float(prev.get("close"))
                    if pc and item["last_close"]:
                        item["chg_pct"] = round((item["last_close"] / pc - 1) * 100, 2)
            if is_intraday:
                intra = get_intraday(code)
                if intra and intra.get("available"):
                    item["price"] = intra.get("price")
                    item["chg_pct"] = intra.get("chg_pct")
                    item["intraday"] = True
            if "price" not in item:
                item["price"] = item.get("last_close")
            items.append(item)
        except Exception as e:
            errors.append({"symbol": c, "error": str(e)[:200]})
    return {"items": items, "errors": errors, "session_state": session_state}
