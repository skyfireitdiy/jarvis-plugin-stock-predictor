# -*- coding: utf-8 -*-
"""
stock_analyze 工具：A股单维度深度分析。

可单独分析技术面(tech)/资金面(fund)/情绪面(sentiment)/大盘(market)/消息面(news)，
不传 dimension 则全部分析。
"""

import os

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_API_PATH = os.path.join(_PLUGIN_DIR, "plugin", "api.py")

_importlib = __import__("importlib")
_spec = _importlib.util.spec_from_file_location("stock_predictor_api", _API_PATH)
api = _importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)


class StockAnalyzeTool:
    name = "stock_analyze"
    description = (
        "分析A股某只股票的某个维度（技术面/资金面/情绪面/大盘/消息面），"
        "或全部维度。输入股票代码与可选维度名。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "A股股票代码，如 600519"},
            "dimension": {
                "type": "string",
                "description": "要分析的维度：tech(技术面)/fund(资金面)/sentiment(情绪面)/market(大盘)/news(消息面)，不填则全部分析",
                "enum": ["tech", "fund", "sentiment", "market", "news"],
            },
        },
        "required": ["symbol"],
    }

    @staticmethod
    def check() -> bool:
        return True

    def execute(self, args):
        try:
            symbol = args.get("symbol", "").strip()
            dimension = args.get("dimension") or None
            if not symbol:
                return {"success": False, "stdout": "", "stderr": "缺少参数 symbol"}
            r = api.analyze(symbol, dimension)
            if not r.get("success"):
                return {"success": False, "stdout": "", "stderr": r.get("error", "分析失败")}
            lines = [f"🔍 股票 {r['symbol']} 维度分析"]
            lines.append("─" * 40)
            for k, d in r["dimensions"].items():
                lines.append(f"【{d['name']}】得分 {d['score']}")
                for it in d["evidence"]:
                    lines.append(f"    - {it}")
                lines.append("")
            return {"success": True, "stdout": "\n".join(lines).rstrip(), "stderr": ""}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "stdout": "", "stderr": f"执行异常: {e}"}
