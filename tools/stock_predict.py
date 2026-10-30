# -*- coding: utf-8 -*-
"""
stock_predict 工具：A股多维度原始数据快照（供 Agent 自行分析）。

设计原则：**本工具只提供数据，不做任何预测结论。**
不输出方向/涨跌幅区间/置信度/得分——这些由 Agent（大模型）基于返回的
原始数据自行研判。理由：大模型能理解新闻语义、题材逻辑与盘口异动，
规则引擎只能做关键词计数，且规则口径无法随市场环境灵活调整。

返回内容（结构化文本，便于 Agent 阅读理解）：
  1. 交易时段状态（盘中/午休/盘前/收盘），盘中额外给出实时快照
  2. 日K + 技术指标（MA/MACD/RSI/BOLL/量比/换手/ATR）
  3. 个股主力资金流（近10日）
  4. 市场情绪（涨停池家数/最高连板/行业分布）
  5. 大盘环境（上证近60日）
  6. 消息面新闻原文（交由 Agent 做语义理解）
  7. 两融余额、北向资金、限售解禁
  8. 数据可用性清单（哪项缺失，Agent 据此判断可信度）
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


# ---------------------------------------------------------------------------
# 格式化辅助
# ---------------------------------------------------------------------------
SESSION_TXT = {
    "pre_open": "盘前（未开盘）",
    "trading": "盘中交易中",
    "lunch_break": "午间休市（仍属盘中）",
    "closed": "已收盘",
    "no_data": "无数据",
}


def _f(v, nd=2, suffix=""):
    """安全格式化数值。"""
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        return f"{v:.{nd}f}{suffix}"
    return str(v)


def _fmt_intraday(snap):
    lines = ["", "【一、盘中实时快照】"]
    if not snap:
        lines.append("  当前非盘中（或盘中数据不可用），无实时快照。")
        return lines
    lines.append(
        f"  数据时间 {snap.get('trade_date')} {snap.get('updated_at')}"
        f"（来源 {snap.get('source')}{'，仅最新价' if snap.get('partial') else ''}）"
    )
    lines.append(f"  现价 {_f(snap.get('price'))}   昨收 {_f(snap.get('prev_close'))}   涨跌 {_f(snap.get('chg_pct'), 2, '%')}")
    lines.append(
        f"  开 {_f(snap.get('open'))}  高 {_f(snap.get('high'))}  低 {_f(snap.get('low'))}  "
        f"均价(VWAP) {_f(snap.get('vwap'), 3)}"
    )
    lines.append(
        f"  相对均价 {_f(snap.get('chg_vs_vwap'), 2, '%')}  相对开盘 {_f(snap.get('chg_vs_open'), 2, '%')}  "
        f"日内位置 {_f(snap.get('pos_in_range'), 1, '%')}"
    )
    lines.append(
        f"  盘中量比 {_f(snap.get('vol_ratio_intraday'))}  30分钟动量 {_f(snap.get('mom30'), 2, '%')}  "
        f"振幅 {_f(snap.get('amplitude'), 2, '%')}  已交易 {snap.get('trade_minutes')} 分钟"
    )
    return lines


def _fmt_indicators(ind):
    lines = ["", "【二、技术指标（最新交易日）】"]
    if not ind:
        lines.append("  技术指标不可用。")
        return lines
    lines.append(f"  日期 {ind.get('date')}  收盘 {_f(ind.get('close'))}  涨跌 {_f(ind.get('chg_pct'), 2, '%')}")
    lines.append(
        f"  MA5 {_f(ind.get('ma5'))}  MA10 {_f(ind.get('ma10'))}  "
        f"MA20 {_f(ind.get('ma20'))}  MA60 {_f(ind.get('ma60'))}"
    )
    lines.append(
        f"  MACD: DIF {_f(ind.get('dif'), 4)}  DEA {_f(ind.get('dea'), 4)}  柱 {_f(ind.get('macd'), 4)}"
    )
    lines.append(
        f"  RSI14 {_f(ind.get('rsi14'), 2)}  BOLL 上 {_f(ind.get('boll_up'))} / 中 {_f(ind.get('boll_mid'))} / 下 {_f(ind.get('boll_low'))}"
    )
    lines.append(
        f"  量比 {_f(ind.get('vol_ratio'))}  换手率 {_f(ind.get('turnover'), 2, '%')}  "
        f"5日均换手 {_f(ind.get('turnover_ma5'), 2, '%')}  ATR14 {_f(ind.get('atr14'))}"
    )
    return lines


def _fmt_kline(kline, n=10):
    lines = [f"", f"【三、近 {n} 日K线（含指标）】"]
    if not kline:
        lines.append("  K线数据不可用。")
        return lines
    lines.append("  日期        开      高      低      收      涨跌%   量比   换手%   MA5     MA20")
    for r in kline[-n:]:
        lines.append(
            f"  {str(r.get('date'))[:10]}  "
            f"{_f(r.get('open')):>7} {_f(r.get('high')):>7} {_f(r.get('low')):>7} {_f(r.get('close')):>7} "
            f"{_f(r.get('chg_pct'), 2):>7} {_f(r.get('vol_ratio')):>6} {_f(r.get('turnover'), 2):>6} "
            f"{_f(r.get('ma5')):>7} {_f(r.get('ma20')):>7}"
        )
    return lines


def _fmt_fund_flow(ff):
    lines = ["", "【四、个股主力资金流（近10日）】"]
    if not ff:
        lines.append("  资金流数据不可用（数据源限流或接口异常）。")
        return lines
    lines.append("  日期        收盘    涨跌%   主力净额(万)  主力净占比  超大单净额(万)")
    for r in ff[-10:]:
        main_amt = r.get("主力净流入-净额")
        sup_amt = r.get("超大单净流入-净额")
        lines.append(
            f"  {str(r.get('日期'))[:10]}  {_f(r.get('收盘价')):>7} {_f(r.get('涨跌幅'), 2):>7} "
            f"{_f(main_amt / 10000 if isinstance(main_amt, (int, float)) else None, 1):>12} "
            f"{_f(r.get('主力净流入-净占比'), 2):>10} "
            f"{_f(sup_amt / 10000 if isinstance(sup_amt, (int, float)) else None, 1):>13}"
        )
    return lines


def _fmt_zt(zt):
    lines = ["", "【五、市场情绪（涨停池）】"]
    if not zt:
        lines.append("  涨停池数据不可用。")
        return lines
    lines.append(f"  涨停家数 {zt.get('count')}   最高连板 {zt.get('max_board')}")
    rank = zt.get("industry_rank") or []
    if rank:
        lines.append("  涨停行业分布（前10）：" + "  ".join(f"{x['industry']}({x['count']})" for x in rank))
    lst = zt.get("list") or []
    if lst:
        top = sorted(lst, key=lambda x: -(x.get("连板数") or 0))[:8]
        lines.append("  高标个股：" + "  ".join(
            f"{x.get('名称')}({x.get('连板数')}板)" for x in top
        ))
    return lines


def _fmt_index(idx):
    lines = ["", "【六、大盘环境（上证指数近60日）】"]
    if not idx:
        lines.append("  大盘数据不可用。")
        return lines
    last = idx[-1]
    lines.append(f"  最新 {last.get('date')}  收 {_f(last.get('close'))}")
    for n in (5, 10, 20, 60):
        if len(idx) >= n:
            base = idx[-n].get("close")
            if base:
                lines.append(f"  近{n}日涨跌 {_f((last.get('close') / base - 1) * 100, 2, '%')}")
    lines.append("  近10日收盘：" + "  ".join(_f(r.get("close")) for r in idx[-10:]))
    return lines


def _fmt_news(news):
    lines = ["", "【七、消息面新闻（原文，需语义理解）】"]
    if not news:
        lines.append("  近期无相关新闻。")
        return lines
    for i, n in enumerate(news[:15], 1):
        title = n.get("新闻标题") or ""
        tm = n.get("发布时间") or ""
        src = n.get("文章来源") or ""
        content = (n.get("新闻内容") or "").replace("\n", " ")
        if len(content) > 180:
            content = content[:180] + "…"
        lines.append(f"  {i}. [{tm}] {title}（{src}）")
        if content:
            lines.append(f"     {content}")
    return lines


def _fmt_macro(margin, north, unlock):
    lines = ["", "【八、两融 / 北向 / 解禁】"]
    if margin:
        last = margin[-1]
        keys = [k for k in last.keys() if k not in ("日期",)]
        lines.append(f"  两融（沪市，{last.get('日期')}）：" + "  ".join(
            f"{k} {_f(last.get(k))}" for k in keys[:5]
        ))
    else:
        lines.append("  两融数据不可用。")
    if north:
        for r in north[:4]:
            lines.append(
                f"  北向/南向 {r.get('交易日')} {r.get('板块')}：净买额 {_f(r.get('成交净买额'))}  "
                f"净流入 {_f(r.get('资金净流入'))}"
            )
    else:
        lines.append("  北向资金数据不可用。")
    if unlock:
        lines.append("  未来60天解禁：")
        for r in unlock[:5]:
            lines.append(
                f"    {r.get('解禁时间')}  {r.get('限售股类型')}  "
                f"数量 {_f(r.get('解禁数量'))}  市值 {_f(r.get('实际解禁市值'))}  "
                f"占流通比 {_f(r.get('占解禁前流通市值比例'), 2, '%')}"
            )
    else:
        lines.append("  未来60天无解禁记录。")
    return lines


def _fmt_quality(q):
    lines = ["", "【九、数据可用性】"]
    label = {
        "ok": "✅", "empty": "⚪空", "error": "❌失败",
        "none": "⚪无", "not_available": "⚪非盘中",
    }
    lines.append("  " + "  ".join(f"{k}:{label.get(v, v)}" for k, v in q.items()))
    bad = [k for k, v in q.items() if v == "error"]
    if bad:
        lines.append(f"  ⚠️ 以下数据源本次获取失败，分析时请说明数据缺失：{', '.join(bad)}")
    return lines


class StockPredictTool:
    name = "stock_predict"
    description = (
        "获取A股某只股票的完整原始数据快照，供你（Agent）自行做多维度分析。"
        "返回：交易时段状态、盘中实时快照（现价/均价/日内位置/盘中量比/30分钟动量）、"
        "日K+技术指标（MA/MACD/RSI/BOLL/量比/换手/ATR）、个股主力资金流、"
        "涨停池情绪、大盘环境、新闻原文、两融/北向/解禁、数据可用性清单。"
        "**本工具不输出任何预测结论**——方向、涨跌幅区间、置信度需你基于这些数据自行研判，"
        "并明确标注盘中/非盘中口径差异与数据缺失情况。输入为股票代码（如 600519）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "A股股票代码，如 600519 或 000001，支持 sh600519 格式"},
            "days": {"type": "integer", "description": "日K回溯天数，默认 120"},
        },
        "required": ["symbol"],
    }

    @staticmethod
    def check() -> bool:
        return True

    def execute(self, args):
        try:
            symbol = (args.get("symbol") or "").strip()
            if not symbol:
                return {"success": False, "stdout": "", "stderr": "缺少参数 symbol（股票代码）"}
            days = args.get("days") or 120
            try:
                days = max(30, min(int(days), 500))
            except Exception:  # noqa: BLE001
                days = 120

            snap = api.get_snapshot(symbol, days=days)
            if not snap:
                return {"success": False, "stdout": "", "stderr": f"未获取到 {symbol} 的数据"}

            state = snap.get("session_state")
            lines = [
                f"📊 股票 {snap.get('code')} 原始数据快照",
                f"数据时间 {snap.get('updated_at')}   交易时段：{SESSION_TXT.get(state, state)}",
                "─" * 60,
            ]
            if not snap.get("kline"):
                lines.append("❌ 未获取到日K数据，请检查股票代码是否正确（或该股已退市/停牌）。")
                lines.append("")
                lines.append(snap.get("disclaimer", api.DISCLAIMER))
                return {"success": True, "stdout": "\n".join(lines), "stderr": ""}

            lines += _fmt_intraday(snap.get("intraday"))
            lines += _fmt_indicators(snap.get("indicators"))
            lines += _fmt_kline(snap.get("kline"), n=10)
            lines += _fmt_fund_flow(snap.get("fund_flow"))
            lines += _fmt_zt(snap.get("zt_pool"))
            lines += _fmt_index(snap.get("index"))
            lines += _fmt_news(snap.get("news"))
            lines += _fmt_macro(snap.get("margin"), snap.get("north_flow"), snap.get("unlock"))
            lines += _fmt_quality(snap.get("data_quality") or {})

            lines.append("")
            lines.append("─" * 60)
            lines.append("📌 以上为客观数据汇总，不含任何预测结论。请基于数据自行分析，并注意：")
            lines.append("   · 盘中数据为实时快照，与收盘数据口径不同；")
            lines.append("   · 数据可用性中标注失败/空的维度，分析时应说明缺失。")
            lines.append("")
            lines.append(snap.get("disclaimer", api.DISCLAIMER))
            return {"success": True, "stdout": "\n".join(lines), "stderr": ""}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "stdout": "", "stderr": f"执行异常: {e}"}
