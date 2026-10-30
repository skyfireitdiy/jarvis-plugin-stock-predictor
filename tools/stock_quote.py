# -*- coding: utf-8 -*-
"""
stock_quote 工具：A股行情快照（轻量）。

只取"最小必要"行情数据：最新价、涨跌幅、均线位置、RSI、量比、换手。
盘中自动附带实时价与盘中量比。**不输出任何预测结论**，仅客观数值。
适合 Agent 快速确认一只股票当前状态，再决定是否调用 stock_predict 取全量数据。
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

SESSION_TXT = {
    "pre_open": "盘前", "trading": "盘中", "lunch_break": "午间休市",
    "closed": "已收盘", "no_data": "无数据",
}


def _f(v, nd=2, suffix=""):
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        return f"{v:.{nd}f}{suffix}"
    return str(v)


class StockQuoteTool:
    name = "stock_quote"
    description = (
        "获取A股某只股票的轻量行情快照：最新价/涨跌幅、MA5/10/20/60 均线、RSI14、"
        "量比、换手率、ATR；盘中额外返回实时价与盘中量比。"
        "**只返回客观数值，不含任何预测结论**。输入为股票代码（如 600519）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "A股股票代码，如 600519"},
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
            code = api.norm_symbol(symbol)
            df = api.get_kline(code, days=120)
            if df is None or df.empty:
                return {"success": False, "stdout": "", "stderr": f"未获取到 {code} 的行情数据（代码可能有误或已退市）"}

            ind_df = api._calc_indicators(df)
            last = api._last_row(ind_df)
            atr = api._compute_atr(ind_df, 14)
            in_session, state = api.is_trading_session()

            lines = [
                f"💹 股票 {code} 行情快照（{SESSION_TXT.get(state, state)}）",
                "─" * 44,
                f"最新收盘：{_f(last.get('close'))}（{last.get('date')}）",
            ]
            # 上一交易日涨跌幅
            if len(ind_df) >= 2:
                prev_close = api._safe_float(ind_df["close"].iloc[-2])
                cur = api._safe_float(last.get("close"))
                if prev_close and cur:
                    lines.append(f"当日涨跌：{_f((cur / prev_close - 1) * 100, 2, '%')}")
            lines.append(
                f"均线：MA5 {_f(last.get('ma5'))}  MA10 {_f(last.get('ma10'))}  "
                f"MA20 {_f(last.get('ma20'))}  MA60 {_f(last.get('ma60'))}"
            )
            lines.append(
                f"MACD：DIF {_f(last.get('dif'), 4)}  DEA {_f(last.get('dea'), 4)}  柱 {_f(last.get('macd'), 4)}"
            )
            lines.append(
                f"RSI14：{_f(last.get('rsi14'))}   BOLL 上 {_f(last.get('boll_up'))} / 中 {_f(last.get('boll_mid'))} / 下 {_f(last.get('boll_low'))}"
            )
            lines.append(
                f"量比：{_f(last.get('vol_ratio'))}   换手率：{_f(last.get('turnover'), 2, '%')}   ATR14：{_f(atr)}"
            )

            # 盘中实时
            if in_session:
                intra = api.get_intraday(code)
                if intra and intra.get("available"):
                    lines.append("")
                    lines.append(
                        f"⏱ 盘中实时（{intra.get('updated_at')}）：现价 {_f(intra.get('price'))}  "
                        f"涨跌 {_f(intra.get('chg_pct'), 2, '%')}  均价 {_f(intra.get('vwap'), 3)}"
                    )
                    lines.append(
                        f"   日内位置 {_f(intra.get('pos_in_range'), 1, '%')}  "
                        f"盘中量比 {_f(intra.get('vol_ratio_intraday'))}  "
                        f"30分钟动量 {_f(intra.get('mom30'), 2, '%')}"
                    )
                else:
                    lines.append("")
                    lines.append(f"⏱ 盘中实时数据暂不可用（{intra.get('reason') if intra else '未知'}）")

            lines.append("")
            lines.append(api.DISCLAIMER)
            return {"success": True, "stdout": "\n".join(lines), "stderr": ""}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "stdout": "", "stderr": f"执行异常: {e}"}
