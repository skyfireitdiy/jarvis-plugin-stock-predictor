# -*- coding: utf-8 -*-
"""
stock_analyze 工具：A股单维度原始数据（供 Agent 按维度聚焦分析）。

与 stock_predict 的区别：stock_predict 一次给全量数据，本工具可按维度只取
一部分数据，减少 token 消耗。**同样只给数据，不给结论。**

维度：
  tech      技术面：日K + MA/MACD/RSI/BOLL/量比/换手/ATR
  fund      资金面：个股主力资金流（近10日）+ 北向资金
  sentiment 情绪面：涨停池家数/最高连板/行业分布/高标个股
  market    大盘：上证指数近60日走势
  news      消息面：新闻原文（交由 Agent 做语义理解）
  intraday  盘中：实时快照（现价/均价/日内位置/盘中量比/30分钟动量）
不传 dimension 则返回全部维度。
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

DIM_ALIAS = {
    "tech": "tech", "技术": "tech", "技术面": "tech",
    "fund": "fund", "资金": "fund", "资金面": "fund",
    "sentiment": "sentiment", "情绪": "sentiment", "情绪面": "sentiment",
    "market": "market", "大盘": "market",
    "news": "news", "消息": "news", "消息面": "news",
    "intraday": "intraday", "盘中": "intraday",
}

DIM_TITLE = {
    "tech": "技术面", "fund": "资金面", "sentiment": "情绪面",
    "market": "大盘环境", "news": "消息面", "intraday": "盘中实时",
}


def _f(v, nd=2, suffix=""):
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        return f"{v:.{nd}f}{suffix}"
    return str(v)


def _dim_tech(code, snap):
    ind = snap.get("indicators")
    kline = snap.get("kline") or []
    lines = [f"【技术面】{code}"]
    if not ind:
        return lines + ["  技术指标不可用（未取到日K）。"]
    lines.append(f"  最新交易日 {ind.get('date')}  收盘 {_f(ind.get('close'))}")
    lines.append(
        f"  MA5 {_f(ind.get('ma5'))}  MA10 {_f(ind.get('ma10'))}  "
        f"MA20 {_f(ind.get('ma20'))}  MA60 {_f(ind.get('ma60'))}"
    )
    lines.append(f"  MACD: DIF {_f(ind.get('dif'), 4)}  DEA {_f(ind.get('dea'), 4)}  柱 {_f(ind.get('macd'), 4)}")
    lines.append(f"  RSI14 {_f(ind.get('rsi14'))}  BOLL 上 {_f(ind.get('boll_up'))} / 中 {_f(ind.get('boll_mid'))} / 下 {_f(ind.get('boll_low'))}")
    lines.append(f"  量比 {_f(ind.get('vol_ratio'))}  换手 {_f(ind.get('turnover'), 2, '%')}  5日均换手 {_f(ind.get('turnover_ma5'), 2, '%')}  ATR14 {_f(ind.get('atr14'))}")
    if kline:
        lines.append("  近10日K线：")
        lines.append("    日期        开      高      低      收      涨跌%   量比   换手%")
        for r in kline[-10:]:
            lines.append(
                f"    {str(r.get('date'))[:10]}  {_f(r.get('open')):>7} {_f(r.get('high')):>7} "
                f"{_f(r.get('low')):>7} {_f(r.get('close')):>7} {_f(r.get('chg_pct'), 2):>7} "
                f"{_f(r.get('vol_ratio')):>6} {_f(r.get('turnover'), 2):>6}"
            )
    return lines


def _dim_fund(code, snap):
    ff = snap.get("fund_flow")
    north = snap.get("north_flow")
    lines = [f"【资金面】{code}"]
    if ff:
        lines.append("  个股主力资金流（近10日）：")
        lines.append("    日期        收盘    涨跌%   主力净额(万)  主力净占比  超大单(万)  大单(万)")
        for r in ff[-10:]:
            main_amt = r.get("主力净流入-净额")
            sup_amt = r.get("超大单净流入-净额")
            big_amt = r.get("大单净流入-净额")
            lines.append(
                f"    {str(r.get('日期'))[:10]}  {_f(r.get('收盘价')):>7} {_f(r.get('涨跌幅'), 2):>7} "
                f"{_f(main_amt / 10000 if isinstance(main_amt, (int, float)) else None, 1):>12} "
                f"{_f(r.get('主力净流入-净占比'), 2):>10} "
                f"{_f(sup_amt / 10000 if isinstance(sup_amt, (int, float)) else None, 1):>10} "
                f"{_f(big_amt / 10000 if isinstance(big_amt, (int, float)) else None, 1):>9}"
            )
    else:
        lines.append("  个股资金流数据不可用（数据源限流或接口异常）。")
    if north:
        lines.append("  北向/南向资金：")
        for r in north[:4]:
            lines.append(
                f"    {r.get('交易日')} {r.get('板块')}：净买额 {_f(r.get('成交净买额'))}  "
                f"净流入 {_f(r.get('资金净流入'))}  涨/平/跌 {r.get('上涨数')}/{r.get('持平数')}/{r.get('下跌数')}"
            )
    else:
        lines.append("  北向资金数据不可用。")
    return lines


def _dim_sentiment(code, snap):
    zt = snap.get("zt_pool")
    lines = ["【情绪面】市场涨停池"]
    if not zt:
        return lines + ["  涨停池数据不可用。"]
    lines.append(f"  涨停家数 {zt.get('count')}   最高连板 {zt.get('max_board')}")
    rank = zt.get("industry_rank") or []
    if rank:
        lines.append("  涨停行业分布（前10）：" + "  ".join(f"{x['industry']}({x['count']})" for x in rank))
    lst = zt.get("list") or []
    if lst:
        top = sorted(lst, key=lambda x: -(x.get("连板数") or 0))[:10]
        lines.append("  连板高标：")
        for x in top:
            lines.append(
                f"    {x.get('名称')}({x.get('代码')}) {x.get('连板数')}板  "
                f"涨跌 {_f(x.get('涨跌幅'), 2, '%')}  封板资金 {_f(x.get('封板资金'))}  "
                f"炸板 {x.get('炸板次数')}次  行业 {x.get('所属行业')}"
            )
    return lines


def _dim_market(code, snap):
    idx = snap.get("index")
    lines = ["【大盘环境】上证指数"]
    if not idx:
        return lines + ["  大盘数据不可用。"]
    last = idx[-1]
    lines.append(f"  最新 {last.get('date')}  收 {_f(last.get('close'))}")
    for n in (5, 10, 20, 60):
        if len(idx) >= n:
            base = idx[-n].get("close")
            if base:
                lines.append(f"  近{n}日涨跌 {_f((last.get('close') / base - 1) * 100, 2, '%')}")
    lines.append("  近10日收盘：" + "  ".join(_f(r.get("close")) for r in idx[-10:]))
    return lines


def _dim_news(code, snap):
    news = snap.get("news") or []
    lines = [f"【消息面】{code} 相关新闻（原文，需语义理解）"]
    if not news:
        return lines + ["  近期无相关新闻。"]
    for i, n in enumerate(news[:15], 1):
        lines.append(f"  {i}. [{n.get('发布时间')}] {n.get('新闻标题')}（{n.get('文章来源')}）")
        content = (n.get("新闻内容") or "").replace("\n", " ")
        if content:
            lines.append(f"     {content[:200]}{'…' if len(content) > 200 else ''}")
    return lines


def _dim_intraday(code, snap):
    intra = snap.get("intraday")
    lines = [f"【盘中实时】{code}"]
    if not intra:
        state = snap.get("session_state")
        return lines + [f"  当前非盘中（session_state={state}），无实时快照。"]
    lines.append(f"  数据时间 {intra.get('trade_date')} {intra.get('updated_at')}（来源 {intra.get('source')}）")
    lines.append(f"  现价 {_f(intra.get('price'))}  昨收 {_f(intra.get('prev_close'))}  涨跌 {_f(intra.get('chg_pct'), 2, '%')}")
    lines.append(f"  开 {_f(intra.get('open'))}  高 {_f(intra.get('high'))}  低 {_f(intra.get('low'))}  VWAP {_f(intra.get('vwap'), 3)}")
    lines.append(
        f"  相对均价 {_f(intra.get('chg_vs_vwap'), 2, '%')}  相对开盘 {_f(intra.get('chg_vs_open'), 2, '%')}  "
        f"日内位置 {_f(intra.get('pos_in_range'), 1, '%')}"
    )
    lines.append(
        f"  盘中量比 {_f(intra.get('vol_ratio_intraday'))}  30分钟动量 {_f(intra.get('mom30'), 2, '%')}  "
        f"振幅 {_f(intra.get('amplitude'), 2, '%')}  已交易 {intra.get('trade_minutes')} 分钟"
    )
    return lines


DIM_FUNCS = {
    "tech": _dim_tech,
    "fund": _dim_fund,
    "sentiment": _dim_sentiment,
    "market": _dim_market,
    "news": _dim_news,
    "intraday": _dim_intraday,
}

# 各维度所需的快照数据块（传给 api.get_snapshot 的 sections），
# 避免只取一个维度却把九大数据源全部拉一遍（耗时从 ~10s 降到 1~2s）。
DIM_SECTIONS = {
    "tech": ["kline"],
    "fund": ["fund"],
    "sentiment": ["zt_pool"],
    "market": ["index"],
    "news": ["news"],
    "intraday": ["kline", "intraday"],
}


class StockAnalyzeTool:
    name = "stock_analyze"
    description = (
        "按维度获取A股某只股票的原始数据，供你（Agent）聚焦分析某一面。"
        "维度：tech(技术面)/fund(资金面)/sentiment(情绪面)/market(大盘)/news(消息面)/intraday(盘中实时)，"
        "不传 dimension 则返回全部维度。**只返回客观数据，不含任何预测结论**。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "A股股票代码，如 600519"},
            "dimension": {
                "type": "string",
                "description": "维度：tech/fund/sentiment/market/news/intraday，不填则全部",
                "enum": ["tech", "fund", "sentiment", "market", "news", "intraday"],
            },
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
                return {"success": False, "stdout": "", "stderr": "缺少参数 symbol"}
            raw_dim = (args.get("dimension") or "").strip()
            dim = DIM_ALIAS.get(raw_dim, raw_dim) if raw_dim else None
            if dim and dim not in DIM_FUNCS:
                return {
                    "success": False, "stdout": "",
                    "stderr": f"不支持的维度 {raw_dim}，可选：{', '.join(DIM_FUNCS.keys())}",
                }

            # 只拉取所需维度的数据块，避免单维度查询也触发全量九源抓取
            if dim:
                sections = DIM_SECTIONS.get(dim)
            else:
                sections = None
            snap = api.get_snapshot(symbol, days=120, sections=sections)
            code = snap.get("code") or api.norm_symbol(symbol)
            # 仅当"所取维度全部无数据"时才判定失败（单维度查询不会拉 kline，
            # 不能再用 kline/intraday 作为唯一有效性判据）
            if sections is None:
                has_data = bool(snap.get("kline") or snap.get("intraday"))
            else:
                # sections 里可能有别名（如 fund → fund_flow/north_flow），需归一化后再查
                has_data = any(snap.get(k) for k in api._resolve_sections(sections))
            if not has_data:
                return {"success": False, "stdout": "", "stderr": f"未获取到 {code} 的数据（代码可能有误或已退市）"}

            dims = [dim] if dim else list(DIM_FUNCS.keys())
            lines = [
                f"🔍 {code} 维度数据（{snap.get('updated_at')}，session_state={snap.get('session_state')}）",
                "─" * 56,
            ]
            for d in dims:
                lines.append("")
                lines.extend(DIM_FUNCS[d](code, snap))

            q = snap.get("data_quality") or {}
            bad = [k for k, v in q.items() if v == "error"]
            if bad:
                lines.append("")
                lines.append(f"⚠️ 数据源获取失败：{', '.join(bad)}（分析时请说明缺失）")
            lines.append("")
            lines.append(api.DISCLAIMER)
            return {"success": True, "stdout": "\n".join(lines), "stderr": ""}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "stdout": "", "stderr": f"执行异常: {e}"}
