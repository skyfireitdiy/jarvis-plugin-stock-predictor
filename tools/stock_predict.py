# -*- coding: utf-8 -*-
"""
stock_predict 工具：A股次日走势综合预测。

综合量价技术面 / 资金结构面 / 情绪题材面 / 大盘环境 / 消息事件面 五维信号，
输出方向、预测涨跌幅区间、触发信号、置信度与逐项可解释证据。

使用方式：Agent 对话中触发（如"预测明天 600519 走势"），或直接调用工具。
"""

import os
import sys

# 加载插件私有功能 api.py（plugin/ 不在 sys.path，用 importlib 按文件加载）
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_API_PATH = os.path.join(_PLUGIN_DIR, "plugin", "api.py")

_importlib = __import__("importlib")
_spec = _importlib.util.spec_from_file_location("stock_predictor_api", _API_PATH)
api = _importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)


def _fmt_evidence(items):
    return "\n".join(f"    - {it}" for it in items)


def _fmt_report(r):
    """把 predict() 返回的 dict 格式化为可读文本。"""
    if not r.get("success"):
        return f"❌ {r.get('error', '未知错误')}"

    lines = []
    lines.append(f"📊 股票 {r['symbol']} 次日走势预测（基于 {r['trade_date']} 收盘数据）")
    lines.append("─" * 50)
    lines.append(f"🎯 预测方向：{r['direction']}")
    lines.append(f"📈 预测涨跌幅区间：{r['range_low']:+.2f}% ~ {r['range_high']:+.2f}%")
    lines.append(f"🧮 综合得分：{r['score']} / 100")
    lines.append(f"🔒 置信度：{r['confidence']}%")
    if r.get("signals"):
        lines.append("")
        lines.append("⚡ 触发信号：")
        for s in r["signals"]:
            lines.append(f"    {s}")
    lines.append("")
    lines.append("📋 各维度明细：")
    for key in ("tech", "fund", "sentiment", "market", "news"):
        d = r["dims"][key]
        lines.append(f"  【{d['name']}】得分 {d['score']}（权重 {d['weight']*100:.0f}%）")
        lines.append(_fmt_evidence(d["evidence"]))
    lines.append("")
    lines.append(r["disclaimer"])
    return "\n".join(lines)


class StockPredictTool:
    name = "stock_predict"
    description = (
        "预测A股某只股票次日走势：综合量价技术面/资金结构面/情绪题材面/"
        "大盘环境/消息事件面五维信号，输出方向(看涨/看跌/震荡)、预测涨跌幅区间、"
        "触发信号、置信度与逐项可解释证据。输入为股票代码（如 600519）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "A股股票代码，如 600519 或 000001，支持 sh600519 格式"},
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
                return {"success": False, "stdout": "", "stderr": "缺少参数 symbol（股票代码）"}
            r = api.predict(symbol)
            if not r.get("success"):
                return {"success": False, "stdout": "", "stderr": r.get("error", "预测失败")}
            return {"success": True, "stdout": _fmt_report(r), "stderr": ""}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "stdout": "", "stderr": f"执行异常: {e}"}
