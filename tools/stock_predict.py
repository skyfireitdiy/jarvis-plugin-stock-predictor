# -*- coding: utf-8 -*-
"""
stock_predict 工具：A股中短线走势综合预测。
综合量价技术面 / 资金结构面 / 情绪题材面 / 大盘环境 / 消息事件面 五维信号，
盘中时自动注入盘中走势维度（实时价相对均价/开盘/日内位置/盘中量比/30分钟动量）。
支持 mode 参数选择是否引入 LLM 综合研判（规则 + AI 双轨对照）。
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
    is_intra = r.get("is_intraday")
    if is_intra:
        snap = r.get("intraday") or {}
        state = r.get("session_state")
        state_txt = {"trading": "盘中交易中", "lunch_break": "午间休市"}.get(state, state)
        lines.append(f"📊 股票 {r['symbol']} 盘中实时预测（{state_txt}，数据截至 {snap.get('updated_at', '--')}）")
    else:
        lines.append(f"📊 股票 {r['symbol']} 次日走势预测（基于 {r['trade_date']} 收盘数据）")
    lines.append("─" * 50)
    if is_intra:
        snap = r.get("intraday") or {}
        lines.append(
            f"💹 实时行情：现价 {snap.get('price')}  "
            f"涨跌 {snap.get('chg_pct'):+.2f}%  "
            f"（昨收 {snap.get('prev_close')}）"
        )
        lines.append(
            f"    相对均价 {snap.get('chg_vs_vwap'):+.2f}%   "
            f"日内位置 {snap.get('pos_in_range')}%   "
            f"盘中量比 {snap.get('vol_ratio_intraday')}"
        )
        lines.append(f"📈 后续走势区间（相对现价 {r.get('ref_price')}）："
                     f"{r['range_low']:+.2f}% ~ {r['range_high']:+.2f}%")
    else:
        lines.append(f"📈 预测涨跌幅区间：{r['range_low']:+.2f}% ~ {r['range_high']:+.2f}%")
    lines.append(f"🎯 预测方向：{r['direction']}")
    lines.append(f"🧮 综合得分：{r['score']} / 100")
    lines.append(f"🔒 置信度：{r['confidence']}%")
    if r.get("signals"):
        lines.append("")
        lines.append("⚡ 触发信号：")
        for s in r["signals"]:
            lines.append(f"    {s}")
    lines.append("")
    lines.append("📋 各维度明细：")
    for key in ("intraday", "tech", "fund", "sentiment", "market", "news"):
        d = r["dims"].get(key)
        if not d:
            continue
        lines.append(f"  【{d['name']}】得分 {d['score']}（权重 {d['weight']*100:.0f}%）")
        lines.append(_fmt_evidence(d["evidence"]))
    # LLM 研判
    llm = r.get("llm")
    if llm:
        lines.append("")
        lines.append("─" * 50)
        if llm.get("available"):
            lines.append(f"🤖 AI 研判：{llm.get('direction')}（置信度 {llm.get('confidence')}%）")
            if r.get("conflict"):
                lines.append("⚠️ 注意：规则与 AI 研判方向分歧，谨慎参考")
            lines.append(f"    {llm.get('reasoning')}")
            if llm.get("risk_flags"):
                lines.append("    风险提示：")
                for f in llm["risk_flags"]:
                    lines.append(f"      · {f}")
            lines.append(f"    {llm.get('disclaimer', '')}")
        else:
            lines.append(f"🤖 AI 研判不可用：{llm.get('error', '未知原因')}（已降级为纯规则结果）")
    lines.append("")
    lines.append(r["disclaimer"])
    return "\n".join(lines)
class StockPredictTool:
    name = "stock_predict"
    description = (
        "预测A股某只股票走势：综合量价技术面/资金结构面/情绪题材面/大盘环境/消息事件面"
        "五维信号；盘中调用时自动注入盘中走势维度（实时价相对均价/开盘/日内位置/盘中量比/"
        "30分钟动量），输出方向(看涨/看跌/震荡)、预测涨跌幅区间、触发信号、置信度与逐项证据。"
        "mode 可选 rule(纯规则,默认)/llm(规则+AI研判)/hybrid(双轨对照,冲突告警)。"
        "输入为股票代码（如 600519）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "A股股票代码，如 600519 或 000001，支持 sh600519 格式"},
            "mode": {
                "type": "string",
                "enum": ["rule", "llm", "hybrid"],
                "description": "预测模式：rule=纯规则(默认，最快)；llm=规则+AI研判；hybrid=规则与AI双轨对照，方向冲突时告警",
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
            if not symbol:
                return {"success": False, "stdout": "", "stderr": "缺少参数 symbol（股票代码）"}
            mode = (args.get("mode") or "rule").strip().lower()
            if mode not in ("rule", "llm", "hybrid"):
                mode = "rule"
            r = api.predict(symbol, mode=mode)
            if not r.get("success"):
                return {"success": False, "stdout": "", "stderr": r.get("error", "预测失败")}
            return {"success": True, "stdout": _fmt_report(r), "stderr": ""}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "stdout": "", "stderr": f"执行异常: {e}"}
