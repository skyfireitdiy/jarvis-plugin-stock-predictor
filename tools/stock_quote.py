# -*- coding: utf-8 -*-
"""
stock_quote 工具：A股行情快照。

获取最新收盘价、当日涨跌幅、量比、均线位置、RSI 等关键指标，
适合快速瞄一眼个股当前状态。
"""

import os

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_API_PATH = os.path.join(_PLUGIN_DIR, "plugin", "api.py")

_importlib = __import__("importlib")
_spec = _importlib.util.spec_from_file_location("stock_predictor_api", _API_PATH)
api = _importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)


class StockQuoteTool:
    name = "stock_quote"
    description = (
        "获取A股某只股票当前行情快照：最新收盘价、当日涨跌幅、量比、"
        "MA5/10/20/60均线位置、RSI。输入为股票代码（如 600519）。"
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
            symbol = args.get("symbol", "").strip()
            if not symbol:
                return {"success": False, "stdout": "", "stderr": "缺少参数 symbol"}
            r = api.quote(symbol)
            if not r.get("success"):
                return {"success": False, "stdout": "", "stderr": r.get("error", "获取失败")}
            lines = [
                f"💹 股票 {r['symbol']} 行情快照（{r['date']}）",
                "─" * 40,
                f"最新收盘：{r['close']}  (当日 {r['chg_pct']:+.2f}%)",
            ]
            if r.get("volume_ratio") is not None:
                lines.append(f"量比：{r['volume_ratio']}")
            for n in ("ma5", "ma10", "ma20", "ma60"):
                if r.get(n) is not None:
                    lines.append(f"MA{n[2:]}：{r[n]}")
            if r.get("rsi14") is not None:
                lines.append(f"RSI14：{r['rsi14']}")
            return {"success": True, "stdout": "\n".join(lines), "stderr": ""}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "stdout": "", "stderr": f"执行异常: {e}"}
