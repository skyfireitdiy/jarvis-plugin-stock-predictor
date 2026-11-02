// stock-predictor 插件前端侧边栏视图（v0.2.0 纯数据面板）。
// 约定：纯浏览器 ES module，用 window.Vue 渲染，导出 default 组件，不 import 'vue'。
//
// 设计原则：**前端只展示客观数据，不做任何预测判断。**
// 所有分析结论由 Agent（大模型）在对话中给出——点击"让 Agent 分析"按钮
// 会把当前股票的数据摘要发给当前活跃 Agent，由 Agent 做多维度研判。
//
// 注意：模板中引用的所有函数必须定义在 methods/computed 上（v0.1.2 空白 bug 教训）；
//       根 div 必须 height:100% + overflow:auto + box-sizing:border-box（v0.1.3 滚动 bug 教训）。
const PLUGIN = "stock-predictor";

// 自选股列表持久化 key（按插件固定，浏览器本地存储）
const WATCHLIST_KEY = "jarvis_stock_predictor_watchlist";

// 兜底免责声明（后端 get_snapshot 正常返回时会带 disclaimer 覆盖）
const FALLBACK_DISCLAIMER =
  "⚠️ 本面板仅展示公开行情数据的客观汇总，不构成任何投资建议。股市有风险，入市需谨慎，据此操作盈亏自负。";

// 交易时段中文
const SESSION_TXT = {
  pre_open: "盘前",
  trading: "盘中交易中",
  lunch_break: "午间休市",
  closed: "已收盘",
  no_data: "无数据",
};

// 复刻 App.vue 的 gateway 地址解析（侧边栏组件无 props 注入，需自行解析）
function parseGatewayAddress(address) {
  address = (address || "").trim();
  if (!address) return null;
  if (address.includes("://")) {
    try {
      const url = new URL(address);
      return {
        protocol: url.protocol.replace(":", ""),
        host: url.hostname,
        port:
          url.port ||
          (url.protocol === "https:" || url.protocol === "wss:" ? "443" : "80"),
        path: url.pathname,
      };
    } catch {
      return null;
    }
  }
  if (address.includes(":")) {
    const parts = address.split(":");
    if (parts.length === 2) {
      return { protocol: null, host: parts[0], port: parts[1], path: "" };
    }
  }
  return { protocol: null, host: address, port: "8000", path: "" };
}
// 获取当前页面的 HTTP 协议（与 App.vue getHttpProtocol 一致）
function getHttpProtocol() {
  return window.location.protocol === "https:" ? "https" : "http";
}
function gatewayBase() {
  const parsed = parseGatewayAddress(
    localStorage.getItem("jarvis_gateway_url") || "",
  );
  const host = (parsed && parsed.host) || "127.0.0.1";
  const port = (parsed && parsed.port) || "8000";
  const protocol =
    parsed && parsed.protocol === "https" ? "https" : getHttpProtocol();
  return `${protocol}://${host}:${port}`;
}
// 通过 gateway 插件功能代理端点调用插件私有功能
async function callFunction(functionName, args, nodeId) {
  const authedFetch =
    (typeof window !== "undefined" && window.__jarvisFetch) || fetch;
  const targetNodeId = String(nodeId || "master").trim() || "master";
  const resp = await authedFetch(
    `${gatewayBase()}/api/plugins/${encodeURIComponent(targetNodeId)}/function-call`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        plugin: PLUGIN,
        function: functionName,
        arguments: args || {},
      }),
    },
  );
  let result;
  try {
    result = await resp.json();
  } catch (e) {
    throw new Error("网关响应异常", { cause: e });
  }
  if (!result || !result.success) {
    const err = (result && result.error) || {};
    throw new Error(err.message || err.code || "调用失败");
  }
  return result.result;
}

// A股配色：红涨绿跌。以下为模块顶层纯函数，仅供 methods 内调用；
// 模板通过实例方法 chgColor/maColor 间接访问（见 methods）。
function upColorTop(v) {
  const n = Number(v);
  if (Number.isNaN(n) || n === 0) return "#dddddd";
  return n > 0 ? "#ef4444" : "#22c55e";
}
// 均线位置色：价在均线上方红、下方绿
function maColorTop(price, ma) {
  const p = Number(price);
  const m = Number(ma);
  if (Number.isNaN(p) || Number.isNaN(m) || !m) return "#888888";
  return p >= m ? "#ef4444" : "#22c55e";
}

export default {
  name: "StockPredictorSidebarView",
  data() {
    return {
      searchInput: "",
      watchlist: [],
      current: "",
      loading: false,
      pendingSymbol: "",
      error: "",
      snap: null,
      quoteCache: {},
      showKline: true,
      showFund: true,
      showNews: true,
      showMacro: false,
      showZt: false,
      sending: false,
    };
  },
  mounted() {
    this.loadWatchlist();
    // 打开面板时对自选股做一次轻量名称获取（仅拉名称，不触发 10 秒全量抓取），
    // 让自选股 chip 能显示「名称 代码」。完整行情（价格/涨跌）由用户点击后 refreshQuotes 拉取。
    this.loadWatchlistNames();
  },
  computed: {
    fallbackDisclaimer() {
      return FALLBACK_DISCLAIMER;
    },
    sessionText() {
      const s = this.snap && this.snap.session_state;
      return SESSION_TXT[s] || s || "--";
    },
    isIntraday() {
      return !!(this.snap && this.snap.is_intraday);
    },
    intra() {
      return (this.snap && this.snap.intraday) || null;
    },
    ind() {
      return (this.snap && this.snap.indicators) || null;
    },
    kline() {
      const k = (this.snap && this.snap.kline) || [];
      return Array.isArray(k) ? k.slice(-60) : [];
    },
    fundFlow() {
      const f = (this.snap && this.snap.fund_flow) || [];
      return Array.isArray(f) ? f.slice(-10).reverse() : [];
    },
    zt() {
      return (this.snap && this.snap.zt_pool) || null;
    },
    indexList() {
      const i = (this.snap && this.snap.index) || [];
      return Array.isArray(i) ? i : [];
    },
    indexSummary() {
      const list = this.indexList;
      if (list.length === 0) return null;
      const last = list[list.length - 1];
      const chg = (n) => {
        if (list.length < n) return null;
        const base = Number(list[list.length - n].close);
        const cur = Number(last.close);
        if (!base || !cur) return null;
        return (cur / base - 1) * 100;
      };
      return {
        date: String(last.date || "").slice(0, 10),
        close: last.close,
        d5: chg(5),
        d10: chg(10),
        d20: chg(20),
        d60: chg(60),
      };
    },
    newsList() {
      const n = (this.snap && this.snap.news) || [];
      return Array.isArray(n) ? n.slice(0, 10) : [];
    },
    margin() {
      const m = (this.snap && this.snap.margin) || [];
      return Array.isArray(m) && m.length > 0 ? m[m.length - 1] : null;
    },
    north() {
      const n = (this.snap && this.snap.north_flow) || [];
      return Array.isArray(n) ? n : [];
    },
    unlockList() {
      const u = (this.snap && this.snap.unlock) || [];
      return Array.isArray(u) ? u : [];
    },
    quality() {
      return (this.snap && this.snap.data_quality) || {};
    },
    failedSources() {
      return Object.keys(this.quality).filter(
        (k) => this.quality[k] === "error",
      );
    },
    disclaimer() {
      return (this.snap && this.snap.disclaimer) || FALLBACK_DISCLAIMER;
    },
  },
  methods: {
    // ---- 模板可访问的工具方法（模板只能访问实例方法，不能引用模块顶层函数）----
    chgColor(v) {
      return upColorTop(v);
    },
    maColor(price, ma) {
      return maColorTop(price, ma);
    },
    fmt(v, nd) {
      if (v === null || v === undefined || v === "") return "--";
      const n = Number(v);
      if (Number.isNaN(n)) return String(v);
      return nd === undefined ? String(n) : n.toFixed(nd);
    },
    pct(v, nd) {
      if (v === null || v === undefined) return "--";
      const n = Number(v);
      if (Number.isNaN(n)) return "--";
      const d = nd === undefined ? 2 : nd;
      return (n > 0 ? "+" : "") + n.toFixed(d) + "%";
    },
    money(v) {
      if (v === null || v === undefined) return "--";
      const n = Number(v);
      if (Number.isNaN(n)) return "--";
      const abs = Math.abs(n);
      if (abs >= 1e8) return (n / 1e8).toFixed(2) + "亿";
      if (abs >= 1e4) return (n / 1e4).toFixed(1) + "万";
      return n.toFixed(0);
    },
    qIcon(v) {
      if (v === "ok") return "✅";
      if (v === "error") return "❌";
      return "⚪";
    },
    // ---- 自选股管理 ----
    loadWatchlist() {
      try {
        const raw = localStorage.getItem(WATCHLIST_KEY);
        const arr = raw ? JSON.parse(raw) : [];
        this.watchlist = Array.isArray(arr)
          ? arr.filter((s) => /^\d{6}$/.test(String(s).trim()))
          : [];
      } catch {
        this.watchlist = [];
      }
    },
    saveWatchlist() {
      try {
        localStorage.setItem(WATCHLIST_KEY, JSON.stringify(this.watchlist));
      } catch {
        /* 忽略 */
      }
    },
    addToWatchlist(code) {
      code = String(code || "").trim();
      if (!/^\d{6}$/.test(code)) return;
      if (!this.watchlist.includes(code)) {
        this.watchlist.push(code);
        this.saveWatchlist();
      }
    },
    removeFromWatchlist(code) {
      this.watchlist = this.watchlist.filter((c) => c !== code);
      this.saveWatchlist();
      if (this.current === code) {
        if (this.watchlist.length > 0) {
          this.switchStock(this.watchlist[0]);
        } else {
          this.current = "";
          this.snap = null;
          this.error = "";
        }
      }
    },
    switchStock(code) {
      code = String(code || "").trim();
      if (!/^\d{6}$/.test(code)) return;
      this.current = code;
      this.searchInput = code;
      this.load(code);
    },
    doSearch() {
      const code = String(this.searchInput || "").trim();
      if (!/^\d{6}$/.test(code)) {
        this.toast("请输入 6 位 A股股票代码", "warning");
        return;
      }
      this.addToWatchlist(code);
      this.switchStock(code);
    },
    // ---- 数据加载 ----
    // 后端 get_snapshot_json 是同步阻塞调用（约 10s），并发请求会阻塞整个网关。
    // 因此前端同一时刻只允许一个快照请求：加载中再次点击只记录最新目标，
    // 等当前请求结束后再加载，避免连点两只股票把网关卡死。
    async load(symbol) {
      const code = String(symbol || "").trim();
      if (!/^\d{6}$/.test(code)) return;
      if (this.loading) {
        this.pendingSymbol = code;
        return;
      }
      this.loading = true;
      this.error = "";
      this.snap = null;
      try {
        const data = await callFunction("get_snapshot_json", {
          symbol: code,
          days: 120,
        });
        if (!data || typeof data !== "object") {
          throw new Error("返回数据为空");
        }
        if (data.success === false) {
          throw new Error(data.error || "数据获取失败");
        }
        // 加载期间用户已切换到别的股票，丢弃本次结果
        if (this.current !== code) return;
        this.snap = data;
        this.addToWatchlist(code);
        this.refreshQuotes(true);
      } catch (e) {
        if (this.current !== code) return;
        const msg = (e && e.message) || "数据获取失败";
        this.error = msg;
        this.toast(msg, "error");
      } finally {
        this.loading = false;
        const next = this.pendingSymbol;
        this.pendingSymbol = "";
        if (next && next !== code) this.load(next);
      }
    },
    refresh() {
      if (this.current) this.load(this.current);
    },
    async refreshQuotes(silent) {
      if (this.watchlist.length === 0) return;
      const now = Date.now();
      const need = this.watchlist.filter((c) => {
        const q = this.quoteCache[c];
        return !q || !q._ts || now - q._ts > 30000;
      });
      if (need.length === 0) return;
      try {
        const res = await callFunction("watchlist", { symbols: need });
        if (res && Array.isArray(res.items)) {
          for (const q of res.items) {
            if (q && q.symbol) {
              this.quoteCache[q.symbol] = Object.assign({}, q, { _ts: now });
            }
          }
        }
      } catch (e) {
        if (!silent) this.toast((e && e.message) || "自选行情获取失败", "error");
      }
    },
    // 轻量获取自选股名称（仅名称，不拉行情），供自选 chip 显示「名称 代码」
    async loadWatchlistNames() {
      if (this.watchlist.length === 0) return;
      try {
        const res = await callFunction("get_names", {
          symbols: this.watchlist,
        });
        if (res && res.names && typeof res.names === "object") {
          for (const code of Object.keys(res.names)) {
            const name = res.names[code];
            if (!name) continue;
            this.quoteCache[code] = Object.assign({}, this.quoteCache[code], {
              name: name,
            });
          }
        }
      } catch (e) {
        // 名称获取失败不阻断，自选 chip 回退显示代码（静默，不打扰用户）
      }
    },
    // ---- 让 Agent 分析 ----
    // 构建发给 Agent 的数据摘要文本（两个发送动作共用）
    buildPrompt() {
      const s = this.snap;
      const ind = this.ind || {};
      const intra = this.intra;
      const lines = [];
      lines.push(
        "【A股数据】" + s.code + "  数据时间 " + s.updated_at + "  交易时段：" + this.sessionText,
      );
      if (intra) {
        lines.push(
          "盘中：现价 " + this.fmt(intra.price) +
            "（昨收 " + this.fmt(intra.prev_close) + "，涨跌 " + this.pct(intra.chg_pct) + "），" +
            "均价 " + this.fmt(intra.vwap, 3) +
            "，日内位置 " + this.fmt(intra.pos_in_range, 1) + "%" +
            "，盘中量比 " + this.fmt(intra.vol_ratio_intraday) +
            "，30分钟动量 " + this.pct(intra.mom30) +
            "，振幅 " + this.pct(intra.amplitude) +
            "，已交易 " + this.fmt(intra.trade_minutes) + " 分钟",
        );
      } else {
        lines.push("当前非盘中（" + this.sessionText + "），无实时快照。");
      }
      if (ind && ind.date) {
        lines.push(
          "技术指标（" + ind.date + "）：收盘 " + this.fmt(ind.close) +
            "，MA5 " + this.fmt(ind.ma5) + " / MA10 " + this.fmt(ind.ma10) +
            " / MA20 " + this.fmt(ind.ma20) + " / MA60 " + this.fmt(ind.ma60) +
            "，MACD DIF " + this.fmt(ind.dif, 4) + " DEA " + this.fmt(ind.dea, 4) +
            " 柱 " + this.fmt(ind.macd, 4) +
            "，RSI14 " + this.fmt(ind.rsi14) +
            "，BOLL 上 " + this.fmt(ind.boll_up) + " / 中 " + this.fmt(ind.boll_mid) +
            " / 下 " + this.fmt(ind.boll_low) +
            "，量比 " + this.fmt(ind.vol_ratio) +
            "，换手 " + this.pct(ind.turnover) +
            "，ATR14 " + this.fmt(ind.atr14),
        );
      }
      const ff = this.fundFlow.slice(0, 5);
      if (ff.length > 0) {
        lines.push(
          "主力资金（近5日）：" +
            ff
              .map(
                (r) =>
                  String(r["日期"] || "").slice(5, 10) +
                  " " +
                  this.money(r["主力净流入-净额"]),
              )
              .join("；"),
        );
      }
      if (this.zt) {
        lines.push(
          "市场情绪：涨停 " + this.fmt(this.zt.count) + " 家，最高 " +
            this.fmt(this.zt.max_board) + " 连板",
        );
      }
      if (this.indexSummary) {
        const ix = this.indexSummary;
        lines.push(
          "大盘（上证 " + ix.date + " 收 " + this.fmt(ix.close) + "）：" +
            "近5日 " + this.pct(ix.d5) + "，近20日 " + this.pct(ix.d20) +
            "，近60日 " + this.pct(ix.d60),
        );
      }
      if (this.newsList.length > 0) {
        lines.push("近期新闻：");
        this.newsList.forEach((n, i) => {
          const title = n["新闻标题"] || "";
          const content = String(n["新闻内容"] || "").trim();
          // 标题与内容相同则只给标题，避免重复；内容过长截断到 120 字
          const body = content && content !== title
            ? "，" + content.slice(0, 120)
            : "";
          lines.push(
            "  " + (i + 1) + ". [" + String(n["发布时间"] || "").slice(0, 16) +
              "] " + title + body,
          );
        });
      }
      if (this.failedSources.length > 0) {
        lines.push(
          "注意：以下数据源本次获取失败，分析时请说明：" + this.failedSources.join("、"),
        );
      }
      lines.push("");
      lines.push(
        "请基于以上数据，从技术面、资金面、情绪面、大盘环境、消息面多维度分析，" +
          "给出你的判断（方向倾向、关键依据、主要风险）。" +
          (intra ? "注意：当前为盘中，请结合盘中走势分析。" : "当前为收盘后数据。"),
      );

      const text = lines.join("\n");
      return text;
    },
    // 动作一：发给当前正在对话的 Agent
    askAgent() {
      if (!this.snap) {
        this.toast("请先加载一只股票的数据", "warning");
        return;
      }
      const send =
        typeof window !== "undefined" && window.__jarvisSendToActiveAgent;
      if (typeof send !== "function") {
        this.toast("当前环境不支持发送给 Agent", "warning");
        return;
      }
      this.sending = true;
      try {
        send(this.buildPrompt(), { silent: true });
        this.toast("已发送给当前 Agent，请在对话中查看分析", "success");
      } catch (e) {
        this.toast("发送失败：" + ((e && e.message) || "未知错误"), "error");
      } finally {
        this.sending = false;
      }
    },
    // 动作二：新建一个独立 Agent 专门分析这只股票（复用宿主创建 Agent 弹窗，用户可确认/改参数）
    askNewAgent() {
      if (!this.snap) {
        this.toast("请先加载一只股票的数据", "warning");
        return;
      }
      if (typeof window === "undefined") return;
      const openModal = window.__jarvisOpenCreateAgentModal;
      const create = window.__jarvisCreateAgentForTask;
      if (typeof openModal !== "function" && typeof create !== "function") {
        this.toast("当前环境不支持创建 Agent", "warning");
        return;
      }
      const code = this.snap.code || "";
      const prompt = this.buildPrompt();
      this.sending = true;
      const finish = (msg, type) => {
        this.sending = false;
        this.toast(msg, type);
      };
      if (typeof openModal === "function") {
        // 优先走弹窗：不预填工作目录，由用户自行选择节点与目录
        Promise.resolve(
          openModal({
            task: prompt,
            name: "A股分析 " + code,
          }),
        )
          .then((res) => {
            if (res && res.confirmed) {
              finish("已创建新 Agent 分析 " + code, "success");
            } else {
              finish("已取消创建", "warning");
            }
          })
          .catch((e) => finish("创建失败：" + ((e && e.message) || "未知错误"), "error"));
        return;
      }
      // 兜底：宿主没有弹窗钩子时直接创建（需要工作目录，缺省用当前用户主目录）
      const workingDir =
        (typeof window.__jarvisGetActiveAgentInfo === "function" &&
          (window.__jarvisGetActiveAgentInfo() || {}).working_dir) ||
        "";
      if (!workingDir) {
        finish("宿主未提供创建弹窗，且无法确定工作目录", "warning");
        return;
      }
      Promise.resolve(create({ workingDir, task: prompt, name: "A股分析 " + code }))
        .then((r) => {
          if (r && r.success) finish("已创建新 Agent 分析 " + code, "success");
          else finish("创建失败：" + ((r && r.error) || "未知错误"), "error");
        })
        .catch((e) => finish("创建失败：" + ((e && e.message) || "未知错误"), "error"));
    },
    toast(message, type) {
      const t = typeof window !== "undefined" && window.__jarvisShowToast;
      if (typeof t === "function") {
        try {
          t(message, type || "info");
        } catch {
          /* 忽略 */
        }
        return;
      }
      if (type === "error") this.error = message;
    },
    // ---- SVG K线图（蜡烛 + MA5/MA20 + 成交量）----
    klineSvg() {
      const data = this.kline;
      if (!Array.isArray(data) || data.length === 0) return "";
      const n = data.length;
      const W = 560;
      const H = 300;
      const plotLeft = 10;
      const plotRight = W - 10;
      const plotTop = 14;
      const plotBottom = 212;
      const volTop = 228;
      const volBottom = 288;
      const plotW = plotRight - plotLeft;
      const plotH = plotBottom - plotTop;
      const slotW = plotW / n;
      const candleW = Math.max(1, slotW * 0.6);
      const closes = data.map((d) => Number(d.close));
      const ma = (arr, period) => {
        const out = [];
        for (let i = 0; i < arr.length; i++) {
          if (i < period - 1) {
            out.push(null);
            continue;
          }
          let s = 0;
          let c = 0;
          for (let j = i - period + 1; j <= i; j++) {
            const v = arr[j];
            if (v !== null && v !== undefined && !Number.isNaN(v)) {
              s += v;
              c++;
            }
          }
          out.push(c > 0 ? s / c : null);
        }
        return out;
      };
      const ma5 = ma(closes, 5);
      const ma20 = ma(closes, 20);
      let minP = Infinity;
      let maxP = -Infinity;
      for (const d of data) {
        const lo = Number(d.low);
        const hi = Number(d.high);
        if (!Number.isNaN(lo)) minP = Math.min(minP, lo);
        if (!Number.isNaN(hi)) maxP = Math.max(maxP, hi);
      }
      if (!isFinite(minP) || !isFinite(maxP)) {
        minP = 0;
        maxP = 1;
      } else if (minP === maxP) {
        maxP = minP + 1;
      }
      const pad = (maxP - minP) * 0.06;
      minP -= pad;
      maxP += pad;
      const y = (p) => plotTop + ((maxP - p) / (maxP - minP)) * plotH;
      const x = (i) => plotLeft + i * slotW + slotW / 2;
      let maxVol = 0;
      for (const d of data) {
        const v = Number(d.volume);
        if (!Number.isNaN(v)) maxVol = Math.max(maxVol, v);
      }
      maxVol = maxVol || 1;
      const volH = (v) => ((v || 0) / maxVol) * (volBottom - volTop);

      let svg = '<svg viewBox="0 0 ' + W + " " + H + '" width="100%" xmlns="http://www.w3.org/2000/svg" style="display:block;font-family:system-ui,sans-serif;">';
      svg += '<rect x="0" y="0" width="' + W + '" height="' + H + '" fill="#1a1a1a" rx="6"/>';

      for (let g = 0; g <= 4; g++) {
        const gy = plotTop + (plotH / 4) * g;
        svg += '<line x1="' + plotLeft + '" y1="' + gy + '" x2="' + plotRight + '" y2="' + gy + '" stroke="#2a2a2a" stroke-width="1"/>';
        const price = maxP - ((maxP - minP) / 4) * g;
        svg += '<text x="' + (plotLeft + 2) + '" y="' + (gy - 3) + '" fill="#666" font-size="9">' + price.toFixed(2) + "</text>";
      }

      const linePath = (arr, color) => {
        let d = "";
        for (let i = 0; i < arr.length; i++) {
          if (arr[i] === null || arr[i] === undefined || Number.isNaN(arr[i]))
            continue;
          const px = x(i);
          const py = y(arr[i]);
          d += d
            ? " L" + px.toFixed(2) + "," + py.toFixed(2)
            : "M" + px.toFixed(2) + "," + py.toFixed(2);
        }
        return d
          ? '<path d="' + d + '" fill="none" stroke="' + color + '" stroke-width="1.4" opacity="0.9"/>'
          : "";
      };
      svg += linePath(ma20, "#3b82f6");
      svg += linePath(ma5, "#f59e0b");

      for (let i = 0; i < n; i++) {
        const d = data[i];
        const o = Number(d.open);
        const c = Number(d.close);
        const hi = Number(d.high);
        const lo = Number(d.low);
        if ([o, c, hi, lo].some((v) => Number.isNaN(v))) continue;
        const cx = x(i);
        const up = c >= o;
        const col = up ? "#ef4444" : "#22c55e";
        const yc = y(c);
        const yo = y(o);
        const yhi = y(hi);
        const ylo = y(lo);
        svg += '<line x1="' + cx + '" y1="' + yhi + '" x2="' + cx + '" y2="' + ylo + '" stroke="' + col + '" stroke-width="1"/>';
        const top = Math.min(yc, yo);
        const bodyH = Math.max(1, Math.abs(yc - yo));
        svg += '<rect x="' + (cx - candleW / 2).toFixed(2) + '" y="' + top.toFixed(2) + '" width="' + candleW.toFixed(2) + '" height="' + bodyH.toFixed(2) + '" fill="' + (up ? col : "none") + '" stroke="' + col + '" stroke-width="1"/>';
        const v = Number(d.volume);
        if (!Number.isNaN(v)) {
          const vh = volH(v);
          svg += '<rect x="' + (cx - candleW / 2).toFixed(2) + '" y="' + (volBottom - vh).toFixed(2) + '" width="' + candleW.toFixed(2) + '" height="' + vh.toFixed(2) + '" fill="' + col + '" opacity="0.55"/>';
        }
      }

      const idxs = [0, Math.floor(n / 2), n - 1];
      for (const i of idxs) {
        const label = String(data[i].date || "").slice(5, 10) || String(i);
        svg += '<text x="' + x(i) + '" y="' + (volBottom + 12) + '" fill="#666" font-size="9" text-anchor="middle">' + label + "</text>";
      }
      svg += '<text x="' + (plotLeft + 2) + '" y="' + (plotTop + 10) + '" fill="#f59e0b" font-size="9">MA5</text>';
      svg += '<text x="' + (plotLeft + 32) + '" y="' + (plotTop + 10) + '" fill="#3b82f6" font-size="9">MA20</text>';
      svg += "</svg>";
      return svg;
    },
  },
  template: `
  <div style="display:flex;flex-direction:column;gap:10px;padding:10px;font-size:13px;color:#ddd;height:100%;overflow:auto;box-sizing:border-box;">
    <!-- 搜索框 -->
    <div style="display:flex;gap:6px;">
      <input v-model="searchInput" placeholder="输入 6 位股票代码" @keydown.enter.prevent="doSearch"
        style="flex:1;min-width:0;padding:6px 8px;background:#1e1e1e;color:#ddd;border:1px solid #444;border-radius:4px;font-size:13px;outline:none;"
        maxlength="6"/>
      <button @click="doSearch" :disabled="loading"
        style="padding:6px 12px;background:#1f6feb;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;">
        {{ loading ? '加载中' : '查询' }}
      </button>
    </div>

    <!-- 自选股 -->
    <div v-if="watchlist.length > 0" style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;">
      <span style="font-size:11px;color:#888;">自选:</span>
      <span v-for="c in watchlist" :key="c" @click="switchStock(c)"
        style="display:inline-flex;align-items:center;gap:4px;padding:2px 6px;background:#252526;border:1px solid #3c3c3c;border-radius:10px;cursor:pointer;font-size:11px;color:#ddd;"
        :style="c === current ? 'border-color:#1f6feb;color:#58a6ff;' : ''">
        <span :style="{ color: quoteCache[c] ? chgColor(quoteCache[c].chg_pct) : '#ddd' }">
          {{ quoteCache[c] && quoteCache[c].name ? quoteCache[c].name + ' ' + c : c }}
        </span>
        <span v-if="quoteCache[c] && quoteCache[c].price != null" style="font-size:10px;opacity:0.85;">{{ fmt(quoteCache[c].price) }}</span>
        <span v-if="quoteCache[c] && quoteCache[c].chg_pct != null" style="font-size:10px;"
          :style="{ color: chgColor(quoteCache[c].chg_pct) }">{{ pct(quoteCache[c].chg_pct) }}</span>
        <span @click.stop="removeFromWatchlist(c)" style="color:#888;font-size:10px;cursor:pointer;" title="删除">✕</span>
      </span>
    </div>

    <!-- 加载中 -->
    <div v-if="loading" style="padding:20px;text-align:center;color:#888;font-size:12px;">
      正在获取行情数据（多数据源，约 10 秒）…
    </div>

    <!-- 错误 -->
    <div v-else-if="error && !snap"
      style="padding:14px;background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.4);border-radius:6px;color:#f87171;font-size:12px;">
      {{ error }}
    </div>

    <!-- 空态 -->
    <div v-else-if="!current"
      style="padding:24px;text-align:center;color:#666;font-size:12px;border:1px dashed #333;border-radius:6px;">
      输入股票代码查询，或从上方自选股选择<br/>
      <span style="font-size:11px;color:#555;">支持沪深 A股（6 位代码，如 600519 / 000001）</span>
    </div>

    <!-- 数据面板 -->
    <template v-else-if="snap">
      <!-- 头部 -->
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
        <span style="font-weight:600;font-size:15px;color:#eee;">{{ snap.code }}</span>
        <span v-if="snap.name" style="font-size:13px;color:#ccc;">{{ snap.name }}</span>
        <span style="padding:1px 8px;border-radius:10px;font-size:10px;"
          :style="isIntraday
            ? 'background:rgba(34,197,94,0.15);color:#4ade80;border:1px solid rgba(34,197,94,0.4);'
            : 'background:rgba(136,136,136,0.15);color:#aaa;border:1px solid rgba(136,136,136,0.35);'">
          {{ isIntraday ? '● ' : '' }}{{ sessionText }}
        </span>
        <span style="flex:1;"></span>
        <button @click="refresh" title="刷新"
          style="padding:3px 8px;background:#252526;color:#aaa;border:1px solid #3c3c3c;border-radius:4px;cursor:pointer;font-size:11px;">⟳</button>
      </div>

      <!-- 盘中实时快照（仅盘中显示） -->
      <div v-if="isIntraday && intra"
        style="display:flex;flex-wrap:wrap;gap:10px;padding:8px 10px;background:#161b22;border:1px solid #2a3a4a;border-radius:6px;">
        <div style="display:flex;flex-direction:column;gap:2px;">
          <span style="font-size:10px;color:#888;">现价</span>
          <span style="font-size:15px;font-weight:600;" :style="{ color: chgColor(intra.chg_pct) }">{{ fmt(intra.price) }}</span>
        </div>
        <div style="display:flex;flex-direction:column;gap:2px;">
          <span style="font-size:10px;color:#888;">涨跌</span>
          <span style="font-size:13px;font-weight:600;" :style="{ color: chgColor(intra.chg_pct) }">{{ pct(intra.chg_pct) }}</span>
        </div>
        <div style="display:flex;flex-direction:column;gap:2px;">
          <span style="font-size:10px;color:#888;">均价VWAP</span>
          <span style="font-size:13px;color:#ddd;">{{ fmt(intra.vwap, 3) }}</span>
        </div>
        <div style="display:flex;flex-direction:column;gap:2px;">
          <span style="font-size:10px;color:#888;">日内位置</span>
          <span style="font-size:13px;color:#ddd;">{{ fmt(intra.pos_in_range, 1) }}%</span>
        </div>
        <div style="display:flex;flex-direction:column;gap:2px;">
          <span style="font-size:10px;color:#888;">盘中量比</span>
          <span style="font-size:13px;color:#ddd;">{{ fmt(intra.vol_ratio_intraday) }}</span>
        </div>
        <div style="display:flex;flex-direction:column;gap:2px;">
          <span style="font-size:10px;color:#888;">30分动量</span>
          <span style="font-size:13px;" :style="{ color: chgColor(intra.mom30) }">{{ pct(intra.mom30) }}</span>
        </div>
        <div style="display:flex;flex-direction:column;gap:2px;">
          <span style="font-size:10px;color:#888;">振幅</span>
          <span style="font-size:13px;color:#ddd;">{{ pct(intra.amplitude) }}</span>
        </div>
        <div style="width:100%;font-size:10px;color:#666;">
          数据截至 {{ intra.updated_at || snap.updated_at }} · 来源 {{ intra.source || '--' }}
          <span v-if="intra.partial" style="color:#f59e0b;">（降级数据）</span>
        </div>
      </div>
      <div v-else
        style="padding:6px 10px;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:6px;font-size:11px;color:#888;">
        当前非盘中（{{ sessionText }}），展示最近交易日收盘数据。
      </div>

      <!-- 技术指标 -->
      <div v-if="ind && ind.date"
        style="background:#1e1e1e;border:1px solid #333;border-radius:8px;padding:10px;display:flex;flex-direction:column;gap:8px;">
        <div style="display:flex;align-items:center;gap:8px;">
          <span style="font-size:12px;color:#aaa;font-weight:600;">技术指标</span>
          <span style="font-size:10px;color:#666;">{{ String(ind.date).slice(0, 10) }}</span>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:8px 14px;">
          <div style="display:flex;flex-direction:column;gap:1px;">
            <span style="font-size:10px;color:#888;">收盘</span>
            <span style="font-size:13px;font-weight:600;" :style="{ color: chgColor(ind.chg_pct) }">{{ fmt(ind.close) }}</span>
          </div>
          <div style="display:flex;flex-direction:column;gap:1px;">
            <span style="font-size:10px;color:#888;">涨跌</span>
            <span style="font-size:13px;" :style="{ color: chgColor(ind.chg_pct) }">{{ pct(ind.chg_pct) }}</span>
          </div>
          <div style="display:flex;flex-direction:column;gap:1px;">
            <span style="font-size:10px;color:#888;">MA5</span>
            <span style="font-size:13px;" :style="{ color: maColor(ind.close, ind.ma5) }">{{ fmt(ind.ma5) }}</span>
          </div>
          <div style="display:flex;flex-direction:column;gap:1px;">
            <span style="font-size:10px;color:#888;">MA10</span>
            <span style="font-size:13px;" :style="{ color: maColor(ind.close, ind.ma10) }">{{ fmt(ind.ma10) }}</span>
          </div>
          <div style="display:flex;flex-direction:column;gap:1px;">
            <span style="font-size:10px;color:#888;">MA20</span>
            <span style="font-size:13px;" :style="{ color: maColor(ind.close, ind.ma20) }">{{ fmt(ind.ma20) }}</span>
          </div>
          <div style="display:flex;flex-direction:column;gap:1px;">
            <span style="font-size:10px;color:#888;">MA60</span>
            <span style="font-size:13px;" :style="{ color: maColor(ind.close, ind.ma60) }">{{ fmt(ind.ma60) }}</span>
          </div>
          <div style="display:flex;flex-direction:column;gap:1px;">
            <span style="font-size:10px;color:#888;">量比</span>
            <span style="font-size:13px;color:#ddd;">{{ fmt(ind.vol_ratio) }}</span>
          </div>
          <div style="display:flex;flex-direction:column;gap:1px;">
            <span style="font-size:10px;color:#888;">换手</span>
            <span style="font-size:13px;color:#ddd;">{{ pct(ind.turnover) }}</span>
          </div>
          <div style="display:flex;flex-direction:column;gap:1px;">
            <span style="font-size:10px;color:#888;">RSI14</span>
            <span style="font-size:13px;color:#ddd;">{{ fmt(ind.rsi14, 1) }}</span>
          </div>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:8px 14px;padding-top:6px;border-top:1px solid #2a2a2a;">
          <div style="display:flex;flex-direction:column;gap:1px;">
            <span style="font-size:10px;color:#888;">MACD DIF/DEA/柱</span>
            <span style="font-size:12px;" :style="{ color: chgColor(ind.macd) }">
              {{ fmt(ind.dif, 4) }} / {{ fmt(ind.dea, 4) }} / {{ fmt(ind.macd, 4) }}
            </span>
          </div>
          <div style="display:flex;flex-direction:column;gap:1px;">
            <span style="font-size:10px;color:#888;">BOLL 上/中/下</span>
            <span style="font-size:12px;color:#ddd;">{{ fmt(ind.boll_up) }} / {{ fmt(ind.boll_mid) }} / {{ fmt(ind.boll_low) }}</span>
          </div>
          <div style="display:flex;flex-direction:column;gap:1px;">
            <span style="font-size:10px;color:#888;">ATR14</span>
            <span style="font-size:12px;color:#ddd;">{{ fmt(ind.atr14) }}</span>
          </div>
        </div>
      </div>

      <!-- K线 -->
      <div style="background:#1e1e1e;border:1px solid #333;border-radius:8px;padding:10px;display:flex;flex-direction:column;gap:8px;">
        <div style="display:flex;align-items:center;gap:8px;cursor:pointer;" @click="showKline = !showKline">
          <span style="font-size:12px;color:#aaa;font-weight:600;">日K线（近 {{ kline.length }} 日）</span>
          <span style="flex:1;"></span>
          <span style="font-size:10px;color:#666;">{{ showKline ? '收起' : '展开' }}</span>
        </div>
        <div v-if="showKline && kline.length > 0" style="border:1px solid #2a2a2a;border-radius:6px;overflow:hidden;"
          v-html="klineSvg()"></div>
        <div v-else-if="showKline" style="padding:8px;text-align:center;color:#666;font-size:11px;">K线数据不可用</div>
      </div>

      <!-- 主力资金 -->
      <div v-if="fundFlow.length > 0"
        style="background:#1e1e1e;border:1px solid #333;border-radius:8px;padding:10px;display:flex;flex-direction:column;gap:6px;">
        <div style="display:flex;align-items:center;gap:8px;cursor:pointer;" @click="showFund = !showFund">
          <span style="font-size:12px;color:#aaa;font-weight:600;">主力资金（近 {{ fundFlow.length }} 日）</span>
          <span style="flex:1;"></span>
          <span style="font-size:10px;color:#666;">{{ showFund ? '收起' : '展开' }}</span>
        </div>
        <template v-if="showFund">
          <div v-for="(r, i) in fundFlow" :key="i"
            style="display:flex;align-items:center;gap:8px;font-size:11px;padding:2px 0;border-bottom:1px solid #232323;">
            <span style="width:44px;color:#888;">{{ String(r['日期'] || '').slice(5, 10) }}</span>
            <span style="flex:1;color:#ccc;">主力 {{ money(r['主力净流入-净额']) }}</span>
            <span :style="{ color: chgColor(r['主力净流入-净占比']) }">{{ pct(r['主力净流入-净占比']) }}</span>
          </div>
        </template>
      </div>

      <!-- 市场情绪（涨停池） -->
      <div v-if="zt"
        style="background:#1e1e1e;border:1px solid #333;border-radius:8px;padding:10px;display:flex;flex-direction:column;gap:6px;">
        <div style="display:flex;align-items:center;gap:8px;cursor:pointer;" @click="showZt = !showZt">
          <span style="font-size:12px;color:#aaa;font-weight:600;">市场情绪</span>
          <span style="flex:1;"></span>
          <span style="font-size:10px;color:#666;">{{ showZt ? '收起' : '展开' }}</span>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:8px 14px;">
          <div style="display:flex;flex-direction:column;gap:1px;">
            <span style="font-size:10px;color:#888;">涨停家数</span>
            <span style="font-size:13px;color:#ef4444;font-weight:600;">{{ fmt(zt.count) }}</span>
          </div>
          <div style="display:flex;flex-direction:column;gap:1px;">
            <span style="font-size:10px;color:#888;">最高连板</span>
            <span style="font-size:13px;color:#ef4444;font-weight:600;">{{ fmt(zt.max_board) }}</span>
          </div>
        </div>
        <template v-if="showZt && zt.industry_rank && zt.industry_rank.length > 0">
          <div style="font-size:11px;color:#888;margin-top:4px;">涨停行业分布</div>
          <div style="display:flex;flex-wrap:wrap;gap:5px;">
            <span v-for="(it, i) in zt.industry_rank.slice(0, 10)" :key="i"
              style="padding:1px 7px;background:#252526;border:1px solid #3c3c3c;border-radius:9px;font-size:10px;color:#ddd;">
              {{ it['行业'] || it.industry || it.name }} {{ it['涨停数'] || it.count || '' }}
            </span>
          </div>
        </template>
        <template v-if="showZt && zt.list && zt.list.length > 0">
          <div style="font-size:11px;color:#888;margin-top:4px;">涨停个股（前 10）</div>
          <div style="display:flex;flex-wrap:wrap;gap:5px;">
            <span v-for="(it, i) in zt.list.slice(0, 10)" :key="i"
              style="padding:1px 7px;background:rgba(239,68,68,0.12);border:1px solid rgba(239,68,68,0.3);border-radius:9px;font-size:10px;color:#f87171;">
              {{ it['名称'] || it.name }} {{ it['代码'] || it.code || '' }}
            </span>
          </div>
        </template>
      </div>

      <!-- 大盘环境 -->
      <div v-if="indexSummary"
        style="background:#1e1e1e;border:1px solid #333;border-radius:8px;padding:10px;display:flex;flex-direction:column;gap:6px;">
        <div style="display:flex;align-items:center;gap:8px;cursor:pointer;" @click="showMacro = !showMacro">
          <span style="font-size:12px;color:#aaa;font-weight:600;">大盘环境（上证指数）</span>
          <span style="flex:1;"></span>
          <span style="font-size:10px;color:#666;">{{ showMacro ? '收起' : '展开' }}</span>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:8px 14px;">
          <div style="display:flex;flex-direction:column;gap:1px;">
            <span style="font-size:10px;color:#888;">收盘</span>
            <span style="font-size:13px;color:#ddd;font-weight:600;">{{ fmt(indexSummary.close) }}</span>
          </div>
          <div style="display:flex;flex-direction:column;gap:1px;">
            <span style="font-size:10px;color:#888;">近5日</span>
            <span style="font-size:13px;" :style="{ color: chgColor(indexSummary.d5) }">{{ pct(indexSummary.d5) }}</span>
          </div>
          <div style="display:flex;flex-direction:column;gap:1px;">
            <span style="font-size:10px;color:#888;">近20日</span>
            <span style="font-size:13px;" :style="{ color: chgColor(indexSummary.d20) }">{{ pct(indexSummary.d20) }}</span>
          </div>
          <div style="display:flex;flex-direction:column;gap:1px;">
            <span style="font-size:10px;color:#888;">近60日</span>
            <span style="font-size:13px;" :style="{ color: chgColor(indexSummary.d60) }">{{ pct(indexSummary.d60) }}</span>
          </div>
        </div>
        <template v-if="showMacro">
          <div v-if="margin" style="font-size:11px;color:#aaa;margin-top:4px;">
            两融（{{ String(margin['日期'] || '').slice(0, 10) }}）：融资余额 {{ money(margin['融资余额']) }}，融券余额 {{ money(margin['融券余额']) }}
          </div>
          <div v-if="north.length > 0" style="font-size:11px;color:#aaa;">
            北向资金近 {{ north.length }} 条记录可用（详见数据源）
          </div>
          <div v-if="unlockList.length > 0" style="font-size:11px;color:#f59e0b;">
            近期解禁 {{ unlockList.length }} 条，注意流通盘压力
          </div>
        </template>
      </div>

      <!-- 新闻 -->
      <div v-if="newsList.length > 0"
        style="background:#1e1e1e;border:1px solid #333;border-radius:8px;padding:10px;display:flex;flex-direction:column;gap:6px;">
        <div style="display:flex;align-items:center;gap:8px;cursor:pointer;" @click="showNews = !showNews">
          <span style="font-size:12px;color:#aaa;font-weight:600;">相关新闻</span>
          <span style="flex:1;"></span>
          <span style="font-size:10px;color:#666;">{{ showNews ? '收起' : '展开' }}</span>
        </div>
        <template v-if="showNews">
          <div v-for="(n, i) in newsList" :key="i"
            style="display:flex;flex-direction:column;gap:1px;padding:3px 0;border-bottom:1px solid #232323;">
            <span style="font-size:11px;color:#ccc;line-height:1.4;">{{ n['新闻标题'] || n.title || '--' }}</span>
            <span style="font-size:10px;color:#666;">{{ String(n['发布时间'] || n.time || '').slice(0, 16) }}</span>
          </div>
        </template>
      </div>

      <!-- 数据可用性 -->
      <div style="display:flex;flex-wrap:wrap;gap:4px 8px;font-size:10px;color:#666;">
        <span v-for="(v, k) in quality" :key="k">{{ qIcon(v) }} {{ k }}</span>
      </div>

      <!-- 让 Agent 分析 -->
      <!-- 注意：父容器是 flex column，按钮默认 flex-shrink:1 会被压窄；
           中文长串不会自动换行，文字会溢出被裁掉。必须 flex-shrink:0 + width:100% + nowrap。 -->
      <button @click="askAgent" :disabled="sending"
        style="display:block;flex:0 0 auto;width:100%;min-height:44px;box-sizing:border-box;padding:12px 10px;background:#1f6feb;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:14px;font-weight:600;line-height:1.5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
        {{ sending ? '发送中…' : '💬 发给当前 Agent' }}
      </button>
      <button @click="askNewAgent" :disabled="sending"
        style="display:block;flex:0 0 auto;width:100%;min-height:44px;box-sizing:border-box;padding:12px 10px;background:#238636;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:14px;font-weight:600;line-height:1.5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
        {{ sending ? '处理中…' : '🆕 新建 Agent 分析' }}
      </button>
      <div style="font-size:10px;color:#666;line-height:1.5;text-align:center;">
        本面板只展示客观数据，不做预测。可发给当前 Agent 继续对话，或新建一个独立 Agent 专门分析这只股票。
      </div>
    </template>

    <!-- 免责声明 -->
    <div style="padding:8px 10px;background:rgba(245,158,11,0.08);border:1px solid rgba(245,158,11,0.25);border-radius:6px;color:#f59e0b;font-size:11px;line-height:1.5;">
      {{ disclaimer }}
    </div>
  </div>
  `,};
