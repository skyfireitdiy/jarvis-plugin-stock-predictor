# -*- coding: utf-8 -*-
"""
stock-predictor 插件私有功能（运行于 gateway，供 Agent 工具薄封装调用）。

职责：
  1. A股数据接入（akshare 多源：东财主源 + 新浪备用源，含缓存/重试/降级）
  2. 技术指标计算（MA/MACD/RSI/BOLL/量比/换手）
  3. 五维打分引擎（量价技术面 40% / 资金结构面 25% / 情绪题材面 20% / 大盘环境 10% / 消息事件面 5%）
  4. 综合输出：方向 + 涨跌幅区间 + 触发信号 + 置信度 + 逐项证据

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

# ---------------------------------------------------------------------------
# 常量与配置
# ---------------------------------------------------------------------------
WEIGHTS = {
    "tech": 0.40,       # 量价技术面
    "fund": 0.25,       # 资金结构面
    "sentiment": 0.20,  # 情绪题材面
    "market": 0.10,     # 大盘环境
    "news": 0.05,       # 消息事件面
}

DIM_NAMES = {
    "tech": "量价技术面",
    "fund": "资金结构面",
    "sentiment": "情绪题材面",
    "market": "大盘环境",
    "news": "消息事件面",
}

DISCLAIMER = (
    "⚠️ 本预测仅为基于公开行情数据的量化参考，不构成任何投资建议。"
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
}

_cache = {}
_cache_lock = threading.Lock()

# 注：同花顺全市场资金流快照(stock_fund_flow_individual)内部需翻 100+ 页爬全市场
# 5000+ 行，实测约 60s+，且会抢占网络拖慢其它请求，故不纳入自动降级路径。
# 东财资金流失败时资金维度中性处理即可（见 get_fund_flow）。
# 全市场类数据模块级缓存（北向 / 两融），避免 daily_report 逐只重复请求
_margin_cache = {"data": None, "ts": 0}
_north_cache = {"data": None, "ts": 0}
_zt_cache = {"data": None, "ts": 0}
_index_cache = {"data": None, "ts": 0}
_kline_cache = {"data": {}}  # code -> {"df": DataFrame, "ts": float}

# 消息面情绪词库（精简版，可扩展）
BULL_KEYWORDS = [
    "增长", "大涨", "涨停", "突破", "创新高", "利好", "中标", "签约",
    "回购", "增持", "预增", "扭亏", "超预期", "放量", "新高", "获准",
    "获批", "战略合作", "订单", "提价", "涨价", "盈利", "分红", "送转",
]
BEAR_KEYWORDS = [
    "下跌", "跌停", "破位", "利空", "减持", "质押", "亏损", "预减",
    "预亏", "处罚", "违规", "立案", "调查", "退市", "警示", "下滑",
    "计提", "商誉", "减持计划", "股东减持", "风险", "纠纷", "诉讼",
]


# ---------------------------------------------------------------------------
# 工具函数：缓存 / 重试
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
# 技术指标计算
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
# 五维打分
# ---------------------------------------------------------------------------
def _score_tech(kline):
    """量价技术面打分 [-100, 100]。"""
    evidence = []
    if kline is None or len(kline) < 20:
        return {"score": 0, "evidence": ["技术面数据不足，中性处理"], "available": False}
    ind = _calc_indicators(kline)
    r = _last_row(ind)
    px = float(r["close"])
    score = 0.0

    # 均线趋势
    trend_pts = 0
    if px > r["ma5"]: trend_pts += 1
    if px > r["ma10"]: trend_pts += 1
    if px > r["ma20"]: trend_pts += 1
    if px > r["ma60"]: trend_pts += 1
    if not pd.isna(r["ma5"]) and not pd.isna(r["ma20"]) and r["ma5"] > r["ma20"]:
        trend_pts += 1
    score += (trend_pts - 2.5) * 10  # 满分±25
    ma_desc = "站上" if px > r["ma20"] else "跌破"
    evidence.append(f"股价{ma_desc}20日线；5/10/20/60日均线多头项 {trend_pts}/5")

    # MACD
    if not pd.isna(r["dif"]) and not pd.isna(r["dea"]):
        if r["dif"] > r["dea"] and r["dif"] > 0:
            score += 12; evidence.append("MACD 金叉且位于零轴上方（多头强势）")
        elif r["dif"] > r["dea"]:
            score += 6; evidence.append("MACD 金叉（短多）")
        elif r["dif"] < r["dea"] and r["dif"] < 0:
            score -= 12; evidence.append("MACD 死叉且位于零轴下方（空头弱势）")
        else:
            score -= 6; evidence.append("MACD 死叉（短空）")

    # RSI
    if not pd.isna(r["rsi14"]):
        rsi = float(r["rsi14"])
        if rsi >= 80: score -= 8; evidence.append(f"RSI={rsi:.0f} 超买，警惕回调")
        elif rsi >= 70: score += 4; evidence.append(f"RSI={rsi:.0f} 偏强")
        elif rsi <= 20: score += 8; evidence.append(f"RSI={rsi:.0f} 超卖，或有反弹")
        elif rsi <= 30: score -= 4; evidence.append(f"RSI={rsi:.0f} 偏弱")
        else: evidence.append(f"RSI={rsi:.0f} 中性")

    # BOLL 位置
    if not pd.isna(r["boll_up"]) and not pd.isna(r["boll_low"]):
        if px > r["boll_up"]: score -= 6; evidence.append("突破布林上轨，短线过热")
        elif px < r["boll_low"]: score += 6; evidence.append("跌破布林下轨，超卖")
        else: evidence.append("布林带内运行")

    # 量比
    if not pd.isna(r["vol_ratio"]):
        vr = float(r["vol_ratio"])
        if vr >= 1.5: score += 5; evidence.append(f"量比={vr:.2f}，显著放量")
        elif vr <= 0.7: score -= 3; evidence.append(f"量比={vr:.2f}，缩量")
        else: evidence.append(f"量比={vr:.2f}，量能平稳")

    # 换手率（中短线活跃度，百分数口径）
    if not pd.isna(r.get("turnover")):
        to = float(r["turnover"])
        if to >= 10:
            score += 4; evidence.append(f"当日换手率 {to:.1f}%，交投极度活跃（短线关注度高）")
        elif to >= 5:
            score += 2; evidence.append(f"当日换手率 {to:.1f}%，交投活跃")
        elif to <= 0.5:
            score -= 3; evidence.append(f"当日换手率 {to:.1f}%，交投清淡，注意流动性")
        else:
            evidence.append(f"当日换手率 {to:.1f}%，交投正常")

    # 近期涨跌（5日动量）
    if len(ind) >= 6:
        mom5 = (px / ind["close"].iloc[-6] - 1) * 100
        if mom5 > 8: score -= 5; evidence.append(f"5日累计涨幅 {mom5:.1f}%，短线涨幅已大")
        elif mom5 < -8: score += 5; evidence.append(f"5日累计跌幅 {mom5:.1f}%，超跌")
        else: evidence.append(f"5日动量 {mom5:+.1f}%")

    return {"score": max(-100, min(100, score)), "evidence": evidence, "available": True}


def _score_fund(fund_df, north_df, margin_df):
    """资金结构面打分。"""
    evidence = []
    score = 0.0
    available = False
    if fund_df is not None and not fund_df.empty:
        available = True
        f = fund_df.copy()
        from_ths = "_source" in f.columns and (f["_source"].fillna("") == "ths").any()
        # 东财源：主力净流入-净占比
        net_col = next((c for c in f.columns if "主力净流入-净占比" in c), None)
        net_amt_col = next((c for c in f.columns if "主力净流入-净额" in c), None)
        if from_ths:
            # 同花顺源：净额为带单位字符串（如 "8887.50万" / "2.02亿"）
            amt = f.iloc[0].get("主力净流入-净额") if net_amt_col else None
            amt_val = _parse_money(amt)
            if amt_val is not None:
                if amt_val > 0:
                    score += 8; evidence.append(f"当日主力资金净流入约 {amt_val/1e8:.2f} 亿元（同花顺口径）")
                elif amt_val < 0:
                    score -= 8; evidence.append(f"当日主力资金净流出约 {-amt_val/1e8:.2f} 亿元（同花顺口径）")
                else:
                    evidence.append("当日主力资金净额约 0（同花顺口径）")
        elif net_col:
            vals = pd.to_numeric(f[net_col], errors="coerce").dropna()
            if len(vals):
                last = float(vals.iloc[-1])
                recent5 = float(vals.tail(5).sum())
                score += max(-6, min(6, last / 5))
                if recent5 > 0:
                    score += 8; evidence.append(f"近5日主力净流入为正（累计 {recent5:.1f}）")
                else:
                    score -= 8; evidence.append(f"近5日主力净流入为负（累计 {recent5:.1f}）")
                trend = "净流入" if last > 0 else "净流出"
                evidence.append(f"最新交易日主力资金{trend}（净占比 {last:.2f}%）")
        elif net_amt_col:
            # 有净额列但无占比列：按净额口径（东财旧版）
            vals = pd.to_numeric(f[net_amt_col], errors="coerce").dropna()
            if len(vals):
                last = float(vals.iloc[-1])
                if last > 0: score += 6; evidence.append(f"最新交易日主力资金净流入（净额 {last:.0f}）")
                else: score -= 6; evidence.append(f"最新交易日主力资金净流出（净额 {last:.0f}）")
    if north_df is not None and not north_df.empty:
        available = True
        try:
            n = north_df.copy()
            col = next((c for c in n.columns if "成交净买额" in c), None)
            if col is not None:
                row = n[n["板块"].astype(str).str.contains("沪股通|深股通", na=False)]
                row = row[row["资金方向"].astype(str).str.contains("北向", na=False)] if "资金方向" in row.columns else row
                if not row.empty:
                    # 成交净买额单位为亿元
                    total = float(pd.to_numeric(row[col], errors="coerce").sum())
                    if total > 0.01: score += 6; evidence.append(f"北向资金净买入 {total:.1f} 亿元")
                    elif total < -0.01: score -= 6; evidence.append(f"北向资金净卖出 {-total:.1f} 亿元")
                    else: evidence.append(f"北向资金当日基本持平（{total:.2f} 亿元）")
        except Exception:  # noqa: BLE001
            pass
    if margin_df is not None and not margin_df.empty:
        available = True
        try:
            m = margin_df.copy()
            bal_col = next((c for c in m.columns if "融资融券余额" in c), None)
            if bal_col:
                vals = pd.to_numeric(m[bal_col], errors="coerce").dropna()
                if len(vals) >= 2:
                    delta = float(vals.iloc[-1] - vals.iloc[-2])
                    if delta > 0: score += 3; evidence.append(f"两融余额回升（近1日 {delta/1e8:+.1f} 亿）")
                    else: score -= 3; evidence.append(f"两融余额回落（近1日 {delta/1e8:+.1f} 亿）")
        except Exception:  # noqa: BLE001
            pass
    if not available:
        evidence.append("资金面数据获取失败，中性处理")
    return {"score": max(-100, min(100, score)), "evidence": evidence, "available": available}


def _score_sentiment(zt_df):
    """情绪题材面打分：涨停家数反映市场赚钱效应。"""
    evidence = []
    if zt_df is None or zt_df.empty:
        evidence.append("涨停池数据获取失败（可能非交易日或接口限流），中性处理")
        return {"score": 0, "evidence": evidence, "available": False}
    n = len(zt_df)
    if n >= 100:
        score = 10; evidence.append(f"今日涨停 {n} 家，市场情绪高涨（注意过热）")
    elif n >= 60:
        score = 12; evidence.append(f"今日涨停 {n} 家，赚钱效应良好")
    elif n >= 30:
        score = 4; evidence.append(f"今日涨停 {n} 家，情绪中性偏暖")
    else:
        score = -8; evidence.append(f"今日涨停 {n} 家，市场情绪偏冷")
    try:
        if "连板数" in zt_df.columns:
            max_lb = pd.to_numeric(zt_df["连板数"], errors="coerce").max()
            if pd.notna(max_lb) and max_lb >= 3:
                score += 4; evidence.append(f"最高连板 {int(max_lb)} 板，题材有高度")
    except Exception:  # noqa: BLE001
        pass
    return {"score": max(-100, min(100, score)), "evidence": evidence, "available": True}


def _score_market(index_df):
    """大盘环境打分：指数趋势 + 量能。"""
    evidence = []
    if index_df is None or len(index_df) < 20:
        evidence.append("大盘数据不足，中性处理")
        return {"score": 0, "evidence": evidence, "available": False}
    ind = _calc_indicators(index_df)
    r = _last_row(ind)
    px = float(r["close"])
    score = 0.0
    if not pd.isna(r["ma20"]):
        if px > r["ma20"]:
            score += 10; evidence.append(f"上证指数站上20日线（{px:.0f} > MA20 {r['ma20']:.0f}）")
        else:
            score -= 10; evidence.append(f"上证指数跌破20日线（{px:.0f} < MA20 {r['ma20']:.0f}）")
    if not pd.isna(r.get("vol_ratio", np.nan)):
        vr = float(r["vol_ratio"])
        if vr > 1.3: score += 3; evidence.append("大盘放量")
        elif vr < 0.8: score -= 3; evidence.append("大盘缩量")
    if len(ind) >= 6:
        mom = (px / ind["close"].iloc[-6] - 1) * 100
        evidence.append(f"大盘5日动量 {mom:+.1f}%")
    return {"score": max(-100, min(100, score)), "evidence": evidence, "available": True}


def _score_news(news_df):
    """消息事件面打分：关键词情绪统计。"""
    evidence = []
    if news_df is None or news_df.empty:
        evidence.append("近期无相关新闻，消息面中性")
        return {"score": 0, "evidence": evidence, "available": False}
    text = ""
    for col in ("新闻标题", "新闻内容", "新闻摘要"):
        if col in news_df.columns:
            text += " ".join(news_df[col].astype(str).tolist())
    bull = sum(text.count(k) for k in BULL_KEYWORDS)
    bear = sum(text.count(k) for k in BEAR_KEYWORDS)
    score = float(np.clip((bull - bear) * 2, -15, 15))
    if score > 5:
        evidence.append(f"近期新闻偏利好（利好词 {bull} 次 / 利空词 {bear} 次）")
    elif score < -5:
        evidence.append(f"近期新闻偏利空（利好词 {bull} 次 / 利空词 {bear} 次）")
    else:
        evidence.append(f"近期新闻情绪中性（利好词 {bull} 次 / 利空词 {bear} 次）")
    if "新闻标题" in news_df.columns:
        titles = news_df["新闻标题"].head(3).astype(str).tolist()
        for t in titles:
            evidence.append(f"  · {t[:40]}")
    return {"score": max(-100, min(100, score)), "evidence": evidence, "available": True}


def _event_signals(symbol, kline):
    """事件类触发信号：解禁临近等。"""
    signals = []
    try:
        unlock = get_unlock(symbol)
        if unlock is not None and not unlock.empty:
            for _, row in unlock.iterrows():
                date = str(row.get("解禁时间", ""))[:10]
                val = row.get("实际解禁市值")
                try:
                    val_str = f"，解禁市值约 {float(val)/1e8:.1f} 亿" if pd.notna(val) else ""
                except Exception:  # noqa: BLE001
                    val_str = ""
                signals.append(f"⚠️ 事件：{date} 限售解禁{val_str}，短线留意抛压")
    except Exception:  # noqa: BLE001
        pass
    return signals


def _tech_signals(kline):
    """量价类触发信号。"""
    signals = []
    if kline is None or len(kline) < 25:
        return signals
    ind = _calc_indicators(kline)
    r = _last_row(ind)
    px = float(r["close"])
    if not pd.isna(r["vol_ratio"]) and not pd.isna(r["ma20"]):
        if r["vol_ratio"] >= 1.5 and px > r["ma20"] and len(ind) >= 2 and ind["close"].iloc[-2] <= r["ma20"]:
            signals.append("🔔 放量上穿20日线，若明日放量站稳则短线偏强")
    if not pd.isna(r["vol_ratio"]) and not pd.isna(r["ma5"]):
        if r["vol_ratio"] <= 0.8 and abs(px - r["ma5"]) / r["ma5"] < 0.01:
            signals.append("📈 缩量回踩5日线，回踩确认后有望再攻")
    if not pd.isna(r["vol_ratio"]):
        if r["vol_ratio"] >= 1.8 and len(ind) >= 3 and ind["close"].iloc[-2] > ind["close"].iloc[-3] and px <= ind["close"].iloc[-2]:
            signals.append("🚨 放量滞涨（量增价不涨），警惕冲高回落")
    if len(ind) >= 6:
        up5 = all(not pd.isna(ind["close"].iloc[i]) and ind["close"].iloc[i] > ind["close"].iloc[i - 1]
                  for i in range(-4, 0))
        if up5 and px > r["ma20"]:
            signals.append("⚠️ 连续5日上涨且处20日线上方，短线累计涨幅大，注意高开分歧")
    if not pd.isna(r["dif"]) and not pd.isna(r["dea"]) and len(ind) >= 3:
        if not pd.isna(ind["dif"].iloc[-2]) and not pd.isna(ind["dea"].iloc[-2]):
            if ind["dif"].iloc[-2] < ind["dea"].iloc[-2] and r["dif"] > r["dea"]:
                signals.append("📶 MACD 刚刚金叉，短线动能转强")
    return signals


# ---------------------------------------------------------------------------
# 综合预测入口
# ---------------------------------------------------------------------------
def predict(symbol, days=120):
    """综合五维信号预测次日走势。

    返回 dict：
      success / symbol / name(可选) / trade_date / 方向 / 涨跌幅区间 /
      触发信号列表 / 综合得分 / 置信度 / 各维度明细 / 免责声明
    """
    code = norm_symbol(symbol)
    kline = get_kline(code, days)
    if kline is None or kline.empty:
        return {"success": False, "error": f"无法获取股票 {code} 的行情数据（数据源均不可用或代码无效）"}

    # 并行抓取各维数据（串行但带降级，数据不足不影响整体）
    fund = get_fund_flow(code, 10)
    index = get_index_kline("sh000001", 60)
    zt = get_zt_pool()
    news = get_news(code, 10)
    north = get_north_flow()
    margin = get_margin()

    tech = _score_tech(kline)
    fnd = _score_fund(fund, north, margin)
    sent = _score_sentiment(zt)
    mkt = _score_market(index)
    nws = _score_news(news)

    # 加权综合
    total = (
        tech["score"] * WEIGHTS["tech"]
        + fnd["score"] * WEIGHTS["fund"]
        + sent["score"] * WEIGHTS["sentiment"]
        + mkt["score"] * WEIGHTS["market"]
        + nws["score"] * WEIGHTS["news"]
    )
    total = max(-100, min(100, total))

    # 方向判定
    if total >= 15:
        direction = "看涨"
    elif total <= -15:
        direction = "看跌"
    else:
        direction = "震荡"

    # 涨跌幅区间：以 ATR 为基准，结合方向偏移
    atr = _compute_atr(kline)
    last_close = float(kline["close"].iloc[-1])
    atr_pct = atr / last_close * 100 if last_close else 1.5
    shift = total / 100 * atr_pct * 0.6  # 方向性偏移
    low_pct = max(-10, min(10, shift - atr_pct * 0.5))
    high_pct = max(-10, min(10, shift + atr_pct * 0.5))
    low_pct, high_pct = min(low_pct, high_pct), max(low_pct, high_pct)
    if abs(low_pct) < 0.05 and abs(high_pct) < 0.05:
        low_pct, high_pct = -atr_pct * 0.3, atr_pct * 0.3

    # 置信度：基于可用维度数量 + 总分强度
    avail_dims = sum(1 for d in (tech, fnd, sent, mkt, nws) if d["available"])
    conf = 40 + avail_dims * 5 + min(15, abs(total) / 100 * 15)
    conf = min(85, conf)  # 预测不可能超过 85，留敬畏

    # 触发信号
    signals = _tech_signals(kline) + _event_signals(code, kline)

    # 最近交易日与收盘
    trade_date = str(kline["date"].iloc[-1])[:10]

    return {
        "success": True,
        "symbol": code,
        "trade_date": trade_date,
        "last_close": round(last_close, 2),
        "direction": direction,
        "range_low": round(low_pct, 2),
        "range_high": round(high_pct, 2),
        "score": round(total, 1),
        "confidence": round(conf),
        "signals": signals,
        "dims": {
            "tech": {"name": DIM_NAMES["tech"], "score": round(tech["score"], 1), "evidence": tech["evidence"], "weight": WEIGHTS["tech"]},
            "fund": {"name": DIM_NAMES["fund"], "score": round(fnd["score"], 1), "evidence": fnd["evidence"], "weight": WEIGHTS["fund"]},
            "sentiment": {"name": DIM_NAMES["sentiment"], "score": round(sent["score"], 1), "evidence": sent["evidence"], "weight": WEIGHTS["sentiment"]},
            "market": {"name": DIM_NAMES["market"], "score": round(mkt["score"], 1), "evidence": mkt["evidence"], "weight": WEIGHTS["market"]},
            "news": {"name": DIM_NAMES["news"], "score": round(nws["score"], 1), "evidence": nws["evidence"], "weight": WEIGHTS["news"]},
        },
        "disclaimer": DISCLAIMER,
    }


def quote(symbol):
    """行情快照：最新价 + 当日涨跌 + 量比 + 换手 + 均线位置。"""
    code = norm_symbol(symbol)
    kline = get_kline(code, 30)
    if kline is None or kline.empty:
        return {"success": False, "error": f"无法获取 {code} 行情"}
    ind = _calc_indicators(kline)
    r = _last_row(ind)
    last = float(r["close"])
    prev = float(kline["close"].iloc[-2]) if len(kline) >= 2 else last
    chg = (last / prev - 1) * 100 if prev else 0.0
    out = {
        "success": True,
        "symbol": code,
        "date": str(kline["date"].iloc[-1])[:10],
        "close": round(last, 2),
        "chg_pct": round(chg, 2),
        "volume_ratio": round(float(r["vol_ratio"]), 2) if not pd.isna(r["vol_ratio"]) else None,
        "ma5": round(float(r["ma5"]), 2) if not pd.isna(r["ma5"]) else None,
        "ma10": round(float(r["ma10"]), 2) if not pd.isna(r["ma10"]) else None,
        "ma20": round(float(r["ma20"]), 2) if not pd.isna(r["ma20"]) else None,
        "ma60": round(float(r["ma60"]), 2) if not pd.isna(r["ma60"]) else None,
        "rsi14": round(float(r["rsi14"]), 1) if not pd.isna(r["rsi14"]) else None,
    }
    return out


def analyze(symbol, dimension=None, days=120):
    """单维度深度分析。dimension: tech/fund/sentiment/market/news，None 时全部分析。"""
    code = norm_symbol(symbol)
    kline = get_kline(code, days)
    if kline is None or kline.empty:
        return {"success": False, "error": f"无法获取 {code} 行情"}

    data = {}
    if dimension is None or dimension == "tech":
        data["tech"] = _score_tech(kline)
    if dimension is None or dimension == "fund":
        fund = get_fund_flow(code, 10)
        north = get_north_flow()
        margin = get_margin()
        data["fund"] = _score_fund(fund, north, margin)
    if dimension is None or dimension == "sentiment":
        zt = get_zt_pool()
        data["sentiment"] = _score_sentiment(zt)
    if dimension is None or dimension == "market":
        index = get_index_kline("sh000001", 60)
        data["market"] = _score_market(index)
    if dimension is None or dimension == "news":
        news = get_news(code, 10)
        data["news"] = _score_news(news)

    result = {"success": True, "symbol": code, "dimensions": {}}
    for k, v in data.items():
        result["dimensions"][k] = {
            "name": DIM_NAMES.get(k, k),
            "score": round(v["score"], 1),
            "evidence": v["evidence"],
        }
    return result


def daily_report(symbols, days=120):
    """盘后日报：对一组股票生成预测，供定时任务调用。symbols 为代码列表。"""
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",") if s.strip()]
    reports = []
    for code in symbols:
        try:
            r = predict(code, days)
            reports.append(r)
        except Exception as e:  # noqa: BLE001
            reports.append({"success": False, "symbol": code, "error": str(e)[:100]})
        time.sleep(0.5)  # 控制频率
    return {"success": True, "count": len(reports), "reports": reports}


# ---------------------------------------------------------------------------
# gateway 前端代理白名单（仅这些函数可被调用）
# ---------------------------------------------------------------------------
PUBLIC_FUNCTIONS = ["predict", "quote", "analyze", "daily_report", "get_kline"]
