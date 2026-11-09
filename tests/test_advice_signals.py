# -*- coding: utf-8 -*-
"""quantified-advice 功能单元测试（TDD 红-绿-重构）。

覆盖 NLSpec .jarvis/.df/specs/quantified-advice.md 中：
  - compute_advice_signals 的 12 条方向规则、得分映射、仓位映射
  - 量化参考值口径（入场/止损/目标价/时间窗口）
  - 数据不足/指标缺失的 null 语义与 signal_quality
  - get_snapshot 集成（advice_signals 键 + 既有字段不回归）
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PLUGIN_DIR)

from plugin import api  # noqa: E402


# ---------------------------------------------------------------------------
# 测试数据构造
# ---------------------------------------------------------------------------
def make_kline(n=80, close=10.0, vol=1_000_000, turnover=3.0):
    """构造 n 日上升趋势的日K DataFrame（含指标列）。"""
    dates = pd.bdate_range(end="2026-11-06", periods=n)
    closes = np.linspace(close * 0.8, close, n)
    df = pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": closes * 0.99,
        "high": closes * 1.02,
        "low": closes * 0.98,
        "close": closes,
        "volume": vol,
        "amount": vol * closes,
        "turnover": turnover,
    })
    return api._calc_indicators(df)


def make_cyq(profit=0.6):
    """构造单行筹码分布 DataFrame（获利比例 0~1）。"""
    return pd.DataFrame([{
        "日期": "2026-11-06", "获利比例": profit, "平均成本": 9.5,
        "90成本-低": 8.0, "90成本-高": 11.0, "90集中度": 0.15,
        "70成本-低": 8.8, "70成本-高": 10.2, "70集中度": 0.08,
    }])


def latest_indicators(df):
    """取最新一日指标 dict（含 atr14），模拟 get_snapshot 中 indicators 结构。"""
    last = df.iloc[-1].to_dict()
    last["atr14"] = api._compute_atr(df, 14)
    return last


# ---------------------------------------------------------------------------
# 1. 规则命中
# ---------------------------------------------------------------------------
class TestRules:
    def test_ma_bullish_hit(self):
        """多头排列：close > ma5 > ma10 > ma20 → 命中 ma_bullish（+20）。"""
        df = make_kline()
        sig = api.compute_advice_signals("600519", df, latest_indicators(df), make_cyq())
        assert "ma_bullish" in sig["rules_hit"]
        assert sig["direction_score"] >= 20

    def test_ma_bearish_hit(self):
        """空头排列：close < ma5 < ma10 < ma20 → 命中 ma_bearish（−20）。"""
        df = make_kline(close=10.0)
        # 反转：让最新价低于各均线（构造下跌段）
        n = len(df)
        closes = np.linspace(12.0, 8.0, n)
        df["close"] = closes
        df["open"] = closes * 1.01
        df["high"] = closes * 1.02
        df["low"] = closes * 0.98
        df = api._calc_indicators(df)
        # 中性化 RSI，避免下跌趋势中 rsi_oversold(+10) 抵消 ma_bearish 贡献
        df.loc[df.index[-1], "rsi14"] = 50.0
        sig = api.compute_advice_signals("600519", df, latest_indicators(df), make_cyq())
        assert "ma_bearish" in sig["rules_hit"]
        assert sig["direction_score"] <= -20

    def test_macd_golden_cross(self):
        """构造 DIF 上穿 DEA（金叉）→ 命中 macd_golden_cross（+15）。"""
        df = make_kline()
        # 人为构造金叉：最新一日 dif > dea，前一日 dif < dea
        df.loc[df.index[-2], "dif"] = -0.1
        df.loc[df.index[-2], "dea"] = 0.0
        df.loc[df.index[-1], "dif"] = 0.2
        df.loc[df.index[-1], "dea"] = 0.1
        sig = api.compute_advice_signals("600519", df, latest_indicators(df), make_cyq())
        assert "macd_golden_cross" in sig["rules_hit"]

    def test_macd_dead_cross(self):
        """构造 DIF 下穿 DEA（死叉）→ 命中 macd_dead_cross（−15）。"""
        df = make_kline()
        df.loc[df.index[-2], "dif"] = 0.2
        df.loc[df.index[-2], "dea"] = 0.1
        df.loc[df.index[-1], "dif"] = -0.1
        df.loc[df.index[-1], "dea"] = 0.0
        sig = api.compute_advice_signals("600519", df, latest_indicators(df), make_cyq())
        assert "macd_dead_cross" in sig["rules_hit"]

    def test_rsi_oversold(self):
        """RSI14 < 30 → 命中 rsi_oversold（+10）。"""
        df = make_kline()
        df.loc[df.index[-1], "rsi14"] = 25.0
        sig = api.compute_advice_signals("600519", df, latest_indicators(df), make_cyq())
        assert "rsi_oversold" in sig["rules_hit"]

    def test_rsi_overbought(self):
        """RSI14 > 70 → 命中 rsi_overbought（−10）。"""
        df = make_kline()
        df.loc[df.index[-1], "rsi14"] = 75.0
        sig = api.compute_advice_signals("600519", df, latest_indicators(df), make_cyq())
        assert "rsi_overbought" in sig["rules_hit"]

    def test_boll_break_up(self):
        """收盘价 > BOLL 上轨 → 命中 boll_break_up（+10）。"""
        df = make_kline()
        df.loc[df.index[-1], "close"] = df.loc[df.index[-1], "boll_up"] * 1.02
        sig = api.compute_advice_signals("600519", df, latest_indicators(df), make_cyq())
        assert "boll_break_up" in sig["rules_hit"]

    def test_boll_break_down(self):
        """收盘价 < BOLL 下轨 → 命中 boll_break_down（−10）。"""
        df = make_kline()
        df.loc[df.index[-1], "close"] = df.loc[df.index[-1], "boll_low"] * 0.98
        sig = api.compute_advice_signals("600519", df, latest_indicators(df), make_cyq())
        assert "boll_break_down" in sig["rules_hit"]

    def test_vol_expand(self):
        """量比 > 1.5 且换手 > 5% → 命中 vol_expand（+10）。"""
        df = make_kline(turnover=6.0)
        df.loc[df.index[-1], "vol_ratio"] = 2.0
        sig = api.compute_advice_signals("600519", df, latest_indicators(df), make_cyq())
        assert "vol_expand" in sig["rules_hit"]

    def test_vol_shrink(self):
        """量比 < 0.7 → 命中 vol_shrink（−5）。"""
        df = make_kline()
        df.loc[df.index[-1], "vol_ratio"] = 0.5
        sig = api.compute_advice_signals("600519", df, latest_indicators(df), make_cyq())
        assert "vol_shrink" in sig["rules_hit"]

    def test_cy_profit_up(self):
        """获利盘比例上升（CYQK > 0）→ 命中 cy_profit_up（+10）。"""
        df = make_kline()
        df.loc[df.index[-1], "cyqk"] = 2.0
        sig = api.compute_advice_signals("600519", df, latest_indicators(df), make_cyq())
        assert "cy_profit_up" in sig["rules_hit"]

    def test_cy_profit_down(self):
        """获利盘比例下降（CYQK < 0）→ 命中 cy_profit_down（−10）。"""
        df = make_kline()
        df.loc[df.index[-1], "cyqk"] = -2.0
        sig = api.compute_advice_signals("600519", df, latest_indicators(df), make_cyq())
        assert "cy_profit_down" in sig["rules_hit"]


# ---------------------------------------------------------------------------
# 2. 方向与仓位映射
# ---------------------------------------------------------------------------
class TestDirectionAndPosition:
    def test_bullish_mapping(self):
        """score ≥ +30 → bullish。"""
        df = make_kline()
        # 叠加金叉 + 突破上轨，确保 score ≥ +30（ma_bullish+20 + macd_golden+15 + boll_break_up+10）
        df.loc[df.index[-2], "dif"] = -0.1
        df.loc[df.index[-2], "dea"] = 0.0
        df.loc[df.index[-1], "dif"] = 0.2
        df.loc[df.index[-1], "dea"] = 0.1
        df.loc[df.index[-1], "close"] = df.loc[df.index[-1], "boll_up"] * 1.02
        sig = api.compute_advice_signals("600519", df, latest_indicators(df), make_cyq())
        assert sig["direction"] == "bullish"
        assert sig["position"] in ("half", "heavy")

    def test_bearish_mapping(self):
        """score ≤ −30 → bearish，仓位为 null。"""
        df = make_kline()
        n = len(df)
        closes = np.linspace(12.0, 8.0, n)
        df["close"] = closes
        df["open"] = closes * 1.01
        df["high"] = closes * 1.02
        df["low"] = closes * 0.98
        df = api._calc_indicators(df)
        # 叠加死叉 + 超买 + 破下轨，确保得分 ≤ −30
        df.loc[df.index[-2], "dif"] = 0.2
        df.loc[df.index[-2], "dea"] = 0.1
        df.loc[df.index[-1], "dif"] = -0.1
        df.loc[df.index[-1], "dea"] = 0.0
        df.loc[df.index[-1], "rsi14"] = 75.0
        sig = api.compute_advice_signals("600519", df, latest_indicators(df), make_cyq())
        assert sig["direction"] == "bearish"
        assert sig["position"] is None
        assert sig["position_ratio"] is None

    def test_neutral_mapping(self):
        """|score| < 30 → neutral，仓位 light(0.3)。"""
        df = make_kline()
        # 用中性数据：无强信号，仅轻微多头（ma_bullish +20 → neutral）
        n = len(df)
        closes = np.linspace(9.0, 10.0, n)
        df["close"] = closes
        df["open"] = closes * 0.999
        df["high"] = closes * 1.001
        df["low"] = closes * 0.999
        df = api._calc_indicators(df)
        sig = api.compute_advice_signals("600519", df, latest_indicators(df), make_cyq())
        assert sig["direction"] == "neutral"
        assert sig["position"] == "light"
        assert sig["position_ratio"] == 0.3

    def test_heavy_position(self):
        """score ≥ +50 → heavy(0.7)。"""
        df = make_kline(turnover=6.0)
        # 叠加多个多头信号：ma_bullish(+20) + macd金叉(+15) + boll突破(+10) + vol_expand(+10) + cy_profit_up(+10)
        df.loc[df.index[-2], "dif"] = -0.1
        df.loc[df.index[-2], "dea"] = 0.0
        df.loc[df.index[-1], "dif"] = 0.2
        df.loc[df.index[-1], "dea"] = 0.1
        df.loc[df.index[-1], "close"] = df.loc[df.index[-1], "boll_up"] * 1.02
        df.loc[df.index[-1], "vol_ratio"] = 2.0
        df.loc[df.index[-1], "cyqk"] = 2.0
        sig = api.compute_advice_signals("600519", df, latest_indicators(df), make_cyq())
        assert sig["direction"] == "bullish"
        assert sig["position"] == "heavy"
        assert sig["position_ratio"] == 0.7

    def test_score_range(self):
        """direction_score 限制在 −100 ~ +100。"""
        df = make_kline()
        sig = api.compute_advice_signals("600519", df, latest_indicators(df), make_cyq())
        assert -100 <= sig["direction_score"] <= 100


# ---------------------------------------------------------------------------
# 3. 量化参考值
# ---------------------------------------------------------------------------
class TestQuantifiedValues:
    def test_entry_ref_equals_close(self):
        """入场参考价 = 最新收盘价。"""
        df = make_kline()
        sig = api.compute_advice_signals("600519", df, latest_indicators(df), make_cyq())
        assert sig["entry_ref"] == pytest.approx(df["close"].iloc[-1], rel=1e-6)

    def test_stop_loss_conservative(self):
        """止损价 = 入场价×(1−ATR倍数) 与 BOLL下轨/MA20 取更保守者（更低者）。"""
        df = make_kline()
        ind = latest_indicators(df)
        sig = api.compute_advice_signals("600519", df, ind, make_cyq())
        atr = ind["atr14"]
        entry = sig["entry_ref"]
        # 保守止损应低于 入场价×(1−2*ATR/entry) 与 关键支撑 的较高者
        atr_stop = entry * (1 - 2 * atr / entry)
        support = min(ind.get("boll_low") or entry, ind.get("ma20") or entry)
        conservative = min(atr_stop, support)
        assert sig["stop_loss"] <= conservative + 1e-6
        assert sig["stop_loss"] < entry

    def test_targets_three_tiers(self):
        """目标价三档 T+1/T+5/T+20，且逐档递增。"""
        df = make_kline()
        sig = api.compute_advice_signals("600519", df, latest_indicators(df), make_cyq())
        assert set(sig["targets"].keys()) == {"t1", "t5", "t20"}
        assert sig["targets"]["t1"] <= sig["targets"]["t5"] <= sig["targets"]["t20"]

    def test_timeframe_fixed(self):
        """时间窗口固定三档。"""
        df = make_kline()
        sig = api.compute_advice_signals("600519", df, latest_indicators(df), make_cyq())
        assert sig["timeframe"] == ["T+1", "T+5", "T+20"]


# ---------------------------------------------------------------------------
# 4. 数据不足 / 缺失
# ---------------------------------------------------------------------------
class TestDataQuality:
    def test_empty_kline_insufficient(self):
        """kline 为空 → direction=null, signal_quality=insufficient。"""
        df = pd.DataFrame()
        sig = api.compute_advice_signals("600519", df, {}, pd.DataFrame())
        assert sig["direction"] is None
        assert sig["signal_quality"] == "insufficient"
        assert sig["entry_ref"] is None
        assert sig["stop_loss"] is None
        assert sig["targets"] is None
        assert sig["position"] is None

    def test_missing_indicator_null(self):
        """关键指标缺失（如 atr14 缺失）→ 依赖字段为 null，不伪造。"""
        df = make_kline()
        ind = latest_indicators(df)
        ind.pop("atr14", None)  # 模拟 ATR 缺失
        sig = api.compute_advice_signals("600519", df, ind, make_cyq())
        # 止损依赖 ATR，缺失时应为 null 或退化为支撑（不伪造 ATR 值）
        assert sig["signal_quality"] in ("ok", "partial")
        assert sig["direction"] is not None  # 方向信号不依赖 ATR

    def test_no_exception_on_bad_input(self):
        """异常输入不抛异常，返回 insufficient。"""
        sig = api.compute_advice_signals("600519", None, None, None)
        assert sig["signal_quality"] == "insufficient"
        assert sig["direction"] is None


# ---------------------------------------------------------------------------
# 5. get_snapshot 集成
# ---------------------------------------------------------------------------
class TestSnapshotIntegration:
    def test_snapshot_has_advice_signals(self, monkeypatch):
        """get_snapshot 返回含 advice_signals 键，且既有字段不回归。"""
        df = make_kline()
        cyq = make_cyq()

        monkeypatch.setattr(api, "get_kline", lambda symbol, days=120: df)
        monkeypatch.setattr(api, "get_cyq", lambda symbol: cyq)
        monkeypatch.setattr(api, "_get_stock_name", lambda symbol: "测试股")

        snap = api.get_snapshot("600519", days=80, sections=["kline", "cyq"])
        # 既有字段
        assert snap["symbol"] == "600519"
        assert snap["kline"] is not None
        assert snap["indicators"] is not None
        assert "disclaimer" in snap
        assert "data_quality" in snap
        # 新增字段
        assert "advice_signals" in snap
        sig = snap["advice_signals"]
        assert sig is not None
        assert "direction" in sig
        assert "direction_score" in sig
        assert "entry_ref" in sig
        assert "stop_loss" in sig
        assert "targets" in sig
        assert "position" in sig
        assert "timeframe" in sig
        assert "rules_hit" in sig
        assert "signal_quality" in sig
        # data_quality 新增 advice_signals 键
        assert "advice_signals" in snap["data_quality"]

    def test_snapshot_advice_signals_none_when_no_kline(self, monkeypatch):
        """无 kline 时 advice_signals 为 None。"""
        monkeypatch.setattr(api, "get_kline", lambda symbol, days=120: pd.DataFrame())
        monkeypatch.setattr(api, "get_cyq", lambda symbol: pd.DataFrame())
        monkeypatch.setattr(api, "_get_stock_name", lambda symbol: "测试股")
        snap = api.get_snapshot("600519", days=80, sections=["kline", "cyq"])
        assert snap["advice_signals"] is None
        assert snap["data_quality"].get("advice_signals") == "insufficient"
