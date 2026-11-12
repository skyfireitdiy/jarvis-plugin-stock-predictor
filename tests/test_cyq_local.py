# -*- coding: utf-8 -*-
"""腾讯源本地筹码分布计算功能单元测试。

覆盖：
  - _calc_cyq_local 算法正确性（构造已知K线验证获利比例/平均成本/集中度）
  - _fetch_kline_tx 对腾讯 JSON 响应的解析
  - get_cyq 降级触发逻辑（东财失败 → 自动降级腾讯源）
  - get_cyq 主路径优先（东财可用时用东财，不调腾讯）
  - get_snapshot 的 cyq_source 透传
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
def make_bars(n=60, start=10.0, end=20.0):
    """构造 n 日单边上涨的日K bars（腾讯接口解析后的结构）。"""
    dates = pd.bdate_range(end="2026-11-06", periods=n)
    closes = np.linspace(start, end, n)
    bars = []
    for i, d in enumerate(dates):
        c = closes[i]
        bars.append({
            "date": d.strftime("%Y-%m-%d"),
            "open": c * 0.99,
            "close": c,
            "high": c * 1.02,
            "low": c * 0.98,
            "volume": 100_000.0,
        })
    return bars


def make_em_cyq(profit=0.6):
    """构造东财源返回的筹码分布 DataFrame（模拟 stock_cyq_em 结构）。"""
    return pd.DataFrame([{
        "日期": "2026-11-06", "获利比例": profit, "平均成本": 15.0,
        "90成本-低": 12.0, "90成本-高": 18.0, "90集中度": 0.20,
        "70成本-低": 13.5, "70成本-高": 16.5, "70集中度": 0.10,
    }])


def make_tx_response(bars):
    """构造腾讯接口的 JSON 响应结构。"""
    klines = [[b["date"], b["open"], b["close"], b["high"], b["low"], b["volume"]]
              for b in bars]
    return {"data": {"sz301110": {"qfqday": klines}}}


# ---------------------------------------------------------------------------
# 1. _calc_cyq_local 算法正确性
# ---------------------------------------------------------------------------
class TestCalcCyqLocal:
    def test_single_uptrend(self):
        """单边上涨：获利比例随价格上升单调递增，最新日获利比例高。"""
        bars = make_bars(n=60, start=10.0, end=20.0)
        df = api._calc_cyq_local(bars)
        assert not df.empty
        assert list(df.columns) == [
            "日期", "获利比例", "平均成本",
            "90成本-低", "90成本-高", "90集中度",
            "70成本-低", "70成本-高", "70集中度",
        ]
        # 获利比例 0~1
        assert df["获利比例"].between(0, 1).all()
        # 单边上涨：获利比例应单调不减（历史筹码在低位，价格越高获利越多）
        assert df["获利比例"].is_monotonic_increasing
        # 最新日（价格最高）获利比例应接近 1
        assert df["获利比例"].iloc[-1] > 0.9
        # 平均成本应在 [start, end] 区间内
        assert 10.0 <= df["平均成本"].iloc[-1] <= 20.0
        # 集中度 0~1
        assert df["90集中度"].between(0, 1).all()
        assert df["70集中度"].between(0, 1).all()
        # 90% 区间应覆盖 70% 区间
        assert df["90成本-低"].iloc[-1] <= df["70成本-低"].iloc[-1]
        assert df["90成本-高"].iloc[-1] >= df["70成本-高"].iloc[-1]

    def test_single_downtrend(self):
        """单边下跌：最新日获利比例应很低（几乎全部套牢）。"""
        bars = make_bars(n=60, start=20.0, end=10.0)
        df = api._calc_cyq_local(bars)
        assert not df.empty
        # 最新日价格最低，获利比例应接近 0
        assert df["获利比例"].iloc[-1] < 0.1

    def test_insufficient_bars(self):
        """数据不足（<2 根）返回空 DataFrame（不抛异常）。"""
        df = api._calc_cyq_local([{"date": "2026-11-06", "open": 10, "close": 10,
                                    "high": 10.2, "low": 9.8, "volume": 100}])
        assert df.empty
        df2 = api._calc_cyq_local([])
        assert df2.empty

    def test_bad_bars(self):
        """异常 bars（价格为 0 / 高低颠倒）不抛异常，返回空或合理结果。"""
        df = api._calc_cyq_local([
            {"date": "2026-11-05", "open": 0, "close": 0, "high": 0, "low": 0, "volume": 0},
            {"date": "2026-11-06", "open": 0, "close": 0, "high": 0, "low": 0, "volume": 0},
        ])
        assert isinstance(df, pd.DataFrame)


# ---------------------------------------------------------------------------
# 2. _fetch_kline_tx 解析
# ---------------------------------------------------------------------------
class TestFetchKlineTx:
    def test_parse_response(self, monkeypatch):
        """正确解析腾讯 JSON 响应，字段顺序 [date, open, close, high, low, volume]。"""
        bars = make_bars(n=10)
        resp = make_tx_response(bars)

        class FakeResp:
            status_code = 200
            text = ""

            def json(self):
                return resp

        monkeypatch.setattr(api._requests, "get", lambda *a, **k: FakeResp())
        out = api._fetch_kline_tx("301110")
        assert len(out) == 10
        assert out[0]["date"] == bars[0]["date"]
        assert out[0]["close"] == pytest.approx(bars[0]["close"])
        assert out[0]["volume"] == pytest.approx(bars[0]["volume"])

    def test_http_error_returns_empty(self, monkeypatch):
        """HTTP 非 200 返回空列表。"""

        class FakeResp:
            status_code = 500

        monkeypatch.setattr(api._requests, "get", lambda *a, **k: FakeResp())
        assert api._fetch_kline_tx("301110") == []

    def test_exception_returns_empty(self, monkeypatch):
        """网络异常返回空列表（不抛异常）。"""

        def boom(*a, **k):
            raise OSError("network down")

        monkeypatch.setattr(api._requests, "get", boom)
        assert api._fetch_kline_tx("301110") == []

    def test_tx_symbol(self):
        """交易所前缀映射正确。"""
        assert api._tx_symbol("301110") == "sz301110"
        assert api._tx_symbol("600519") == "sh600519"
        assert api._tx_symbol("830799") == "bj830799"


# ---------------------------------------------------------------------------
# 3. get_cyq 降级与主路径
# ---------------------------------------------------------------------------
class TestGetCyqFallback:
    def test_fallback_to_tencent_when_em_fails(self, monkeypatch):
        """东财源抛异常 → 自动降级腾讯源本地计算，source=tencent_local。"""
        # 清缓存
        api._cyq_cache["data"].clear()
        api._cyq_cache["ts"].clear()

        def boom(*a, **k):
            raise OSError("eastmoney blocked")

        monkeypatch.setattr(api, "ak", type("AK", (), {"stock_cyq_em": boom})())
        monkeypatch.setattr(api, "_fetch_kline_tx",
                            lambda symbol, days=120: make_bars(n=60, start=10, end=20))
        df = api.get_cyq("301110")
        assert not df.empty
        assert df.attrs.get("source") == "tencent_local"
        assert "获利比例" in df.columns
        assert df["获利比例"].iloc[-1] > 0.9  # 单边上涨获利高

    def test_fallback_when_em_returns_empty(self, monkeypatch):
        """东财源返回空 DataFrame → 降级腾讯源。"""
        api._cyq_cache["data"].clear()
        api._cyq_cache["ts"].clear()

        monkeypatch.setattr(api, "ak", type("AK", (), {"stock_cyq_em": lambda *a, **k: pd.DataFrame()})())
        monkeypatch.setattr(api, "_fetch_kline_tx",
                            lambda symbol, days=120: make_bars(n=60, start=10, end=20))
        df = api.get_cyq("301110")
        assert not df.empty
        assert df.attrs.get("source") == "tencent_local"

    def test_em_primary_when_available(self, monkeypatch):
        """东财源可用 → 优先东财，source=eastmoney，不调腾讯。"""
        api._cyq_cache["data"].clear()
        api._cyq_cache["ts"].clear()

        em_df = make_em_cyq(profit=0.6)
        monkeypatch.setattr(api, "ak", type("AK", (), {"stock_cyq_em": lambda *a, **k: em_df})())
        called = {"tx": False}
        monkeypatch.setattr(api, "_fetch_kline_tx",
                            lambda symbol, days=120: called.__setitem__("tx", True) or make_bars())
        df = api.get_cyq("301110")
        assert not df.empty
        assert df.attrs.get("source") == "eastmoney"
        assert not called["tx"], "东财可用时不应调用腾讯源"
        # 东财数据未被改写
        assert df["获利比例"].iloc[0] == pytest.approx(0.6)

    def test_both_fail_returns_empty(self, monkeypatch):
        """东财与腾讯都失败 → 返回空 DataFrame（不抛异常）。"""
        api._cyq_cache["data"].clear()
        api._cyq_cache["ts"].clear()

        def boom(*a, **k):
            raise OSError("fail")

        monkeypatch.setattr(api, "ak", type("AK", (), {"stock_cyq_em": boom})())
        monkeypatch.setattr(api, "_fetch_kline_tx", lambda symbol, days=120: [])
        df = api.get_cyq("301110")
        assert df.empty


# ---------------------------------------------------------------------------
# 4. get_snapshot cyq_source 透传
# ---------------------------------------------------------------------------
class TestSnapshotCyqSource:
    def test_cyq_source_tencent(self, monkeypatch):
        """get_snapshot 透传 cyq_source=tencent_local。"""
        df = make_bars(n=60, start=10, end=20)
        cyq = api._calc_cyq_local(df)
        cyq.attrs["source"] = "tencent_local"

        monkeypatch.setattr(api, "get_kline", lambda symbol, days=120: api._calc_indicators(
            pd.DataFrame({
                "date": [b["date"] for b in df],
                "open": [b["open"] for b in df],
                "high": [b["high"] for b in df],
                "low": [b["low"] for b in df],
                "close": [b["close"] for b in df],
                "volume": [b["volume"] for b in df],
                "amount": [b["close"] * b["volume"] for b in df],
                "turnover": [3.0] * len(df),
            })
        ))
        monkeypatch.setattr(api, "get_cyq", lambda symbol: cyq)
        monkeypatch.setattr(api, "_get_stock_name", lambda symbol: "测试股")

        snap = api.get_snapshot("600519", days=80, sections=["kline", "cyq"])
        assert snap["cyq_source"] == "tencent_local"
        assert snap["data_quality"].get("cyq") == "ok"
        assert snap["cyq"] is not None and len(snap["cyq"]) > 0
        # 获利比例经 _df_to_records 后保留
        assert snap["cyq"][-1]["获利比例"] > 0.9

    def test_cyq_source_none_when_empty(self, monkeypatch):
        """cyq 为空时 cyq_source 为 None。"""
        monkeypatch.setattr(api, "get_kline", lambda symbol, days=120: pd.DataFrame())
        monkeypatch.setattr(api, "get_cyq", lambda symbol: pd.DataFrame())
        monkeypatch.setattr(api, "_get_stock_name", lambda symbol: "测试股")
        snap = api.get_snapshot("600519", days=80, sections=["kline", "cyq"])
        assert snap["cyq"] is None
        assert snap["cyq_source"] is None
