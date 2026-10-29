# -*- coding: utf-8 -*-
"""
stock_daily_report 工具：盘后预测日报。

对一组关注股票生成次日预测报告，供定时任务调用（每交易日盘后）。
关注列表可通过 symbols 参数传入（逗号分隔），或使用默认列表。
"""

import os

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_API_PATH = os.path.join(_PLUGIN_DIR, "plugin", "api.py")

_importlib = __import__("importlib")
_spec = _importlib.util.spec_from_file_location("stock_predictor_api", _API_PATH)
api = _importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)

# 默认关注列表（可改）
DEFAULT_SYMBOLS = ["600519", "000858", "300750"]


class StockDailyReportTool:
    name = "stock_daily_report"
    description = (
        "生成A股关注列表的盘后预测日报：对每只股票输出次日方向/涨跌幅区间/"
        "触发信号/置信度。供定时任务或手动批量使用。"
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
            syms = [s.strip() for s in symbols.split(",") if s.strip()] or DEFAULT_SYMBOLS
            r = api.daily_report(syms)
            if not r.get("success"):
                return {"success": False, "stdout": "", "stderr": r.get("error", "日报生成失败")}
            lines = [f"📰 A股盘后预测日报（{len(r['reports'])} 只）", "═" * 50]
            for rep in r["reports"]:
                if not rep.get("success"):
                    lines.append(f"❌ {rep.get('symbol','?')}: {rep.get('error','失败')}")
                    lines.append("")
                    continue
                lines.append(f"📊 {rep['symbol']}（基于 {rep['trade_date']} 收盘）")
                lines.append(f"   🎯 {rep['direction']}  📈 {rep['range_low']:+.2f}%~{rep['range_high']:+.2f}%  🔒 置信度 {rep['confidence']}%")
                for s in rep.get("signals", [])[:3]:
                    lines.append(f"   ⚡ {s}")
                lines.append("")
            lines.append(api.DISCLAIMER)
            return {"success": True, "stdout": "\n".join(lines), "stderr": ""}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "stdout": "", "stderr": f"执行异常: {e}"}
