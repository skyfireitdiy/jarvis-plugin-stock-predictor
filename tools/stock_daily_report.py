# -*- coding: utf-8 -*-
"""
stock_daily_report 工具：关注列表盘后数据摘要（零 LLM 成本）。

对一组关注股票输出**客观数据摘要**（收盘价、涨跌幅、均线位置、量比、换手、
主力资金、近期新闻标题），不调用 LLM、不做预测、不给结论。
适合定时任务每交易日盘后生成"数据快照"，由 Agent 或人事后解读。

如需分析结论，请用 stock_predict 取全量数据后由 Agent 自行研判。
"""
import os

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_API_PATH = os.path.join(_PLUGIN_DIR, "plugin", "api.py")
import importlib.util as _importlib_util

# 用 spec_from_file_location 动态加载插件私有 api.py（tools 与 plugin 是兄弟目录，
# 不能直接 import；注意必须显式 import importlib.util，只 import importlib 时
# 干净进程里 importlib.util 可能尚未加载，会抛 AttributeError）。
_spec = _importlib_util.spec_from_file_location("stock_predictor_api", _API_PATH)
api = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(api)

# 默认关注列表（可通过 symbols 参数覆盖）
DEFAULT_SYMBOLS = ["600519", "000858", "300750"]


def _f(v, nd=2, suffix=""):
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        return f"{v:.{nd}f}{suffix}"
    return str(v)


def _one(code):
    """取单只股票的数据摘要。"""
    df = api.get_kline(code, days=60)
    if df is None or df.empty:
        return {"symbol": code, "ok": False, "error": "无K线数据"}
    ind_df = api._calc_indicators(df)
    last = api._last_row(ind_df)
    item = {
        "symbol": code,
        "ok": True,
        "date": str(last.get("date"))[:10],
        "close": api._safe_float(last.get("close")),
        "ma5": api._safe_float(last.get("ma5")),
        "ma10": api._safe_float(last.get("ma10")),
        "ma20": api._safe_float(last.get("ma20")),
        "ma60": api._safe_float(last.get("ma60")),
        "rsi14": api._safe_float(last.get("rsi14")),
        "vol_ratio": api._safe_float(last.get("vol_ratio")),
        "turnover": api._safe_float(last.get("turnover")),
        "atr14": api._compute_atr(ind_df, 14),
        "chg_pct": None,
        "fund_flow_5d": None,
        "news_titles": [],
    }
    if len(ind_df) >= 2:
        prev = api._safe_float(ind_df["close"].iloc[-2])
        if prev and item["close"]:
            item["chg_pct"] = round((item["close"] / prev - 1) * 100, 2)

    # 近5日主力净流入合计（万元）
    try:
        ff = api.get_fund_flow(code)
        if ff is not None and not ff.empty and "主力净流入-净额" in ff.columns:
            vals = ff["主力净流入-净额"].tail(5).dropna()
            if len(vals):
                item["fund_flow_5d"] = round(float(vals.sum()) / 10000, 1)
    except Exception:  # noqa: BLE001
        pass

    # 近期新闻标题（最多3条）
    try:
        news = api.get_news(code)
        if news is not None and not news.empty:
            col = "新闻标题" if "新闻标题" in news.columns else news.columns[0]
            item["news_titles"] = [str(x)[:60] for x in news[col].head(3).tolist()]
    except Exception:  # noqa: BLE001
        pass
    return item


class StockDailyReportTool:
    name = "stock_daily_report"
    description = (
        "生成A股关注列表的盘后**数据摘要**：每只股票的收盘价、涨跌幅、均线位置、RSI、"
        "量比、换手、ATR、近5日主力净流入、近期新闻标题。"
        "**不调用 LLM、不做预测、不含任何结论**，仅供 Agent 或人事后解读。"
        "供定时任务或手动批量使用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbols": {
                "type": "string",
                "description": "关注股票代码，逗号分隔，如 '600519,000858'；不填则用默认列表",
            },
        },
    }

    @staticmethod
    def check() -> bool:
        return True

    def execute(self, args):
        try:
            symbols = (args.get("symbols") or "").strip()
            syms = [s.strip() for s in symbols.replace("，", ",").split(",") if s.strip()] or DEFAULT_SYMBOLS
            syms = syms[:20]  # 限制批量上限，防止超时

            items = []
            for i, s in enumerate(syms):
                if i > 0:
                    api.time.sleep(0.4)  # 防限流
                try:
                    items.append(_one(api.norm_symbol(s)))
                except Exception as e:  # noqa: BLE001
                    items.append({"symbol": s, "ok": False, "error": str(e)[:120]})

            ok_items = [x for x in items if x.get("ok")]
            lines = [
                f"📰 A股关注列表数据摘要（{len(ok_items)}/{len(items)} 只成功）",
                "═" * 58,
            ]
            for it in items:
                if not it.get("ok"):
                    lines.append(f"❌ {it.get('symbol')}: {it.get('error')}")
                    lines.append("")
                    continue
                lines.append(f"📊 {it['symbol']}（{it['date']} 收盘）")
                lines.append(f"   收盘 {_f(it['close'])}   涨跌 {_f(it.get('chg_pct'), 2, '%')}")
                lines.append(
                    f"   MA5 {_f(it['ma5'])}  MA10 {_f(it['ma10'])}  "
                    f"MA20 {_f(it['ma20'])}  MA60 {_f(it['ma60'])}"
                )
                lines.append(
                    f"   RSI14 {_f(it['rsi14'])}  量比 {_f(it['vol_ratio'])}  "
                    f"换手 {_f(it['turnover'], 2, '%')}  ATR14 {_f(it['atr14'])}"
                )
                if it.get("fund_flow_5d") is not None:
                    lines.append(f"   近5日主力净流入合计 {_f(it['fund_flow_5d'], 1)} 万元")
                if it.get("news_titles"):
                    lines.append("   近期新闻：")
                    for t in it["news_titles"]:
                        lines.append(f"     · {t}")
                lines.append("")

            lines.append("─" * 58)
            lines.append("📌 以上为客观数据摘要，不含任何预测或投资建议。")
            lines.append("   如需分析结论，请用 stock_predict 取全量数据后由 Agent 研判。")
            lines.append("")
            lines.append(api.DISCLAIMER)
            return {"success": True, "stdout": "\n".join(lines), "stderr": ""}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "stdout": "", "stderr": f"执行异常: {e}"}
